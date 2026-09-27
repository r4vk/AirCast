"""Find AirPlay receivers (RAOP) on the LAN and work out what kind of device each is."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import pyatv
from pyatv.const import DeviceModel, OperatingSystem, Protocol
from pyatv.interface import BaseConfig
from pyatv.protocols.airplay.utils import AirPlayFlags, parse_features

from aircast.config import DEVICE_TYPES

_LOGGER = logging.getLogger(__name__)

# Device types (see config.DEVICE_TYPES), used for per-type `media_types` defaults.
APPLE_TV = "apple_tv"
TV = "tv"
HOMEPOD = "homepod"
AIRPORT = "airport"
COMPUTER = "computer"
SPEAKER = "speaker"
assert set(DEVICE_TYPES) == {APPLE_TV, TV, HOMEPOD, AIRPORT, COMPUTER, SPEAKER}

_VIDEO_FLAGS = AirPlayFlags.SupportsAirPlayVideoV1 | AirPlayFlags.SupportsAirPlayVideoV2
_APPLE_TV_MODELS = {
    DeviceModel.AppleTVGen1, DeviceModel.Gen2, DeviceModel.Gen3, DeviceModel.Gen4,
    DeviceModel.Gen4K, DeviceModel.AppleTV4KGen2, DeviceModel.AppleTV4KGen3,
}
_HOMEPOD_MODELS = {DeviceModel.HomePod, DeviceModel.HomePodMini, DeviceModel.HomePodGen2}
_AIRPORT_MODELS = {DeviceModel.AirPortExpress, DeviceModel.AirPortExpressGen2}


@dataclass(frozen=True)
class AirPlayTarget:
    identifier: str
    name: str
    address: str
    model: str
    conf: BaseConfig
    device_type: str = SPEAKER
    supports_video: bool = False
    supports_photo: bool = False


def _features(conf: BaseConfig) -> AirPlayFlags:
    flags = AirPlayFlags(0)
    for protocol, key in ((Protocol.AirPlay, "features"), (Protocol.RAOP, "ft")):
        service = conf.get_service(protocol)
        value = service.properties.get(key) if service is not None else None
        if value:
            try:
                flags |= parse_features(value)
            except Exception:  # malformed TXT record from a third-party device
                _LOGGER.debug("Cannot parse AirPlay features %r of %s", value, conf.name)
    return flags


def classify(model: DeviceModel, raw_model: str, os: OperatingSystem, video: bool) -> str:
    raw = (raw_model or "").lower()
    # HomePods report tvOS too, so they must be recognised first.
    if model in _HOMEPOD_MODELS or raw.startswith(("audioaccessory", "homepod")):
        return HOMEPOD
    if model in _APPLE_TV_MODELS or os == OperatingSystem.TvOS or raw.startswith("appletv"):
        return APPLE_TV
    if model in _AIRPORT_MODELS or os == OperatingSystem.AirPortOS or raw.startswith("airport"):
        return AIRPORT
    if model == DeviceModel.Music or os == OperatingSystem.MacOS or raw.startswith(("mac", "imac")):
        return COMPUTER
    if video:
        # Third-party receivers that accept AirPlay video are televisions (Samsung, LG, ...).
        return TV
    return SPEAKER


def _raop_identifier(conf: BaseConfig) -> str | None:
    # Not conf.identifier: that prefers the AirPlay service's id once it is scanned too,
    # and the RAOP id is what identifiers in configs, UUIDs and Cast ports are keyed on.
    service = conf.get_service(Protocol.RAOP)
    return service.identifier if service is not None else None


def target_from_conf(conf: BaseConfig) -> AirPlayTarget:
    info = conf.device_info
    flags = _features(conf)
    # URL playback goes over the AirPlay service, so video needs it to be present too.
    video = bool(flags & _VIDEO_FLAGS) and conf.get_service(Protocol.AirPlay) is not None
    return AirPlayTarget(
        identifier=_raop_identifier(conf) or conf.identifier,
        name=conf.name,
        address=str(conf.address),
        model=info.raw_model or str(info.model),
        conf=conf,
        device_type=classify(info.model, info.raw_model or "", info.operating_system, video),
        supports_video=video,
        supports_photo=bool(flags & AirPlayFlags.SupportsAirPlayPhoto),
    )


async def scan(timeout: int = 5, hosts: list[str] | None = None) -> list[AirPlayTarget]:
    loop = asyncio.get_running_loop()
    confs = await pyatv.scan(
        loop, timeout=timeout, protocol={Protocol.RAOP, Protocol.AirPlay}, hosts=hosts
    )
    targets: list[AirPlayTarget] = []
    for conf in confs:
        if not _raop_identifier(conf):
            continue
        targets.append(target_from_conf(conf))
    _LOGGER.debug("Scan found %d AirPlay receiver(s)", len(targets))
    return targets
