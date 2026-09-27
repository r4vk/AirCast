"""End-to-end Cast tests: pychromecast (a real open-source sender) against our receiver."""

from __future__ import annotations

import asyncio
import socket
import uuid

import pytest

from aircast.cast.auth import SelfSignedProvider
from aircast.cast.receiver import CastReceiver
from aircast.player import PlayerState
from tests.conftest import requires_ffmpeg, wait_for

pychromecast = pytest.importorskip("pychromecast")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def receiver(player, tmp_path):
    rx = CastReceiver("Kitchen (AirCast)", player, SelfSignedProvider(tmp_path), _free_port(),
                      host="127.0.0.1")
    await rx.start()
    yield rx
    await rx.stop()


def _connect(port: int):
    service = pychromecast.models.HostServiceInfo("127.0.0.1", port)
    info = pychromecast.models.CastInfo(
        services={service}, uuid=uuid.uuid4(), model_name="AirCast", friendly_name="Kitchen",
        host="127.0.0.1", port=port, cast_type="audio", manufacturer="AirCast",
    )
    cast = pychromecast.get_chromecast_from_cast_info(info, zconf=None, tries=1, timeout=5)
    cast.wait(timeout=10)
    return cast


async def test_sender_connects_and_sees_status(receiver):
    cast = await asyncio.to_thread(_connect, receiver.port)
    try:
        assert cast.status is not None
        assert cast.status.volume_level == pytest.approx(0.5)
        await asyncio.to_thread(cast.set_volume, 0.2)
        await wait_for(lambda: receiver.player.volume == pytest.approx(20))
    finally:
        await asyncio.to_thread(cast.disconnect, 5)


@requires_ffmpeg
async def test_play_media_via_default_media_receiver(receiver, tone_file, fake_output):
    # pychromecast only accepts URLs; serve the tone file over HTTP.
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def _serve(_request):
        return web.FileResponse(tone_file)

    app = web.Application()
    app.router.add_get("/tone.wav", _serve)
    async with TestServer(app, host="127.0.0.1") as server:
        url = str(server.make_url("/tone.wav"))
        cast = await asyncio.to_thread(_connect, receiver.port)
        try:
            mc = cast.media_controller
            await asyncio.to_thread(mc.play_media, url, "audio/wav", title="Tone")
            await wait_for(lambda: receiver.player.state == PlayerState.PLAYING, timeout=10)
            assert receiver.app is not None and receiver.app.app_id == "CC1AD845"
            assert receiver.player.media.title == "Tone"
            await wait_for(lambda: mc.status.player_state == "PLAYING", timeout=5)

            await asyncio.to_thread(mc.pause)
            await wait_for(lambda: receiver.player.state == PlayerState.PAUSED)
            await wait_for(lambda: mc.status.player_state == "PAUSED", timeout=5)

            await asyncio.to_thread(mc.play)
            await wait_for(lambda: receiver.player.state == PlayerState.PLAYING)
            await wait_for(lambda: receiver.player.state == PlayerState.STOPPED, timeout=10)
            await wait_for(lambda: mc.status.player_state == "IDLE", timeout=5)
            assert mc.status.idle_reason == "FINISHED"
            assert fake_output.played
        finally:
            await asyncio.to_thread(cast.disconnect, 5)


async def test_non_url_content_is_rejected(receiver):
    cast = await asyncio.to_thread(_connect, receiver.port)
    try:
        mc = cast.media_controller
        await asyncio.to_thread(mc.play_media, "spotify:track:123", "audio/mpeg")
        await asyncio.sleep(1)
        assert receiver.player.media is None
    finally:
        await asyncio.to_thread(cast.disconnect, 5)


@requires_ffmpeg
async def test_launching_other_app_stops_cast_playback(receiver, tone_url):
    await receiver.launch("CC1AD845")
    await receiver.load({"media": {"contentId": tone_url, "contentType": "audio/wav"}})
    await wait_for(lambda: receiver.player.state == PlayerState.PLAYING)
    await receiver.launch("ABCDEF12")
    assert receiver.player.state == PlayerState.STOPPED
    assert receiver.media_status() == []
