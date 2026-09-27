from __future__ import annotations

import logging

import yaml
from pyatv.conf import AppleTV, ManualService
from pyatv.const import Protocol
from pyatv.interface import DeviceInfo
from pyatv.protocols.raop import device_info as raop_device_info

from aircast.airplay.discovery import target_from_conf
from aircast.bridge import Bridge
from aircast.config import Config, DeviceOverride, load_config
from aircast.devices import DevicesFile
from aircast.logs import configure_logging

VIDEO_FEATURES = "0x5A7FFFF7,0x1E"  # Apple TV 4K: AirPlay video v1 + photo
AUDIO_FEATURES = "0x445F8A00,0x1C340"  # AirPlay 2 speaker without video


def make_target(identifier="AA:BB:CC:DD:EE:01", name="Kitchen", model="AudioAccessory5,1",
                features=AUDIO_FEATURES, airplay=True):
    # As pyatv.scan does: device info is derived from the RAOP TXT record.
    conf = AppleTV("192.0.2.20", name,
                   device_info=DeviceInfo(raop_device_info("_raop._tcp.local", {"am": model})))
    conf.add_service(ManualService(identifier, Protocol.RAOP, 7000,
                                   {"am": model, "ft": features}))
    if airplay:
        conf.add_service(ManualService(identifier.replace(":", "-"), Protocol.AirPlay, 7000,
                                       {"model": model, "features": features}))
    return target_from_conf(conf)


def test_identifier_comes_from_raop():
    # pyatv's conf.identifier would prefer the AirPlay service's id.
    assert make_target(identifier="AABBCCDDEEFF").identifier == "AABBCCDDEEFF"
    assert make_target(identifier="AA:BB").conf.identifier == "AA-BB"


def test_classification():
    assert make_target(model="AppleTV11,1", features=VIDEO_FEATURES).device_type == "apple_tv"
    homepod = make_target(model="AudioAccessory5,1")
    assert homepod.device_type == "homepod" and not homepod.supports_video
    assert make_target(model="AirPort10,115").device_type == "airport"
    assert make_target(model="AudioAccessory1,1").device_type == "homepod"
    assert make_target(model="MacBookPro18,1").device_type == "computer"
    assert make_target(model="Sonos One").device_type == "speaker"
    tv = make_target(model="QN55Q80", features=VIDEO_FEATURES)
    assert tv.device_type == "tv" and tv.supports_video and tv.supports_photo
    # Video is played over the AirPlay service; RAOP alone is audio only.
    assert not make_target(model="QN55Q80", features=VIDEO_FEATURES, airplay=False).supports_video


def test_resolve_per_protocol_and_media():
    cfg = Config(exclude_cast=["Kitchen"], devices={
        "Office": DeviceOverride(dlna=False),
        "Garage": DeviceOverride(dlna=False, cast=False),
    })
    kitchen = cfg.resolve(make_target(name="Kitchen"))
    assert kitchen.bridged and kitchen.dlna and not kitchen.cast
    assert kitchen.media == {"audio"}
    office = cfg.resolve(make_target(name="Office"))
    assert office.bridged and office.cast and not office.dlna
    garage = cfg.resolve(make_target(name="Garage"))
    assert not garage.bridged and "both off" in garage.status

    atv = cfg.resolve(make_target(name="Living", model="AppleTV11,1", features=VIDEO_FEATURES))
    assert atv.device_type == "apple_tv" and atv.media == {"audio", "video"}


def test_resolve_video_needs_capability_and_image_is_not_published():
    cfg = Config(devices={"Kitchen": DeviceOverride(media=["audio", "video", "image"])})
    assert cfg.resolve(make_target(name="Kitchen")).media == {"audio"}
    cfg = Config(devices={"Kitchen": DeviceOverride(media=["image"])})
    settings = cfg.resolve(make_target(name="Kitchen"))
    assert not settings.bridged and "no playable media" in settings.status


def test_resolve_precedence_and_exclusion():
    target = make_target(name="Kitchen")
    discovered = DeviceOverride(enabled=False, name="From file", cast=False)
    cfg = Config(devices={"Kitchen": DeviceOverride(enabled=True)})
    settings = cfg.resolve(target, discovered)
    # config.yaml wins field by field; unset fields fall back to devices.yaml.
    assert settings.bridged and settings.display_name == "From file" and not settings.cast
    assert not Config(exclude=["Kitchen"]).resolve(target).bridged
    assert not Config(include=["Other"]).resolve(target).bridged
    assert not Config(enable_new_devices=False).resolve(target).bridged
    # global switches still apply
    assert not Config(dlna_enabled=False, cast_enabled=False).resolve(target).bridged


