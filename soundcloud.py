"""SoundCloud core: full metadata extraction, fast download, cover art and tagging.

Strategy
--------
* yt-dlp does the extraction/download (it always tracks SoundCloud's changes).
* We additionally capture the *raw* SoundCloud API JSON that yt-dlp already
  fetched (one hook, no extra request) to read fields yt-dlp does not expose:
  publisher_metadata (artist / album / ISRC / composer), visuals, label, etc.
* FFmpeg embeds cover + tags without re-encoding whenever possible.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import aiohttp
from yt_dlp import YoutubeDL

log = logging.getLogger("soundcloud")

EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sc")
INFO_MAX_AGE = 180  # seconds; older extracted info is re-extracted before download


class ScError(Exception):
    """Error with a message that is safe to show to the user."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


# --------------------------------------------------------------------------- #
# URL detection
# --------------------------------------------------------------------------- #
URL_RE = re.compile(
    r"https?://(?:(?:www|m|on)\.)?soundcloud\.com/[^\s<>\"']+"
    r"|https?://soundcloud\.app\.goo\.gl/[^\s<>\"']+",
    re.I,
)


def find_url(text: Optional[str]) -> Optional[str]:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,;:!?)]}»") if m else None


# --------------------------------------------------------------------------- #
# Capture raw SoundCloud API json (already downloaded by yt-dlp)
# --------------------------------------------------------------------------- #
_RAW: dict[str, dict] = {}


def _install_hook() -> None:
    try:
        from yt_dlp.extractor.soundcloud import SoundcloudIE

        original = SoundcloudIE._extract_info_dict
        if getattr(original, "_sc_hooked", False):
            return

        def wrapper(self, info, *args, **kwargs):
            try:
                if isinstance(info, dict) and info.get("id") is not None:
                    if len(_RAW) > 200:
                        _RAW.clear()
                    _RAW[str(info["id"])] = info
            except Exception:  # never break extraction because of the hook
                pass
            return original(self, info, *args, **kwargs)

        wrapper._sc_hooked = True  # type: ignore[attr-defined]
        SoundcloudIE._extract_info_dict = wrapper
    except Exception as exc:  # yt-dlp internals changed: fall back to normalized info
        log.warning("raw-metadata hook not installed: %s", exc)


