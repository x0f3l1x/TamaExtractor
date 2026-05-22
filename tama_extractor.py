#!/usr/bin/env python3
"""
TamaExtractor — Tamagotchi Smart sprite extractor (reference implementation)
============================================================================

Decodes the sprite pack of a Tamagotchi Smart firmware dump and exports every
image set to PNG.

The firmware format was reverse-engineered by zenzoa for the tool "Smarty Pants"
(Rust, MIT-licensed). This file is an independent Python re-implementation of the
*read* path, written to match Smarty Pants' output. The source of truth for every
offset and bitfield below is:

    https://github.com/zenzoa/smartypants
        src-tauri/src/firmware.rs          (pack base offsets)
        src-tauri/src/sprite_pack.rs       (16-byte header)
        src-tauri/src/sprite_pack/palette.rs   (ARGB1555 color)
        src-tauri/src/sprite_pack/sprite.rs    (sprite_def + pixel unpacking)
        src-tauri/src/sprite_pack/image_def.rs (image_def + assembly)

⚠️  Bring your own dump. This script ships NO Bandai data. Read your own device's
    flash, point this at the .bin, and keep the firmware/sprites out of any repo.

Usage:
    pip install pillow
    python tama_extractor.py my_dump.bin --out sprites/
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("This script needs Pillow:  pip install pillow")


# ---------------------------------------------------------------------------
# Constants (from firmware.rs)
# ---------------------------------------------------------------------------

# Where the sprite pack lives inside a full 16 MB firmware dump.
SPRITE_PACK_BASE = 0x730000

# Some patched firmwares prepend a 1024-byte header; it starts with this magic.
PATCH_HEADER_MAGIC = bytes([0x4F, 0x86, 0xA0, 0x86, 0x0A, 0xFE, 0x84, 0x30])
PATCH_HEADER_SIZE = 1024

# props bitfield lookup tables (sprite.rs)
BPP_TABLE = [2, 4, 6, 8]
DIM_TABLE = [8, 16, 32, 64]


# ---------------------------------------------------------------------------
# Little-endian readers (mirror data_view.rs)
# ---------------------------------------------------------------------------

def u8(d: bytes, i: int) -> int:
    return d[i]


def u16(d: bytes, i: int) -> int:
    return struct.unpack_from("<H", d, i)[0]


def i16(d: bytes, i: int) -> int:
    return struct.unpack_from("<h", d, i)[0]


def u32(d: bytes, i: int) -> int:
    return struct.unpack_from("<I", d, i)[0]


# ---------------------------------------------------------------------------
# Color: ARGB1555  (palette.rs::Color::from_word)
# ---------------------------------------------------------------------------

def color_from_word(word: int) -> tuple[int, int, int, int]:
    """ARGB1555 (16-bit) -> (r, g, b, a) 8-bit per channel.

    bit 15      : 1 = transparent, 0 = opaque
    bits 14-10  : red    (5 bits)
    bits  9- 5  : green  (5 bits)
    bits  4- 0  : blue   (5 bits)

    NOTE: channels are scaled 5->8 bits with a plain left-shift of 3 (max 248),
    exactly as Smarty Pants does. (A perceptually nicer expansion would be
    (v << 3) | (v >> 2), but we match the reference tool here.)
    """
    r = (word & 0x7C00) >> 7   # == ((word >> 10) & 0x1F) << 3
    g = (word & 0x03E0) >> 2   # == ((word >>  5) & 0x1F) << 3
    b = (word & 0x001F) << 3   # ==  (word        & 0x1F) << 3
    a = 0 if (word >> 15) else 255
    return (r, g, b, a)


def get_palettes(data: bytes) -> list[tuple[int, int, int, int]]:
    """Flat array of 2-byte colors -> list of RGBA tuples."""
    return [color_from_word(u16(data, i * 2)) for i in range(len(data) // 2)]


# ---------------------------------------------------------------------------
# Sprites: sprite_def (8 bytes) + pixel unpacking  (sprite.rs)
# ---------------------------------------------------------------------------

@dataclass
class Sprite:
    index: int
    width: int
    height: int
    bpp: int
    offset_x: int
    offset_y: int
    is_quadrupled: bool
    pixels: list[int]  # palette indices, length == width * height


def unpack_indices(data: bytes, start: int, num_pixels: int, bpp: int) -> list[int]:
    """Read num_pixels palette indices of `bpp` bits each, MSB-first within
    each byte and MSB-first across the bits of a pixel (matches get_bits)."""
    indices = []
    bitpos = start * 8
    for _ in range(num_pixels):
        val = 0
        for _ in range(bpp):
            byte = data[bitpos >> 3]
            bit = (byte >> (7 - (bitpos & 7))) & 1
            val = (val << 1) | bit
            bitpos += 1
        indices.append(val)
    return indices


def get_sprites(sprite_defs: bytes, pixel_data: bytes) -> list[Sprite]:
    sprites: list[Sprite] = []
    i = 0
    while i + 8 <= len(sprite_defs):
        pixel_data_index = u16(sprite_defs, i)
        offset_x = i16(sprite_defs, i + 2)
        offset_y = i16(sprite_defs, i + 4)
        props = u16(sprite_defs, i + 6)

        bpp = BPP_TABLE[props & 0x0003]
        width = DIM_TABLE[(props & 0x0030) >> 4]
        height = DIM_TABLE[(props & 0x00C0) >> 6]
        # bits 2-3 flipped, 8-11 palette_bank, 12-13 depth, 14 blend: unused on Smart
        is_quadrupled = bool((props & 0x8000) >> 15)
        if is_quadrupled:
            width *= 4
            height *= 4

        byte_count = width * height * bpp // 8
        start = pixel_data_index * byte_count  # index is in units of this sprite's size
        pixels = unpack_indices(pixel_data, start, width * height, bpp)

        sprites.append(Sprite(len(sprites), width, height, bpp,
                              offset_x, offset_y, is_quadrupled, pixels))
        i += 8
    return sprites


# ---------------------------------------------------------------------------
# Image sets: image_def (6 bytes) + grid assembly  (image_def.rs)
# ---------------------------------------------------------------------------

@dataclass
class ImageSet:
    index: int
    width: int
    height: int
    palette: list[tuple[int, int, int, int]]
    frames: list[list[int]] = field(default_factory=list)  # each: width*height indices


def get_image_sets(image_defs: bytes, sprites: list[Sprite],
                   colors: list[tuple[int, int, int, int]]) -> list[ImageSet]:
    image_sets: list[ImageSet] = []
    i = 0
    n = len(image_defs)
    while i + 6 <= n:
        first_sprite_index = u16(image_defs, i)
        next_sprite_index = u16(image_defs, i + 6) if i + 6 < n else len(sprites)
        width_in_sprites = u8(image_defs, i + 2)
        height_in_sprites = u8(image_defs, i + 3)
        first_palette_index = u16(image_defs, i + 4)

        if first_sprite_index >= len(sprites):
            raise ValueError(f"image_def {len(image_sets)}: sprite index out of range")
        first_sprite = sprites[first_sprite_index]

        width = width_in_sprites * first_sprite.width
        height = height_in_sprites * first_sprite.height
        bpp = first_sprite.bpp

        colors_per_palette = 2 ** bpp
        cstart = first_palette_index * 4          # palettes indexed in chunks of 4 colors
        cend = cstart + colors_per_palette
        if cend > len(colors):
            raise ValueError(f"image_def {len(image_sets)}: palette out of range")
        palette = colors[cstart:cend]

        sprites_per_frame = width_in_sprites * height_in_sprites
        if sprites_per_frame == 0:
            i += 6
            continue
        frame_count = (next_sprite_index - first_sprite_index) // sprites_per_frame

        frames: list[list[int]] = []
        for j in range(frame_count):
            buf = [0] * (width * height)
            base_sprite = first_sprite_index + j * sprites_per_frame
            for m in range(sprites_per_frame):
                sp = sprites[base_sprite + m]
                col = m % width_in_sprites
                row = m // width_in_sprites
                for n_idx, pix in enumerate(sp.pixels):
                    x = (n_idx % sp.width) + col * sp.width
                    y = (n_idx // sp.width) + row * sp.height
                    buf[x + y * width] = pix
            frames.append(buf)

        image_sets.append(ImageSet(len(image_sets), width, height, palette, frames))
        i += 6
    return image_sets


def render(image_set: ImageSet, frame: list[int]) -> Image.Image:
    img = Image.new("RGBA", (image_set.width, image_set.height))
    px = img.load()
    pal = image_set.palette
    w = image_set.width
    for idx, ci in enumerate(frame):
        color = pal[ci] if ci < len(pal) else (0, 0, 0, 0)
        px[idx % w, idx // w] = color
    return img


# ---------------------------------------------------------------------------
# Top-level: load firmware, parse sprite pack, export
# ---------------------------------------------------------------------------

def parse_sprite_pack(pack: bytes):
    image_defs_off = u32(pack, 0)
    sprite_defs_off = u32(pack, 4)
    palettes_off = u32(pack, 8)
    pixel_data_off = u32(pack, 12)

    if not (16 <= image_defs_off <= sprite_defs_off <= palettes_off
            <= pixel_data_off <= len(pack)):
        raise ValueError(
            "Sprite pack header offsets look wrong "
            f"({image_defs_off:#x}, {sprite_defs_off:#x}, "
            f"{palettes_off:#x}, {pixel_data_off:#x}). "
            "Wrong --base, or this isn't a sprite pack."
        )

    colors = get_palettes(pack[palettes_off:pixel_data_off])
    sprites = get_sprites(pack[sprite_defs_off:palettes_off], pack[pixel_data_off:])
    image_sets = get_image_sets(pack[image_defs_off:sprite_defs_off], sprites, colors)
    return image_sets, sprites, colors


def locate_sprite_pack(data: bytes, base: int | None, raw: bool) -> bytes:
    if raw:
        return data
    start = SPRITE_PACK_BASE if base is None else base
    if data[:8] == PATCH_HEADER_MAGIC:
        start += PATCH_HEADER_SIZE
        print(f"[i] Patch header detected, shifting base to {start:#x}")
    if start >= len(data):
        sys.exit(f"Base {start:#x} is past end of file ({len(data):#x} bytes).")
    return data[start:]


def contact_sheet(images: list[Image.Image], cols: int = 8, pad: int = 2) -> Image.Image:
    if not images:
        return Image.new("RGBA", (1, 1))
    cw = max(im.width for im in images) + pad
    ch = max(im.height for im in images) + pad
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * cw + pad, rows * ch + pad), (40, 40, 40, 255))
    for k, im in enumerate(images):
        x = pad + (k % cols) * cw
        y = pad + (k // cols) * ch
        sheet.alpha_composite(im, (x, y))
    return sheet


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract Tamagotchi Smart sprites to PNG.")
    ap.add_argument("dump", help="firmware .bin (your own dump)")
    ap.add_argument("--out", default="sprites", help="output directory")
    ap.add_argument("--base", type=lambda s: int(s, 0),
                    help=f"sprite pack base offset (default {SPRITE_PACK_BASE:#x})")
    ap.add_argument("--raw-sprite-pack", action="store_true",
                    help="input is already just the sprite pack, not a full firmware")
    args = ap.parse_args()

    data = Path(args.dump).read_bytes()
    pack = locate_sprite_pack(data, args.base, args.raw_sprite_pack)
    image_sets, sprites, colors = parse_sprite_pack(pack)

    print(f"[i] {len(colors)} colors, {len(sprites)} sprites, "
          f"{len(image_sets)} image sets")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    firsts: list[Image.Image] = []
    for iset in image_sets:
        for f, frame in enumerate(iset.frames):
            img = render(iset, frame)
            name = f"set_{iset.index:03d}_frame_{f:02d}.png"
            img.save(out / name)
            if f == 0:
                firsts.append(img)

    contact_sheet(firsts).save(out / "_contact_sheet.png")
    print(f"[✓] Wrote PNGs + _contact_sheet.png to {out}/")


if __name__ == "__main__":
    main()
