"""Protocol-neutral playback engine: URL -> ffmpeg -> PCM -> AudioOutput.

The DLNA and Cast frontends both drive a Player; the Player owns the transcoder and
reports state changes back to every frontend through listeners.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_SIZE = 2
FRAME_BYTES = CHANNELS * SAMPLE_SIZE


class PlayerState(StrEnum):
    NO_MEDIA = "NO_MEDIA_PRESENT"
    STOPPED = "STOPPED"
    TRANSITIONING = "TRANSITIONING"
    PLAYING = "PLAYING"
    PAUSED = "PAUSED_PLAYBACK"


class IdleReason(StrEnum):
    NONE = ""
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    ERROR = "ERROR"


@dataclass
class Media:
    url: str
    mime: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    artwork: str | None = None
    duration: float | None = None
    live: bool = False
    didl: str | None = None  # original DIDL-Lite metadata, echoed back to DLNA controllers
    cast: dict[str, Any] = field(default_factory=dict)  # original Cast media object


class PcmStream:
    """Raw s16le/44.1k/stereo stream read from a transcoder, with frame accounting."""

    def __init__(self, reader: asyncio.StreamReader, on_first_frame: Callable[[], None]):
        self._reader = reader
        self._on_first_frame = on_first_frame
        self.frames = 0

    @property
    def seconds(self) -> float:
        return self.frames / SAMPLE_RATE

    async def read_frames(self, nframes: int) -> bytes:
        try:
            data = await self._reader.readexactly(nframes * FRAME_BYTES)
        except asyncio.IncompleteReadError as exc:
            data = exc.partial[: len(exc.partial) - len(exc.partial) % FRAME_BYTES]
        if data and self.frames == 0:
            self._on_first_frame()
        self.frames += len(data) // FRAME_BYTES
        return data


class AudioOutput(ABC):
    """A sink that can play one PcmStream at a time."""

    @abstractmethod
    async def play(self, stream: PcmStream, media: Media) -> None:
        """Play until the stream ends or stop() is called."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the current play() call, if any."""

    @abstractmethod
    async def set_volume(self, level: float) -> None:
        """Set volume, 0-100. Applied on next play() when idle."""

    async def close(self) -> None:
        await self.stop()