_install_hook()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def first(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def to_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def iso_date(v) -> Optional[str]:
    m = re.match(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", str(v or ""))
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def parse_tags(s: Optional[str]) -> list[str]:
    if not s:
        return []
    return [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', s)]


_DASH = re.compile(r"\s+[-\u2013\u2014]\s+")
_SPLIT = re.compile(
    r"\s*(?:,|;|&|\u00d7|\s+x\s+|\s+(?:feat\.?|ft\.?|featuring|with|vs\.?)\s+)\s*", re.I
)
_FEAT = re.compile(
    r"[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]+)[\)\]]"
    r"|\s(?:feat\.?|ft\.?|featuring)\s+(.+)$",
    re.I,
)


def split_names(s: Optional[str]) -> list[str]:
    out = []
    for p in _SPLIT.split(s or ""):
        p = p.strip(" \t-\u2013\u2014_*")
        if p:
            out.append(p)
    return out


def dedupe(names: list[str]) -> list[str]:
    seen, out = set(), []
    for n in names:
        k = norm(n)
        if k and k not in seen:
            seen.add(k)
            out.append(n)
    return out


def derive_artists_and_title(raw_title: str, pm_artist: Optional[str], uploader: Optional[str]):
    """All performers (metadata + 'A & B - Song' + '(feat. C)') and a clean title."""
    title = (raw_title or "").strip() or "Unknown"
    pm_names = split_names(pm_artist)
    names = list(pm_names)
    prefix_used = False

    parts = _DASH.split(title, maxsplit=1)
    if len(parts) == 2 and 0 < len(parts[0]) <= 80 and parts[1].strip():
        left = split_names(parts[0])
        known = {norm(n) for n in pm_names}
        if uploader:
            known.add(norm(uploader))
        # With official metadata, only trust the prefix when it matches a known artist.
        if left and (not pm_names or any(norm(n) in known for n in left)):
            names += left
            title = parts[1].strip()
            prefix_used = True

    if not prefix_used and not pm_names and uploader:
        names.append(uploader)

    for m in _FEAT.finditer(title):
        names += split_names(m.group(1) or m.group(2) or "")

    return dedupe(names) or [uploader or "SoundCloud"], title


_SIZE_RE = re.compile(
    r"-(?:original|large|small|tiny|badge|mini|crop|t\d+x\d+)\.(jpg|jpeg|png|webp)$", re.I
)


def image_variants(url: str) -> list[str]:
    """SoundCloud serves the same image in many sizes; try the biggest first."""
    m = _SIZE_RE.search(url)
    if not m:
        return [url]
    base, ext = url[: m.start()], m.group(1)
    out = [f"{base}-original.{ext}", f"{base}-original.jpg", f"{base}-t500x500.jpg", url]
    return list(dict.fromkeys(out))


@dataclass
class TrackMeta:
    url: str
    track_id: str
    title: str
    artists: list[str]
    uploader: str = ""
    album: Optional[str] = None
    genre: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    description: Optional[str] = None
    duration: float = 0.0
    full_duration: Optional[float] = None
    date: Optional[str] = None
    plays: Optional[int] = None
    likes: Optional[int] = None
    reposts: Optional[int] = None
    comments: Optional[int] = None
    label: Optional[str] = None
    composer: Optional[str] = None
    isrc: Optional[str] = None
    copyright: Optional[str] = None
    license: Optional[str] = None
    explicit: bool = False
    preview_only: bool = False
    cover_urls: list[str] = field(default_factory=list)

    @property
    def artists_text(self) -> str:
        return ", ".join(self.artists)

    @property
    def year(self) -> Optional[str]:
        return self.date[:4] if self.date else None


@dataclass
class PlaylistInfo:
    title: str
    uploader: str
    entries: list  # [{"url", "title", "duration"}]

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def duration(self) -> float:
        return float(sum((e.get("duration") or 0) for e in self.entries))


def build_meta(info: dict, raw: Optional[dict], url: str) -> TrackMeta:
    raw = raw or {}
    pm = raw.get("publisher_metadata") or {}
    user = raw.get("user") or {}

    uploader = first(user.get("username"), info.get("uploader")) or ""
    yt_artists = info.get("artists")
    pm_artist = first(
        pm.get("artist"),
        ", ".join(yt_artists) if isinstance(yt_artists, list) else None,
        info.get("artist"),
    )
    artists, title = derive_artists_and_title(
        first(raw.get("title"), info.get("title")) or "Unknown", pm_artist, uploader
    )

    genres = info.get("genres")
    tags = parse_tags(raw.get("tag_list")) or list(info.get("tags") or [])
    stream_ms = first(raw.get("duration"))
    full_ms = first(raw.get("full_duration"))
    license_ = first(raw.get("license"), info.get("license"))

    # Cover candidates: track artwork -> track visuals -> yt-dlp thumbnails -> uploader avatar
    covers: list[str] = []
    if raw.get("artwork_url"):
        covers += image_variants(raw["artwork_url"])
    for v in ((raw.get("visuals") or {}).get("visuals") or []):
        if isinstance(v, dict) and v.get("visual_url"):
            covers.append(v["visual_url"])
    thumbs = sorted(
        info.get("thumbnails") or [],
        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
        reverse=True,
    )
    for t in thumbs:
        if t.get("url"):
            covers += image_variants(t["url"])
    if info.get("thumbnail"):
        covers += image_variants(info["thumbnail"])
    if user.get("avatar_url"):
        covers += image_variants(user["avatar_url"])

    return TrackMeta(
        url=first(raw.get("permalink_url"), info.get("webpage_url"), url),
        track_id=str(first(raw.get("id"), info.get("id"), "")),
        title=title,
        artists=artists,
        uploader=uploader,
        album=first(pm.get("album_title"), pm.get("release_title"), info.get("album")),
        genre=first(raw.get("genre"), info.get("genre"), genres[0] if genres else None),
        tags=tags,
        description=(first(raw.get("description"), info.get("description")) or "").strip() or None,
        duration=(stream_ms / 1000) if stream_ms else float(info.get("duration") or 0),
        full_duration=(full_ms / 1000) if full_ms else None,
        date=iso_date(first(raw.get("release_date"), raw.get("display_date"),
                            raw.get("created_at"), info.get("upload_date"))),
        plays=to_int(first(raw.get("playback_count"), info.get("view_count"))),
        likes=to_int(first(raw.get("likes_count"), raw.get("favoritings_count"), info.get("like_count"))),
        reposts=to_int(first(raw.get("reposts_count"), info.get("repost_count"))),
        comments=to_int(first(raw.get("comment_count"), info.get("comment_count"))),
        label=first(raw.get("label_name"), pm.get("label_name")),
        composer=pm.get("writer_composer"),
        isrc=pm.get("isrc"),
        copyright=first(pm.get("p_line_for_display"), pm.get("c_line_for_display")),
        license=license_,
        explicit=bool(pm.get("explicit")),
        preview_only=(raw.get("policy") == "SNIP"),
        cover_urls=list(dict.fromkeys(covers)),
    )


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #
def friendly_error(exc: Exception) -> str:
    s = str(exc).lower()
    if "404" in s or "not found" in s or "unable to resolve" in s or "does not exist" in s:
        return "آهنگ پیدا نشد. ممکن است حذف شده یا لینک اشتباه باشد."
    if "403" in s or "forbidden" in s or "private" in s or "sign in" in s or "login" in s:
        return "این آهنگ خصوصی است یا دسترسی به آن محدود شده است."
    if "geo" in s or "country" in s or "not available in your" in s:
        return "این آهنگ در منطقه سرور در دسترس نیست."
    if "429" in s or "too many" in s:
        return "تعداد درخواست‌ها زیاد است. چند لحظه بعد دوباره امتحان کنید."
    return "خطایی در دریافت از ساندکلاد رخ داد. دوباره امتحان کنید."


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def _fetch_sync(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
        "retries": 3,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ScError("اطلاعاتی از این لینک دریافت نشد.")
    if info.get("_type") == "playlist" or info.get("entries"):
        if "Set" not in str(info.get("extractor_key", "")):
            raise ScError("لینک پروفایل پشتیبانی نمی‌شود. لینک یک آهنگ یا پلی‌لیست را بفرستید.")
        entries = []
        for e in info.get("entries") or []:
            u = (e or {}).get("url") or (e or {}).get("webpage_url")
            if u:
                entries.append({"url": u, "title": e.get("title") or "", "duration": e.get("duration")})
        if not entries:
            raise ScError("پلی‌لیست خالی است یا قابل دسترسی نیست.")
        return PlaylistInfo(
            title=info.get("title") or "Playlist",
            uploader=info.get("uploader") or "",
            entries=entries,
        )
    raw = _RAW.pop(str(info.get("id")), None)
    return info, build_meta(info, raw, url), time.monotonic()


async def fetch(url: str):
    """Returns (yt-dlp info, TrackMeta, fetched_at) for a track, or PlaylistInfo for a set."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(EXECUTOR, _fetch_sync, url)
    except ScError:
        raise
    except Exception as exc:
        log.warning("fetch failed for %s: %s", url, exc)
        raise ScError(friendly_error(exc)) from exc


# --------------------------------------------------------------------------- #
# Download (real progress via yt-dlp hooks)
# --------------------------------------------------------------------------- #
ProgressCb = Callable[[int, int, float, Optional[float], float], None]


def _download_sync(info: dict, url: str, workdir: Path, fetched_at: float, cb: ProgressCb) -> Path:
    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                frac = done / total
            elif d.get("fragment_count"):
                frac = (d.get("fragment_index") or 0) / d["fragment_count"]
            else:
                frac = 0.0
            cb(done, total, d.get("speed") or 0.0, d.get("eta"), max(0.0, min(frac, 0.99)))
        elif d.get("status") == "finished":
            size = d.get("total_bytes") or d.get("downloaded_bytes") or 0
            cb(size, size, 0.0, 0, 1.0)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(workdir / "audio.%(ext)s"),
        "concurrent_fragment_downloads": 8,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 20,
        "overwrites": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }

    def run(reuse: bool) -> None:
        with YoutubeDL(opts) as ydl:
            if reuse:
                ydl.process_ie_result(copy.deepcopy(info), download=True)
            else:
                ydl.extract_info(url, download=True)

    reuse = (time.monotonic() - fetched_at) < INFO_MAX_AGE
    try:
        run(reuse)
    except Exception as exc:
        if not reuse:
            raise
        log.info("reuse of extracted info failed (%s); extracting again", exc)
        for p in workdir.glob("audio.*"):
            p.unlink(missing_ok=True)
        run(False)

    files = [p for p in workdir.glob("audio.*") if p.suffix not in {".part", ".ytdl", ".temp"}]
    if not files:
        raise ScError("فایل صوتی دریافت نشد.")
    return max(files, key=lambda p: p.stat().st_size)


async def download(info: dict, url: str, workdir: Path, fetched_at: float, cb: ProgressCb) -> Path:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(EXECUTOR, _download_sync, info, url, workdir, fetched_at, cb)
    except ScError:
        raise
    except Exception as exc:
        log.warning("download failed for %s: %s", url, exc)
        raise ScError(friendly_error(exc)) from exc


# --------------------------------------------------------------------------- #
# FFmpeg: cover preparation and tagging
# --------------------------------------------------------------------------- #
async def run_ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="ignore")[-600:])


_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


async def prepare_cover(meta: TrackMeta, workdir: Path) -> tuple[Optional[Path], Optional[Path]]:
    """Download the best available artwork -> (embedded cover jpg, telegram thumb jpg)."""
    cover, thumb, raw = workdir / "cover.jpg", workdir / "thumb.jpg", workdir / "cover.raw"
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers=_UA) as session:
        for url in meta.cover_urls:
            try:
                async with session.get(url) as r:
                    if r.status != 200:
                        continue
                    data = await r.read()
                if len(data) < 2000:
                    continue
                raw.write_bytes(data)
                await run_ffmpeg(
                    "-i", str(raw), "-frames:v", "1", "-pix_fmt", "yuvj420p",
                    "-vf", "scale='min(1400,iw)':'min(1400,ih)':force_original_aspect_ratio=decrease",
                    "-q:v", "2", str(cover),
                )
                for q in ("5", "12", "20"):
                    await run_ffmpeg(
                        "-i", str(cover), "-frames:v", "1", "-pix_fmt", "yuvj420p",
                        "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                        "-q:v", q, str(thumb),
                    )
                    if thumb.stat().st_size <= 190_000:
                        break
                return cover, thumb
            except Exception as exc:
                log.debug("cover candidate failed %s: %s", url, exc)
    log.info("no cover found for track %s", meta.track_id)
    return None, None


def safe_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", s)
    return re.sub(r"\s+", " ", s).strip(" .")[:120] or "track"


def metadata_args(m: TrackMeta) -> list[str]:
    comment = m.url + (f"\n\n{m.description[:3000]}" if m.description else "")
    items = {
        "title": m.title,
        "artist": m.artists_text,
        "album_artist": m.artists_text,
        "album": m.album or m.title,
        "genre": m.genre,
        "date": m.date,
        "comment": comment,
        "copyright": first(m.copyright, m.license),
        "composer": m.composer,
        "publisher": m.label,
        "isrc": m.isrc,
    }
    out: list[str] = []
    for k, v in items.items():
        if v:
            out += ["-metadata", f"{k}={v}"]
    return out


async def build_audio(src: Path, cover: Optional[Path], meta: TrackMeta, workdir: Path) -> Path:
    ext = src.suffix.lower().lstrip(".")
    name = safe_filename(f"{meta.artists_text} - {meta.title}")
    if ext == "mp3":
        out, codec = workdir / f"{name}.mp3", ["-c:a", "copy"]
    elif ext in ("m4a", "mp4", "aac"):
        out, codec = workdir / f"{name}.m4a", ["-c:a", "copy"]
    else:  # opus / ogg / flac / wav ... -> mp3 so Telegram's music player accepts it
        lossless = ext in ("wav", "flac", "aiff", "aif", "alac")
        out = workdir / f"{name}.mp3"
        codec = ["-c:a", "libmp3lame", "-b:a", "320k" if lossless else "192k"]

    args = ["-i", str(src)]
    if cover:
        args += ["-i", str(cover)]
    args += ["-map", "0:a:0"]
    if cover:
        args += ["-map", "1:v:0", "-c:v", "copy"]
    args += codec + ["-map_metadata", "-1"] + metadata_args(meta)
    if cover:
        args += ["-disposition:v:0", "attached_pic",
                 "-metadata:s:v:0", "title=Cover", "-metadata:s:v:0", "comment=Cover (front)"]
    if out.suffix == ".mp3":
        args += ["-id3v2_version", "3", "-write_id3v1", "1"]
    args.append(str(out))

    try:
        await run_ffmpeg(*args)
    except RuntimeError as exc:
        if not cover:
            raise
        log.warning("tagging with cover failed (%s); retrying without cover", exc)
        return await build_audio(src, None, meta, workdir)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced no output")
    return out
