#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the language tables and the fonts of a stock V1.2.0 image (no device needed).

    python3 tools/language_tables.py --stock APP_2C53T_V1.2.0_251015.bin
    python3 tools/language_tables.py --stock APP_2C53T_V1.2.0_251015.bin --list 5

Stock V1.2.0 offers Chinese and English but still carries its texts in eight languages. This
reads your own copy of the image and reports, for each language: that every table indexed by
the language byte has a valid string in its row, and that each of the six fonts from 15 to
28 px has a glyph for every character of those strings (a character without one ends the string on the screen).
--list N prints the strings of language N as they are in your image; they are FNIRSI's, which
is why this repository holds the addresses and not the texts.

The addresses are V1.2.0 facts (docs/stock_v120_notes.md, "Languages"); any other image is
refused by sha256.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path

APP_BASE = 0x08007000
STOCK_SHA256 = "a17c5c35c97bb898f15672a1747bc1041d8ed507c16999ddba0d1e4e2ec0c760"
LANGUAGES = ("Chinese", "English", "Russian", "Japanese", "Spanish", "Portuguese", "German", "Korean")

# (table, strings per language). One pointer per string, the eight languages one after another.
TABLES = tuple((base, 1) for base in (
    0x0804C904, 0x0804C398, 0x0804C3B8, 0x0804C1C8, 0x0804C1E8, 0x0804C1A8, 0x0804C188, 0x0804BFD0, 0x0804BFF0,
    0x0804C010, 0x0804C030, 0x0804C050, 0x0804C070, 0x0804C090, 0x0804C0B0, 0x0804C338, 0x0804C358, 0x0804C378,
    0x0804C934, 0x0801E13C, 0x0801E15C, 0x0801E420, 0x0801F264, 0x0801F284, 0x0801F2A4)) + (
    (0x0804C43C, 12),       # the meter's function names
    (0x0804C804, 8),        # the oscilloscope's menu
    (0x0804C61C, 13),       # the generator's waveform names
    (0x0804C258, 7),        # the settings menu
    (0x0804C218, 2),        # version and model
    (0x0804C5BC, 3),        # the generator's labels
)

# line height in pixels -> address of the font's single character map (lv_font_fmt_txt_cmap_t).
# Its list holds the code points themselves: the lookup at 0x0802F868 searches it for the character.
FONT_CMAPS = {15: 0x08077B10, 16: 0x080819A8, 18: 0x0808C3CC, 20: 0x0809A194, 25: 0x080A1630, 28: 0x080BA1B8}


class Image:
    def __init__(self, data: bytes):
        self.data = data

    def u16(self, addr: int) -> int:
        return struct.unpack_from("<H", self.data, addr - APP_BASE)[0]

    def u32(self, addr: int) -> int:
        return struct.unpack_from("<I", self.data, addr - APP_BASE)[0]

    def string(self, addr: int):
        """The UTF-8 string at addr, or None if addr is not a string in the image."""
        off = addr - APP_BASE
        if not 0 <= off < len(self.data):
            return None
        end = self.data.find(b"\0", off)
        if end < 0 or end - off > 200:
            return None
        try:
            return self.data[off:end].decode("utf-8")
        except UnicodeDecodeError:
            return None


def font_characters(img: Image, cmap: int) -> set:
    listed, count = img.u32(cmap + 8), img.u16(cmap + 16)
    return {img.u16(listed + 2 * i) for i in range(count)}


def language_strings(img: Image, language: int):
    """The strings of one language (1..8) in table order, and how many pointers were not strings."""
    out, bad = [], 0
    for base, per in TABLES:
        for j in range(per):
            s = img.string(img.u32(base + 4 * ((language - 1) * per + j)))
            if s is None:
                bad += 1
            else:
                out.append(s)
    return out, bad


def report(img: Image) -> tuple:
    """(lines, ok): the check of every language against every font."""
    fonts = {px: font_characters(img, cmap) for px, cmap in FONT_CMAPS.items()}
    lines = ["fonts     " + ", ".join("%d px: %d characters" % (px, len(c)) for px, c in fonts.items())]
    ok = True
    for language, name in enumerate(LANGUAGES, 1):
        strings, bad = language_strings(img, language)
        used = set("".join(strings)) - {"\n", "\r"}
        missing = {px: sorted(c for c in used if ord(c) not in chars) for px, chars in fonts.items()}
        partial = sorted({c for m in missing.values() for c in m})
        nowhere = [c for c in partial if all(c in m for m in missing.values())]
        text = "%-10s %2d strings, %3d characters, %d bad pointers" % (name, len(strings), len(used), bad)
        if not partial:
            text += ": every font has them all"
        else:
            have = [px for px, m in missing.items() if not m]
            text += ": %s only in the %s px font" % (" ".join("U+%04X" % ord(c) for c in partial),
                                                       "/".join(str(px) for px in have) or "?")
        if nowhere:
            text += "; in no font: %s" % " ".join("U+%04X" % ord(c) for c in nowhere)
        lines.append(text)
        if bad or nowhere:
            ok = False
    return lines, ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check the language tables and fonts of a stock V1.2.0 image.")
    ap.add_argument("--stock", required=True, type=Path, help="APP_2C53T_V1.2.0_251015.bin (your own copy)")
    ap.add_argument("--list", type=int, choices=range(1, 9), metavar="N",
                    help="print the strings of language N (1 Chinese ... 5 Spanish ... 8 Korean)")
    args = ap.parse_args(argv)
    data = args.stock.read_bytes()
    if hashlib.sha256(data).hexdigest() != STOCK_SHA256:
        print("error: not stock V1.2.0 (sha256 differs): the addresses here are V1.2.0 facts", file=sys.stderr)
        return 2
    img = Image(data)
    if args.list:
        strings, _ = language_strings(img, args.list)
        print("%s, %d strings:" % (LANGUAGES[args.list - 1], len(strings)))
        for s in strings:
            print("  " + s.replace("\n", " / "))
        return 0
    lines, ok = report(img)
    print("\n".join(lines))
    print("result    %s" % ("every table has its eight rows and no string would be cut short" if ok
                            else "something is missing: see above"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
