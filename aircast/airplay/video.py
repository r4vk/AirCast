"""VideoOutput that hands a URL to an AirPlay video receiver (Apple TV, AirPlay TVs)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

import pyatv
from pyatv.const import Protocol
from pyatv.interface import BaseConfig
from pyatv.storage.memory_storage import MemoryStorage

from aircast.player import Media, VideoOutput

_LOGGER = logging.getLogger(__name__)


class AirPlayVideoOutput(VideoOutput):
    """Plays via pyatv's AirPlay `play_url`; the receiver fetches the media itself.

    Apple TVs usually require AirPlay pairing first (`atvremote --id <id> --protocol airplay
    pair`); the resulting credentials go into `airplay_credentials` for the device.
    """

    def __init__(
        self, conf: BaseConfig, *, credentials: str | None = None, password: str | None = None
    ) -> None:
        self._conf = conf
        self._credentials = credentials
        self._password = password
        self._storage = MemoryStorage()
        self._atv: pyatv.interface.AppleTV | None = None
        self._stopping = False

    def update_config(self, conf: BaseConfig) -> None:
        self._conf = conf

    async def play_url(self, media: Media, position: float, on_started: Callable[[], None]) -> None:
        settings = await self._storage.get_settings(self._conf)
        settings.protocols.airplay.credentials = self._credentials
        settings.protocols.airplay.password = self._password
        loop = asyncio.get_running_loop()
        self._stopping = False
        atv = await pyatv.connect(self._conf, loop, protocol=Protocol.AirPlay,
                                  storage=self._storage)
        self._atv = atv
        try:
            if self._stopping:
                return
            # pyatv only returns once playback has ended; the receiver starts buffering
            # right after the request is accepted.
            on_started()
            await atv.stream.play_url(media.url, position=position)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            # pyatv cancels its own play task when the connection is closed by stop().
        finally:
            if self._atv is atv:
                self._atv = None
                atv.close()

    async def stop(self) -> None:
        self._stopping = True
        atv, self._atv = self._atv, None
        if atv is None:
            return
        with contextlib.suppress(Exception):
            await atv.remote_control.stop()
        atv.close()
