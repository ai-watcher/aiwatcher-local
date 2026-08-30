#!/usr/bin/env python3
"""Rasterise the mark to the PNG sizes the browser extension's manifest names.

The extension needs real files at fixed sizes, and downsampling the original
2000px artwork would bake in its white background. Drawing from the same
geometry the SVG uses keeps the two from drifting, and gives transparent RGBA.

    python3 logo/render_icons.py

Geometry is the SVG's, in its own viewBox units. Coverage is supersampled 4x
per axis, so the corners and the stroke edges anti-alias.
"""
import math, struct, zlib, pathlib

VIEW = (-10, -10, 449, 369)          # x, y, w, h -- the SVG's padded viewBox
BLUE = (0x00, 0x52, 0xF5)
INK = (0x14, 0x13, 0x14)
RINGS = [                            # x, y, w, h, r, stroke -- back to front
    (20, 20, 260, 220, 65, 40, BLUE),
    (149, 137, 260, 192, 65, 40, INK),
]
SIZES = (16, 48, 128)
SS = 4                               # supersamples per axis


def on_ring(px, py, x, y, w, h, r, stroke):
    cx, cy = x + w / 2, y + h / 2
    ex, ey = w / 2 - r, h / 2 - r
    qx, qy = abs(px - cx) - ex, abs(py - cy) - ey
    d = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r
    return abs(d) <= stroke / 2


def render(size):
    vx, vy, vw, vh = VIEW
    scale = max(vw, vh) / size
    rows = []
    for iy in range(size):
        row = bytearray(b"\x00")     # PNG filter byte: none
        for ix in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(SS):
                for sx in range(SS):
                    px = vx + (ix + (sx + 0.5) / SS) * scale
                    py = vy + (iy + (sy + 0.5) / SS) * scale
                    hit = None
                    for *geom, colour in RINGS:
                        if on_ring(px, py, *geom):
                            hit = colour          # later rings paint over
                    if hit:
                        acc[0] += hit[0]; acc[1] += hit[1]; acc[2] += hit[2]; acc[3] += 255
            n = SS * SS
            a = acc[3] / n
            if a <= 0:
                row += b"\x00\x00\x00\x00"
            else:
                # Un-premultiply: the colour is the average over covered samples.
                k = acc[3] / 255
                row += bytes((round(acc[0] / k), round(acc[1] / k), round(acc[2] / k), round(a)))
        rows.append(bytes(row))
    return b"".join(rows)


def write_png(path, size, raw):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)   # 8-bit RGBA
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent.parent / "browser-extension" / "icons"
    for size in SIZES:
        target = out / f"icon{size}.png"
        write_png(target, size, render(size))
        print(f"{target.relative_to(target.parents[2])}  {target.stat().st_size} bytes")
