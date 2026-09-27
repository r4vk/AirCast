from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from aircast.player import AudioOutput, Media, PcmStream, Player

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


class FakeOutput(AudioOutput):
    """Consumes PCM ~10x faster than real time and records what it played."""

    def __init__(self) -> None:
        self.played: list[tuple[str, int]] = []
        self.volumes: list[float] = []
        self._stop = asyncio.Event()

    async def play(self, stream: PcmStream, media: Media) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            data = await stream.read_frames(4410)
            if not data:
                break
            await asyncio.sleep(0.01)
        self.played.append((media.url, stream.frames))

    async def stop(self) -> None:
        self._stop.set()

    async def set_volume(self, level: float) -> None:
        self.volumes.append(level)


@pytest.fixture
def fake_output() -> FakeOutput:
    return FakeOutput()


@pytest.fixture
async def player(fake_output: FakeOutput):
    instance = Player(fake_output, output_latency=0.0, name="test")
    yield instance
    await instance.shutdown()


@pytest.fixture(scope="session")
def tone_file(tmp_path_factory) -> Path:
    """A 3 second silent-ish stereo WAV produced by ffmpeg."""
    path = tmp_path_factory.mktemp("media") / "tone.wav"
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=3", "-af", "volume=0.01", "-ac", "2", str(path)],
            check=True,
        )
    return path


async def wait_for(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)
