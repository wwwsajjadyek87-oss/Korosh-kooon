"""Telegram layer (aiogram 3): confirmation flow, progress updates, sending."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiofiles
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InputFile,
    Message,
    ReplyParameters,
)

import soundcloud as sc
import ui

log = logging.getLogger("bot")


@dataclass
class Settings:
    token: str
    max_concurrent_jobs: int = 3
    max_jobs_per_user: int = 2
    max_file_mb: int = 49
    bot_api_url: str = ""
    progress_interval_private: float = 1.2
    progress_interval_group: float = 3.0
    pending_ttl: int = 600
    max_playlist_tracks: int = 25
    max_playlist_tracks_group: int = 10
    playlist_gap_private: float = 0.7
    playlist_gap_group: float = 3.5


@dataclass
class Job:
    token: str
    owner_id: Optional[int]
    chat_id: int
    thread_id: Optional[int]
    link_msg_id: int
    msg: Message
    url: str
    is_group: bool
    created: float
    prefetch: Optional[asyncio.Task] = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)


class ProgressFile(InputFile):
    """Streams a file to Telegram and reports the bytes really sent."""

    def __init__(self, path: Path, state: ui.ProgressState, chunk_size: int = 64 * 1024):
        super().__init__(filename=path.name, chunk_size=chunk_size)
        self.path = path
        self.state = state

    async def read(self, bot: Bot):
        async with aiofiles.open(self.path, "rb") as f:
            while True:
                chunk = await f.read(self.chunk_size)
                if not chunk:
                    break
                self.state.on_upload_chunk(len(chunk))
                yield chunk


async def safe_edit(msg: Message, text: str, markup=None) -> float:
    """Edit a message. Returns seconds to wait if Telegram asked us to slow down."""
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramRetryAfter as e:
        return float(e.retry_after)
    except TelegramBadRequest:
        pass  # not modified / message gone
    except TelegramAPIError as e:
        log.debug("edit failed: %s", e)
    return 0.0


async def safe_delete(msg: Message) -> None:
    with contextlib.suppress(TelegramAPIError):
        await msg.delete()


class SoundCloudBot:
    def __init__(self, bot: Bot, settings: Settings):
        self.bot = bot
        self.s = settings
        self.pending: dict[str, Job] = {}
        self.active: dict[int, int] = {}
        self.active_playlists: set[int] = set()
        self.running: dict[str, Job] = {}
        self.sem = asyncio.Semaphore(settings.max_concurrent_jobs)
        self.tasks: set[asyncio.Task] = set()
        self.router = Router()
        r = self.router
        r.message.register(self.on_start, CommandStart())
        r.message.register(self.on_help, Command("help"))
        r.message.register(self.on_text, F.text | F.caption)
        r.callback_query.register(self.on_confirm, F.data.startswith("dl:"))

    # ------------------------------------------------------------------ #
    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _delete_later(self, msg: Message, delay: float) -> None:
        await asyncio.sleep(delay)
        await safe_delete(msg)

    # ------------------------------------------------------------------ #
    async def on_start(self, m: Message) -> None:
        await m.answer(ui.WELCOME)

    async def on_help(self, m: Message) -> None:
        await m.answer(ui.HELP)

    @staticmethod
    def _extract_url(m: Message) -> Optional[str]:
        url = sc.find_url(m.text or m.caption)
        if url:
            return url
        for e in (m.entities or m.caption_entities or []):
            if e.type == "text_link" and e.url:
                url = sc.find_url(e.url)
                if url:
                    return url
        return None

    async def on_text(self, m: Message) -> None:
        if m.from_user and m.from_user.is_bot and not m.sender_chat:
            return
        url = self._extract_url(m)
        is_private = m.chat.type == ChatType.PRIVATE
        if not url:
            if is_private and m.text and not m.text.startswith("/"):
                await m.answer(ui.SEND_LINK_HINT)
            return

        token = secrets.token_hex(5)
        owner = None if m.sender_chat else (m.from_user.id if m.from_user else None)
        try:
            msg = await m.reply(ui.confirm_text(None), reply_markup=ui.confirm_keyboard(token))
        except TelegramAPIError as e:
            log.warning("cannot reply in chat %s: %s", m.chat.id, e)
            return

        job = Job(
            token=token, owner_id=owner, chat_id=m.chat.id,
            thread_id=m.message_thread_id if m.is_topic_message else None,
            link_msg_id=m.message_id, msg=msg, url=url, is_group=not is_private,
            created=time.time(),
        )
        self.pending[token] = job
        # Read the track info while the user is still deciding -> download starts instantly.
        job.prefetch = self.spawn(self._prefetch(job))

    async def _prefetch(self, job: Job):
        try:
            res = await sc.fetch(job.url)
        except sc.ScError as e:
            if self.pending.pop(job.token, None):  # still waiting for the user's decision
                await safe_edit(job.msg, ui.error_text(e.user_message))
                self.spawn(self._delete_later(job.msg, 20))
                return None
            raise  # user already confirmed; run_job reports it
        if isinstance(res, sc.PlaylistInfo):
            text = ui.playlist_confirm_text(res, self._cap(job))
        else:
            text = ui.confirm_text(res[1])
        if job.token in self.pending:
            await safe_edit(job.msg, text, ui.confirm_keyboard(job.token))
        return res

    def _cap(self, job: Job) -> int:
        return self.s.max_playlist_tracks_group if job.is_group else self.s.max_playlist_tracks

    # ------------------------------------------------------------------ #
    async def on_confirm(self, cb: CallbackQuery) -> None:
        try:
            _, choice, token = (cb.data or "").split(":")
        except ValueError:
            await cb.answer()
            return
        if choice == "s":  # stop a running playlist
            run = self.running.get(token)
            if not run:
                await cb.answer(ui.EXPIRED, show_alert=True)
            elif run.owner_id is not None and cb.from_user.id != run.owner_id:
                await cb.answer(ui.NOT_YOURS, show_alert=True)
            else:
                run.stop.set()
                await cb.answer(ui.STOPPING)
            return
        job = self.pending.get(token)
        if not job:
            await cb.answer(ui.EXPIRED, show_alert=True)
            if isinstance(cb.message, Message):
                with contextlib.suppress(TelegramAPIError):
                    await cb.message.edit_reply_markup(reply_markup=None)
            return
        if job.owner_id is not None and cb.from_user.id != job.owner_id:
            await cb.answer(ui.NOT_YOURS, show_alert=True)
            return

        if choice == "n":
            self.pending.pop(token, None)
            if job.prefetch:
                job.prefetch.cancel()
            await cb.answer()
            await safe_edit(job.msg, f"<b>{ui.CANCELLED}</b>")
            self.spawn(self._delete_later(job.msg, 5))
            return

        key = job.owner_id or job.chat_id
        if self.active.get(key, 0) >= self.s.max_jobs_per_user:
            await cb.answer(ui.BUSY, show_alert=True)
            return
        self.pending.pop(token, None)
        await cb.answer()
        self.spawn(self.run_job(job))

    # ------------------------------------------------------------------ #
    async def _progress_loop(self, msg: Message, st: ui.ProgressState, interval: float, token: str) -> None:
        last = ""
        while True:
            text = ui.progress_text(st)
            if text != last:
                wait = await safe_edit(msg, text, ui.stop_keyboard(token) if st.pl_total else None)
                if wait:
                    interval = min(interval * 1.5, 10.0)
                    await asyncio.sleep(wait)
                    continue
                last = text
            await asyncio.sleep(interval)

    async def run_job(self, job: Job) -> None:
        key = job.owner_id or job.chat_id
        self.active[key] = self.active.get(key, 0) + 1
        st = ui.ProgressState()
        interval = self.s.progress_interval_group if job.is_group else self.s.progress_interval_private
        updater = asyncio.create_task(self._progress_loop(job.msg, st, interval, job.token))
        workdir = Path(tempfile.mkdtemp(prefix="scbot_"))
        error: Optional[str] = None
        summary: Optional[str] = None
        delivered = False
        try:
            res = await job.prefetch
            if res is None:
                raise sc.ScError(ui.GENERIC_ERROR)
            if isinstance(res, sc.PlaylistInfo):
                summary = await self._run_playlist(job, res, st, workdir)
            else:
                if self.sem.locked():
                    st.set_stage("queued")
                async with self.sem:
                    await self._deliver_track(job, st, workdir, job.url, res, extra_desc=True)
                delivered = True
        except sc.ScError as e:
            error = e.user_message
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("job failed: %s", job.url)
            error = ui.GENERIC_ERROR
        finally:
            updater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater
            await asyncio.to_thread(shutil.rmtree, workdir, True)
            self.active[key] = max(0, self.active.get(key, 1) - 1)

        if summary:
            await safe_delete(job.msg)
            with contextlib.suppress(TelegramAPIError):
                await self.bot.send_message(
                    job.chat_id, summary, message_thread_id=job.thread_id,
                    reply_parameters=ReplyParameters(message_id=job.link_msg_id,
                                                     allow_sending_without_reply=True),
                )
        elif delivered:
            await safe_delete(job.msg)
        elif error:
            await safe_edit(job.msg, ui.error_text(error))
            self.spawn(self._delete_later(job.msg, 30))

    async def _deliver_track(self, job: Job, st: ui.ProgressState, workdir: Path, url: str,
                             res, extra_desc: bool) -> None:
        """Full pipeline for one track: cover || download -> tag -> upload."""
        st.set_stage("info")
        info, meta, fetched_at = res
        st.title, st.artists = meta.title, meta.artists_text

        cover_task = asyncio.create_task(sc.prepare_cover(meta, workdir))  # parallel with download
        try:
            st.set_stage("download")
            src = await sc.download(info, url, workdir, fetched_at, st.on_download)

            st.set_stage("process")
            cover, thumb = await cover_task
            audio = await sc.build_audio(src, cover, meta, workdir)
            size = audio.stat().st_size
            if size > self.s.max_file_mb * 1024 * 1024:
                raise sc.ScError(ui.TOO_BIG)

            st.set_stage("upload")
            st.total = size
            await self._send(job, meta, audio, thumb, st, extra_desc)
        finally:
            if not cover_task.done():
                cover_task.cancel()

    async def _run_playlist(self, job: Job, pl: sc.PlaylistInfo, st: ui.ProgressState,
                            workdir: Path) -> str:
        key = job.owner_id or job.chat_id
        if key in self.active_playlists:
            raise sc.ScError(ui.PL_BUSY)
        self.active_playlists.add(key)
        self.running[job.token] = job
        entries = pl.entries[: self._cap(job)]
        gap = self.s.playlist_gap_group if job.is_group else self.s.playlist_gap_private
        st.pl_title, st.pl_total = pl.title, len(entries)
        ok, failed, handled = 0, [], 0
        try:
            for i, e in enumerate(entries, 1):
                if job.stop.is_set():
                    break
                st.pl_index = i
                st.title, st.artists = e.get("title") or "", ""
                tdir = workdir / f"t{i}"
                tdir.mkdir(exist_ok=True)
                try:
                    if self.sem.locked():  # single tracks of other users interleave fairly
                        st.set_stage("queued")
                    async with self.sem:
                        st.set_stage("info")
                        res = await sc.fetch(e["url"])
                        if isinstance(res, sc.PlaylistInfo):
                            raise sc.ScError(ui.GENERIC_ERROR)
                        await self._deliver_track(job, st, tdir, e["url"], res, extra_desc=False)
                    ok += 1
                except sc.ScError:
                    failed.append(e.get("title") or e["url"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("playlist track failed: %s", e.get("url"))
                    failed.append(e.get("title") or e["url"])
                finally:
                    handled += 1
                    await asyncio.to_thread(shutil.rmtree, tdir, True)
                if i < len(entries) and not job.stop.is_set():
                    await asyncio.sleep(gap)
        finally:
            self.active_playlists.discard(key)
            self.running.pop(job.token, None)
        stopped = job.stop.is_set() and handled < len(entries)
        return ui.playlist_summary(pl.title, ok, failed, stopped, pl.total - len(entries))

    async def _send(self, job: Job, meta: sc.TrackMeta, audio: Path,
                    thumb: Optional[Path], st: ui.ProgressState, extra_desc: bool = True) -> None:
        cap, truncated = ui.caption(meta)
        reply = ReplyParameters(message_id=job.link_msg_id, allow_sending_without_reply=True)
        for attempt in range(3):
            st.done, st.frac = 0, 0.0
            st.t0 = time.monotonic()
            try:
                await self.bot.send_audio(
                    chat_id=job.chat_id,
                    message_thread_id=job.thread_id,
                    audio=ProgressFile(audio, st),
                    title=meta.title[:255],
                    performer=meta.artists_text[:255],
                    duration=int(meta.duration) or None,
                    thumbnail=FSInputFile(thumb) if thumb else None,
                    caption=cap,
                    reply_parameters=reply,
                    reply_markup=ui.link_keyboard(meta.url),
                    request_timeout=900,
                )
                break
            except TelegramRetryAfter as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(e.retry_after + 1)
        if extra_desc and truncated and meta.description:
            from html import escape
            text = escape(meta.description)[:3800]
            with contextlib.suppress(TelegramAPIError):
                await self.bot.send_message(
                    job.chat_id, f"<blockquote expandable>{text}</blockquote>",
                    message_thread_id=job.thread_id, reply_parameters=reply,
                )

    # ------------------------------------------------------------------ #
    async def janitor(self) -> None:
        """Expire confirmation messages nobody answered."""
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for token, job in list(self.pending.items()):
                if now - job.created > self.s.pending_ttl:
                    self.pending.pop(token, None)
                    if job.prefetch:
                        job.prefetch.cancel()
                    await safe_edit(job.msg, ui.EXPIRED)
                    self.spawn(self._delete_later(job.msg, 10))


async def run(settings: Settings) -> None:
    if settings.bot_api_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.bot_api_url, is_local=True))
    else:
        session = AiohttpSession()
    bot = Bot(
        settings.token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    app = SoundCloudBot(bot, settings)
    dp = Dispatcher()
    dp.include_router(app.router)

    me = await bot.get_me()
    log.info("started as @%s", me.username)
    with contextlib.suppress(TelegramAPIError):
        await bot.set_my_commands([
            BotCommand(command="start", description="شروع"),
            BotCommand(command="help", description="راهنما"),
        ])

    janitor = asyncio.create_task(app.janitor())
    try:
        await dp.start_polling(
            bot, allowed_updates=["message", "callback_query"], drop_pending_updates=True
        )
    finally:
        janitor.cancel()
        await bot.session.close()
