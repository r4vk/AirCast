"""AudioOutput that streams PCM to an AirPlay receiver using pyatv."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pyatv
import pyatv.protocols.raop as _raop
from pyatv.const import Protocol
from pyatv.interface import BaseConfig, MediaMetadata
from pyatv.protocols.raop.audio_source import AudioSource
from pyatv.settings import AirPlayVersion
from pyatv.storage.memory_storage import MemoryStorage

from aircast.player import CHANNELS, SAMPLE_RATE, SAMPLE_SIZE, AudioOutput, Media, PcmStream

_LOGGER = logging.getLogger(__name__)


class _PcmAudioSource(AudioSource):
    """Feeds raw PCM to pyatv, bypassing its miniaudio decoder (which cannot handle pipes)."""

    def __init__(self, stream: PcmStream) -> None:
        self._stream = stream

    async def readframes(self, nframes: int) -> bytes:
        return await self._stream.read_frames(nframes)

    async def get_metadata(self) -> MediaMetadata:
        return MediaMetadata()

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def channels(self) -> int:
        return CHANNELS

    @property
    def sample_size(self) -> int:
        return SAMPLE_SIZE

    @property
    def duration(self) -> int:
        return 0


def _install_source_hook() -> None:
    """Let RaopStream.stream_file accept a ready AudioSource.

    pyatv 0.18 only accepts paths, URLs or file objects, all of which go through miniaudio.
    """
    original = _raop.open_source
    if getattr(original, "_aircast", False):
        return

    async def open_source(source, *args, **kwargs):
        if isinstance(source, AudioSource):
            return source
        return await original(source, *args, **kwargs)

    open_source._aircast = True  # type: ignore[attr-defined]
    _raop.open_source = open_source


_install_source_hook()


class AirPlayOutput(AudioOutput):
    def __init__(
        self,
        conf: BaseConfig,
        *,
        airplay_version: str = "auto",
        password: str | None = None,
        credentials: str | None = None,
    ) -> None:
        self._conf = conf
        self._storage = MemoryStorage()
        self._airplay_version = airplay_version
        self._password = password
        self._credentials = credentials
        self._atv: pyatv.interface.AppleTV | None = None
        self._pending_volume: float | None = None
        self._connecting: asyncio.Future | None = None
        self._lock = asyncio.Lock()

    def update_config(self, conf: BaseConfig) -> None:
        """Take a fresher scan result (e.g. after the target changed IP)."""
        self._conf = conf

    @property
    def address(self) -> str:
        return str(self._conf.address)

    @property
    def volume(self) -> float | None:
        if self._atv is not None:
            with contextlib.suppress(Exception):
                return self._atv.audio.volume
        return self._pending_volume

    async def _connect(self) -> pyatv.interface.AppleTV:
        settings = await self._storage.get_settings(self._conf)
        raop = settings.protocols.raop
        raop.protocol_version = AirPlayVersion(str(self._airplay_version))
        raop.password = self._password
        raop.credentials = self._credentials
        loop = asyncio.get_running_loop()
        return await pyatv.connect(
            self._conf, loop, protocol=Protocol.RAOP, storage=self._storage
        )

    async def play(self, stream: PcmStream, media: Media) -> None:
        async with self._lock:
            self._connecting = asyncio.ensure_future(self._connect())
            try:
                atv = await self._connecting
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                return  # stop() arrived while still connecting
            finally:
                self._connecting = None
            self._atv = atv
        try:
            if self._pending_volume is not None:
                with contextlib.suppress(Exception):
                    await atv.audio.set_volume(self._pending_volume)
            metadata = MediaMetadata(
                title=media.title or "AirCast",
                artist=media.artist,
                album=media.album,
                duration=media.duration,
            )
            await atv.stream.stream_file(_PcmAudioSource(stream), metadata=metadata)
        finally:
            async with self._lock:
                self._atv = None
            atv.close()

    async def stop(self) -> None:
        if self._connecting is not None:
            self._connecting.cancel()
        atv = self._atv
        if atv is not None:
            with contextlib.suppress(Exception):
                await atv.remote_control.stop()

    async def set_volume(self, level: float) -> None:
        self._pending_volume = level
        atv = self._atv
        if atv is not None:
            try:
                await atv.audio.set_volume(level)
            except Exception as exc:
                _LOGGER.debug("Volume change failed on %s: %s", self._conf.name, exc)
