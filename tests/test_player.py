from __future__ import annotations

import pytest

from aircast.player import IdleReason, Media, PlayerError, PlayerState
from tests.conftest import requires_ffmpeg, wait_for

pytestmark = requires_ffmpeg


async def test_plays_to_end(player, fake_output, tone_file):
    await player.load(Media(url=str(tone_file)), autoplay=True)
    await wait_for(lambda: player.state == PlayerState.PLAYING)
    await wait_for(lambda: player.state == PlayerState.STOPPED)
    assert player.idle_reason == IdleReason.FINISHED
    url, frames = fake_output.played[-1]
    assert url == str(tone_file)
    assert abs(frames - 3 * 44100) < 2000


async def test_pause_resume_keeps_position(player, tone_file):
    await player.load(Media(url=str(tone_file)), autoplay=True)
    await wait_for(lambda: player.position > 1.0)
    await player.pause()
    assert player.state == PlayerState.PAUSED
    paused_at = player.position
    assert paused_at > 1.0
    await player.play()
    await wait_for(lambda: player.state == PlayerState.PLAYING)
    assert player.position >= paused_at


async def test_stop_is_cancelled(player, tone_file):
    await player.load(Media(url=str(tone_file)), autoplay=True)
    await wait_for(lambda: player.state == PlayerState.PLAYING)
    await player.stop()
    assert player.state == PlayerState.STOPPED
    assert player.idle_reason == IdleReason.CANCELLED


async def test_next_media_advances(player, fake_output, tone_file):
    await player.load(Media(url=str(tone_file)), autoplay=True)
    await player.set_next(Media(url=str(tone_file), title="second"))
    generation = player.media_generation
    await wait_for(lambda: player.media_generation == generation + 1, timeout=10)
    assert player.media.title == "second"


async def test_bad_url_reports_error(player):
    await player.load(Media(url="/nonexistent/file.mp3"), autoplay=True)
    await wait_for(lambda: player.state == PlayerState.STOPPED)
    assert player.idle_reason == IdleReason.ERROR
    assert player.last_error


async def test_seek_live_rejected(player, tone_file):
    await player.load(Media(url=str(tone_file), live=True))
    with pytest.raises(PlayerError):
        await player.seek(10)


async def test_volume_forwarded(player, fake_output):
    await player.set_volume(130)
    assert player.volume == 100
    await player.set_muted(True)
    assert fake_output.volumes == [100, 0]


async def test_local_files_blocked_without_opt_in(fake_output, tone_file):
    from aircast.player import Player

    strict = Player(fake_output, output_latency=0.0, name="strict")
    try:
        await strict.load(Media(url=str(tone_file)), autoplay=True)
        await wait_for(lambda: strict.state == PlayerState.STOPPED)
        assert strict.idle_reason == IdleReason.ERROR
    finally:
        await strict.shutdown()
