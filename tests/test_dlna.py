from __future__ import annotations

import asyncio
from xml.etree import ElementTree as ET

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aircast.dlna import scpd
from aircast.dlna.renderer import DlnaRenderer, add_routes
from aircast.player import PlayerState
from tests.conftest import requires_ffmpeg, wait_for

DIDL = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
    '<item id="1" parentID="0" restricted="1"><dc:title>Song &amp; Co</dc:title>'
    "<upnp:artist>Artist</upnp:artist><upnp:class>object.item.audioItem.musicTrack</upnp:class>"
    '<res protocolInfo="http-get:*:audio/wav:*" duration="0:00:03.000">x</res></item></DIDL-Lite>'
)


@pytest.fixture
async def client(player):
    renderers: dict[str, DlnaRenderer] = {}
    renderer = DlnaRenderer("1234", "Living Room (AirCast)", player, "serial")
    renderers["1234"] = renderer
    app = web.Application()
    add_routes(app, renderers)
    async with TestClient(TestServer(app)) as test_client:
        test_client.renderer = renderer
        yield test_client
    renderer.close()


def envelope(service: str, action: str, **args: str) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:{action} xmlns:u="{scpd.SERVICES[service]}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    )


async def soap(client, service: str, action: str, **args: str) -> tuple[int, dict[str, str]]:
    resp = await client.post(
        f"/dlna/1234/{service}/control",
        data=envelope(service, action, **args),
        headers={"SOAPACTION": f'"{scpd.SERVICES[service]}#{action}"',
                 "Content-Type": 'text/xml; charset="utf-8"'},
    )
    root = ET.fromstring(await resp.text())
    body = root[0][0]
    return resp.status, {child.tag.rpartition("}")[2]: child.text or "" for child in body.iter()}


async def test_description_and_scpd_are_valid_xml(client):
    resp = await client.get("/dlna/1234/description.xml")
    root = ET.fromstring(await resp.text())
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    assert root.find("d:device/d:friendlyName", ns).text == "Living Room (AirCast)"
    assert root.find("d:device/d:UDN", ns).text == "uuid:1234"
    for service in scpd.SERVICES:
        resp = await client.get(f"/dlna/1234/{service}.xml")
        assert resp.status == 200
        ET.fromstring(await resp.text())


async def test_unknown_device_404(client):
    resp = await client.get("/dlna/nope/description.xml")
    assert resp.status == 404


async def test_invalid_action_is_upnp_fault(client):
    status, values = await soap(client, "AVTransport", "Record", InstanceID="0")
    assert status == 500
    assert values["errorCode"] == "401"


async def test_protocol_info_lists_audio(client):
    _, values = await soap(client, "ConnectionManager", "GetProtocolInfo")
    assert "http-get:*:audio/flac:*" in values["Sink"]


async def test_volume_roundtrip(client, fake_output):
    await soap(client, "RenderingControl", "SetVolume", InstanceID="0", Channel="Master",
               DesiredVolume="35")
    _, values = await soap(client, "RenderingControl", "GetVolume", InstanceID="0",
                           Channel="Master")
    assert values["CurrentVolume"] == "35"
    assert fake_output.volumes == [35]


async def test_play_without_media_is_701(client):
    status, values = await soap(client, "AVTransport", "Play", InstanceID="0", Speed="1")
    assert status == 500
    assert values["errorCode"] == "701"


@requires_ffmpeg
async def test_set_uri_play_and_query(client, tone_file):
    meta = DIDL.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    status, _ = await soap(client, "AVTransport", "SetAVTransportURI", InstanceID="0",
                           CurrentURI=str(tone_file), CurrentURIMetaData=meta)
    assert status == 200
    player = client.renderer.player
    assert player.media.title == "Song & Co"
    assert player.media.artist == "Artist"
    assert player.media.duration == 3.0

    _, values = await soap(client, "AVTransport", "GetTransportInfo", InstanceID="0")
    assert values["CurrentTransportState"] == "STOPPED"

    await soap(client, "AVTransport", "Play", InstanceID="0", Speed="1")
    await wait_for(lambda: player.state == PlayerState.PLAYING)
    _, values = await soap(client, "AVTransport", "GetPositionInfo", InstanceID="0")
    assert values["TrackURI"] == str(tone_file)
    assert values["TrackDuration"] == "0:00:03"

    _, values = await soap(client, "AVTransport", "GetMediaInfo", InstanceID="0")
    assert values["CurrentURI"] == str(tone_file)
    assert "Song" in values["CurrentURIMetaData"]


@requires_ffmpeg
async def test_gena_subscription_receives_last_change(client, tone_file):
    received: list[tuple[dict, str]] = []

    async def notify(request: web.Request) -> web.Response:
        received.append((dict(request.headers), await request.text()))
        return web.Response()

    callback_app = web.Application()
    callback_app.router.add_route("NOTIFY", "/cb", notify)
    async with TestServer(callback_app) as callback:
        resp = await client.request(
            "SUBSCRIBE", "/dlna/1234/AVTransport/event",
            headers={"CALLBACK": f"<{callback.make_url('/cb')}>", "NT": "upnp:event",
                     "TIMEOUT": "Second-300"},
        )
        assert resp.status == 200
        sid = resp.headers["SID"]
        assert resp.headers["TIMEOUT"] == "Second-300"

        await wait_for(lambda: len(received) >= 1)
        headers, body = received[0]
        assert headers["SID"] == sid and headers["SEQ"] == "0"
        assert "NO_MEDIA_PRESENT" in body

        await soap(client, "AVTransport", "SetAVTransportURI", InstanceID="0",
                   CurrentURI=str(tone_file), CurrentURIMetaData="")
        await soap(client, "AVTransport", "Play", InstanceID="0", Speed="1")
        await wait_for(lambda: any("PLAYING" in b for _, b in received))

        resp = await client.request("UNSUBSCRIBE", "/dlna/1234/AVTransport/event",
                                    headers={"SID": sid})
        assert resp.status == 200
        await asyncio.sleep(0)


def test_time_helpers():
    assert scpd.format_time(3725.9) == "1:02:05"
    assert scpd.parse_time("1:02:05.500") == 3725.5
    assert scpd.parse_time("02:05") == 125
    assert scpd.parse_time("bogus") is None


def test_parse_didl_live_stream():
    didl = DIDL.replace("musicTrack", "audioBroadcast")
    info = scpd.parse_didl(didl)
    assert info["live"] is True
    assert info["mime"] == "audio/wav"
