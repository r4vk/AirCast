"""Find AirPlay audio receivers (RAOP) on the LAN."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import pyatv
from pyatv.const import Protocol
from pyatv.interface import BaseConfig

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AirPlayTarget:
    identifier: str
    name: str
    address: str
    model: str
    conf: BaseConfig


async def scan(timeout: int = 5, hosts: list[str] | None = None) -> list[AirPlayTarget]:
    loop = asyncio.get_running_loop()
    confs = await pyatv.scan(loop, timeout=timeout, protocol=Protocol.RAOP, hosts=hosts)
    targets: list[AirPlayTarget] = []
    for conf in confs:
        if conf.get_service(Protocol.RAOP) is None or not conf.identifier:
            continue
        info = conf.device_info
        targets.append(
            AirPlayTarget(
                identifier=conf.identifier,
                name=conf.name,
                address=str(conf.address),
                model=info.raw_model or str(info.model),
                conf=conf,
            )
        )
    _LOGGER.debug("Scan found %d AirPlay receiver(s)", len(targets))
    return targets