def test_media_types_and_new_keys_from_yaml(tmp_path, caplog):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "media_types:\n  apple_tv: [audio]\n  toaster: [audio]\n"
        "exclude_dlna: [Kitchen]\nlog_file: aircast.log\n"
        "devices:\n  Kitchen:\n    media: audio,bogus\n    typo: 1\n"
    )
    with caplog.at_level(logging.WARNING):
        cfg = load_config(str(cfg_file), env={"AIRCAST_EXCLUDE_CAST": "Office",
                                              "AIRCAST_STATE_DIR": str(tmp_path)})
    assert cfg.media_types["apple_tv"] == ["audio"]
    assert cfg.exclude_dlna == ["Kitchen"] and cfg.exclude_cast == ["Office"]
    assert cfg.devices["Kitchen"].media == ["audio"]
    assert cfg.log_path() == tmp_path / "aircast.log"
    for word in ("toaster", "bogus", "typo"):
        assert word in caplog.text
    atv = cfg.resolve(make_target(name="Living", model="AppleTV11,1", features=VIDEO_FEATURES))
    assert atv.media == {"audio"}


def test_devices_file_is_created_and_keeps_edits(tmp_path):
    path = tmp_path / "devices.yaml"
    cfg = Config(state_dir=str(tmp_path))
    kitchen = make_target(name="Kitchen")
    atv = make_target("AA:BB:CC:DD:EE:02", "Living", "AppleTV11,1", VIDEO_FEATURES)
    devices = DevicesFile(path)
    devices.reload_if_changed()
    devices.update(cfg, [(t, cfg.resolve(t)) for t in (kitchen, atv)])

    data = yaml.safe_load(path.read_text())["devices"]
    assert data[kitchen.identifier]["media"] == ["audio"]
    assert data[atv.identifier]["media"] == ["audio", "video"]
    assert data[atv.identifier]["info"]["type"] == "apple_tv"
    assert data[kitchen.identifier]["info"]["status"] == "bridged"

    # The user ignores the Apple TV for Cast and disables the kitchen.
    data[atv.identifier]["cast"] = False
    data[kitchen.identifier]["enabled"] = False
    path.write_text(yaml.safe_dump({"devices": data}))
    assert devices.reload_if_changed()
    assert not cfg.resolve(kitchen, devices.get(kitchen.identifier)).bridged
    assert not cfg.resolve(atv, devices.get(atv.identifier)).cast

    devices.update(cfg, [(t, cfg.resolve(t, devices.get(t.identifier))) for t in (kitchen, atv)])
    data = yaml.safe_load(path.read_text())["devices"]
    assert data[kitchen.identifier]["enabled"] is False
    assert data[kitchen.identifier]["info"]["status"] == "ignored: disabled"
    assert data[atv.identifier]["cast"] is False
    assert not devices.reload_if_changed()  # our own write is not a user change


async def test_bridge_sync_writes_file_and_applies_edits(tmp_path):
    cfg = Config(state_dir=str(tmp_path), host_ip="127.0.0.1")
    bridge = Bridge(cfg)
    kitchen = make_target(name="Kitchen")
    office = make_target("AA:BB:CC:DD:EE:03", "Office")
    await bridge.sync([kitchen, office])
    assert set(bridge.devices) == {kitchen.identifier, office.identifier}
    assert (tmp_path / "devices.yaml").exists()

    data = yaml.safe_load((tmp_path / "devices.yaml").read_text())
    data["devices"][office.identifier]["enabled"] = False
    data["devices"][kitchen.identifier]["name"] = "Cuisine"
    (tmp_path / "devices.yaml").write_text(yaml.safe_dump(data))

    await bridge.sync([kitchen, office])
    assert set(bridge.devices) == {kitchen.identifier}
    assert bridge.devices[kitchen.identifier].display_name == "Cuisine"
    assert bridge.discovered[office.identifier].settings.status == "ignored: disabled"
    await bridge.stop()


def test_log_file(tmp_path):
    cfg = Config(state_dir=str(tmp_path), log_file="logs/aircast.log", log_max_size=1)
    configure_logging(cfg)
    try:
        logging.getLogger("aircast.test").warning("hello file")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert "hello file" in (tmp_path / "logs" / "aircast.log").read_text()
    finally:
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)
            handler.close()