class Player:
    def __init__(
        self,
        output: AudioOutput,
        *,
        ffmpeg: str = "ffmpeg",
        output_latency: float = 2.0,
        name: str = "player",
        allow_local_files: bool = False,
    ) -> None:
        self.name = name
        self.output = output
        self.ffmpeg = ffmpeg
        self.output_latency = output_latency
        # URLs come from unauthenticated LAN senders: never let ffmpeg open local files
        # (directly or via file:/concat: references inside a fetched playlist).
        self.allow_local_files = allow_local_files

        self.state = PlayerState.NO_MEDIA
        self.idle_reason = IdleReason.NONE
        self.last_error: str | None = None
        self.media: Media | None = None
        self.next_media: Media | None = None
        self.volume = 50.0
        self.muted = False
        # Increments on every media change, so frontends can tell tracks apart.
        self.media_generation = 0

        self._listeners: list[Callable[[Player], None]] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stream: PcmStream | None = None
        self._run_id = 0
        self._offset = 0.0
        self._paused_at = 0.0

    # -- listeners ---------------------------------------------------------

    def add_listener(self, callback: Callable[[Player], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Player], None]) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback(self)
            except Exception:  # a broken frontend must not break playback
                _LOGGER.exception("Player listener failed")

    def _set_state(self, state: PlayerState, reason: IdleReason = IdleReason.NONE) -> None:
        self.state = state
        self.idle_reason = reason
        self._notify()

    # -- queries -------------------------------------------------------------

    @property
    def position(self) -> float:
        if self.state == PlayerState.PLAYING and self._stream is not None:
            return self._offset + max(0.0, self._stream.seconds - self.output_latency)
        if self.state == PlayerState.PAUSED:
            return self._paused_at
        return 0.0

    # -- commands ----------------------------------------------------------

    async def load(self, media: Media, *, autoplay: bool = False, start: float = 0.0) -> None:
        async with self._lock:
            await self._halt()
            self.media = media
            self.next_media = None
            self.media_generation += 1
            self.last_error = None
            self._paused_at = start
            if autoplay:
                await self._start(start)
            else:
                self._set_state(PlayerState.STOPPED)

    async def set_next(self, media: Media | None) -> None:
        self.next_media = media
        self._notify()

    async def play(self) -> None:
        async with self._lock:
            if self.media is None:
                raise PlayerError("no media loaded")
            if self.state in (PlayerState.PLAYING, PlayerState.TRANSITIONING):
                return
            start = self._paused_at if self.state == PlayerState.PAUSED else 0.0
            await self._start(0.0 if self.media.live else start)

    async def pause(self) -> None:
        async with self._lock:
            if self.state not in (PlayerState.PLAYING, PlayerState.TRANSITIONING):
                return
            position = self.position
            await self._halt()
            self._paused_at = position
            self._set_state(PlayerState.PAUSED)

    async def stop(self) -> None:
        async with self._lock:
            await self._halt()
            self._paused_at = 0.0
            if self.media is None:
                self._set_state(PlayerState.NO_MEDIA)
            else:
                self._set_state(PlayerState.STOPPED, IdleReason.CANCELLED)

    async def seek(self, seconds: float) -> None:
        async with self._lock:
            if self.media is None:
                raise PlayerError("no media loaded")
            if self.media.live:
                raise PlayerError("live stream is not seekable")
            seconds = max(0.0, seconds)
            if self.state in (PlayerState.PLAYING, PlayerState.TRANSITIONING):
                await self._start(seconds)
            else:
                self._paused_at = seconds
                self._notify()

    async def next(self) -> None:
        async with self._lock:
            if self.next_media is None:
                raise PlayerError("no next media")
            await self._advance()

    async def set_volume(self, level: float) -> None:
        self.volume = min(100.0, max(0.0, float(level)))
        self.muted = False
        await self.output.set_volume(self.volume)
        self._notify()

    async def set_muted(self, muted: bool) -> None:
        self.muted = muted
        await self.output.set_volume(0.0 if muted else self.volume)
        self._notify()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._halt()
        await self.output.close()

    # -- internals ---------------------------------------------------------

    def _ffmpeg_args(self, url: str, offset: float) -> list[str]:
        args = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if not self.allow_local_files:
            args += ["-protocol_whitelist", "http,https,tcp,tls,crypto"]
        if url.startswith(("http://", "https://")):
            args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if offset > 0:
            args += ["-ss", f"{offset:.3f}"]
        args += ["-i", url, "-vn", "-f", "s16le", "-acodec", "pcm_s16le"]
        args += ["-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "pipe:1"]
        return args

    async def _start(self, offset: float) -> None:
        await self._halt()
        assert self.media is not None
        self._run_id += 1
        run_id = self._run_id
        self._offset = offset
        self._paused_at = offset

        _LOGGER.info("[%s] Playing %s from %.1fs", self.name, self.media.url, offset)
        proc = await asyncio.create_subprocess_exec(
            *self._ffmpeg_args(self.media.url, offset),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        def first_frame() -> None:
            if run_id == self._run_id and self.state == PlayerState.TRANSITIONING:
                self._set_state(PlayerState.PLAYING)

        assert proc.stdout is not None
        self._proc = proc
        self._stream = PcmStream(proc.stdout, first_frame)
        self._set_state(PlayerState.TRANSITIONING)
        # Drain stderr continuously: a full pipe would block ffmpeg and freeze the audio.
        stderr_task = asyncio.create_task(_drain_tail(proc.stderr))
        self._task = asyncio.create_task(
            self._run(run_id, proc, stderr_task, self._stream, self.media)
        )

    async def _run(
        self,
        run_id: int,
        proc: asyncio.subprocess.Process,
        stderr_task: asyncio.Task,
        stream: PcmStream,
        media: Media,
    ) -> None:
        error: str | None = None
        try:
            await self.output.play(stream, media)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.warning("[%s] Output failed: %s", self.name, exc)
            error = f"output: {exc}"
        finally:
            stderr = await _terminate(proc, stderr_task)

        if error is None and proc.returncode not in (0, None) and stream.frames == 0:
            error = f"ffmpeg: {stderr.strip()[-300:] or f'exit code {proc.returncode}'}"

        # Check before taking the lock: _halt() holds it while waiting for this task.
        if run_id != self._run_id:
            return  # superseded by stop/seek/load; the new owner sets the state
        async with self._lock:
            if run_id != self._run_id:
                return
            self._task = None  # this task is finishing; do not await it from _halt()
            self._proc = None
            if error:
                _LOGGER.warning("[%s] Playback failed: %s", self.name, error)
                self.last_error = error
                self._set_state(PlayerState.STOPPED, IdleReason.ERROR)
            elif self.next_media is not None:
                await self._advance()
            else:
                self._paused_at = 0.0
                self._set_state(PlayerState.STOPPED, IdleReason.FINISHED)

    async def _advance(self) -> None:
        self.media = self.next_media
        self.next_media = None
        self.media_generation += 1
        await self._start(0.0)

    async def _halt(self) -> None:
        """Stop the running stream (if any) without touching the public state."""
        self._run_id += 1
        task, self._task = self._task, None
        proc, self._proc = self._proc, None
        if task is None or task is asyncio.current_task():
            return
        with contextlib.suppress(Exception):
            await self.output.stop()
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        except Exception:
            pass


class PlayerError(Exception):
    pass


async def _terminate(proc: asyncio.subprocess.Process, stderr_task: asyncio.Task) -> str:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    # Drain both pipes to EOF so their transports close instead of leaking per track.
    with contextlib.suppress(Exception):
        if proc.stdout is not None:
            await asyncio.wait_for(proc.stdout.read(), timeout=2)
    stderr = b""
    try:
        stderr = await asyncio.wait_for(stderr_task, timeout=2)
    except Exception:
        stderr_task.cancel()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5)
    return stderr.decode(errors="replace")


async def _drain_tail(reader: asyncio.StreamReader | None, limit: int = 4096) -> bytes:
    """Read a stream to EOF, keeping only the last `limit` bytes."""
    if reader is None:
        return b""
    tail = b""
    while chunk := await reader.read(4096):
        tail = (tail + chunk)[-limit:]
    return tail
