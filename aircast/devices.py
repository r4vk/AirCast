"""devices.yaml: every AirPlay device AirCast has seen, editable by the user.

AirCast writes an entry for each newly discovered device (like AirConnect's `-i`), keeps
whatever the user changed, and re-reads the file when it changes, so settings apply
without a restart. The `info` block is informational and rewritten by AirCast.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from aircast.config import DeviceOverride

if TYPE_CHECKING:
    from aircast.airplay.discovery import AirPlayTarget
    from aircast.config import Config, DeviceSettings

_LOGGER = logging.getLogger(__name__)

HEADER = """\
# AirCast: AirPlay devices found on the network. Generated automatically; your edits are kept.
#
# Per device (keyed by AirPlay identifier):
#   enabled: false        ignore the device completely
#   dlna: false           do not publish it as a DLNA/UPnP renderer
#   cast: false           do not publish it as a Google Cast receiver
#   media: [audio, video] media types to publish (video needs an AirPlay video device)
#   name: My speaker      name of the virtual device
#   type: tv              override the detected device type
#   password / credentials / airplay_credentials / airplay_version: see README
# `info` is refreshed by AirCast on every scan. Settings under `devices:` in config.yaml
# take precedence over this file.
"""


class DevicesFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, dict[str, Any]] = {}
        self._overrides: dict[str, DeviceOverride] = {}
        self._mtime: int | None = None
        self._write_failed = False

    def get(self, identifier: str) -> DeviceOverride | None:
        return self._overrides.get(identifier)

    def reload_if_changed(self) -> bool:
        """Re-read the file if it changed on disk. Returns True when it was reloaded."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            _LOGGER.warning("Cannot read %s, keeping previous settings: %s", self.path, exc)
            self._mtime = mtime
            return False
        devices = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(devices, dict):
            devices = {}
        self._entries = {str(k): dict(v) if isinstance(v, dict) else {} for k, v in devices.items()}
        self._overrides = {
            ident: DeviceOverride.from_mapping(entry, f"{self.path.name}: {ident}")
            for ident, entry in self._entries.items()
        }
        if self._mtime is not None:
            _LOGGER.info("Reloaded %s", self.path)
        self._mtime = mtime
        return True

    def update(self, config: Config, seen: list[tuple[AirPlayTarget, DeviceSettings]]) -> None:
        """Add new devices and refresh `info`; write only when something changed."""
        changed = not self.path.exists()  # create it on the first scan, even if empty
        for target, settings in seen:
            entry = self._entries.get(target.identifier)
            if entry is None:
                entry = {
                    "enabled": config.enable_new_devices,
                    "dlna": True,
                    "cast": True,
                    "media": config.default_media(target.device_type, target.supports_video),
                }
                self._entries[target.identifier] = entry
                _LOGGER.info("Added '%s' to %s", target.name, self.path)
                changed = True
            info = {
                "name": target.name,
                "model": target.model,
                "type": target.device_type,
                "address": target.address,
                "video": target.supports_video,
                "status": settings.status,
            }
            if entry.get("info") != info:
                entry.pop("info", None)
                entry["info"] = info  # keep it last, after the editable keys
                changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        body = yaml.safe_dump({"devices": self._entries}, sort_keys=False, allow_unicode=True,
                              default_flow_style=None)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(HEADER + body, encoding="utf-8")
            os.replace(tmp, self.path)
            self._mtime = self.path.stat().st_mtime_ns
            self._overrides = {
                ident: DeviceOverride.from_mapping(entry, f"{self.path.name}: {ident}")
                for ident, entry in self._entries.items()
            }
            self._write_failed = False
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            if not self._write_failed:
                _LOGGER.warning("Cannot write %s: %s", self.path, exc)
            self._write_failed = True
