"""HTTP app: status page, JSON API, health check, plus the DLNA routes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from aiohttp import web

from aircast import __version__
from aircast.dlna.renderer import add_routes

if TYPE_CHECKING:
    from aircast.bridge import Bridge

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>AirCast</title>
<style>
:root {{ color-scheme: light dark; --fg:#1d1d1f; --bg:#fafafa; --muted:#6e6e73; --line:#d2d2d7; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg:#f5f5f7; --bg:#1c1c1e; --muted:#a1a1a6;
  --line:#3a3a3c; }} }}
body {{ font: 15px/1.45 system-ui, sans-serif; color: var(--fg); background: var(--bg);
  margin: 0 auto; max-width: 960px; padding: 24px 16px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
p.meta {{ color: var(--muted); margin: 0 0 20px; }}
.wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
  vertical-align: top; }}
th {{ font-weight: 600; color: var(--muted); font-size: 13px; }}
td small {{ color: var(--muted); }}
h2 {{ font-size: 17px; margin: 28px 0 8px; }}
</style></head><body>
<h1>AirCast</h1>
<p class="meta">v{version} &middot; host {host} &middot; DLNA {dlna} &middot; Cast {cast}</p>
<div class="wrap"><table>
<thead><tr><th>Virtual device</th><th>AirPlay target</th><th>State</th><th>Now playing</th>
<th>Vol</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<h2>Discovered AirPlay devices</h2>
<p class="meta">{devices_file}</p>
<div class="wrap"><table>
<thead><tr><th>AirPlay device</th><th>Type</th><th>Published as</th><th>Media</th>
<th>Status</th></tr></thead>
<tbody>{discovered}</tbody></table></div>
</body></html>"""


def _row(device: dict) -> str:
    media = device["media"] or {}
    playing = media.get("title") or media.get("url") or ""
    if media.get("artist"):
        playing = f"{escape(media['artist'])} &ndash; {escape(playing)}"
    else:
        playing = escape(playing)
    error = f"<br><small>{escape(device['last_error'])}</small>" if device["last_error"] else ""
    return (
        f"<tr><td>{escape(device['name'])}</td>"
        f"<td>{escape(device['airplay_name'])}<br><small>{escape(device['model'])} &middot; "
        f"{escape(device['address'])}</small></td>"
        f"<td>{escape(device['state'])}{error}</td><td>{playing}</td>"
        f"<td>{device['volume']}</td></tr>"
    )


def _discovered_row(device: dict) -> str:
    protocols = [p for p, key in (("DLNA", "dlna"), ("Cast", "cast")) if device[key]]
    video = " &middot; AirPlay video" if device["supports_video"] else ""
    return (
        f"<tr><td>{escape(device['airplay_name'])}<br><small>{escape(device['id'])} &middot; "
        f"{escape(device['address'])}</small></td>"
        f"<td>{escape(device['type'])}<br><small>{escape(device['model'])}{video}</small></td>"
        f"<td>{' + '.join(protocols) or '&ndash;'}</td>"
        f"<td>{escape(', '.join(device['media'])) or '&ndash;'}</td>"
        f"<td>{escape(device['status'])}</td></tr>"
    )


def create_app(bridge: Bridge) -> web.Application:
    app = web.Application()

    def devices() -> list[dict]:
        return [device.to_json() for device in bridge.devices.values()]

    def discovered() -> list[dict]:
        return [entry.to_json() for entry in bridge.discovered.values()]

    async def index(_request: web.Request) -> web.Response:
        rows = "".join(_row(d) for d in devices()) or (
            '<tr><td colspan="5">No AirPlay receivers found yet.</td></tr>'
        )
        found = "".join(_discovered_row(d) for d in discovered()) or (
            '<tr><td colspan="5">Nothing discovered yet.</td></tr>'
        )
        path = bridge.devices_file.path if bridge.devices_file is not None else None
        html = _PAGE.format(
            version=__version__, host=escape(bridge.host_ip), rows=rows, discovered=found,
            devices_file=f"Edit {escape(str(path))} to ignore devices or change what they are "
            "published as." if path else "devices_file is disabled.",
            dlna="on" if bridge.config.dlna_enabled else "off",
            cast="on" if bridge.config.cast_enabled else "off",
        )
        return web.Response(text=html, content_type="text/html")

    async def api_devices(_request: web.Request) -> web.Response:
        return web.json_response({"devices": devices(), "discovered": discovered()})

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "version": __version__, "devices": len(bridge.devices)}
        )

    app.router.add_get("/", index)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_get("/healthz", healthz)
    add_routes(app, bridge.renderers)
    return app
