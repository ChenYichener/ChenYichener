#!/usr/bin/env python3
"""Turn a portrait photo into the fixed-width ASCII art used by the profile card.

Pure standard library, no third-party dependencies: PNG is decoded by hand
(8-bit RGB / RGBA / gray), then the same recipe that produced the ART block in
update_profile.py is applied:

    crop -> grayscale -> gaussian blur -> area-average downsample -> 3x3 median
         -> tonemap (lo/hi/gamma) -> character ramp -> blank near-black cells

Usage:
    python3 tools/ascii_avatar.py tools/avatar.png            # print the art
    python3 tools/ascii_avatar.py tools/avatar.png --out art.txt

To use another portrait, replace tools/avatar.png (recommended: a high-contrast
portrait, dark background, subject lit) and re-run with `--tune`, then paste the
result into update_profile.py's ART block and re-run update_profile.py.
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib

# darkest -> brightest.  The card paints every character with one colour, so a
# dense glyph reads as "bright pixel" on a dark card: map brightness to density.
RAMP = " .:-=+*#%@"
MAX_COLS = 46          # beyond this the art runs into the info column at x=390
CHAR_W = 7.9           # px width of one monospace char at font-size 13
LINE_H = 15            # px line height used by the SVG


# --------------------------------------------------------------------------- PNG
def read_png(path: str) -> tuple[int, int, bytes]:
    """Decode a non-interlaced 8-bit PNG into raw RGB bytes."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG (convert it first: magick in.png out.png)")
    pos, idat, width = 8, [], 0
    height = depth = color = interlace = 0
    palette = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
    if depth != 8 or interlace:
        raise ValueError(f"{path}: need 8-bit non-interlaced PNG (got depth={depth})")
    raw = zlib.decompress(b"".join(idat))
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise ValueError(f"{path}: unsupported PNG colour type {color}")

    stride = width * channels
    out = bytearray(width * height * 3)
    prev = bytearray(stride)
    src = 0
    for y in range(height):
        ftype = raw[src]
        line = bytearray(raw[src + 1:src + 1 + stride])
        src += 1 + stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            raise ValueError(f"{path}: bad PNG filter {ftype}")
        row = y * width * 3
        for x in range(width):
            i = x * channels
            if color == 2:
                out[row + 3 * x:row + 3 * x + 3] = line[i:i + 3]
            elif color == 6:
                out[row + 3 * x:row + 3 * x + 3] = line[i:i + 3]
            elif color == 0:
                v = line[i]
                out[row + 3 * x:row + 3 * x + 3] = bytes((v, v, v))
            elif color == 4:
                v = line[i]
                out[row + 3 * x:row + 3 * x + 3] = bytes((v, v, v))
            else:  # palette
                pi = line[i] * 3
                out[row + 3 * x:row + 3 * x + 3] = palette[pi:pi + 3]
        prev = line
    return width, height, bytes(out)


# ------------------------------------------------------------------------ image
def crop(w: int, h: int, rgb: bytes, box: tuple[int, int, int, int]) -> tuple[int, int, bytes]:
    x0, y0, cw, ch = box
    if x0 + cw > w or y0 + ch > h:
        raise ValueError(f"crop {box} outside image {w}x{h}")
    out = bytearray(cw * ch * 3)
    for y in range(ch):
        src = ((y0 + y) * w + x0) * 3
        out[y * cw * 3:(y + 1) * cw * 3] = rgb[src:src + cw * 3]
    return cw, ch, bytes(out)


def grayscale(w: int, h: int, rgb: bytes) -> list[float]:
    return [
        0.299 * rgb[3 * i] + 0.587 * rgb[3 * i + 1] + 0.114 * rgb[3 * i + 2]
        for i in range(w * h)
    ]


def gaussian_blur(gray: list[float], w: int, h: int, sigma: float) -> list[float]:
    if sigma <= 0:
        return gray
    radius = max(1, int(math.ceil(sigma * 3)))
    kernel = [math.exp(-(x * x) / (2 * sigma * sigma)) for x in range(-radius, radius + 1)]
    total = sum(kernel)
    kernel = [k / total for k in kernel]
    tmp = [0.0] * (w * h)
    out = [0.0] * (w * h)
    for y in range(h):
        base = y * w
        for x in range(w):
            acc = 0.0
            for k, weight in enumerate(kernel):
                xx = x + k - radius
                xx = 0 if xx < 0 else (w - 1 if xx >= w else xx)
                acc += gray[base + xx] * weight
            tmp[base + x] = acc
    for x in range(w):
        for y in range(h):
            acc = 0.0
            for k, weight in enumerate(kernel):
                yy = y + k - radius
                yy = 0 if yy < 0 else (h - 1 if yy >= h else yy)
                acc += tmp[yy * w + x] * weight
            out[y * w + x] = acc
    return out


