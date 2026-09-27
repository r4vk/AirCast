# AirCast

**Stream from Android (and anything else) to AirPlay speakers.**

AirCast is a small bridge that runs in a container on your LAN. It finds every AirPlay
speaker on the network and publishes each one as a **DLNA/UPnP media renderer** and a
**Google Cast receiver**. Whatever a phone sends to those virtual devices is decoded with
ffmpeg and streamed to the real speaker over AirPlay.

It is the reverse direction of [AirConnect](https://github.com/philippe44/AirConnect)
(which makes Chromecast/UPnP devices appear as AirPlay targets for Apple devices).

```
 Android / PC / tablet                 AirCast (container, host network)            Speakers
┌───────────────────────┐  DLNA   ┌─────────────────────────────────────────┐  AirPlay  ┌─────────┐
│ BubbleUPnP, VLC, ...  │───────▶│ UPnP MediaRenderer ┐                     │─────────▶│ AirPlay │
│ Cast-enabled apps     │───────▶│ Cast V2 receiver   ├▶ Player ▶ ffmpeg ▶ pyatv (RAOP) │ speaker │
└───────────────────────┘  Cast   └─────────────────────────────────────────┘           └─────────┘
                                   ▲ discovery: mDNS (_raop, _googlecast) + SSDP
```

## Features

- Automatic discovery of AirPlay 1 and AirPlay 2 audio receivers (via [pyatv](https://pyatv.dev)).
- One virtual device per speaker, with stable identifiers across restarts.
- **DLNA/UPnP MediaRenderer:1** — AVTransport (incl. gapless `SetNextAVTransportURI`),
  RenderingControl (volume/mute), ConnectionManager, GENA eventing.
- **Google Cast receiver** — Cast V2 channel over TLS, Default Media Receiver media
  namespace (LOAD / PLAY / PAUSE / SEEK / STOP / volume), multiple senders, status broadcasts.
- Any format ffmpeg can read: MP3, AAC/M4A, FLAC, ALAC, Opus, Vorbis, WAV, HLS, internet radio.
- Status page and JSON API (`/`, `/api/devices`, `/healthz`).
- Configuration through environment variables or a YAML file; per-device overrides.
- Multi-arch image: `linux/amd64`, `linux/arm64`, `linux/arm/v7` (NAS, Raspberry Pi).

## What works with what — read this first

| Sender | Protocol | Works? |
|---|---|---|
| DLNA/UPnP controllers (BubbleUPnP, Hi-Fi Cast, VLC, foobar2000, Kodi, Home Assistant DLNA, ...) | DLNA | ✅ Yes |
| Open-source Cast senders (pychromecast, catt, Home Assistant `media_player.play_media`, Music Assistant) | Cast | ✅ Yes |
| Official Android / Chrome Cast senders (Cast button in stock apps, Google Home, Android output switcher) | Cast | ⚠️ Only with device-auth material you provide — see [docs/CAST_AUTH.md](docs/CAST_AUTH.md) |
| Apps with their own receiver code (Spotify, Netflix, YouTube's MDX, DRM content) | Cast | ❌ No emulated receiver can play these |

**Why the Cast caveat?** Official Cast senders verify that the receiver holds a device
certificate signed by Google. AirCast does not and will not ship such material. Out of the
box it uses a self-signed identity, which open-source senders accept and official senders
reject. For stock Android apps, **use DLNA** (e.g. BubbleUPnP as the controller) — it is the
reliable path.

> Tip: many network speakers that support AirPlay also expose DLNA or Spotify Connect
> natively. Check your speaker's capabilities before adding a bridge.

## Quick start (Docker)

AirCast needs **host networking** (multicast discovery and AirPlay UDP back-channels do not
survive Docker NAT). Linux hosts only; Docker Desktop on macOS/Windows does not pass
multicast to containers.

```bash
docker run -d --name aircast --network host --restart unless-stopped \
  -v "$PWD/data:/data" ghcr.io/r4vk/aircast:latest
```

or with Compose:

```bash
git clone https://github.com/r4vk/AirCast && cd AirCast
docker compose up -d
docker compose logs -f
```

Open `http://<host>:49152/` to see the discovered speakers. Your phone's DLNA controller
should now list e.g. `Kitchen (AirCast)`.

List the AirPlay receivers AirCast can see:

```bash
docker run --rm --network host ghcr.io/r4vk/aircast:latest --scan
```

## Running without Docker

Requires Python ≥ 3.11 and ffmpeg.

```bash
pip install .
aircast --scan
aircast -c config.yaml        # or configure via AIRCAST_* variables
```

## Configuration

Every option can be set in `/data/config.yaml` (see [config.example.yaml](config.example.yaml))
or as an environment variable `AIRCAST_<OPTION>`; environment variables win.

| Option | Default | Description |
|---|---|---|
| `host_ip` | auto | LAN address to advertise (set on multi-homed hosts) |
| `http_port` | `49152` | Status page, JSON API and DLNA endpoints |
| `cast_base_port` | `8010` | First Cast port; each speaker gets its own (persisted) |
| `dlna_enabled` / `cast_enabled` | `true` | Toggle the frontends |
| `include` / `exclude` | empty | Names or identifiers to bridge / skip (comma-separated in env) |
| `enable_new_devices` | `true` | `false` = only bridge devices listed under `devices` |
| `name_format` | `{name} (AirCast)` | Name of the virtual device |
| `scan_interval` | `30` | Seconds between discovery scans |
| `remove_after_missed_scans` | `3` | Drop a speaker after this many missed scans (never while playing) |
| `airplay_version` | `auto` | Force `1` or `2` if a speaker misbehaves |
| `output_latency` | `2.0` | Seconds subtracted from the reported position |
| `cast_auth` | `selfsigned` | `selfsigned` or `files` ([docs/CAST_AUTH.md](docs/CAST_AUTH.md)) |
| `cast_auth_dir` | — | Directory for `cast_auth: files` |
| `cast_model` | `AirCast` | Model name advertised over mDNS |
| `ffmpeg` | `ffmpeg` | Path to ffmpeg |
| `state_dir` | `/data` | Certificates, port map, default config location |
| `log_level` | `INFO` | `DEBUG` for protocol traces (`-v` on the CLI) |

Per-device overrides (`devices:` in YAML, keyed by identifier from `aircast --scan` or by name):
`enabled`, `name`, `password`, `credentials`, `airplay_version`.

## Network ports

| Port | Proto | Purpose |
|---|---|---|
| 49152 | TCP | Status page, API, DLNA description/control/events |
| 8010… | TCP | Cast receivers (one per speaker) |
| 1900 | UDP | SSDP (DLNA discovery) |
| 5353 | UDP | mDNS (AirPlay + Cast discovery) |
| ephemeral | UDP | AirPlay timing/control/audio (outbound + replies) |

## Known limitations

- Latency is about two seconds (AirPlay buffering). Fine for music, not for video lip-sync.
- Pause is implemented as stop + resume at the last position (seekable media only; live
  streams restart).
- Each Cast receiver listens on its own port. Some tools (pychromecast, Home Assistant) label
  receivers on ports other than 8009 as "groups"; playback is unaffected.
- AirPlay speakers that require HomeKit pairing (some TVs) are not supported; speakers with
  a simple password are (`devices.<id>.password`).
- Multi-room sync across several AirPlay speakers is not implemented.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && pytest
```

The Cast tests drive the receiver with pychromecast as a real sender; DLNA tests exercise
SOAP control and GENA eventing over HTTP. Architecture notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

`reference/` contains [AirConnect](https://github.com/philippe44/AirConnect) and
[ains/aircast](https://github.com/ains/aircast) as git submodules for study only; no code is
copied from them.

## Credits and license

MIT — see [LICENSE](LICENSE). Built on [pyatv](https://github.com/postlund/pyatv) (MIT),
[aiohttp](https://github.com/aio-libs/aiohttp), [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf)
and [FFmpeg](https://ffmpeg.org). Inspired by AirConnect by philippe44.

AirPlay is a trademark of Apple Inc.; Google Cast and Chromecast are trademarks of Google LLC.
This project is not affiliated with or endorsed by either company.
