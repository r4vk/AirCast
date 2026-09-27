"""Orchestration: discover AirPlay targets and run DLNA + Cast frontends for each."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from aircast.airplay.discovery import AirPlayTarget, scan
from aircast.airplay.output import AirPlayOutput
from aircast.airplay.video import AirPlayVideoOutput
from aircast.config import Config, DeviceSettings
from aircast.devices import DevicesFile
from aircast.dlna import scpd
from aircast.dlna.renderer import DlnaRenderer
from aircast.dlna.ssdp import SsdpDevice, SsdpServer
from aircast.media import VIDEO, sink_protocol_info
from aircast.player import Player

_LOGGER = logging.getLogger(__name__)

# Fixed namespace: identifiers derived from it stay stable across restarts and installs,
# so controllers remember the virtual devices.
_NAMESPACE = uuid.UUID("4f3c2a9e-6b1d-4c1e-9f5a-a1c0a57b12d3")


@dataclass
class DiscoveredDevice:
    """An AirPlay device seen on the network, bridged or not."""

    target: AirPlayTarget
    settings: DeviceSettings
    missed_scans: int = 0

    def to_json(self) -> dict:
        return {
            "id": self.target.identifier,
            "airplay_name": self.target.name,
            "address": self.target.address,
            "model": self.target.model,
            "type": self.settings.device_type,
            "supports_video": self.target.supports_video,
            "status": self.settings.status,
            "dlna": self.settings.dlna,
            "cast": self.settings.cast,
            "media": sorted(self.settings.media),
        }


@dataclass
class VirtualDevice:
    target: AirPlayTarget
    settings: DeviceSettings
    display_name: str
    player: Player
    output: AirPlayOutput
    dlna_key: str
    cast_id: str
    video: AirPlayVideoOutput | None = None
    cast_port: int | None = None
    dlna: DlnaRenderer | None = None
    cast: object | None = None  # CastReceiver, imported lazily
    missed_scans: int = 0

    @property
    def cast_capabilities(self) -> str:
        from aircast.cast.mdns import AUDIO_CAPABILITIES, VIDEO_CAPABILITIES

        return VIDEO_CAPABILITIES if self.video is not None else AUDIO_CAPABILITIES

    def to_json(self) -> dict:
        media = self.player.media
        return {
            "id": self.target.identifier,
            "name": self.display_name,
            "airplay_name": self.target.name,
            "address": self.target.address,
            "model": self.target.model,
            "type": self.settings.device_type,
            "media_types": sorted(self.settings.media),
            "state": self.player.state.value,
            "position": round(self.player.position, 1),
            "volume": round(self.player.volume),
            "media": None if media is None else {
                "url": media.url, "title": media.title, "artist": media.artist,
            },
            "last_error": self.player.last_error,
            "dlna_udn": f"uuid:{self.dlna_key}" if self.dlna else None,
            "cast_port": self.cast_port if self.cast else None,
        }


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.host_ip = config.resolved_host_ip()
        self.devices: dict[str, VirtualDevice] = {}
        self.discovered: dict[str, DiscoveredDevice] = {}
        path = config.devices_path()
        self.devices_file = DevicesFile(path) if path is not None else None
        self.renderers: dict[str, DlnaRenderer] = {}
        self._state_path = Path(config.state_dir) / "state.json"
        self._slots: dict[str, int] = {}
        self._runner: web.AppRunner | None = None
        self._ssdp: SsdpServer | None = None
        self._mdns = None
        self._auth = None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        from aircast.web import create_app

        self._load_state()
        _LOGGER.info("AirCast starting on %s (DLNA=%s, Cast=%s)", self.host_ip,
                     self.config.dlna_enabled, self.config.cast_enabled)

        app = create_app(self)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.config.http_port).start()
        _LOGGER.info("HTTP (status page + DLNA) on http://%s:%d/", self.host_ip,
                     self.config.http_port)

        if self.config.dlna_enabled:
            self._ssdp = SsdpServer(self.host_ip)
            await self._ssdp.start()

        if self.config.cast_enabled:
            from aircast.cast.auth import load_auth_provider
            from aircast.cast.mdns import CastAdvertiser

            self._auth = load_auth_provider(
                self.config.cast_auth, self.config.state_dir, self.config.cast_auth_dir
            )
            self._mdns = CastAdvertiser(self.host_ip)
            if self.config.cast_auth == "selfsigned":
                _LOGGER.warning(
                    "Cast uses self-signed device auth: open-source senders work, official "
                    "Android/Chrome senders will reject it. See docs/CAST_AUTH.md."
                )

        self._task = asyncio.create_task(self._discovery_loop())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for device in list(self.devices.values()):
            await self._remove(device)
        if self._mdns is not None:
            await self._mdns.close()
        if self._ssdp is not None:
            await self._ssdp.stop()
        if self._runner is not None:
            await self._runner.cleanup()

    # -- persistent state ------------------------------------------------------

    def _load_state(self) -> None:
        with contextlib.suppress(FileNotFoundError, ValueError, OSError):
            self._slots = dict(json.loads(self._state_path.read_text())["slots"])

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"slots": self._slots}, indent=2))
        except OSError as exc:
            _LOGGER.warning("Cannot persist state to %s: %s", self._state_path, exc)

    def _cast_port_for(self, identifier: str) -> int:
        if identifier not in self._slots:
            used = set(self._slots.values())
            self._slots[identifier] = next(i for i in range(len(used) + 1) if i not in used)
            self._save_state()
        return self.config.cast_base_port + self._slots[identifier]

    # -- discovery -------------------------------------------------------------

    async def _discovery_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                targets = await scan(timeout=self.config.scan_timeout)
                await self.sync(targets)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Discovery failed")
            await asyncio.sleep(self.config.scan_interval)

    async def sync(self, targets: list[AirPlayTarget]) -> None:
        if self.devices_file is not None:
            self.devices_file.reload_if_changed()
        seen = set()
        resolved: list[tuple[AirPlayTarget, DeviceSettings]] = []
        for target in targets:
            discovered = self.devices_file.get(target.identifier) if self.devices_file else None
            settings = self.config.resolve(target, discovered)
            resolved.append((target, settings))
            known = self.discovered.get(target.identifier)
            # New bridged devices are logged by _add(); log ignored ones and status changes.
            if (known is None and not settings.bridged) or (
                    known is not None and known.settings.status != settings.status):
                _LOGGER.info("AirPlay device '%s' (%s, %s): %s", target.name,
                             settings.device_type, target.address, settings.status)
            self.discovered[target.identifier] = DiscoveredDevice(target, settings)

            device = self.devices.get(target.identifier)
            if not settings.bridged:
                if device is not None:
                    await self._remove(device)
                continue
            seen.add(target.identifier)
            if device is not None and device.settings.signature != settings.signature:
                _LOGGER.info("Settings of '%s' changed, re-creating its virtual device",
                             target.name)
                await self._remove(device)
                device = None
            if device is None:
                try:
                    await self._add(target, settings)
                except Exception:
                    _LOGGER.exception("Cannot create virtual device for %s", target.name)
                continue
            device.missed_scans = 0
            device.output.update_config(target.conf)
            if device.video is not None:
                device.video.update_config(target.conf)
            if settings.display_name != device.display_name:
                await self._rename(device, target, settings.display_name)
            device.target = target
            device.settings = settings

        if self.devices_file is not None:
            self.devices_file.update(self.config, resolved)

        scanned = {target.identifier for target in targets}
        for identifier, entry in list(self.discovered.items()):
            if identifier in scanned:
                continue
            entry.missed_scans += 1
            if (entry.missed_scans >= self.config.remove_after_missed_scans
                    and identifier not in self.devices):
                del self.discovered[identifier]

        for identifier, device in list(self.devices.items()):
            if identifier in seen:
                continue
            device.missed_scans += 1
            busy = device.player.state.value in ("PLAYING", "TRANSITIONING")
            if device.missed_scans >= self.config.remove_after_missed_scans and not busy:
                _LOGGER.info("AirPlay device '%s' disappeared", device.target.name)
                self.discovered.pop(identifier, None)
                await self._remove(device)

    async def _add(self, target: AirPlayTarget, settings: DeviceSettings) -> None:
        cfg = self.config
        override = settings.override
        name = settings.display_name
        output = AirPlayOutput(
            target.conf,
            airplay_version=override.airplay_version or cfg.airplay_version,
            password=override.password,
            credentials=override.credentials,
        )
        video = None
        if VIDEO in settings.media:
            video = AirPlayVideoOutput(
                target.conf,
                credentials=override.airplay_credentials or override.credentials,
                password=override.password,
            )
        player = Player(output, ffmpeg=cfg.ffmpeg, output_latency=cfg.output_latency,
                        name=target.name, media_kinds=settings.media, video_output=video)
        device = VirtualDevice(
            target=target,
            settings=settings,
            display_name=name,
            player=player,
            output=output,
            video=video,
            dlna_key=str(uuid.uuid5(_NAMESPACE, f"dlna:{target.identifier}")),
            cast_id=uuid.uuid5(_NAMESPACE, f"cast:{target.identifier}").hex,
        )
        protocols = [p for p, on in (("DLNA", settings.dlna), ("Cast", settings.cast)) if on]
        _LOGGER.info("New virtual device '%s' for '%s' (%s, %s): %s, media: %s", name,
                     target.name, target.model, target.address, "+".join(protocols),
                     ", ".join(sorted(settings.media)))

        if settings.dlna and self._ssdp is not None:
            device.dlna = DlnaRenderer(device.dlna_key, name, player, target.identifier,
                                       sink_protocol_info(settings.media))
            self.renderers[device.dlna_key] = device.dlna
            self._ssdp.add(SsdpDevice(
                udn=device.dlna.udn,
                location=f"http://{self.host_ip}:{cfg.http_port}{device.dlna.base}/description.xml",
                device_type=scpd.DEVICE_TYPE,
                service_types=list(scpd.SERVICES.values()),
            ))

        if settings.cast and self._auth is not None:
            from aircast.cast.receiver import CastReceiver

            device.cast_port = self._cast_port_for(target.identifier)
            receiver = CastReceiver(name, player, self._auth, device.cast_port)
            await receiver.start()
            device.cast = receiver
            await self._mdns.register(device.cast_id, name, cfg.cast_model, device.cast_port,
                                      device.cast_capabilities)

        self.devices[target.identifier] = device

    async def _rename(self, device: VirtualDevice, target: AirPlayTarget, name: str) -> None:
        _LOGGER.info("Virtual device renamed '%s' -> '%s'", device.display_name, name)
        device.display_name = name
        if device.dlna is not None:
            device.dlna.friendly_name = name
        if device.cast is not None:
            device.cast.friendly_name = name  # type: ignore[attr-defined]
            await self._mdns.register(device.cast_id, name, self.config.cast_model,
                                      device.cast_port, device.cast_capabilities)

    async def _remove(self, device: VirtualDevice) -> None:
        self.devices.pop(device.target.identifier, None)
        if device.dlna is not None:
            if self._ssdp is not None:
                self._ssdp.remove(device.dlna.udn)
            self.renderers.pop(device.dlna_key, None)
            await device.dlna.close()
        if device.cast is not None:
            with contextlib.suppress(Exception):
                await self._mdns.unregister(device.cast_id)
            await device.cast.stop()  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):
            await device.player.shutdown()
