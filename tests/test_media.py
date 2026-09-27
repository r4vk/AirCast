from __future__ import annotations

import asyncio

import pytest

from aircast.media import media_kind, sink_protocol_info
from aircast.player import IdleReason, Media, Player, PlayerError, PlayerState, VideoOutput
from tests.conftest import wait_for


class FakeVideo(VideoOutput):
    def __init__(self) -> None:
        self.played: list[tuple[str, float]] = []
        self._stop = asyncio.Event()

    async def play_url(self, media, position, on_started) -> None:
        self._stop.clear()
        self.played.append((media.url, position))
        on_started()
        await asyncio.wait_for(self._stop.wait(), timeout=10)

    async def stop(self) -> None:
        self._stop.set()


def test_media_kind():
    assert media_kind("video/mp4") == "video"
    assert media_kind("audio/mpeg; charset=x") == "audio"
    assert media_kind("image/jpeg") == "image"
    assert media_kind(None, "http://host/film.mkv?x=1") == "video"
    assert media_kind(None, "http://host/radio") == "audio"
    assert media_kind("application/x-mpegurl", "http://host/a.m3u8") == "audio"


def test_sink_protocol_info_follows_media_types():
    audio = sink_protocol_info({"audio"})
    assert "audio/flac" in audio and "video/" not in audio
    both = sink_protocol_info({"audio", "video"})
    assert "video/mp4" in both and "audio/flac" in both
    assert "audio/" not in sink_protocol_info({"video"})


async def test_video_goes_to_video_output(fake_output):
    video = FakeVideo()
    player = Player(fake_output, name="tv", media_kinds={"audio", "video"}, video_output=video)
    try:
        await player.load(Media(url="http://host/film.mp4", mime="video/mp4"), autoplay=True,
                          start=12.0)
        await wait_for(lambda: player.state == PlayerState.PLAYING)
        assert video.played == [("http://host/film.mp4", 12.0)]
        assert player.position >= 12.0
        await player.pause()
        assert player.state == PlayerState.PAUSED and player.position >= 12.0
        await player.stop()
        assert player.idle_reason == IdleReason.CANCELLED
        assert fake_output.played == []  # nothing went through ffmpeg
    finally:
        await player.shutdown()


async def test_video_finishing_reports_finished(fake_output):
    video = FakeVideo()
    player = Player(fake_output, media_kinds={"audio", "video"}, video_output=video)
    try:
        await player.load(Media(url="http://host/film.mp4", mime="video/mp4"), autoplay=True)
        await wait_for(lambda: player.state == PlayerState.PLAYING)
        await video.stop()  # the receiver ends playback
        await wait_for(lambda: player.state == PlayerState.STOPPED)
        assert player.idle_reason == IdleReason.FINISHED
    finally:
        await player.shutdown()


async def test_routing_and_rejection(fake_output):
    audio_only = Player(fake_output)
    assert audio_only.route(Media(url="http://h/a.mp3", mime="audio/mpeg")) == "audio"
    # A video on an audio device plays its soundtrack.
    assert audio_only.route(Media(url="http://h/f.mp4", mime="video/mp4")) == "audio"
    with pytest.raises(PlayerError, match="images"):
        await audio_only.load(Media(url="http://h/p.jpg", mime="image/jpeg"))
    with pytest.raises(PlayerError):
        await audio_only.set_next(Media(url="http://h/p.jpg", mime="image/jpeg"))

    video_only = Player(fake_output, media_kinds={"video"}, video_output=FakeVideo())
    with pytest.raises(PlayerError, match="audio is not enabled"):
        await video_only.load(Media(url="http://h/a.mp3", mime="audio/mpeg"))
    assert video_only.state == PlayerState.NO_MEDIA
