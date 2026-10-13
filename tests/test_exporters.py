import xml.etree.ElementTree as ET

from rekordbox_mcp.engine.exporters import EXPORTERS, ExportCue, ExportSet, ExportTrack


def _set():
    return ExportSet(name="My Set", tracks=[
        ExportTrack("1", "One", "A", 128.0, "Am", 200, r"C:\Music\one.mp3", 0.1, [ExportCue(12.5, "green", "IN")]),
        ExportTrack("2", "Two", "B", 130.0, "Em", 210, "soundcloud:tracks:123", 0.0, []),
        ExportTrack("3", "Three", "C", 131.0, None, 190, "/home/me/three.flac", 0.0, [ExportCue(30, "red", "OUT")])])


def test_rekordbox_xml():
    s = _set()
    name, data = EXPORTERS["rekordbox_xml"](s)
    root = ET.fromstring(data)
    tracks = root.find("COLLECTION").findall("TRACK")
    assert name == "My_Set.xml" and len(tracks) == 2
    assert tracks[0].get("Location") == "file://localhost/C:/Music/one.mp3"
    pm = tracks[0].find("POSITION_MARK")
    assert pm.get("Num") == "-1" and pm.get("Green") == "226"
    assert any("streamed" in w for w in s.warnings)


def test_serato_crate_bytes():
    s = _set()
    name, data = EXPORTERS["serato_crate"](s)
    assert data.startswith(b"vrsn") and data.count(b"otrk") == 2
    assert "Music/one.mp3".encode("utf-16-be") in data


def test_m3u_and_json():
    _, m = EXPORTERS["m3u8"](_set())
    assert m.startswith(b"#EXTM3U") and b"# streamed: 2" in m
    _, j = EXPORTERS["json"](_set())
    assert b'"comment": "IN"' in j
