"""Cast V2 receiver: TLS server speaking the Cast channel protocol for one Player.

Implements the platform namespaces (connection, heartbeat, deviceauth, receiver) and the
Default Media Receiver media namespace. Any app LAUNCH is accepted and treated as a media
receiver, so senders that "fling" a media URL via the media namespace work regardless of
their app id. Apps that need their own receiver-side code (DRM, proprietary protocols)
cannot work with any emulated receiver.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from aircast.cast import proto
from aircast.cast.auth import AuthError, AuthProvider
from aircast.player import IdleReason, Media, Player, PlayerError, PlayerState

_LOGGER = logging.getLogger(__name__)

NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_AUTH = "urn:x-cast:com.google.cast.tp.deviceauth"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"

RECEIVER_ID = "receiver-0"
DEFAULT_MEDIA_RECEIVER = "CC1AD845"
KNOWN_APPS = {DEFAULT_MEDIA_RECEIVER: "Default Media Receiver"}

MAX_MESSAGE = 64 * 1024
IDLE_TIMEOUT = 60  # senders PING every ~5 s

# PAUSE | SEEK | STREAM_VOLUME | STREAM_MUTE
SUPPORTED_MEDIA_COMMANDS = 1 | 2 | 4 | 8

_PLAYER_STATE = {
    PlayerState.PLAYING: "PLAYING",
    PlayerState.TRANSITIONING: "BUFFERING",
    PlayerState.PAUSED: "PAUSED",
    PlayerState.STOPPED: "IDLE",
    PlayerState.NO_MEDIA: "IDLE",
}


@dataclass
class CastApp:
    app_id: str
    session_id: str
    transport_id: str

    @property
    def display_name(self) -> str:
        return KNOWN_APPS.get(self.app_id, "AirCast Media Receiver")


class CastReceiver:
    def __init__(
        self,
        friendly_name: str,
        player: Player,
        auth: AuthProvider,
        port: int,
        host: str = "0.0.0.0",
    ) -> None:
        self.friendly_name = friendly_name
        self.player = player
        self.auth = auth
        self.port = port
        self.host = host
        self.app: CastApp | None = None
        self.media_session_id = 0
        self._cast_generation = -1  # player.media_generation of the media Cast loaded
        self._clients: set[_Client] = set()
        self._server: asyncio.Server | None = None
        self._broadcast_pending = False
        player.add_listener(self._on_player_change)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._accept, self.host, self.port, ssl=self.auth.ssl_context()
        )
        _LOGGER.info("[%s] Cast receiver on port %d", self.friendly_name, self.port)

    async def stop(self) -> None:
        self.player.remove_listener(self._on_player_change)
        if self._server is not None:
            self._server.close()
        for client in list(self._clients):
            client.close()
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(self, reader, writer)
        self._clients.add(client)
        try:
            await client.run()
        finally:
            self._clients.discard(client)
            client.close()

    # -- status --------------------------------------------------------------

    @property
    def owns_media(self) -> bool:
        return self.media_session_id > 0 and self.player.media_generation == self._cast_generation

    def receiver_status(self) -> dict[str, Any]:
        apps = []
        if self.app is not None:
            media = self.player.media if self.owns_media else None
            apps.append({
                "appId": self.app.app_id,
                "appType": "WEB",
                "displayName": self.app.display_name,
                "iconUrl": "",
                "isIdleScreen": False,
                "launchedFromCloud": False,
                "namespaces": [{"name": NS_MEDIA}],
                "sessionId": self.app.session_id,
                "statusText": (media.title if media and media.title else "Ready To Cast"),
                "transportId": self.app.transport_id,
                "universalAppId": self.app.app_id,
            })
        return {
            "applications": apps,
            "isActiveInput": True,
            "isStandBy": False,
            "userEq": {},
            "volume": {
                "controlType": "attenuation",
                "level": round(self.player.volume / 100, 3),
                "muted": self.player.muted,
                "stepInterval": 0.05,
            },
        }

    def media_status(self) -> list[dict[str, Any]]:
        if self.media_session_id == 0:
            return []
        player = self.player
        status: dict[str, Any] = {
            "mediaSessionId": self.media_session_id,
            "playbackRate": 1,
            "currentTime": round(player.position, 3),
            "supportedMediaCommands": SUPPORTED_MEDIA_COMMANDS,
            "volume": {"level": round(player.volume / 100, 3), "muted": player.muted},
            "currentItemId": self.media_session_id,
            "repeatMode": "REPEAT_OFF",
        }
        if not self.owns_media:
            # Another frontend (DLNA) took over the speaker.
            status["playerState"] = "IDLE"
            status["idleReason"] = "INTERRUPTED"
            return [status]
        state = _PLAYER_STATE[player.state]
        status["playerState"] = state
        if state == "IDLE":
            reason = player.idle_reason
            status["idleReason"] = (reason.value if reason != IdleReason.NONE else "FINISHED")
        media = player.media
        if media is not None:
            status["media"] = media.cast or {"contentId": media.url}
            if media.duration and "duration" not in status["media"]:
                status["media"]["duration"] = media.duration
        return [status]

    # -- broadcasts ----------------------------------------------------------

    def _on_player_change(self, _player: Player) -> None:
        if self._broadcast_pending or not self._clients:
            return
        self._broadcast_pending = True
        asyncio.get_running_loop().call_later(0.1, self._broadcast_media)

    def _broadcast_media(self) -> None:
        self._broadcast_pending = False
        if self.app is None or self.media_session_id == 0:
            return
        payload = {"type": "MEDIA_STATUS", "requestId": 0, "status": self.media_status()}
        for client in list(self._clients):
            client.send_to_connected(self.app.transport_id, NS_MEDIA, payload)

    def broadcast_receiver_status(self, exclude: _Client | None = None) -> None:
        payload = {"type": "RECEIVER_STATUS", "requestId": 0, "status": self.receiver_status()}
        for client in list(self._clients):
            if client is not exclude:
                client.send_to_connected(RECEIVER_ID, NS_RECEIVER, payload)

    # -- app lifecycle -------------------------------------------------------

    async def launch(self, app_id: str) -> None:
        if self.app is not None and self.app.app_id == app_id:
            return
        if self.owns_media:
            # A new app replaces the old one; do not leave orphaned audio playing.
            await self.player.stop()
        session = str(uuid.uuid4())
        self.app = CastApp(app_id=app_id, session_id=session, transport_id=session)
        self.media_session_id = 0
        _LOGGER.info("[%s] Launched app %s", self.friendly_name, app_id)

    async def stop_app(self) -> None:
        if self.owns_media:
            await self.player.stop()
        self.app = None
        self.media_session_id = 0

    async def load(self, payload: dict[str, Any]) -> Media:
        cast_media = dict(payload.get("media") or {})
        url = cast_media.get("contentUrl") or cast_media.get("contentId") or ""
        if not str(url).startswith(("http://", "https://")):
            raise PlayerError(f"unsupported contentId {url!r}")
        metadata = cast_media.get("metadata") or {}
        images = metadata.get("images") or []
        media = Media(
            url=str(url),
            mime=cast_media.get("contentType"),
            title=metadata.get("title"),
            artist=metadata.get("artist") or metadata.get("albumArtist")
            or metadata.get("subtitle"),
            album=metadata.get("albumName"),
            artwork=images[0].get("url") if images and isinstance(images[0], dict) else None,
            duration=_as_float(cast_media.get("duration")),
            live=cast_media.get("streamType") == "LIVE",
            cast=cast_media,
        )
        start = _as_float(payload.get("currentTime")) or 0.0
        await self.player.load(media, autoplay=payload.get("autoplay", True), start=start)
        self.media_session_id += 1
        self._cast_generation = self.player.media_generation
        return media


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class _Client:
    def __init__(
        self, receiver: CastReceiver, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.receiver = receiver
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        # Virtual connections opened by the sender: (sender id, receiver-side id)
        self.connections: set[tuple[str, str]] = set()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.close()

    async def run(self) -> None:
        _LOGGER.debug("Cast sender connected from %s", self.peer)
        try:
            while True:
                header = await asyncio.wait_for(self.reader.readexactly(4), IDLE_TIMEOUT)
                length = int.from_bytes(header, "big")
                if length > MAX_MESSAGE:
                    _LOGGER.warning("Oversized Cast message from %s", self.peer)
                    return
                body = await asyncio.wait_for(self.reader.readexactly(length), IDLE_TIMEOUT)
                try:
                    message = proto.CastMessage.decode(body)
                except ValueError as exc:
                    _LOGGER.warning("Malformed Cast message from %s: %s", self.peer, exc)
                    return
                await self.dispatch(message)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, OSError):
            pass
        finally:
            _LOGGER.debug("Cast sender %s disconnected", self.peer)

    # -- sending -----------------------------------------------------------

    def send(self, source: str, destination: str, namespace: str, payload: Any) -> None:
        if isinstance(payload, bytes):
            message = proto.CastMessage(source, destination, namespace, payload_binary=payload)
        else:
            message = proto.CastMessage(
                source, destination, namespace, payload_utf8=json.dumps(payload)
            )
        try:
            self.writer.write(proto.frame(message))
        except (ConnectionError, RuntimeError):
            self.close()

    def reply(self, request: proto.CastMessage, payload: Any) -> None:
        self.send(request.destination_id, request.source_id, request.namespace, payload)

    def send_to_connected(self, local_id: str, namespace: str, payload: Any) -> None:
        for sender_id, dest in list(self.connections):
            if dest == local_id:
                self.send(local_id, sender_id, namespace, payload)

    # -- dispatch ----------------------------------------------------------

    async def dispatch(self, msg: proto.CastMessage) -> None:
        if msg.namespace == NS_AUTH:
            self.handle_auth(msg)
            return
        try:
            payload = json.loads(msg.payload_utf8 or "{}")
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        _LOGGER.debug("Cast %s %s -> %s: %s", msg.namespace.rpartition(".")[2], msg.source_id,
                      msg.destination_id, kind)

        if msg.namespace == NS_CONNECTION:
            if kind == "CONNECT":
                self.connections.add((msg.source_id, msg.destination_id))
            elif kind == "CLOSE":
                self.connections.discard((msg.source_id, msg.destination_id))
        elif msg.namespace == NS_HEARTBEAT:
            if kind == "PING":
                self.reply(msg, {"type": "PONG"})
        elif msg.namespace == NS_RECEIVER:
            await self.handle_receiver(msg, kind, payload)
        elif msg.namespace == NS_MEDIA:
            await self.handle_media(msg, kind, payload)

    def handle_auth(self, msg: proto.CastMessage) -> None:
        challenge = proto.decode_auth_challenge(msg.payload_binary or b"")
        if challenge is None:
            return
        try:
            response = self.receiver.auth.respond(challenge)
            self.reply(msg, proto.encode_auth_response(response))
        except (AuthError, ValueError) as exc:
            _LOGGER.warning("Cast device auth failed: %s", exc)
            self.reply(msg, proto.encode_auth_error(0))

    async def handle_receiver(self, msg: proto.CastMessage, kind: str | None, payload: dict) -> None:
        receiver = self.receiver
        request_id = payload.get("requestId", 0)
        if kind == "GET_STATUS":
            pass
        elif kind == "LAUNCH":
            await receiver.launch(str(payload.get("appId") or DEFAULT_MEDIA_RECEIVER))
        elif kind == "STOP":
            await receiver.stop_app()
        elif kind == "SET_VOLUME":
            await _apply_volume(receiver.player, payload.get("volume") or {})
        elif kind == "GET_APP_AVAILABILITY":
            apps = payload.get("appId") or []
            self.reply(msg, {
                "type": "GET_APP_AVAILABILITY",
                "responseType": "GET_APP_AVAILABILITY",
                "requestId": request_id,
                "availability": {app: "APP_AVAILABLE" for app in apps},
            })
            return
        else:
            self.reply(msg, {"type": "INVALID_REQUEST", "requestId": request_id,
                             "reason": "INVALID_COMMAND"})
            return
        self.reply(msg, {"type": "RECEIVER_STATUS", "requestId": request_id,
                         "status": receiver.receiver_status()})
        if kind != "GET_STATUS":
            receiver.broadcast_receiver_status(exclude=self)

    async def handle_media(self, msg: proto.CastMessage, kind: str | None, payload: dict) -> None:
        receiver = self.receiver
        player = receiver.player
        request_id = payload.get("requestId", 0)
        try:
            if kind == "LOAD":
                if receiver.app is None:
                    await receiver.launch(DEFAULT_MEDIA_RECEIVER)
                try:
                    await receiver.load(payload)
                except PlayerError as exc:
                    _LOGGER.info("[%s] LOAD rejected: %s", receiver.friendly_name, exc)
                    self.reply(msg, {"type": "LOAD_FAILED", "requestId": request_id})
                    return
                except Exception:
                    _LOGGER.exception("[%s] LOAD failed", receiver.friendly_name)
                    self.reply(msg, {"type": "LOAD_FAILED", "requestId": request_id})
                    return
            elif kind == "QUEUE_LOAD":
                items = payload.get("items") or []
                index = int(payload.get("startIndex") or 0)
                if not items or index >= len(items):
                    raise PlayerError("empty queue")
                item = items[index]
                await receiver.load({"media": item.get("media"), "autoplay": True,
                                     "currentTime": item.get("startTime", 0)})
            elif kind == "GET_STATUS":
                pass
            elif not receiver.owns_media:
                self.reply(msg, {"type": "INVALID_PLAYER_STATE", "requestId": request_id})
                return
            elif kind == "PLAY":
                await player.play()
            elif kind == "PAUSE":
                await player.pause()
            elif kind == "STOP":
                await player.stop()
            elif kind == "SEEK":
                if "currentTime" in payload:
                    await player.seek(float(payload["currentTime"]))
                elif "relativeTime" in payload:
                    await player.seek(player.position + float(payload["relativeTime"]))
                if payload.get("resumeState") == "PLAYBACK_PAUSE":
                    await player.pause()
                elif payload.get("resumeState") == "PLAYBACK_START":
                    await player.play()
            elif kind == "SET_VOLUME":
                await _apply_volume(player, payload.get("volume") or {})
            else:
                self.reply(msg, {"type": "INVALID_REQUEST", "requestId": request_id,
                                 "reason": "INVALID_COMMAND"})
                return
        except (PlayerError, ValueError, TypeError) as exc:
            _LOGGER.info("[%s] %s failed: %s", receiver.friendly_name, kind, exc)
            self.reply(msg, {"type": "INVALID_REQUEST", "requestId": request_id,
                             "reason": "INVALID_PARAMS"})
            return
        except Exception:
            _LOGGER.exception("[%s] %s failed", receiver.friendly_name, kind)
            self.reply(msg, {"type": "INVALID_REQUEST", "requestId": request_id,
                             "reason": "INVALID_COMMAND"})
            return
        self.reply(msg, {"type": "MEDIA_STATUS", "requestId": request_id,
                         "status": receiver.media_status()})


async def _apply_volume(player: Player, volume: dict[str, Any]) -> None:
    if "level" in volume and volume["level"] is not None:
        await player.set_volume(float(volume["level"]) * 100)
    if "muted" in volume and volume["muted"] is not None:
        await player.set_muted(bool(volume["muted"]))
