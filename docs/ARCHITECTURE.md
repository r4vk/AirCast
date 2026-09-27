# Architecture

## Components

| Module | Role |
|---|---|
| `aircast/bridge.py` | Discovery loop; creates/renames/removes one `VirtualDevice` per AirPlay target |
| `aircast/airplay/discovery.py` | `pyatv.scan` (RAOP + AirPlay services); device type and video capability |
| `aircast/airplay/output.py` | `AudioOutput` that streams raw PCM to a speaker with pyatv |
| `aircast/airplay/video.py` | `VideoOutput` that hands video URLs to Apple TV / AirPlay TVs (`play_url`) |
| `aircast/player.py` | Protocol-neutral engine: URL → ffmpeg → PCM → output, or URL → video output; state, position, queue |
| `aircast/media.py` | Media kinds (audio/video/image) and the MIME types advertised for each |
| `aircast/config.py` | Options, per-device overrides, resolution into effective `DeviceSettings` |
| `aircast/devices.py` | `devices.yaml`: discovered devices, user edits kept, reloaded on change |
| `aircast/logs.py` | Console + rotating file logging |
| `aircast/dlna/ssdp.py` | SSDP responder/advertiser for all virtual renderers |
| `aircast/dlna/renderer.py` | UPnP SOAP control + GENA eventing, mapped onto `Player` |
| `aircast/dlna/scpd.py` | Service descriptions, device description, DIDL-Lite helpers |
| `aircast/cast/proto.py` | Minimal protobuf codec for `CastMessage` / `DeviceAuthMessage` |
| `aircast/cast/auth.py` | Device-auth providers (`selfsigned`, `files`) |
| `aircast/cast/receiver.py` | Cast V2 TLS server: connection, heartbeat, deviceauth, receiver, media |
| `aircast/cast/mdns.py` | `_googlecast._tcp` advertisement via python-zeroconf |
| `aircast/web.py` | Status page, `/api/devices`, `/healthz`, DLNA routes |

## Data flow

1. A sender hands over a media URL (DLNA `SetAVTransportURI` or Cast `LOAD`).
2. `Player` starts `ffmpeg -i <url> -f s16le -ar 44100 -ac 2 pipe:1`.
3. `AirPlayOutput` wraps the pipe in a pyatv `AudioSource` and calls `stream_file`; pyatv
   negotiates AirPlay 1 or 2 and paces the RTP stream in real time.
4. The number of frames pyatv has pulled (minus `output_latency`) is the reported position.
5. Every state change notifies both frontends: DLNA sends `LastChange` events, Cast
   broadcasts `MEDIA_STATUS`. If one frontend takes over the speaker, the other reports
   `IDLE/INTERRUPTED` (Cast) or the new URI (DLNA).

## Design decisions

- **pyatv for AirPlay.** Handles AirPlay 2 transient pairing, encryption and timing, which
  many current speakers require (AirPlay 1 `ANNOUNCE` is rejected by some recent firmware).
  pyatv 0.18 can only stream from files/URLs through miniaudio, which cannot decode from a
  pipe, so `output.py` installs a small hook that lets `stream_file` accept a ready
  `AudioSource`. pyatv is pinned for that reason.
- **ffmpeg for decoding.** Covers AAC/M4A (common for Cast and phone libraries), HLS and
  radio streams that miniaudio does not.
- **Pause = stop + resume.** RAOP has no reliable pause for a live PCM pipe; resuming
  restarts ffmpeg with `-ss`.
- **One Cast port per speaker.** All virtual receivers share the host IP; the mDNS SRV
  record carries the port, as Cast groups do. Slots are persisted in `state.json`.
- **Stable identity.** DLNA UDN and Cast id are UUIDv5 of the AirPlay identifier, so
  controllers keep remembering devices across restarts and reinstalls.
- **No Google material.** See [CAST_AUTH.md](CAST_AUTH.md).

## Reference projects (in `reference/`, study only)

Both go the other way (AirPlay → Chromecast/UPnP). Ideas taken:

- **AirConnect** (philippe44, MIT): per-target virtual devices named with a format string,
  stable ids, removal only after repeated misses and never while playing, per-device config
  overrides, UPnP control-point behaviour (many controllers poll `GetTransportInfo` /
  `GetPositionInfo` instead of relying on events, so both must be accurate), DLNA
  `protocolInfo` conventions, Cast sender message sequence (CONNECT → LAUNCH → transport
  CONNECT → LOAD, PING every few seconds), which defines what our receiver must answer.
- **ains/aircast** (MIT): minimal Python 2 prototype (shairport-sync → FLAC over HTTP →
  pychromecast); confirmed the "decode once, fan out" pipeline shape.

No code was copied from either project.
