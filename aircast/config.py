"""Configuration: optional YAML file, overridden by AIRCAST_* environment variables."""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from aircast import media as kinds

if TYPE_CHECKING:
    from aircast.airplay.discovery import AirPlayTarget

_LOGGER = logging.getLogger(__name__)

DEVICE_TYPES = ("apple_tv", "tv", "homepod", "airport", "computer", "speaker")

# What each device type is published for unless configured otherwise. Video is only
# offered when the device itself advertises AirPlay video support.
DEFAULT_MEDIA_TYPES: dict[str, list[str]] = {
    "apple_tv": [kinds.AUDIO, kinds.VIDEO],
    "tv": [kinds.AUDIO, kinds.VIDEO],
    "computer": [kinds.AUDIO, kinds.VIDEO],
    "homepod": [kinds.AUDIO],
    "airport": [kinds.AUDIO],
    "speaker": [kinds.AUDIO],
}


@dataclass
class DeviceOverride:
    """Per-target settings, keyed by AirPlay identifier (usually the MAC) or name.

    None means "not set here": the next layer (devices.yaml, then global options) decides.
    """

    enabled: bool | None = None
    name: str | None = None
    dlna: bool | None = None
    cast: bool | None = None
    media: list[str] | None = None
    type: str | None = None
    password: str | None = None
    credentials: str | None = None
    airplay_credentials: str | None = None
    airplay_version: str | None = None

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> DeviceOverride:
        if not isinstance(raw, dict):
            if raw is not None:
                _LOGGER.warning("%s: expected a mapping, got %r", where, raw)
            return cls()
        known = {item.name for item in fields(cls)}
        values = {}
        for key, value in raw.items():
            if key in known:
                values[key] = value
            elif key != "info":  # informational block written by AirCast itself
                _LOGGER.warning("%s: unknown key ignored: %s", where, key)
        override = cls(**values)
        if override.media is not None:
            override.media = normalize_media(override.media, where)
        if override.type is not None and override.type not in DEVICE_TYPES:
            _LOGGER.warning("%s: unknown type %r (expected one of %s)", where, override.type,
                            ", ".join(DEVICE_TYPES))
            override.type = None
        return override

    def merged(self, other: DeviceOverride | None) -> DeviceOverride:
        """This override with every field that `other` sets taking precedence."""
        if other is None:
            return self
        result = DeviceOverride(**{item.name: getattr(self, item.name) for item in fields(self)})
        for item in fields(other):
            value = getattr(other, item.name)
            if value is not None:
                setattr(result, item.name, value)
        return result


def normalize_media(value: Any, where: str) -> list[str]:
    items = value.split(",") if isinstance(value, str) else list(value or [])
    result: list[str] = []
    for item in (str(i).strip().lower() for i in items):
        if not item:
            continue
        if item not in kinds.KINDS:
            _LOGGER.warning("%s: unknown media type %r (expected audio, video, image)", where, item)
        elif item not in result:
            result.append(item)
    return result


@dataclass(frozen=True)
class DeviceSettings:
    """Effective settings for one discovered AirPlay device."""

    enabled: bool
    dlna: bool
    cast: bool
    media: frozenset[str]
    device_type: str
    display_name: str
    reason: str = ""  # why the device is not bridged, when it is not
    override: DeviceOverride = field(default_factory=DeviceOverride, compare=False)

    @property
    def bridged(self) -> bool:
        return self.enabled and (self.dlna or self.cast) and bool(self.media)

    @property
    def signature(self) -> tuple:
        """Everything that requires re-creating the virtual device when it changes."""
        o = self.override
        return (self.dlna, self.cast, self.media, o.password, o.credentials,
                o.airplay_credentials, o.airplay_version)

    @property
    def status(self) -> str:
        return "bridged" if self.bridged else f"ignored: {self.reason}"


