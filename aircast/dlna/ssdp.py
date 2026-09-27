"""Minimal SSDP (UPnP discovery) responder and advertiser for any number of root devices."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import socket
import struct
from dataclasses import dataclass, field

from aircast import __version__

_LOGGER = logging.getLogger(__name__)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
MAX_AGE = 1800
NOTIFY_INTERVAL = 300
SERVER = f"Linux UPnP/1.0 AirCast/{__version__}"


@dataclass
class SsdpDevice:
    udn: str  # "uuid:..."
    location: str
    device_type: str
    service_types: list[str] = field(default_factory=list)

    def targets(self) -> list[tuple[str, str]]:
        """(NT/ST, USN) pairs this device announces."""
        pairs = [
            ("upnp:rootdevice", f"{self.udn}::upnp:rootdevice"),
            (self.udn, self.udn),
            (self.device_type, f"{self.udn}::{self.device_type}"),
        ]
        pairs += [(svc, f"{self.udn}::{svc}") for svc in self.service_types]
        return pairs


def _matches(search_target: str, target: str) -> bool:
    if search_target in ("ssdp:all", target):
        return True
    # Version-less or lower-version searches ("...:MediaRenderer:1" matches ":1" only here).
    if search_target.startswith("urn:") and target.startswith("urn:"):
        s_base, _, s_ver = search_target.rpartition(":")
        t_base, _, t_ver = target.rpartition(":")
        return s_base == t_base and s_ver.isdigit() and t_ver.isdigit() and int(s_ver) <= int(t_ver)
    return False


def parse_headers(data: bytes) -> tuple[str, dict[str, str]]:
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().upper()] = value.strip()
    return lines[0], headers


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, server: SsdpServer) -> None:
        self.server = server

    def datagram_received(self, data: bytes, addr) -> None:
        self.server.handle_datagram(data, addr)


class SsdpServer:
    def __init__(self, host_ip: str) -> None:
        self.host_ip = host_ip
        self.devices: dict[str, SsdpDevice] = {}
        self._listen: asyncio.DatagramTransport | None = None
        self._send: socket.socket | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", SSDP_PORT))
        mreq = struct.pack("4s4s", socket.inet_aton(SSDP_ADDR), socket.inet_aton(self.host_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
        self._listen, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)

        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.host_ip))
        send.bind((self.host_ip, 0))
        send.setblocking(False)
        self._send = send
        self._task = asyncio.create_task(self._notify_loop())
        _LOGGER.info("SSDP listening on %s:%d", self.host_ip, SSDP_PORT)

    async def stop(self) -> None:
        for device in list(self.devices.values()):
            self.remove(device.udn)
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._listen:
            self._listen.close()
        if self._send:
            self._send.close()

    def add(self, device: SsdpDevice) -> None:
        self.devices[device.udn] = device
        self._notify(device, "ssdp:alive")

    def remove(self, udn: str) -> None:
        device = self.devices.pop(udn, None)
        if device is not None:
            self._notify(device, "ssdp:byebye")

    # -- outgoing ------------------------------------------------------------

    def _sendto(self, payload: str, addr: tuple[str, int]) -> None:
        if self._send is None:
            return
        try:
            self._send.sendto(payload.encode(), addr)
        except OSError as exc:
            _LOGGER.debug("SSDP send to %s failed: %s", addr, exc)

    def _notify(self, device: SsdpDevice, nts: str) -> None:
        for nt, usn in device.targets():
            lines = [
                "NOTIFY * HTTP/1.1",
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
                f"NT: {nt}",
                f"NTS: {nts}",
                f"USN: {usn}",
            ]
            if nts == "ssdp:alive":
                lines += [
                    f"CACHE-CONTROL: max-age={MAX_AGE}",
                    f"LOCATION: {device.location}",
                    f"SERVER: {SERVER}",
                ]
            self._sendto("\r\n".join(lines) + "\r\n\r\n", (SSDP_ADDR, SSDP_PORT))

    async def _notify_loop(self) -> None:
        while True:
            await asyncio.sleep(NOTIFY_INTERVAL)
            for device in list(self.devices.values()):
                self._notify(device, "ssdp:alive")

    # -- incoming ------------------------------------------------------------

    def handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        start, headers = parse_headers(data)
        if not start.upper().startswith("M-SEARCH"):
            return
        if headers.get("MAN", "").strip('"') != "ssdp:discover":
            return
        search_target = headers.get("ST", "")
        try:
            mx = max(0, min(int(headers.get("MX", "1")), 5))
        except ValueError:
            mx = 1
        responses = self.responses_for(search_target)
        if responses:
            asyncio.get_running_loop().call_later(
                random.uniform(0, mx * 0.5), self._reply, responses, addr
            )

    def responses_for(self, search_target: str) -> list[str]:
        out = []
        for device in self.devices.values():
            for target, usn in device.targets():
                if not _matches(search_target, target):
                    continue
                st = target if search_target == "ssdp:all" else search_target
                out.append(
                    "\r\n".join(
                        [
                            "HTTP/1.1 200 OK",
                            f"CACHE-CONTROL: max-age={MAX_AGE}",
                            "EXT:",
                            f"LOCATION: {device.location}",
                            f"SERVER: {SERVER}",
                            f"ST: {st}",
                            f"USN: {usn}",
                        ]
                    )
                    + "\r\n\r\n"
                )
        return out

    def _reply(self, responses: list[str], addr: tuple[str, int]) -> None:
        for response in responses:
            self._sendto(response, addr)
