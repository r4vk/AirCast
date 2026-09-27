"""UPnP MediaRenderer:1 service descriptions, device description and DIDL-Lite helpers."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

from aircast import __version__

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
CM = "urn:schemas-upnp-org:service:ConnectionManager:1"
DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"

SERVICES = {"AVTransport": AVT, "RenderingControl": RC, "ConnectionManager": CM}
SERVICE_IDS = {
    "AVTransport": "urn:upnp-org:serviceId:AVTransport",
    "RenderingControl": "urn:upnp-org:serviceId:RenderingControl",
    "ConnectionManager": "urn:upnp-org:serviceId:ConnectionManager",
}

SINK_MIME_TYPES = [
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "audio/aac", "audio/aacp",
    "audio/flac", "audio/x-flac", "audio/wav", "audio/x-wav", "audio/wave", "audio/ogg",
    "audio/x-ogg", "audio/opus", "audio/webm", "audio/x-ms-wma", "audio/L16", "audio/L24",
    "audio/x-aiff", "audio/aiff", "application/ogg", "application/x-mpegurl",
    "application/vnd.apple.mpegurl", "video/mp4", "video/mpeg", "video/webm",
    "video/x-matroska",
]
SINK_PROTOCOL_INFO = ",".join(f"http-get:*:{mime}:*" for mime in SINK_MIME_TYPES)

# (name, datatype, sendEvents, allowed values)
_VARS: dict[str, list[tuple[str, str, bool, list[str] | None]]] = {
    "AVTransport": [
        ("TransportState", "string", False,
         ["STOPPED", "PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING", "NO_MEDIA_PRESENT"]),
        ("TransportStatus", "string", False, ["OK", "ERROR_OCCURRED"]),
        ("PlaybackStorageMedium", "string", False, ["NETWORK", "NONE"]),
        ("RecordStorageMedium", "string", False, ["NOT_IMPLEMENTED"]),
        ("PossiblePlaybackStorageMedia", "string", False, None),
        ("PossibleRecordStorageMedia", "string", False, None),
        ("CurrentPlayMode", "string", False, ["NORMAL"]),
        ("TransportPlaySpeed", "string", False, ["1"]),
        ("RecordMediumWriteStatus", "string", False, ["NOT_IMPLEMENTED"]),
        ("CurrentRecordQualityMode", "string", False, ["NOT_IMPLEMENTED"]),
        ("PossibleRecordQualityModes", "string", False, None),
        ("NumberOfTracks", "ui4", False, None),
        ("CurrentTrack", "ui4", False, None),
        ("CurrentTrackDuration", "string", False, None),
        ("CurrentMediaDuration", "string", False, None),
        ("CurrentTrackMetaData", "string", False, None),
        ("CurrentTrackURI", "string", False, None),
        ("AVTransportURI", "string", False, None),
        ("AVTransportURIMetaData", "string", False, None),
        ("NextAVTransportURI", "string", False, None),
        ("NextAVTransportURIMetaData", "string", False, None),
        ("RelativeTimePosition", "string", False, None),
        ("AbsoluteTimePosition", "string", False, None),
        ("RelativeCounterPosition", "i4", False, None),
        ("AbsoluteCounterPosition", "i4", False, None),
        ("CurrentTransportActions", "string", False, None),
        ("LastChange", "string", True, None),
        ("A_ARG_TYPE_SeekMode", "string", False, ["REL_TIME", "ABS_TIME", "TRACK_NR"]),
        ("A_ARG_TYPE_SeekTarget", "string", False, None),
        ("A_ARG_TYPE_InstanceID", "ui4", False, None),
    ],
    "RenderingControl": [
        ("PresetNameList", "string", False, None),
        ("Mute", "boolean", False, None),
        ("Volume", "ui2", False, None),
        ("LastChange", "string", True, None),
        ("A_ARG_TYPE_Channel", "string", False, ["Master"]),
        ("A_ARG_TYPE_InstanceID", "ui4", False, None),
        ("A_ARG_TYPE_PresetName", "string", False, ["FactoryDefaults"]),
    ],
    "ConnectionManager": [
        ("SourceProtocolInfo", "string", True, None),
        ("SinkProtocolInfo", "string", True, None),
        ("CurrentConnectionIDs", "string", True, None),
        ("A_ARG_TYPE_ConnectionStatus", "string", False,
         ["OK", "ContentFormatMismatch", "InsufficientBandwidth", "UnreliableChannel", "Unknown"]),
        ("A_ARG_TYPE_ConnectionManager", "string", False, None),
        ("A_ARG_TYPE_Direction", "string", False, ["Input", "Output"]),
        ("A_ARG_TYPE_ProtocolInfo", "string", False, None),
        ("A_ARG_TYPE_ConnectionID", "i4", False, None),
        ("A_ARG_TYPE_AVTransportID", "i4", False, None),
        ("A_ARG_TYPE_RcsID", "i4", False, None),
    ],
}

_I = ("InstanceID", "A_ARG_TYPE_InstanceID")

# action -> [(argument, direction, related state variable)]
_ACTIONS: dict[str, dict[str, list[tuple[str, str, str]]]] = {
    "AVTransport": {
        "SetAVTransportURI": [("InstanceID", "in", _I[1]), ("CurrentURI", "in", "AVTransportURI"),
                              ("CurrentURIMetaData", "in", "AVTransportURIMetaData")],
        "SetNextAVTransportURI": [("InstanceID", "in", _I[1]),
                                  ("NextURI", "in", "NextAVTransportURI"),
                                  ("NextURIMetaData", "in", "NextAVTransportURIMetaData")],
        "GetMediaInfo": [("InstanceID", "in", _I[1]), ("NrTracks", "out", "NumberOfTracks"),
                         ("MediaDuration", "out", "CurrentMediaDuration"),
                         ("CurrentURI", "out", "AVTransportURI"),
                         ("CurrentURIMetaData", "out", "AVTransportURIMetaData"),
                         ("NextURI", "out", "NextAVTransportURI"),
                         ("NextURIMetaData", "out", "NextAVTransportURIMetaData"),
                         ("PlayMedium", "out", "PlaybackStorageMedium"),
                         ("RecordMedium", "out", "RecordStorageMedium"),
                         ("WriteStatus", "out", "RecordMediumWriteStatus")],
        "GetTransportInfo": [("InstanceID", "in", _I[1]),
                             ("CurrentTransportState", "out", "TransportState"),
                             ("CurrentTransportStatus", "out", "TransportStatus"),
                             ("CurrentSpeed", "out", "TransportPlaySpeed")],
        "GetPositionInfo": [("InstanceID", "in", _I[1]), ("Track", "out", "CurrentTrack"),
                            ("TrackDuration", "out", "CurrentTrackDuration"),
                            ("TrackMetaData", "out", "CurrentTrackMetaData"),
                            ("TrackURI", "out", "CurrentTrackURI"),
                            ("RelTime", "out", "RelativeTimePosition"),
                            ("AbsTime", "out", "AbsoluteTimePosition"),
                            ("RelCount", "out", "RelativeCounterPosition"),
                            ("AbsCount", "out", "AbsoluteCounterPosition")],
        "GetDeviceCapabilities": [("InstanceID", "in", _I[1]),
                                  ("PlayMedia", "out", "PossiblePlaybackStorageMedia"),
                                  ("RecMedia", "out", "PossibleRecordStorageMedia"),
                                  ("RecQualityModes", "out", "PossibleRecordQualityModes")],
        "GetTransportSettings": [("InstanceID", "in", _I[1]),
                                 ("PlayMode", "out", "CurrentPlayMode"),
                                 ("RecQualityMode", "out", "CurrentRecordQualityMode")],
        "GetCurrentTransportActions": [("InstanceID", "in", _I[1]),
                                       ("Actions", "out", "CurrentTransportActions")],
        "Stop": [("InstanceID", "in", _I[1])],
        "Play": [("InstanceID", "in", _I[1]), ("Speed", "in", "TransportPlaySpeed")],
        "Pause": [("InstanceID", "in", _I[1])],
        "Seek": [("InstanceID", "in", _I[1]), ("Unit", "in", "A_ARG_TYPE_SeekMode"),
                 ("Target", "in", "A_ARG_TYPE_SeekTarget")],
        "Next": [("InstanceID", "in", _I[1])],
        "Previous": [("InstanceID", "in", _I[1])],
        "SetPlayMode": [("InstanceID", "in", _I[1]), ("NewPlayMode", "in", "CurrentPlayMode")],
    },
    "RenderingControl": {
        "ListPresets": [("InstanceID", "in", _I[1]),
                        ("CurrentPresetNameList", "out", "PresetNameList")],
        "SelectPreset": [("InstanceID", "in", _I[1]),
                         ("PresetName", "in", "A_ARG_TYPE_PresetName")],
        "GetMute": [("InstanceID", "in", _I[1]), ("Channel", "in", "A_ARG_TYPE_Channel"),
                    ("CurrentMute", "out", "Mute")],
        "SetMute": [("InstanceID", "in", _I[1]), ("Channel", "in", "A_ARG_TYPE_Channel"),
                    ("DesiredMute", "in", "Mute")],
        "GetVolume": [("InstanceID", "in", _I[1]), ("Channel", "in", "A_ARG_TYPE_Channel"),
                      ("CurrentVolume", "out", "Volume")],
        "SetVolume": [("InstanceID", "in", _I[1]), ("Channel", "in", "A_ARG_TYPE_Channel"),
                      ("DesiredVolume", "in", "Volume")],
    },
    "ConnectionManager": {
        "GetProtocolInfo": [("Source", "out", "SourceProtocolInfo"),
                            ("Sink", "out", "SinkProtocolInfo")],
        "GetCurrentConnectionIDs": [("ConnectionIDs", "out", "CurrentConnectionIDs")],
        "GetCurrentConnectionInfo": [
            ("ConnectionID", "in", "A_ARG_TYPE_ConnectionID"),
            ("RcsID", "out", "A_ARG_TYPE_RcsID"),
            ("AVTransportID", "out", "A_ARG_TYPE_AVTransportID"),
            ("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo"),
            ("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager"),
            ("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID"),
            ("Direction", "out", "A_ARG_TYPE_Direction"),
            ("Status", "out", "A_ARG_TYPE_ConnectionStatus"),
        ],
    },
}


def actions(service: str) -> dict[str, list[tuple[str, str, str]]]:
    return _ACTIONS[service]


def scpd_xml(service: str) -> str:
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<scpd xmlns="urn:schemas-upnp-org:service-1-0">',
           "<specVersion><major>1</major><minor>0</minor></specVersion>", "<actionList>"]
    for action, args in _ACTIONS[service].items():
        out.append(f"<action><name>{action}</name><argumentList>")
        for arg, direction, var in args:
            out.append(
                f"<argument><name>{arg}</name><direction>{direction}</direction>"
                f"<relatedStateVariable>{var}</relatedStateVariable></argument>"
            )
        out.append("</argumentList></action>")
    out.append("</actionList><serviceStateTable>")
    for name, dtype, events, allowed in _VARS[service]:
        out.append(f'<stateVariable sendEvents="{"yes" if events else "no"}">')
        out.append(f"<name>{name}</name><dataType>{dtype}</dataType>")
        if allowed:
            out.append("<allowedValueList>")
            out += [f"<allowedValue>{value}</allowedValue>" for value in allowed]
            out.append("</allowedValueList>")
        if name == "Volume":
            out.append(
                "<allowedValueRange><minimum>0</minimum><maximum>100</maximum>"
                "<step>1</step></allowedValueRange>"
            )
        out.append("</stateVariable>")
    out.append("</serviceStateTable></scpd>")
    return "".join(out)


def device_xml(udn: str, friendly_name: str, base: str, model: str, serial: str) -> str:
    services = "".join(
        f"<service><serviceType>{stype}</serviceType><serviceId>{SERVICE_IDS[name]}</serviceId>"
        f"<SCPDURL>{base}/{name}.xml</SCPDURL><controlURL>{base}/{name}/control</controlURL>"
        f"<eventSubURL>{base}/{name}/event</eventSubURL></service>"
        for name, stype in SERVICES.items()
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        f"<device><deviceType>{DEVICE_TYPE}</deviceType>"
        f"<friendlyName>{escape(friendly_name)}</friendlyName>"
        "<manufacturer>AirCast</manufacturer>"
        "<manufacturerURL>https://github.com/r4vk/AirCast</manufacturerURL>"
        f"<modelDescription>AirPlay bridge for {escape(model)}</modelDescription>"
        "<modelName>AirCast</modelName>"
        f"<modelNumber>{__version__}</modelNumber>"
        f"<serialNumber>{escape(serial)}</serialNumber>"
        f"<UDN>{udn}</UDN>"
        "<dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>"
        f"<serviceList>{services}</serviceList>"
        "<presentationURL>/</presentationURL>"
        "</device></root>"
    )


# -- time and DIDL helpers ----------------------------------------------------


def format_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "0:00:00"
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


_TIME = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def parse_time(value: str) -> float | None:
    value = (value or "").strip()
    match = _TIME.match(value)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
    try:
        return float(value)
    except ValueError:
        return None


_NS = {
    "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
}


def parse_didl(didl: str | None) -> dict[str, object]:
    """Extract title/artist/album/artwork/duration/mime/live from DIDL-Lite; tolerant."""
    result: dict[str, object] = {}
    if not didl or "<" not in didl:
        return result
    try:
        root = safe_fromstring(didl)
    except (ET.ParseError, DefusedXmlException):
        return result
    item = root.find("didl:item", _NS)
    if item is None:
        return result

    def text(path: str) -> str | None:
        node = item.find(path, _NS)
        return node.text.strip() if node is not None and node.text else None

    result["title"] = text("dc:title")
    result["artist"] = text("upnp:artist") or text("dc:creator")
    result["album"] = text("upnp:album")
    result["artwork"] = text("upnp:albumArtURI")
    upnp_class = text("upnp:class") or ""
    result["live"] = "audioBroadcast" in upnp_class
    res = item.find("didl:res", _NS)
    if res is not None:
        duration = parse_time(res.get("duration", ""))
        if duration:
            result["duration"] = duration
        info = res.get("protocolInfo", "").split(":")
        if len(info) >= 3 and info[2] != "*":
            result["mime"] = info[2]
    return {key: value for key, value in result.items() if value not in (None, "")}


def didl_for(title: str | None, artist: str | None, url: str, mime: str | None) -> str:
    """Build a DIDL-Lite item for media that arrived without metadata (e.g. via Cast)."""
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title or 'AirCast')}</dc:title>"
        + (f"<upnp:artist>{escape(artist)}</upnp:artist>" if artist else "")
        + "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res protocolInfo="http-get:*:{escape(mime or "audio/mpeg")}:*">{escape(url)}</res>'
        "</item></DIDL-Lite>"
    )