@dataclass
class Config:
    # Network
    host_ip: str | None = None
    http_port: int = 49152
    cast_base_port: int = 8010

    # Features
    dlna_enabled: bool = True
    cast_enabled: bool = True

    # Discovery
    scan_interval: int = 30
    scan_timeout: int = 5
    remove_after_missed_scans: int = 3
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    exclude_dlna: list[str] = field(default_factory=list)
    exclude_cast: list[str] = field(default_factory=list)
    enable_new_devices: bool = True
    # List of discovered devices, relative to state_dir; empty string disables it.
    devices_file: str = "devices.yaml"

    # Media types published per device type (audio | video | image).
    media_types: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_MEDIA_TYPES.items()}
    )

    # Naming. {name} is the AirPlay device name.
    name_format: str = "{name} (AirCast)"

    # AirPlay output
    airplay_version: str = "auto"  # auto | 1 | 2
    output_latency: float = 2.0  # seconds, used for position reporting only

    # Google Cast
    cast_model: str = "AirCast"
    cast_auth: str = "selfsigned"  # selfsigned | files
    cast_auth_dir: str | None = None

    # Misc
    ffmpeg: str = "ffmpeg"
    state_dir: str = "/data"

    # Logging
    log_level: str = "INFO"
    log_file: str | None = None  # relative paths are resolved against state_dir
    log_max_size: int = 10  # MiB per file before rotating
    log_backups: int = 5  # rotated files to keep

    devices: dict[str, DeviceOverride] = field(default_factory=dict)

    def override_for(
        self, identifier: str, name: str, discovered: DeviceOverride | None = None
    ) -> DeviceOverride:
        """devices.yaml entry (if any) overlaid with the config file's `devices:` entry."""
        own = self.devices.get(identifier) or self.devices.get(name)
        return (discovered or DeviceOverride()).merged(own)

    def _exclusion(self, identifier: str, name: str) -> str:
        keys = {identifier, name}
        if self.include and not keys & set(self.include):
            return "not in include"
        if keys & set(self.exclude):
            return "in exclude"
        return ""

    def is_wanted(
        self, identifier: str, name: str, discovered: DeviceOverride | None = None
    ) -> bool:
        if self._exclusion(identifier, name):
            return False
        override = self.override_for(identifier, name, discovered)
        return self.enable_new_devices if override.enabled is None else override.enabled

    def display_name(
        self, identifier: str, name: str, discovered: DeviceOverride | None = None
    ) -> str:
        override = self.override_for(identifier, name, discovered)
        if override.name:
            return override.name
        return self.name_format.format(name=name)

    def default_media(self, device_type: str, supports_video: bool) -> list[str]:
        media = self.media_types.get(device_type, DEFAULT_MEDIA_TYPES["speaker"])
        return [m for m in media if m in kinds.PLAYABLE_KINDS and (m != kinds.VIDEO or supports_video)]

    def resolve(
        self, target: AirPlayTarget, discovered: DeviceOverride | None = None
    ) -> DeviceSettings:
        ident, name = target.identifier, target.name
        keys = {ident, name}
        override = self.override_for(ident, name, discovered)
        device_type = override.type or target.device_type

        reason = self._exclusion(ident, name)
        enabled = not reason and (
            self.enable_new_devices if override.enabled is None else override.enabled
        )
        if not reason and not enabled:
            reason = "disabled"

        dlna = self.dlna_enabled and override.dlna is not False and not keys & set(self.exclude_dlna)
        cast = self.cast_enabled and override.cast is not False and not keys & set(self.exclude_cast)
        if enabled and not (dlna or cast):
            reason = "DLNA and Cast both off"

        wanted = override.media if override.media is not None else self.media_types.get(
            device_type, DEFAULT_MEDIA_TYPES["speaker"])
        media = {m for m in wanted if m in kinds.PLAYABLE_KINDS}
        if kinds.VIDEO in media and not target.supports_video:
            media.discard(kinds.VIDEO)
            if override.media is not None:
                _LOGGER.warning("'%s' does not support AirPlay video; publishing it without video",
                                name)
        if enabled and (dlna or cast) and not media:
            reason = "no playable media types"

        return DeviceSettings(
            enabled=bool(enabled), dlna=dlna, cast=cast, media=frozenset(media),
            device_type=device_type,
            display_name=override.name or self.name_format.format(name=name),
            reason=reason, override=override,
        )

    def devices_path(self) -> Path | None:
        if not self.devices_file:
            return None
        return Path(self.state_dir) / self.devices_file

    def log_path(self) -> Path | None:
        if not self.log_file:
            return None
        return Path(self.state_dir) / self.log_file

    def resolved_host_ip(self) -> str:
        return self.host_ip or detect_host_ip()


def detect_host_ip() -> str:
    """Return the IP of the interface that routes to the LAN (no packets are sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _coerce(value: str, current: Any) -> Any:
    if isinstance(current, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, list):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def load_config(path: str | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    config = Config()

    path = path or env.get("AIRCAST_CONFIG")
    if path is None:
        default = Path(env.get("AIRCAST_STATE_DIR", config.state_dir)) / "config.yaml"
        path = str(default) if default.exists() else None

    if path:
        _LOGGER.info("Loading config from %s", path)
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        _apply_mapping(config, raw)

    for item in fields(Config):
        if item.name in ("devices", "media_types"):
            continue
        value = env.get(f"AIRCAST_{item.name.upper()}")
        if value is not None:
            setattr(config, item.name, _coerce(value, getattr(config, item.name)))

    return config


def _apply_mapping(config: Config, raw: dict[str, Any]) -> None:
    known = {item.name for item in fields(Config)}
    for key, value in raw.items():
        if key == "devices":
            config.devices = {
                str(dev_id): DeviceOverride.from_mapping(opts or {}, f"devices.{dev_id}")
                for dev_id, opts in (value or {}).items()
            }
        elif key == "media_types":
            for dev_type, media in (value or {}).items():
                if dev_type not in DEVICE_TYPES:
                    _LOGGER.warning("media_types: unknown device type ignored: %s", dev_type)
                    continue
                config.media_types[dev_type] = normalize_media(media, f"media_types.{dev_type}")
        elif key in known:
            setattr(config, key, value)
        else:
            _LOGGER.warning("Unknown config key ignored: %s", key)