def cells(gray: list[float], w: int, h: int, cols: int, rows: int) -> list[list[float]]:
    grid = [[0.0] * cols for _ in range(rows)]
    for cy in range(rows):
        ya, yb = round(cy * h / rows), round((cy + 1) * h / rows)
        yb = max(ya + 1, yb)
        for cx in range(cols):
            xa, xb = round(cx * w / cols), round((cx + 1) * w / cols)
            xb = max(xa + 1, xb)
            acc = 0.0
            for y in range(ya, yb):
                base = y * w
                for x in range(xa, xb):
                    acc += gray[base + x]
            grid[cy][cx] = acc / ((yb - ya) * (xb - xa))
    return grid


def median3(grid: list[list[float]]) -> list[list[float]]:
    rows, cols = len(grid), len(grid[0])
    out = [[0.0] * cols for _ in range(rows)]
    for y in range(rows):
        for x in range(cols):
            near = []
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    near.append(grid[min(rows - 1, max(0, y + dy))][min(cols - 1, max(0, x + dx))])
            near.sort()
            out[y][x] = near[4]
    return out


def to_art(grid: list[list[float]], lo: float, hi: float, gamma: float,
           blank_below: float) -> str:
    lines = []
    for row in grid:
        line = []
        for value in row:
            if value < blank_below:
                line.append(" ")
                continue
            t = (value - lo) / (hi - lo)
            t = min(1.0, max(0.0, t)) ** gamma
            line.append(RAMP[int(t * (len(RAMP) - 1) + 0.5)])
        lines.append("".join(line).rstrip())
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def build(path: str, cols: int, rows: int, *, box=None, sigma: float = 1.5,
          lo: float = 45.0, hi: float = 215.0, gamma: float = 0.9,
          blank_below: float = 30.0) -> str:
    w, h, rgb = read_png(path)
    if box:
        w, h, rgb = crop(w, h, rgb, box)
    gray = grayscale(w, h, rgb)
    gray = gaussian_blur(gray, w, h, sigma)
    grid = median3(cells(gray, w, h, cols, rows))
    return to_art(grid, lo, hi, gamma, blank_below)


# -------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", help="input PNG (8-bit, non-interlaced)")
    ap.add_argument("--crop", default="", help="crop box WxH+X+Y, e.g. 350x413+340+120")
    ap.add_argument("--cols", type=int, default=45)
    ap.add_argument("--rows", type=int, default=28)
    ap.add_argument("--sigma", type=float, default=1.5, help="gaussian blur sigma")
    ap.add_argument("--lo", type=float, default=45.0, help="input level mapped to blank")
    ap.add_argument("--hi", type=float, default=215.0, help="input level mapped to densest")
    ap.add_argument("--gamma", type=float, default=0.9)
    ap.add_argument("--blank-below", type=float, default=30.0,
                    help="cells darker than this become spaces (kills background)")
    ap.add_argument("--out", default="", help="write art to this file instead of stdout")
    args = ap.parse_args(argv)

    if args.cols > MAX_COLS:
        ap.error(f"--cols max is {MAX_COLS} (info column starts at x=390)")
    box = None
    if args.crop:
        size, pos = args.crop.split("+")[0], args.crop.split("+")[1:]
        cw, ch = (int(v) for v in size.split("x"))
        box = (int(pos[0]), int(pos[1]), cw, ch)
    art = build(args.image, args.cols, args.rows, box=box, sigma=args.sigma,
                lo=args.lo, hi=args.hi, gamma=args.gamma,
                blank_below=args.blank_below)

    lines = art.split("\n")
    assert len(lines) <= args.rows
    assert max(len(line) for line in lines) <= MAX_COLS
    drawn = [line for line in lines if line.strip()]
    left = min(len(line) - len(line.lstrip()) for line in drawn)
    right = max(len(line.rstrip()) for line in drawn)
    print(
        f"# {len(lines)} rows, widest {max(len(l) for l in lines)} cols, "
        f"bbox cols {left}-{right} (centre {(left + right) / 2:.1f}, "
        f"canvas centre {args.cols / 2:.1f}), "
        f"aspect {args.cols * CHAR_W / (args.rows * LINE_H):.3f}",
        file=sys.stderr,
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(art + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(art)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
