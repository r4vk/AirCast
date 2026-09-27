"""Media kinds (audio / video / image) and the formats advertised for each."""

from __future__ import annotations

from urllib.parse import urlsplit

AUDIO = "audio"
VIDEO = "video"
IMAGE = "image"
KINDS = (AUDIO, VIDEO, IMAGE)
# Kinds that can actually be played today; image is accepted in config for forward
# compatibility but never advertised.
PLAYABLE_KINDS = (AUDIO, VIDEO)

AUDIO_MIME_TYPES = [
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "audio/aac", "audio/aacp",
    "audio/flac", "audio/x-flac", "audio/wav", "audio/x-wav", "audio/wave", "audio/ogg",
    "audio/x-ogg", "audio/opus", "audio/webm", "audio/x-ms-wma", "audio/L16", "audio/L24",
    "audio/x-aiff", "audio/aiff", "application/ogg", "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
]
# AirPlay video receivers (Apple TV, AirPlay TVs) play H.264/HEVC in MP4/MOV and HLS.
VIDEO_MIME_TYPES = ["video/mp4", "video/quicktime", "video/x-m4v", "video/mpeg"]

_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts", ".mpg", ".mpeg"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".bmp"}


def media_kind(mime: str | None, url: str = "") -> str:
    """Best guess of what a sender wants to play. Playlists and unknown types count as audio."""
    mime = (mime or "").split(";")[0].strip().lower()
    if mime.startswith("video/"):
        return VIDEO
    if mime.startswith("image/"):
        return IMAGE
    if mime:
        return AUDIO
    path = urlsplit(url).path.lower()
    ext = path[path.rfind("."):] if "." in path else ""
    if ext in _VIDEO_EXTENSIONS:
        return VIDEO
    if ext in _IMAGE_EXTENSIONS:
        return IMAGE
    return AUDIO


def sink_mime_types(kinds: frozenset[str] | set[str]) -> list[str]:
    """MIME types a renderer accepts. Video files still play (sound only) on audio targets."""
    mimes = list(AUDIO_MIME_TYPES) if AUDIO in kinds else []
    if VIDEO in kinds:
        mimes += VIDEO_MIME_TYPES
    return mimes


def sink_protocol_info(kinds: frozenset[str] | set[str]) -> str:
    return ",".join(f"http-get:*:{mime}:*" for mime in sink_mime_types(kinds))
