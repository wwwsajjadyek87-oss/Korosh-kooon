"""All user-facing text, keyboards and the progress-bar rendering live here."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from html import escape
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# --------------------------------------------------------------------------- #
# Static texts
# --------------------------------------------------------------------------- #
WELCOME = (
    "<b>دانلودر ساندکلاد</b>\n\n"
    "لینک آهنگ را بفرستید تا با کاور، نام همه خواننده‌ها و اطلاعات کامل برایتان بفرستم.\n\n"
    "در گروه هم کار می‌کنم: کافی است لینک را در گروه بفرستید."
)
HELP = (
    "<b>راهنما</b>\n\n"
    "۱. لینک یک آهنگ ساندکلاد را بفرستید.\n"
    "۲. روی پیام تایید، دکمه سبز را بزنید.\n"
    "۳. فایل با کاور و تگ‌های کامل ارسال می‌شود.\n\n"
    "فقط کسی که لینک را فرستاده می‌تواند دانلود را تایید یا لغو کند."
)
SEND_LINK_HINT = "لینک یک آهنگ ساندکلاد را بفرستید."
EXPIRED = "این درخواست منقضی شده است. لینک را دوباره بفرستید."
NOT_YOURS = "فقط فرستنده لینک می‌تواند این دکمه را بزند."
BUSY = "شما چند دانلود فعال دارید. کمی صبر کنید."
CANCELLED = "دانلود لغو شد."
TOO_BIG = "حجم فایل از حد مجاز ارسال در تلگرام بیشتر است."
GENERIC_ERROR = "مشکلی پیش آمد. دوباره امتحان کنید."
STOPPING = "در حال توقف..."
PL_BUSY = "شما یک پلی‌لیست فعال دارید. تا پایان آن صبر کنید یا آن را متوقف کنید."


def error_text(msg: str) -> str:
    return f"<b>دانلود انجام نشد</b>\n\n{escape(msg)}"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def fmt_time(sec: Optional[float]) -> str:
    if sec is None:
        return "--:--"
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def fmt_num(n: Optional[int]) -> str:
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def bar(frac: float, width: int = 14) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    return "\u25b0" * filled + "\u25b1" * (width - filled)


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #
def _button(text: str, data: str, style: str) -> InlineKeyboardButton:
    # `style` (success = green, danger = red) exists in Bot API 9.4+.
    # If the installed aiogram does not know the field, fall back to a plain button.
    try:
        return InlineKeyboardButton(text=text, callback_data=data, style=style)
    except Exception:
        return InlineKeyboardButton(text=text, callback_data=data)


def confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            _button("\u2713  تایید", f"dl:y:{token}", "success"),
            _button("\u2715  لغو", f"dl:n:{token}", "danger"),
        ]]
    )


def link_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="SoundCloud", url=url)]]
    )


def stop_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("\u2715  توقف", f"dl:s:{token}", "danger")]])


# --------------------------------------------------------------------------- #
# Confirmation message
# --------------------------------------------------------------------------- #
def confirm_text(meta=None) -> str:
    head = "<b>آیا از دانلود اطمینان دارید؟</b>"
    if meta is None:
        return f"{head}\n\n<i>در حال خواندن اطلاعات آهنگ...</i>"
    lines = [f"<b>{escape(meta.title)}</b>", f"<i>{escape(meta.artists_text)}</i>"]
    extra = [fmt_time(meta.duration)] if meta.duration else []
    if meta.genre:
        extra.append(escape(meta.genre))
    if extra:
        lines.append("  \u00b7  ".join(extra))
    if meta.preview_only:
        lines.append("\nتوجه: فقط پیش‌نمایش کوتاه این آهنگ در دسترس است.")
    return head + "\n\n" + "\n".join(lines)


def playlist_confirm_text(pl, cap: int) -> str:
    head = "<b>آیا از دانلود پلی‌لیست اطمینان دارید؟</b>"
    lines = [f"<b>{escape(pl.title)}</b>"]
    if pl.uploader:
        lines.append(f"<i>{escape(pl.uploader)}</i>")
    info = [f"{pl.total} آهنگ"]
    if pl.duration:
        info.append(fmt_time(pl.duration) if pl.duration < 3600
                    else f"{int(pl.duration // 3600)}:{int(pl.duration % 3600 // 60):02d}:{int(pl.duration % 60):02d}")
    lines.append("  \u00b7  ".join(info))
    if pl.total > cap:
        lines.append(f"\nحداکثر {cap} آهنگ اول دانلود می‌شود.")
    lines.append("\nآهنگ‌ها یکی‌یکی ارسال می‌شوند.")
    return head + "\n\n" + "\n".join(lines)


def playlist_summary(title: str, ok: int, failed: list, stopped: bool, skipped: int) -> str:
    head = "<b>پلی‌لیست متوقف شد</b>" if stopped else "<b>پلی‌لیست تمام شد</b>"
    lines = [head, escape(title), "", f"موفق: {ok}"]
    if failed:
        lines.append(f"ناموفق: {len(failed)}")
        lines += [f"\u25cb  {escape(t[:60])}" for t in failed[:10]]
        if len(failed) > 10:
            lines.append(f"و {len(failed) - 10} مورد دیگر")
    if skipped > 0 and not stopped:
        lines.append(f"\nبه خاطر سقف مجاز، {skipped} آهنگ آخر دانلود نشد.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #
STEPS = ("info", "download", "process", "upload")
LABELS = {
    "info": "دریافت اطلاعات",
    "download": "دانلود",
    "process": "افزودن کاور و تگ‌ها",
    "upload": "ارسال",
}


@dataclass
class ProgressState:
    stage: str = "info"
    title: str = ""
    artists: str = ""
    frac: float = 0.0
    done: int = 0
    total: int = 0
    speed: float = 0.0
    eta: Optional[float] = None
    pl_title: str = ""
    pl_index: int = 0
    pl_total: int = 0
    t0: float = field(default_factory=time.monotonic)

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self.frac, self.done, self.total, self.speed, self.eta = 0.0, 0, 0, 0.0, None
        self.t0 = time.monotonic()

    # called from the yt-dlp worker thread
    def on_download(self, done: int, total: int, speed: float, eta, frac: float) -> None:
        self.done, self.total, self.speed, self.eta, self.frac = done, total, speed, eta, frac

    # called by the upload stream for every chunk really sent
    def on_upload_chunk(self, n: int) -> None:
        self.done += n
        elapsed = max(time.monotonic() - self.t0, 0.001)
        self.speed = self.done / elapsed
        if self.total:
            self.frac = min(self.done / self.total, 1.0)
            self.eta = (self.total - self.done) / self.speed if self.speed else None


def progress_text(st: ProgressState) -> str:
    head = "<b>ساندکلاد</b>"
    if st.title:
        head = f"<b>{escape(st.title)}</b>"
        if st.artists:
            head += f"\n<i>{escape(st.artists)}</i>"
    if st.pl_total:
        head = f"<b>{escape(st.pl_title)}</b>\nآهنگ {st.pl_index} از {st.pl_total}\n\n" + (
            head if st.title else "")
        head = head.rstrip()

    if st.stage == "queued":
        return f"{head}\n\nدر صف انتظار..."

    idx = STEPS.index(st.stage) if st.stage in STEPS else 0
    steps = []
    for i, s in enumerate(STEPS):
        mark = "\u2713" if i < idx else ("\u25b8" if i == idx else "\u25cb")
        label = LABELS[s]
        steps.append(f"<b>{mark}  {label}</b>" if i == idx else f"{mark}  {label}")
    text = f"{head}\n\n" + "\n".join(steps)

    if st.pl_total:
        overall = ((st.pl_index - 1) + (idx / len(STEPS))) / st.pl_total
        text += f"\n\nکل: <code>{bar(overall, 10)}</code>  {int(overall * 100)}%"

    if st.stage in ("download", "upload"):
        text += f"\n\n<code>{bar(st.frac)}</code>  <b>{int(st.frac * 100)}%</b>"
        info = []
        if st.speed:
            info.append(f"سرعت: {fmt_bytes(st.speed)}/s")
        if st.eta is not None and st.speed:
            info.append(f"باقی‌مانده: {fmt_time(st.eta)}")
        if info:
            text += "\n" + "  \u00b7  ".join(info)
        if st.total:
            text += f"\nحجم: {fmt_bytes(st.done)} / {fmt_bytes(st.total)}"
        elif st.done:
            text += f"\nحجم: {fmt_bytes(st.done)}"
    return text


# --------------------------------------------------------------------------- #
# Caption under the audio file
# --------------------------------------------------------------------------- #
def _hashtag(t: str) -> str:
    t = re.sub(r"\W+", "_", t.strip()).strip("_")
    return f"#{t}" if t else ""


def caption(meta, limit: int = 1024) -> tuple[str, bool]:
    """HTML caption (<= limit) and whether the description had to be cut."""
    lines = [f"<b>{escape(meta.title)}</b>", escape(meta.artists_text), ""]
    if meta.album and meta.album != meta.title:
        lines.append(f"آلبوم: {escape(meta.album)}")
    if meta.genre:
        lines.append(f"ژانر: {escape(meta.genre)}")
    if meta.date:
        lines.append(f"تاریخ انتشار: {meta.date}")
    if meta.label:
        lines.append(f"لیبل: {escape(meta.label)}")
    if meta.composer:
        lines.append(f"آهنگساز: {escape(meta.composer)}")

    stats = []
    dur = meta.full_duration or meta.duration
    if dur:
        stats.append(f"مدت {fmt_time(dur)}")
    for label, val in (("پخش", meta.plays), ("لایک", meta.likes), ("ریپست", meta.reposts)):
        if val:
            stats.append(f"{label} {fmt_num(val)}")
    if stats:
        lines.append("  \u00b7  ".join(stats))
    if meta.preview_only:
        lines.append("توجه: این فایل فقط پیش‌نمایش کوتاه آهنگ است.")

    tags = " ".join(filter(None, (_hashtag(t) for t in meta.tags[:6])))
    if tags:
        lines += ["", escape(tags)]

    base = "\n".join(lines).rstrip()
    truncated = False
    desc = meta.description
    if desc:
        room = limit - len(base) - 60
        if room > 80:
            cut = desc[:room]
            while len(escape(cut)) > room and cut:
                cut = cut[:-20]
            if len(cut) < len(desc):
                cut = cut.rstrip() + "..."
                truncated = True
            base += f"\n\n<blockquote expandable>{escape(cut)}</blockquote>"
        else:
            truncated = True
    return base, truncated
