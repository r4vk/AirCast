from __future__ import annotations

from aircast.config import load_config
from aircast.dlna.ssdp import SsdpDevice, SsdpServer, parse_headers


def test_env_overrides_yaml(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "http_port: 1234\nname_format: '{name} [bridge]'\n"
        "exclude: [Office]\n"
        "devices:\n  AABBCCDDEEFF:\n    enabled: false\n  Kitchen:\n    name: Cuisine\n"
    )
    cfg = load_config(str(cfg_file), env={"AIRCAST_HTTP_PORT": "5678",
                                          "AIRCAST_CAST_ENABLED": "false",
                                          "AIRCAST_INCLUDE": "Kitchen, Bedroom"})
    assert cfg.http_port == 5678
    assert cfg.cast_enabled is False
    assert cfg.include == ["Kitchen", "Bedroom"]
    assert cfg.display_name("X", "Bedroom") == "Bedroom [bridge]"
    assert cfg.display_name("X", "Kitchen") == "Cuisine"
    assert cfg.is_wanted("X", "Kitchen")
    assert not cfg.is_wanted("X", "Office")
    assert not cfg.is_wanted("AABBCCDDEEFF", "Bedroom")


def test_no_config_file_is_fine(tmp_path):
    cfg = load_config(env={"AIRCAST_STATE_DIR": str(tmp_path)})
    assert cfg.dlna_enabled and cfg.cast_enabled


def _server() -> SsdpServer:
    server = SsdpServer("192.0.2.10")
    server.devices["uuid:abc"] = SsdpDevice(
        udn="uuid:abc", location="http://192.0.2.10:49152/dlna/abc/description.xml",
        device_type="urn:schemas-upnp-org:device:MediaRenderer:1",
        service_types=["urn:schemas-upnp-org:service:AVTransport:1"],
    )
    return server


def test_ssdp_answers_matching_search_targets():
    server = _server()
    assert len(server.responses_for("ssdp:all")) == 4
    [resp] = server.responses_for("urn:schemas-upnp-org:device:MediaRenderer:1")
    _, headers = parse_headers(resp.encode())
    assert headers["USN"] == "uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1"
    assert headers["LOCATION"].endswith("/description.xml")
    assert server.responses_for("urn:schemas-upnp-org:device:MediaServer:1") == []
    assert len(server.responses_for("uuid:abc")) == 1
    assert len(server.responses_for("upnp:rootdevice")) == 1
