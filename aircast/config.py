"""Configuration: optional YAML file, overridden by AIRCAST_* environment variables."""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

_LOGGER = logging.getLogger(__name__)


@dataclass
class DeviceOverride:
    """Per-target settings, keyed by AirPlay identifier (usually the MAC) or name."""

    enabled: bool = True
    name: str | None = None
    password: str | None = None
    credentials: str | None = None
    airplay_version: str | None = None


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
    enable_new_devices: bool = True

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
    log_level: str = "INFO"

    devices: dict[str, DeviceOverride] = field(default_factory=dict)

    def override_for(self, identifier: str, name: str) -> DeviceOverride:
        return self.devices.get(identifier) or self.devices.get(name) or DeviceOverride()

    def is_wanted(self, identifier: str, name: str) -> bool:
        keys = {identifier, name}
        if self.include and not keys & set(self.include):
            return False
        if keys & set(self.exclude):
            return False
        override = self.devices.get(identifier) or self.devices.get(name)
        if override is not None:
            return override.enabled
        return self.enable_new_devices

    def display_name(self, identifier: str, name: str) -> str:
        override = self.override_for(identifier, name)
        if override.name:
            return override.name
        return self.name_format.format(name=name)

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
        if item.name == "devices":
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
                str(dev_id): DeviceOverride(**(opts or {})) for dev_id, opts in value.items()
            }
        elif key in known:
            setattr(config, key, value)
        else:
            _LOGGER.warning("Unknown config key ignored: %s", key)
