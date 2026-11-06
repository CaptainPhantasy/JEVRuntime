from pathlib import Path
import json
import struct

ROOT = Path(__file__).resolve().parents[1] / "assets" / "icons"

def test_transparent_brand_pyramid():
    for name, size in [("favicon-16x16.png",16),("favicon-32x32.png",32),("apple-touch-icon.png",180),("icon-192.png",192),("icon-512.png",512)]:
        data = (ROOT / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", data[16:24]) == (size,size)
        assert data[25] == 6
    for name in ("favicon.svg", "icon.svg"):
        svg = (ROOT / name).read_text()
        assert "<rect width=\"512\" height=\"512\"" not in svg
        assert "<title>JEVRuntime" in svg
    assert len(json.loads((ROOT / "site.webmanifest").read_text())["icons"]) >= 2
    assert (ROOT / "favicon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"
