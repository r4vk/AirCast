"""UPnP/DLNA MediaRenderer frontend for a Player: SOAP control and GENA eventing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import aiohttp
from aiohttp import web
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

from aircast.dlna import scpd
from aircast.player import IdleReason, Media, Player, PlayerError, PlayerState

_LOGGER = logging.getLogger(__name__)

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
EVENT_NS = {
    "AVTransport": "urn:schemas-upnp-org:metadata-1-0/AVT/",
    "RenderingControl": "urn:schemas-upnp-org:metadata-1-0/RCS/",
}
DEFAULT_TIMEOUT = 1800


class UpnpError(Exception):
    def __init__(self, code: int, description: str) -> None:
        super().__init__(description)
        self.code = code
        self.description = description


@dataclass
class Subscription:
    sid: str
    service: str
    callbacks: list[str]
    expires: float
    seq: int = 0


class DlnaRenderer:
    def __init__(self, key: str, friendly_name: str, player: Player, serial: str) -> None:
        self.key = key  # uuid string, also used in URLs
        self.udn = f"uuid:{key}"
        self.friendly_name = friendly_name
        self.player = player
        self.serial = serial
        self.base = f"/dlna/{key}"
        self.current_uri = ""
        self.current_meta = ""
        self.next_uri = ""
        self.next_meta = ""
        self._subs: dict[str, Subscription] = {}
        self._session: aiohttp.ClientSession | None = None
        self._pending_event: asyncio.TimerHandle | None = None
        player.add_listener(self._on_player_change)

    async def close(self) -> None:
        self.player.remove_listener(self._on_player_change)
        if self._pending_event:
            self._pending_event.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    # -- descriptions ------------------------------------------------------

    def description(self) -> str:
        return scpd.device_xml(
            self.udn, self.friendly_name, self.base, self.player.name, self.serial
        )

    # -- state helpers -----------------------------------------------------

    def _sync_uris(self) -> None:
        """Keep URIs in line with the player when another frontend (Cast) changed media."""
        media = self.player.media
        if media is None:
            return
        if media.url != self.current_uri:
            self.current_uri = media.url
            self.current_meta = media.didl or scpd.didl_for(
                media.title, media.artist, media.url, media.mime
            )
        if self.player.next_media is None:
            self.next_uri = self.next_meta = ""

    def transport_actions(self) -> str:
        state = self.player.state
        if state == PlayerState.NO_MEDIA:
            return ""
        acts = ["Stop"]
        if state in (PlayerState.PLAYING, PlayerState.TRANSITIONING):
            acts.append("Pause")
        else:
            acts.append("Play")
        media = self.player.media
        if media is not None and not media.live:
            acts.append("Seek")
        if self.player.next_media is not None:
            acts.append("Next")
        return ",".join(acts)

    def transport_status(self) -> str:
        return "ERROR_OCCURRED" if self.player.idle_reason == IdleReason.ERROR else "OK"

    # -- SOAP --------------------------------------------------------------

    async def handle_action(self, service: str, action: str, args: dict[str, str]) -> dict:
        if action not in scpd.actions(service):
            raise UpnpError(401, "Invalid Action")
        handler = getattr(self, f"_{service}_{action}", None)
        if handler is None:
            raise UpnpError(602, "Optional Action Not Implemented")
        self._sync_uris()
        try:
            return await handler(args) or {}
        except PlayerError as exc:
            raise UpnpError(701, f"Transition not available: {exc}") from exc

    # AVTransport

    async def _AVTransport_SetAVTransportURI(self, args):
        uri = args.get("CurrentURI", "").strip()
        if not uri:
            raise UpnpError(716, "Resource not found")
        if not uri.startswith(("http://", "https://")):
            raise UpnpError(714, "Illegal MIME-type: only http(s) URLs are accepted")
        meta = args.get("CurrentURIMetaData", "")
        media = _media_from(uri, meta)
        playing = self.player.state in (PlayerState.PLAYING, PlayerState.TRANSITIONING)
        self.current_uri, self.current_meta = uri, meta
        self.next_uri = self.next_meta = ""
        await self.player.load(media, autoplay=playing)

    async def _AVTransport_SetNextAVTransportURI(self, args):
        uri = args.get("NextURI", "").strip()
        if uri and not uri.startswith(("http://", "https://")):
            raise UpnpError(714, "Illegal MIME-type: only http(s) URLs are accepted")
        meta = args.get("NextURIMetaData", "")
        self.next_uri, self.next_meta = uri, meta
        await self.player.set_next(_media_from(uri, meta) if uri else None)

    async def _AVTransport_GetMediaInfo(self, args):
        media = self.player.media
        return {
            "NrTracks": "1" if media else "0",
            "MediaDuration": scpd.format_time(media.duration if media else None),
            "CurrentURI": self.current_uri,
            "CurrentURIMetaData": self.current_meta,
            "NextURI": self.next_uri,
            "NextURIMetaData": self.next_meta,
            "PlayMedium": "NETWORK" if media else "NONE",
            "RecordMedium": "NOT_IMPLEMENTED",
            "WriteStatus": "NOT_IMPLEMENTED",
        }

    async def _AVTransport_GetTransportInfo(self, args):
        return {
            "CurrentTransportState": self.player.state.value,
            "CurrentTransportStatus": self.transport_status(),
            "CurrentSpeed": "1",
        }

    async def _AVTransport_GetPositionInfo(self, args):
        media = self.player.media
        position = scpd.format_time(self.player.position)
        return {
            "Track": "1" if media else "0",
            "TrackDuration": scpd.format_time(media.duration if media else None),
            "TrackMetaData": self.current_meta,
            "TrackURI": self.current_uri,
            "RelTime": position,
            "AbsTime": position,
            "RelCount": "2147483647",
            "AbsCount": "2147483647",
        }

    async def _AVTransport_GetDeviceCapabilities(self, args):
        return {"PlayMedia": "NETWORK", "RecMedia": "NOT_IMPLEMENTED",
                "RecQualityModes": "NOT_IMPLEMENTED"}

    async def _AVTransport_GetTransportSettings(self, args):
        return {"PlayMode": "NORMAL", "RecQualityMode": "NOT_IMPLEMENTED"}

    async def _AVTransport_GetCurrentTransportActions(self, args):
        return {"Actions": self.transport_actions()}

    async def _AVTransport_Play(self, args):
        await self.player.play()

    async def _AVTransport_Pause(self, args):
        await self.player.pause()

    async def _AVTransport_Stop(self, args):
        await self.player.stop()

    async def _AVTransport_Seek(self, args):
        unit = args.get("Unit", "")
        if unit not in ("REL_TIME", "ABS_TIME"):
            raise UpnpError(710, "Seek mode not supported")
        target = scpd.parse_time(args.get("Target", ""))
        if target is None:
            raise UpnpError(711, "Illegal seek target")
        await self.player.seek(target)

    async def _AVTransport_Next(self, args):
        if self.player.next_media is None:
            raise UpnpError(701, "Transition not available")
        self.current_uri, self.current_meta = self.next_uri, self.next_meta
        self.next_uri = self.next_meta = ""
        await self.player.next()

    async def _AVTransport_Previous(self, args):
        await self.player.seek(0)

    async def _AVTransport_SetPlayMode(self, args):
        if args.get("NewPlayMode", "NORMAL") != "NORMAL":
            raise UpnpError(712, "Play mode not supported")

    # RenderingControl

    async def _RenderingControl_GetVolume(self, args):
        return {"CurrentVolume": str(round(self.player.volume))}

    async def _RenderingControl_SetVolume(self, args):
        try:
            level = int(args.get("DesiredVolume", ""))
        except ValueError as exc:
            raise UpnpError(402, "Invalid Args") from exc
        await self.player.set_volume(level)

    async def _RenderingControl_GetMute(self, args):
        return {"CurrentMute": "1" if self.player.muted else "0"}

    async def _RenderingControl_SetMute(self, args):
        value = args.get("DesiredMute", "0").strip().lower()
        await self.player.set_muted(value in ("1", "true", "yes"))

    async def _RenderingControl_ListPresets(self, args):
        return {"CurrentPresetNameList": "FactoryDefaults"}

    async def _RenderingControl_SelectPreset(self, args):
        return {}

    # ConnectionManager

    async def _ConnectionManager_GetProtocolInfo(self, args):
        return {"Source": "", "Sink": scpd.SINK_PROTOCOL_INFO}

    async def _ConnectionManager_GetCurrentConnectionIDs(self, args):
        return {"ConnectionIDs": "0"}

    async def _ConnectionManager_GetCurrentConnectionInfo(self, args):
        if args.get("ConnectionID", "0") != "0":
            raise UpnpError(706, "Invalid connection reference")
        return {"RcsID": "0", "AVTransportID": "0", "ProtocolInfo": "",
                "PeerConnectionManager": "", "PeerConnectionID": "-1",
                "Direction": "Input", "Status": "OK"}

    # -- GENA eventing -----------------------------------------------------

    def subscribe(self, service: str, callbacks: list[str], timeout: int) -> Subscription:
        sub = Subscription(f"uuid:{uuid.uuid4()}", service, callbacks, time.monotonic() + timeout)
        self._subs[sub.sid] = sub
        # The initial event must arrive after the SUBSCRIBE response carrying the SID.
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, lambda: loop.create_task(self._send_event(sub)))
        return sub

    def renew(self, sid: str, timeout: int) -> Subscription | None:
        sub = self._subs.get(sid)
        if sub is not None:
            sub.expires = time.monotonic() + timeout
        return sub

    def unsubscribe(self, sid: str) -> bool:
        return self._subs.pop(sid, None) is not None

    def event_body(self, service: str) -> str:
        if service == "ConnectionManager":
            props = {"SourceProtocolInfo": "", "SinkProtocolInfo": scpd.SINK_PROTOCOL_INFO,
                     "CurrentConnectionIDs": "0"}
        else:
            props = {"LastChange": self.last_change(service)}
        inner = "".join(
            f"<e:property><{key}>{escape(value)}</{key}></e:property>"
            for key, value in props.items()
        )
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">{inner}</e:propertyset>'
        )

    def last_change(self, service: str) -> str:
        self._sync_uris()
        if service == "AVTransport":
            media = self.player.media
            values = {
                "TransportState": self.player.state.value,
                "TransportStatus": self.transport_status(),
                "CurrentTransportActions": self.transport_actions(),
                "AVTransportURI": self.current_uri,
                "AVTransportURIMetaData": self.current_meta,
                "CurrentTrackURI": self.current_uri,
                "CurrentTrackMetaData": self.current_meta,
                "NextAVTransportURI": self.next_uri,
                "CurrentTrackDuration": scpd.format_time(media.duration if media else None),
                "CurrentMediaDuration": scpd.format_time(media.duration if media else None),
                "NumberOfTracks": "1" if media else "0",
                "CurrentTrack": "1" if media else "0",
                "CurrentPlayMode": "NORMAL",
                "TransportPlaySpeed": "1",
            }
            fields = "".join(f'<{k} val="{escape(v, {chr(34): "&quot;"})}"/>'
                             for k, v in values.items())
        else:
            fields = (
                f'<Volume channel="Master" val="{round(self.player.volume)}"/>'
                f'<Mute channel="Master" val="{1 if self.player.muted else 0}"/>'
                '<PresetNameList val="FactoryDefaults"/>'
            )
        return f'<Event xmlns="{EVENT_NS[service]}"><InstanceID val="0">{fields}</InstanceID></Event>'

    def _on_player_change(self, _player: Player) -> None:
        if not self._subs:
            return
        loop = asyncio.get_running_loop()
        if self._pending_event is None:
            # Coalesce bursts (TRANSITIONING -> PLAYING) into one NOTIFY.
            self._pending_event = loop.call_later(0.2, self._flush_events)

    def _flush_events(self) -> None:
        self._pending_event = None
        now = time.monotonic()
        for sid, sub in list(self._subs.items()):
            if sub.expires < now:
                self._subs.pop(sid, None)
            elif sub.service != "ConnectionManager":
                asyncio.get_running_loop().create_task(self._send_event(sub))

    async def _send_event(self, sub: Subscription) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        body = self.event_body(sub.service)
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "NT": "upnp:event",
            "NTS": "upnp:propchange",
            "SID": sub.sid,
            "SEQ": str(sub.seq),
        }
        sub.seq = sub.seq + 1 if sub.seq < 0xFFFFFFFF else 1
        for url in sub.callbacks:
            try:
                async with self._session.request("NOTIFY", url, data=body, headers=headers):
                    return
            except (aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.debug("Event delivery to %s failed: %s", url, exc)


def _media_from(uri: str, meta: str) -> Media:
    info = scpd.parse_didl(meta)
    return Media(
        url=uri,
        mime=info.get("mime"),  # type: ignore[arg-type]
        title=info.get("title"),  # type: ignore[arg-type]
        artist=info.get("artist"),  # type: ignore[arg-type]
        album=info.get("album"),  # type: ignore[arg-type]
        artwork=info.get("artwork"),  # type: ignore[arg-type]
        duration=info.get("duration"),  # type: ignore[arg-type]
        live=bool(info.get("live", False)),
        didl=meta or None,
    )


# -- HTTP routes (shared by all renderers) ------------------------------------------


def _soap_response(service_type: str, action: str, values: dict[str, str]) -> str:
    body = "".join(f"<{key}>{escape(str(value))}</{key}>" for key, value in values.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENV}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action}Response xmlns:u="{service_type}">{body}</u:{action}Response>'
        "</s:Body></s:Envelope>"
    )


def _soap_fault(code: int, description: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENV}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>"
        '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{code}</errorCode><errorDescription>{escape(description)}</errorDescription>"
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
    )


def parse_soap(body: bytes, soap_action: str) -> tuple[str, dict[str, str]]:
    action = soap_action.strip().strip('"').rpartition("#")[2]
    root = safe_fromstring(body)
    body_el = root.find(f"{{{SOAP_ENV}}}Body")
    if body_el is None or len(body_el) == 0:
        raise UpnpError(401, "Invalid Action")
    call = body_el[0]
    name = call.tag.rpartition("}")[2]
    args = {child.tag.rpartition("}")[2]: (child.text or "") for child in call}
    return action or name, args


def _xml(text: str, status: int = 200) -> web.Response:
    return web.Response(
        text=text, status=status, content_type="text/xml", charset="utf-8",
        headers={"Server": "Linux UPnP/1.0 AirCast", "EXT": ""},
    )


def add_routes(app: web.Application, renderers: dict[str, DlnaRenderer]) -> None:
    def lookup(request: web.Request) -> DlnaRenderer:
        renderer = renderers.get(request.match_info["key"])
        if renderer is None:
            raise web.HTTPNotFound()
        return renderer

    def service_of(request: web.Request) -> str:
        name = request.match_info["service"]
        if name not in scpd.SERVICES:
            raise web.HTTPNotFound()
        return name

    async def description(request: web.Request) -> web.Response:
        return _xml(lookup(request).description())

    async def scpd_doc(request: web.Request) -> web.Response:
        lookup(request)
        return _xml(scpd.scpd_xml(service_of(request)))

    async def control(request: web.Request) -> web.Response:
        renderer = lookup(request)
        service = service_of(request)
        try:
            action, args = parse_soap(await request.read(), request.headers.get("SOAPACTION", ""))
            _LOGGER.debug("[%s] %s.%s %s", renderer.friendly_name, service, action, args)
            values = await renderer.handle_action(service, action, args)
        except (ET.ParseError, DefusedXmlException):
            return _xml(_soap_fault(401, "Invalid Action"), 500)
        except UpnpError as exc:
            _LOGGER.info("[%s] %s failed: %s", renderer.friendly_name, service, exc.description)
            return _xml(_soap_fault(exc.code, exc.description), 500)
        except Exception:
            # Controllers only understand SOAP faults; never leak a bare HTTP 500.
            _LOGGER.exception("[%s] %s action failed", renderer.friendly_name, service)
            return _xml(_soap_fault(501, "Action Failed"), 500)
        return _xml(_soap_response(scpd.SERVICES[service], action, values))

    async def event(request: web.Request) -> web.Response:
        renderer = lookup(request)
        service = service_of(request)
        timeout = _parse_timeout(request.headers.get("TIMEOUT"))
        sid = request.headers.get("SID")
        if request.method == "UNSUBSCRIBE":
            return web.Response(status=200 if sid and renderer.unsubscribe(sid) else 412)
        if sid:
            sub = renderer.renew(sid, timeout)
            if sub is None:
                return web.Response(status=412)
        else:
            callbacks = [
                part.strip("<> ") for part in request.headers.get("CALLBACK", "").split(">")
                if part.strip("<> ").startswith("http")
            ]
            if not callbacks or request.headers.get("NT") != "upnp:event":
                return web.Response(status=412)
            sub = renderer.subscribe(service, callbacks, timeout)
        return web.Response(
            status=200,
            headers={"SID": sub.sid, "TIMEOUT": f"Second-{timeout}",
                     "Server": "Linux UPnP/1.0 AirCast"},
        )

    app.router.add_get("/dlna/{key}/description.xml", description)
    app.router.add_get("/dlna/{key}/{service}.xml", scpd_doc)
    app.router.add_post("/dlna/{key}/{service}/control", control)
    app.router.add_route("SUBSCRIBE", "/dlna/{key}/{service}/event", event)
    app.router.add_route("UNSUBSCRIBE", "/dlna/{key}/{service}/event", event)


def _parse_timeout(value: str | None) -> int:
    if value and value.lower().startswith("second-"):
        with contextlib.suppress(ValueError):
            return max(60, min(int(value[7:]), 86400))
    return DEFAULT_TIMEOUT
