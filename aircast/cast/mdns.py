"""Advertise virtual Cast receivers as _googlecast._tcp over mDNS."""

from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

_LOGGER = logging.getLogger(__name__)

SERVICE_TYPE = "_googlecast._tcp.local."
# Capability bitmask advertised by audio-only receivers (AUDIO_OUT plus the audio-group
# flag), so senders present the device as a speaker rather than a screen.
AUDIO_CAPABILITIES = "2052"


class CastAdvertiser:
    def __init__(self, host_ip: str) -> None:
        self.host_ip = host_ip
        self._zc = AsyncZeroconf(interfaces=[host_ip], ip_version=IPVersion.V4Only)
        self._infos: dict[str, ServiceInfo] = {}

    async def register(self, cast_id: str, friendly_name: str, model: str, port: int) -> None:
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{model}-{cast_id}.{SERVICE_TYPE}",
            port=port,
            addresses=[socket.inet_aton(self.host_ip)],
            server=f"{cast_id}.local.",
            properties={
                "id": cast_id,
                "cd": cast_id.upper(),
                "rm": "",
                "ve": "05",
                "md": model,
                "ic": "/setup/icon.png",
                "fn": friendly_name,
                "ca": AUDIO_CAPABILITIES,
                "st": "0",
                "bs": "",
                "nf": "1",
                "rs": "",
            },
        )
        await self.unregister(cast_id)
        await self._zc.async_register_service(info, allow_name_change=True)
        self._infos[cast_id] = info
        _LOGGER.info("mDNS: advertising '%s' as Cast device on port %d", friendly_name, port)

    async def unregister(self, cast_id: str) -> None:
        info = self._infos.pop(cast_id, None)
        if info is not None:
            await self._zc.async_unregister_service(info)

    async def close(self) -> None:
        for cast_id in list(self._infos):
            await self.unregister(cast_id)
        await self._zc.async_close()
