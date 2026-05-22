# TamaExtractor — Reverse engineering the Tamagotchi Smart sprite pack

📖 **English** · [Español](README.es.md)

Write-up and tool for understanding how graphics (sprites) are stored in the
firmware of the **Tamagotchi Smart** (2022), and how to extract them to PNG from a
dump of your own flash.

> ⚠️ **Bring your own dump.** This repo contains **code and documentation**, not
> Bandai firmware or sprites. The images and ROM are the intellectual property of
> Bandai/WiZ; this only explains how to work with a dump you extract yourself from
> your own device. Do not upload or redistribute the `.bin` or the assets.

---

## Table of contents

- [The device](#the-device)
- [Format TL;DR](#format-tldr)
- [How I did it (methodology)](#how-i-did-it-methodology)
- [The sprite pack format in detail](#the-sprite-pack-format-in-detail)
- [Using the extractor](#using-the-extractor)
- [Credits](#credits)
- [What is and isn't in this repo](#what-is-and-isnt-in-this-repo)
- [License](#license)

---

## The device

- **Model:** Tamagotchi Smart (the color-screen one, 2022).
- **Flash:** `MX25L12835F`, a 16 MB (128 Mbit) SPI NOR. The whole firmware fits
  there, and most of the space is data (graphics, text, animations), not code.
- **Dump:** a single 16 MB `.bin` read from the flash.

## Format TL;DR

If you just want the magic constants:

| Region | Offset | Contents |
|---|---|---|
| **Data Pack** | `0x6CE000` | Metadata, strings, definitions |
| **Sprite Pack** | `0x730000` | The actual graphics |

- Pixels are **not** direct color: they are **indices into a palette**.
- The palette uses **ARGB1555** color (16 bits per color), **not** RGB565.

```
ARGB1555  (2 bytes per color, little-endian)
 bit 15 : alpha / transparency  (1 = transparent, 0 = opaque)
 bits 14-10 : red    (5 bits)
 bits  9- 5 : green  (5 bits)
 bits  4- 0 : blue   (5 bits)
```

The 5→8 bit scaling is a plain `<< 3` (max 248, not 255), exactly like smartypants
does. Pure white comes out as `(248, 248, 248)`.

Sprite pack layout:

```
0x730000  ┌──────────────────────────┐
          │ header (16 bytes)        │  4× u32 → offsets to the sub-tables
          ├──────────────────────────┤
          │ image_defs (6 bytes ea.) │  links each sprite to its palette
          ├──────────────────────────┤
          │ sprite_defs (8 bytes ea.)│  width, height, bpp, pixel offset
          ├──────────────────────────┤
          │ palettes (ARGB1555)      │  the COLOR lives here
          ├──────────────────────────┤
          │ pixel_data (packed bits) │  palette indices, bit-packed
          └──────────────────────────┘
```

> The **header offsets are relative to the start of the sprite pack**, not the
> start of the firmware. The exact layout of each `*_def` is documented below; the
> canonical reference is still the **smartypants** source (see [Credits](#credits)).

## How I did it (methodology)

I'm writing it as a recipe because it works for reversing almost any unknown
graphics format, not just this one.

### 1. Recon

Hash the dump (for a reference), check its size, and run an **entropy analysis**
with `binwalk` or `ent`. This separates the regions at a glance: code has a
different "texture" than structured data, and compressed/encrypted blobs give
entropy close to 1. A quick `strings` reveals where the text lives.

### 2. Visual triage (the killer trick)

Open the `.bin` as a raw bitmap and slide the **width** until the sprites *snap*
into alignment and appear all at once. Any raw-pixel viewer works (rawpixels.net,
GIMP's raw import, or a 20-line script that paints the bytes as grayscale). This
gives you the rough offset of the graphics and a hint at the stride.

### 3. Guess the format

With sprites now visible, deduce the **bits per pixel**: 1bpp mono, indexed at
2/4/8 bits, or direct color? Color screens are almost always indexed + palette to
save space. Then locate the palette: a run of 2-byte values near the graphics.
Decode candidates and test until the colors make sense. (This is where I found out
it was **ARGB1555** and not RGB565, which was my first wrong hypothesis.)

### 4. Reverse the tables

The firmware doesn't have "loose" pixels: it has arrays of descriptors (width,
height, offset, palette index). The **diffing** method is gold: identify two
sprites you recognize, look at how their descriptors differ, and deduce which field
is which.

### 5. Write the parser incrementally

Hardcode the base offset, read the header, print what you think are pointers and
**validate them**: do they point inside the file? are they aligned? are they
increasing? Build the minimal "render ONE sprite" path, eyeball it, and iterate.

### 6. Validate

Render **all** sprites to a contact sheet and look for visual garbage (wrong width,
wrong bpp, crossed palette) to fix.

## The sprite pack format in detail

### Header (16 bytes, at the pack's base offset)

Four little-endian `u32`, all **relative to the start of the pack**:

| Offset | Field | Points to |
|---|---|---|
| `0`  | `image_defs_offset`  | image-def table |
| `4`  | `sprite_defs_offset`  | sprite-def table |
| `8`  | `palettes_offset`  | color array |
| `12` | `pixel_data_offset` | pixel data |

Each section runs from its offset to the start of the next one (the pixel section
runs to the end of the pack).

### Color: ARGB1555

Each color is 2 bytes LE. The decoder is pure bit arithmetic:

```python
def color_from_word(word: int) -> tuple[int, int, int, int]:
    r = (word & 0x7C00) >> 7   # == ((word >> 10) & 0x1F) << 3
    g = (word & 0x03E0) >> 2   # == ((word >>  5) & 0x1F) << 3
    b = (word & 0x001F) << 3   # ==  (word        & 0x1F) << 3
    a = 0 if (word >> 15) else 255   # bit 15: 1 = transparent
    return (r, g, b, a)
```

### sprite_def (8 bytes each)

| Offset | Type | Field |
|---|---|---|
| `0` | `u16` | `pixel_data_index` |
| `2` | `i16` | `offset_x` |
| `4` | `i16` | `offset_y` |
| `6` | `u16` | `props` (bitfield) |

`props` packs several things. What matters on the Smart:

```
bits 0-1  : bpp index    -> [2, 4, 6, 8] bits per pixel
bits 4-5  : width index  -> [8, 16, 32, 64] px
bits 6-7  : height index -> [8, 16, 32, 64] px
bit  15   : is_quadrupled  (if set, width and height ×4)
```

(Bits 2-3, 8-13 and 14 exist but are unused on the Smart.)

**Key gotcha:** `pixel_data_index` is **not a byte offset**. It's an index in units
of the sprite's own size. The real offset is:

```
byte_count  = width * height * bpp / 8
real_offset = pixel_data_index * byte_count
```

### Pixel unpacking

Pixels are palette indices packed into bits, **MSB first** within each byte and MSB
first across the bits of a pixel:

```python
def unpack_indices(data, start, num_pixels, bpp):
    indices, bitpos = [], start * 8
    for _ in range(num_pixels):
        val = 0
        for _ in range(bpp):
            byte = data[bitpos >> 3]
            bit = (byte >> (7 - (bitpos & 7))) & 1
            val = (val << 1) | bit
            bitpos += 1
        indices.append(val)
    return indices
```

### image_def (6 bytes each)

| Offset | Type | Field |
|---|---|---|
| `0` | `u16` | `first_sprite_index` |
| `2` | `u8`  | `width_in_sprites` |
| `3` | `u8`  | `height_in_sprites` |
| `4` | `u16` | `first_palette_index` |

An image set is a grid of `width_in_sprites × height_in_sprites` sprites, and it can
have several consecutive *frames* (sub-images). The number of sprites in a set is
inferred from the `first_sprite_index` of the **next** image_def.

**Palette gotcha:** `first_palette_index` is counted in **chunks of 4 colors**, so
the offset into the color array is `first_palette_index * 4`. The number of colors
per palette is `2 ** bpp` (4, 16, 64 or 256), and `bpp` is taken from the set's
first sprite.

From there, for each frame: place each sprite into its grid cell, look up the color
of each index in the set's palette, and build the RGBA image you dump to PNG.

## Using the extractor

> The `tama_extractor.py` script expects **you** to hand it your own dump.

```bash
pip install pillow
python tama_extractor.py my_dump.bin --out sprites/
```

It writes one PNG per sprite to `sprites/`, plus a contact sheet so you can check at
a glance that everything decoded correctly.

## Credits

The original reverse engineering of the Tamagotchi Smart firmware format was done by
**zenzoa**, not me. My contribution here is the Python re-implementation and this
educational walkthrough. All credit for discovering the format is theirs:

- **Smarty Pants** — the reference tool (Rust, MIT) to read and edit firmware and
  TamaSma cards: <https://github.com/zenzoa/smartypants>
- Data format documentation and community guides:
  <https://zenzoa.com/pocketfriends/tama-smart.html>

If this write-up was useful to you, go give their repo a star. 🌟

## What is and isn't in this repo

✅ **Yes:** extraction code, format documentation, this write-up.
❌ **No:** no firmware dump, no ROM, no Bandai sprites.

Reverse engineering a device you own to understand it and interoperate is a
legitimate and common activity in the community. Redistributing the ROM or the
brand's graphics is a different matter, which is why this repo does not include them.

## License

[MIT](LICENSE). If you reuse or port code from smartypants, remember to keep their
MIT copyright notice as well.
