#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the stock-link image: the user's own FNIRSI 2C53T stock V1.2.0 APP + two hooks + a
code cave that adds a USB HID link (see link.c).

    python3 build_link_image.py --stock APP_2C53T_V1.2.0_251015.bin --out APP_2C53T_V1.2.0_link.bin

Nothing stock is redistributed: the script reads the stock image you already have and refuses
any other version by sha256, because every address in link.c is a V1.2.0 fact.

What it does:
  1. compiles link.c + stubs.S with arm-none-eabi-gcc (-mgeneral-regs-only, no libc) and
     links them at 0x080BE700 with cave.ld, which asserts there is no .data and no .bss;
  2. verifies the stock image (sha256, length, the original bytes at each hook and the
     stock facts the cave relies on), then overwrites each hook with `b.w <stub>`;
  3. pads with 0xFF to the cave address and appends the cave;
  4. re-reads its own output and checks that only the hook bytes differ inside the
     original image and that each branch lands on its stub.

The result flashes like any stock APP image, through the factory IAP drive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fnlink  # noqa: E402  (the waveform shapes, shared with the host tool)

APP_BASE = 0x08007000
STOCK_SHA256 = "a17c5c35c97bb898f15672a1747bc1041d8ed507c16999ddba0d1e4e2ec0c760"
STOCK_LEN = 0xB7680
CAVE_ADDR = 0x080BE700
CAVE_MAX = 0x1900
NOP = bytes.fromhex("00bf")

# (address, original bytes it replaces, stub symbol, address the stub resumes at)
# The original bytes are replaced by `b.w <stub>` and padded with nops.
HOOKS = (
    # SysTick handler: push {r7, lr}; mov.w r0, #16  ->  resumes at msr BASEPRI, r0
    (0x0802A994, bytes.fromhex("80b54ff01000"), "link_stub_tick", 0x0802A99A),
    # fpga task, tail of the command loop: movw r0, #0xc10  ->  resumes at movt r0, #0x4001
    (0x0803E49A, bytes.fromhex("40f61040"), "link_stub_fpga", 0x0803E49E),
)

# (address, original bytes it replaces, stub symbol). The original instruction is replaced by
# `bl <stub>`, which returns to the instruction after it. Only with --languages.
LANGUAGE_CALLS = (
    # Settings > Language, OK: ldrb.w r0, [r5, #0xf6b]  (the row, stored as the language + 1 below)
    (0x0800BECA, bytes.fromhex("95f86b0f"), "link_stub_language"),
)

# What --languages relies on besides the bytes it replaces.
LANGUAGE_FACTS = (
    (0x0800BD60, bytes.fromhex("2de9f04186b040f2f805c2f20005"), "settings OK handler: push {r4-r8, lr}; r5 = state base"),
    (0x0800BECE, bytes.fromhex("42f66c56013042f65354c2f2000685f8600f"),
     "settings OK handler: adds r0, #1 and strb.w r0, [r5, #0xf60] after the call"),
)

# ---- Settings > Language with one entry per language (--language-menu) ----------------------
# The first instruction of the Language case of stock's options routine, ldrb.w r0, [r3, #0xf69],
# becomes `b.w link_stub_langpanel`; the stub's placeholder becomes `b.w LANGMENU_RESUME`, the
# routine's own exit.
LANGMENU_SITE = (0x0801CF7E, bytes.fromhex("93f8690f"), "link_stub_langpanel")
LANGMENU_RESUME = 0x0801D28C
# (address, stock value, new value, what): the last row the cursor reaches in the options of
# the first setting
LANGMENU_LIMIT = (0x0800B1D8, 0x01, 0x07, "last option of the Language setting")
# The names shown, in the order of the language byte (1 Chinese ... 8 Korean): each language's
# own. Stock's fonts have no glyph for the first character of the usual name of Japanese, so
# that one is in hiragana.
LANGMENU_NAMES = ("中文", "English", "Русский", "にほんご", "Español", "Português", "Deutsch", "한국어")
# stock's five fonts: {height in px: address of the character map}; the map has a pointer to a
# sorted list of 16-bit code points at +8 and their number at +16
FONT_CMAPS = {16: 0x080819A8, 18: 0x0808C3CC, 20: 0x0809A194, 25: 0x080A1630, 28: 0x080BA1B8}
LANGMENU_FACTS = (
    (0x0801CF50, bytes.fromhex("2de9f04f8bb0"), "options routine: push {r4-r11, lr}; sub sp, #0x2c"),
    (0x0801CF5E, bytes.fromhex("93f86a0f00f00f00062800f29081dfe810f00700"),
     "options routine: seven cases on the settings row, the first at 0x0801CF7E"),
    (0x0801D28C, bytes.fromhex("0bb0bde8f08f"), "options routine: add sp, #0x2c; pop {r4-r11, pc}"),
    (0x0801D26E, bytes.fromhex("41f21412c2f20002"), "options routine: text with the font at 0x20001114"),
    (0x0801D480, bytes.fromhex("48f250323e204ff03f0ec2f2"), "options routine: the band descriptor at 0x20008350"),
    (0x0800ABC2, bytes.fromhex("96f86a1f0ff2106201f00f0196f86b0f515c884280f0ec82"),
     "next-option key: stops at the byte of the table at 0x0800B1D8 for the settings row"),
    (0x0800B1D8, bytes.fromhex("01010303000001"), "last option of each of the seven settings"),
    (0x0800BECA, bytes.fromhex("95f86b0f42f66c56013042f65354c2f2000685f8600f"),
     "settings OK handler: the language is the option under the cursor plus one"),
    (0x0800F154, bytes.fromhex("2de9f04f93b0dde9"), "text routine: string and font at [sp], [sp, #4]"),
    (0x08039F6C, bytes.fromhex("2de9f04385b00446"), "picture routine (path, x, y)"),
    (0x0804C180, bytes.fromhex("738cffff5f05"), "menu colours: [1] white, [2] the cursor's"),
    (0x080BC499, b"3:/System file/72.jpg\0", "picture: bar under the cursor"),
    (0x080BC54A, b"3:/System file/75.jpg\0", "picture: mark, chosen, on the bar"),
    (0x080BC5FC, b"3:/System file/78.jpg\0", "picture: mark, not chosen, on the bar"),
    (0x080BC4F2, b"3:/System file/74.jpg\0", "picture: mark, chosen"),
    (0x080BC5CF, b"3:/System file/77.jpg\0", "picture: mark, not chosen"),
)

# ---- texts replaced (--texts) ----------------------------------------------------------------
# Stock keeps its texts as pointers in tables, a row per language. A text is replaced by
# pointing its slot at a string of ours placed after the cave's code: four bytes of data per
# text and no code. What a text may be is checked here against stock's own fonts.
# {height in px: address of the glyph descriptors}: 8 bytes a glyph, in the order of the
# character map's list, the advance in px in the top 12 bits of the first word
FONT_GLYPHS = {16: 0x080807F0, 18: 0x0808B214, 20: 0x08098FDC, 25: 0x080A0468, 28: 0x080B9000}
TEXT_MAX_BYTES = 96
TEXT_FACTS = (
    (0x0800F154, bytes.fromhex("2de9f04f93b0dde9"), "text routine: string and font at [sp], [sp, #4]"),
)

# ---- entries added to the generator's menu (--menu-wave, --menu-usb-wave) -------------------
STOCK_WAVEFORMS = 13
MENU_MAX = 8
MENU_LABEL_MAX = 17                 # stock's longest English name, "Reverse Step Wave"
# stock's table builder: push.w {r4-r11, lr} -> `b.w link_stub_wave`; the stub's placeholder
# becomes `b.w MENU_BUILDER_RESUME`
MENU_BUILDER = (0x08029E14, bytes.fromhex("2de9f04f"), "link_stub_wave")
MENU_BUILDER_RESUME = 0x08029E18
# (address, original instruction, stub): each replaced by `bl <stub>`
MENU_CALLS = tuple((a, bytes.fromhex("50f8340c"), "link_stub_name")      # ldr r0, [r0, #-0x34]: a row's name
                   for a in (0x080157E6, 0x0801583C, 0x08015886, 0x080158D0, 0x0801591A, 0x08015964)) + (
    (0x080108EA, bytes.fromhex("90f8590e"), "link_stub_icon_r0"),        # ldrb.w r0, [r0, #0xe59]: big picture
    (0x080127BC, bytes.fromhex("94f8591e"), "link_stub_icon_r1"),        # ldrb.w r1, [r4, #0xe59]: small picture
    (0x0801F8EE, bytes.fromhex("90f8590e"), "link_stub_icon_r0"),        # ldrb.w r0, [r0, #0xe59]: small picture
)
# (address of an 8-bit immediate, its stock value, what it is): each grows by the number of
# added entries
MENU_IMMEDIATES = (
    (0x0800A9C2, 0x0B, "next-waveform key: last index it steps from"),
    (0x0800C2AA, 0x06, "list opens: first row, compare"), (0x0800C2AE, 0x07, "list opens: first row, limit"),
    (0x0800D912, 0x07, "list opens (2): compare"), (0x0800D916, 0x07, "list opens (2): limit"),
    (0x0800E5F8, 0x07, "list opens (3): compare"), (0x0800E5FC, 0x07, "list opens (3): limit"),
    (0x0802E000, 0x06, "start-up: first row, compare"), (0x0802E004, 0x07, "start-up: first row, limit"),
)
MENU_FACTS = (
    (0x08029E18, bytes.fromhex("8eb0"), "table builder: sub sp, #0x38 after the hook"),
    (0x0802A98C, bytes.fromhex("0eb0bde8f08f"), "table builder: add sp, #0x38; pop {r4-r11, pc} (its only exit)"),
    (0x08029E22, bytes.fromhex("96f8592e0c2a"), "table builder: thirteen cases on the waveform byte"),
    (0x0801579C, bytes.fromhex("2de9f04784b0"), "waveform list: push {r4-r10, lr}"),
    (0x0800A9BE, bytes.fromhex("96f8590e0b2800f2f583"), "next-waveform key: ldrb, cmp #11, bhi"),
    (0x0800C2A6, bytes.fromhex("95f8590e062888bf0720"), "list opens: min(index, 7)"),
    (0x0800D90E, bytes.fromhex("94f8590e072828bf0720"), "list opens (2): min(index, 7)"),
    (0x0800E5F8, bytes.fromhex("072928bf0721"), "list opens (3): min(index, 7)"),
    (0x0802DFFC, bytes.fromhex("9af8590e062888bf0720"), "start-up: min(index, 7)"),
    (0x0804D848, bytes.fromhex("ff07800800097f09"), "stock's sine table"),
)


def parse_menu(waves, usb) -> list:
    """--menu-wave LABEL=SHAPE[:ARG][@ICON] ... and --menu-usb-wave LABEL[@ICON] as a list of entries."""
    entries = []

    def label_icon(text):
        label, _, icon = text.partition("@")
        label = label.strip()
        if not 1 <= len(label) <= MENU_LABEL_MAX or any(not 0x20 <= ord(c) <= 0x7E or c in '"\\' for c in label):
            raise BuildError("menu label %r: 1 to %d printable ASCII characters, no quote or backslash" % (label, MENU_LABEL_MAX))
        try:
            n = int(icon) if icon else 0
        except ValueError:
            raise BuildError("menu icon %r: the index of a stock waveform, 0 to 12" % icon) from None
        if not 0 <= n < STOCK_WAVEFORMS:
            raise BuildError("menu icon %d: the index of a stock waveform, 0 to 12" % n)
        return label, n

    for text in waves or ():
        left, sep, spec = text.partition("=")
        if not sep:
            raise BuildError("--menu-wave %r: LABEL=SHAPE[:ARG][@ICON]" % text)
        spec, _, icon = spec.partition("@")
        label, n = label_icon(left + ("@" + icon if icon else ""))
        shape, _, arg = spec.strip().partition(":")
        try:
            if Path(spec).is_file():
                values = fnlink.parse_wave_text(Path(spec).read_text())
                samples = fnlink.wave_normalise(fnlink.wave_resample(values))
            elif shape == "harmonics":
                samples = fnlink.wave_shape(shape, harmonics=fnlink.parse_harmonics(arg))
            else:
                samples = fnlink.wave_shape(shape, float(arg) if arg else 50.0)
        except (fnlink.LinkError, ValueError) as e:
            raise BuildError("--menu-wave %r: %s" % (text, e)) from None
        entries.append({"label": label, "icon": n, "source": spec.strip(), "samples": samples})
    if usb:
        label, n = label_icon(usb)
        entries.append({"label": label, "icon": n, "source": "usb", "samples": None})
    if len(entries) > MENU_MAX:
        raise BuildError("%d menu entries: at most %d" % (len(entries), MENU_MAX))
    for k, e in enumerate(entries):
        e["index"] = STOCK_WAVEFORMS + k
    return entries


def menu_header(entries) -> str:
    baked = [e for e in entries if e["samples"] is not None]
    usb = [e for e in entries if e["samples"] is None]
    rows = ",\n".join("    { %s }" % ", ".join(str(v) for v in e["samples"]) for e in baked) or "    { 0 }"
    return ("""/* generated by build_link_image.py: the entries added to the generator's menu */
#define MENU_EXTRA      %du
#define MENU_BAKED      %d
#define MENU_USB_INDEX  %du
static const uint16_t menu_wave_data[%d][100] = {
%s
};
static const uint8_t menu_wave_icon[MENU_EXTRA] = { %s };
static const char * const menu_wave_name[MENU_EXTRA] = { %s };
""" % (len(entries), len(baked), usb[0]["index"] if usb else 0, max(len(baked), 1), rows,
       ", ".join(str(e["icon"]) for e in entries), ", ".join('"%s"' % e["label"] for e in entries)))


def font_characters(stock: bytes, cmap: int) -> set:
    """The code points one of stock's fonts has a glyph for."""
    off = cmap - APP_BASE
    listed, count = struct.unpack_from("<I", stock, off + 8)[0], struct.unpack_from("<H", stock, off + 16)[0]
    return set(struct.unpack_from("<%dH" % count, stock, listed - APP_BASE))


def font_advances(stock: bytes, px: int) -> dict:
    """{code point: advance in px} of one of stock's fonts."""
    off = FONT_CMAPS[px] - APP_BASE
    listed = struct.unpack_from("<I", stock, off + 8)[0]
    first = struct.unpack_from("<H", stock, off + 6)[0]            # the glyph of the first character listed
    count = struct.unpack_from("<H", stock, off + 16)[0]
    codes = struct.unpack_from("<%dH" % count, stock, listed - APP_BASE)
    words = struct.unpack_from("<%dI" % (2 * count), stock, FONT_GLYPHS[px] + 8 * first - APP_BASE)
    return {cp: words[2 * i] >> 20 for i, cp in enumerate(codes)}


def text_width(stock: bytes, px: int, text: str) -> int:
    """The width stock's text routine gives a text in one of its fonts: the sum of the advances.
    The routine draws the characters that fit in the box it is given and drops the rest."""
    adv = font_advances(stock, px)
    missing = [c for c in text if ord(c) not in adv]
    if missing:
        raise BuildError("text %r: the %d px font has no %s" % (text, px, " ".join("U+%04X" % ord(c) for c in missing)))
    return sum(adv[ord(c)] for c in text)


def parse_texts(paths, stock: bytes) -> list:
    """--texts FILE ...: lines of `SLOT WAS FONT WIDTH TEXT`. SLOT is the address of the pointer
    replaced and WAS the pointer stock has there; FONT (px) and WIDTH (px) are those of the box
    the text is drawn in, which the text must fit."""
    texts, seen = [], set()
    for path in paths or ():
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise BuildError("--texts %s: %s" % (path, e)) from None
        for n, line in enumerate(lines, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            where = "%s line %d" % (path, n)
            parts = line.split(None, 4)
            try:
                slot, was, px, width = int(parts[0], 16), int(parts[1], 16), int(parts[2]), int(parts[3])
                text = parts[4].strip()
            except (ValueError, IndexError):
                raise BuildError("%s: SLOT WAS FONT WIDTH TEXT, the first two in hex" % where) from None
            if px not in FONT_CMAPS:
                raise BuildError("%s: font %d: one of %s" % (where, px, sorted(FONT_CMAPS)))
            if slot % 4 or not APP_BASE <= slot <= APP_BASE + len(stock) - 4 or slot in seen:
                raise BuildError("%s: slot %#010x: a pointer of the stock image, once" % (where, slot))
            found = struct.unpack_from("<I", stock, slot - APP_BASE)[0]
            if found != was:
                raise BuildError("%s: slot %#010x holds %#010x, not %#010x" % (where, slot, found, was))
            end = stock.find(b"\0", was - APP_BASE) if APP_BASE <= was < APP_BASE + len(stock) else -1
            if not 0 < end - (was - APP_BASE) <= 200:
                raise BuildError("%s: %#010x is not a text of the stock image" % (where, was))
            raw = text.encode("utf-8")
            if not 1 <= len(raw) <= TEXT_MAX_BYTES or any(ord(c) < 0x20 for c in text):
                raise BuildError("%s: a text of 1 to %d bytes" % (where, TEXT_MAX_BYTES))
            need = text_width(stock, px, text)
            if need > width:
                raise BuildError("%s: %r is %d px wide in the %d px font and its box is %d: stock would cut it"
                                 % (where, text, need, px, width))
            seen.add(slot)
            texts.append({"slot": slot, "was": was, "font": px, "box": width, "width": need, "text": text})
    return texts


def place_texts(cave: bytes, texts) -> bytes:
    """The cave with the texts after its code, each text given its address (`at`)."""
    out = bytearray(cave)
    placed = {}
    for t in texts:
        raw = t["text"].encode("utf-8") + b"\0"
        if raw not in placed:
            placed[raw] = CAVE_ADDR + len(out)
            out += raw
        t["at"] = placed[raw]
    out += b"\0" * (-len(out) % 4)
    return bytes(out)


def check_language_names(stock: bytes) -> None:
    """Stock's text routine ends a string at a character without a glyph: refuse such a name."""
    for px, cmap in FONT_CMAPS.items():
        have = font_characters(stock, cmap)
        if len(have) < 500:
            raise BuildError("the %d px font lists %d characters: not the font expected" % (px, len(have)))
        for name in LANGMENU_NAMES:
            missing = [c for c in name if ord(c) not in have]
            if missing:
                raise BuildError("language name %s: the %d px font has no %s" % (
                    name, px, " ".join("U+%04X" % ord(c) for c in missing)))


def language_header() -> str:
    def literal(name):
        out, escaped = "", False
        for c in name:
            if ord(c) < 0x80:
                # a hex escape runs on through hex digits: close the literal before one
                out += ('""' if escaped and c in "0123456789abcdefABCDEF" else "") + c
                escaped = False
            else:
                out += "".join("\\x%02X" % b for b in c.encode("utf-8"))
                escaped = True
        return out
    rows = "\n".join('    "%s",%s/* %s */' % (literal(n), " " * max(1, 44 - len(literal(n))), n) for n in LANGMENU_NAMES)
    return ("""/* generated by build_link_image.py: the names of Settings > Language, UTF-8 */
#define LANGUAGES %du
static const char * const language_name[LANGUAGES] = {
%s
};
""" % (len(LANGMENU_NAMES), rows))


# Stock facts the cave depends on, as (address, expected bytes, what it is). Checked against
# the image so a mismatch stops the build instead of producing a wrong patch.
STOCK_FACTS = (
    (0x08007090, bytes.fromhex("e5e80208"), "USB interrupt vector -> 0x0802E8E4"),
    (0x0802E8EA, bytes.fromhex("42f6243ac2f2000a"), "usb irq: core at 0x20002B24"),
    (0x08040B24, bytes.fromhex("2de9f0410b06"), "usbd_ept_open prologue"),
    (0x08040874, bytes.fromhex("80b501f07f02"), "usbd_ept_close prologue"),
    (0x08040EB4, bytes.fromhex("70b501f07f01"), "usbd_ept_send prologue"),
    (0x08036DB4, bytes.fromhex("b0b54ff2e870"), "serial-string descriptor function"),
    (0x0802A99A, bytes.fromhex("80f31188"), "msr BASEPRI, r0 after the hook"),
    (0x08042796, bytes.fromhex("46f2d420c2f20000"), "xTaskIncrementTick: xTickCount at 0x200062D4"),
    (0x0801067E, bytes.fromhex("40f2f800c2f2000090f8680f0728"), "USB Sharing: system mode at 0x20001060 == 7"),
    (0x080106B6, bytes.fromhex("45f64040c4f20000016821f00201"), "USB Sharing: connect sequence"),
    (0x0802E7E4, bytes.fromhex("44f6106cc2f2000c"), "USART2 irq: meter rx index at 0x20004E10"),
    (0x0802E7F4, bytes.fromhex("44f61161c2f20001"), "USART2 irq: meter rx buffer at 0x20004E11"),
    (0x0802E862, bytes.fromhex("0c2b"), "USART2 irq: a data frame is 12 bytes"),
    (0x0803E498, bytes.fromhex("7868"), "fpga task: ldr r0, [r7, #4] before the hook"),
    (0x0803E49E, bytes.fromhex("c4f201004021"), "fpga task: movt r0, #0x4001; movs r1, #0x40 after the hook"),
    (0x0803E4CA, bytes.fromhex("9df83610"), "fpga task: command byte at [sp, #0x36]"),
    (0x0803E5EE, bytes.fromhex("81f8b025"), "fpga task: command 4 stores into state[0x5B0]"),
)

CFLAGS = [
    "-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=soft", "-mgeneral-regs-only",
    "-Os", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-ffreestanding", "-fno-builtin", "-nostdlib",
    "-fno-tree-loop-distribute-patterns", "-fno-stack-protector",
    "-fno-asynchronous-unwind-tables", "-fomit-frame-pointer", "-ffunction-sections",
]

TOOLS = ("gcc", "ld", "objcopy", "nm")


class BuildError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Thumb-2 B.W (encoding T4, ARMv7-M ARM A7.7.12) ---------------------------------------

def enc_bw(src: int, dst: int) -> bytes:
    """`b.w dst` placed at src."""
    off = dst - (src + 4)
    if off % 2 or not -(1 << 24) <= off < (1 << 24):
        raise BuildError("b.w offset out of range or odd: %#x" % off)
    s = (off >> 24) & 1
    i1 = (off >> 23) & 1
    i2 = (off >> 22) & 1
    j1 = (~(i1 ^ s)) & 1
    j2 = (~(i2 ^ s)) & 1
    hw1 = 0xF000 | (s << 10) | ((off >> 12) & 0x3FF)
    hw2 = 0x9000 | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


def enc_bl(src: int, dst: int) -> bytes:
    """`bl dst` placed at src (encoding T1): the b.w encoding with bit 14 of the second half set."""
    hw1, hw2 = struct.unpack("<HH", enc_bw(src, dst))
    return struct.pack("<HH", hw1, hw2 | 0x4000)


def dec_bw(src: int, code: bytes, link: bool = False) -> int:
    """Target of the `b.w` at src (inverse of enc_bw), or of the `bl` with link=True."""
    hw1, hw2 = struct.unpack("<HH", code[:4])
    if (hw1 & 0xF800) != 0xF000 or (hw2 & 0xD000) != (0xD000 if link else 0x9000):
        raise BuildError("not a %s: %04x %04x" % ("bl" if link else "b.w", hw1, hw2))
    s = (hw1 >> 10) & 1
    i1 = (~(((hw2 >> 13) & 1) ^ s)) & 1
    i2 = (~(((hw2 >> 11) & 1) ^ s)) & 1
    off = (s << 24) | (i1 << 23) | (i2 << 22) | ((hw1 & 0x3FF) << 12) | ((hw2 & 0x7FF) << 1)
    if s:
        off -= 1 << 25
    return src + 4 + off


# --- the stock image ------------------------------------------------------------------------

def validate_stock(stock: bytes, hooks, calls=(), menu=(), langmenu=False, texts=False) -> None:
    got = sha256(stock)
    if got != STOCK_SHA256:
        raise BuildError("stock image sha256 is %s, not V1.2.0 (%s): refusing" % (got, STOCK_SHA256))
    if len(stock) != STOCK_LEN:
        raise BuildError("stock image is %#x bytes, expected %#x" % (len(stock), STOCK_LEN))
    for addr, orig, _, _ in hooks:
        off = addr - APP_BASE
        if stock[off:off + len(orig)] != orig:
            raise BuildError("hook %#010x: found %s, expected %s" % (addr, stock[off:off + len(orig)].hex(), orig.hex()))
    for addr, orig, _ in calls:
        off = addr - APP_BASE
        if stock[off:off + len(orig)] != orig:
            raise BuildError("call %#010x: found %s, expected %s" % (addr, stock[off:off + len(orig)].hex(), orig.hex()))
    if menu:
        for addr, orig, _ in (MENU_BUILDER,) + MENU_CALLS:
            off = addr - APP_BASE
            if stock[off:off + len(orig)] != orig:
                raise BuildError("menu site %#010x: found %s, expected %s" % (addr, stock[off:off + len(orig)].hex(), orig.hex()))
        for addr, value, what in MENU_IMMEDIATES:
            if stock[addr - APP_BASE] != value:
                raise BuildError("menu immediate %#010x (%s): found %#x, expected %#x" % (addr, what, stock[addr - APP_BASE], value))
    if langmenu:
        addr, orig, _ = LANGMENU_SITE
        if stock[addr - APP_BASE:addr - APP_BASE + len(orig)] != orig:
            raise BuildError("language menu site %#010x: found %s, expected %s" % (
                addr, stock[addr - APP_BASE:addr - APP_BASE + len(orig)].hex(), orig.hex()))
        addr, value, _, what = LANGMENU_LIMIT
        if stock[addr - APP_BASE] != value:
            raise BuildError("%#010x (%s): found %#x, expected %#x" % (addr, what, stock[addr - APP_BASE], value))
        check_language_names(stock)
    for addr, want, what in (STOCK_FACTS + (LANGUAGE_FACTS if calls else ()) + (MENU_FACTS if menu else ())
                             + (LANGMENU_FACTS if langmenu else ()) + (TEXT_FACTS if texts else ())):
        off = addr - APP_BASE
        if stock[off:off + len(want)] != want:
            raise BuildError("stock fact failed at %#010x (%s): found %s" % (addr, what, stock[off:off + len(want)].hex()))


# --- the cave -------------------------------------------------------------------------------

def find_tools() -> dict[str, str]:
    tools = {}
    for t in TOOLS:
        name = "arm-none-eabi-" + t
        path = shutil.which(name)
        if not path:
            raise BuildError("%s not found on PATH (Ubuntu: sudo apt install gcc-arm-none-eabi)" % name)
        tools[t] = path
    return tools


def run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError("command failed: %s\n%s%s" % (" ".join(cmd), r.stdout, r.stderr))
    return r.stdout


def build_cave(tools: dict[str, str], vid: int, pid: int, calls=(), menu=(), langmenu=False) -> tuple[bytes, dict[str, int]]:
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        defs = ["-DLINK_USB_VID=%#x" % vid, "-DLINK_USB_PID=%#x" % pid]
        if menu:
            (t / "menu_waves.h").write_text(menu_header(menu))
            defs += ["-DLINK_MENU", "-I", str(t)]
        run([tools["gcc"], *CFLAGS, *defs, "-c", str(HERE / "link.c"), "-o", str(t / "link.o")])
        run([tools["gcc"], *CFLAGS, "-c", str(HERE / "stubs.S"), "-o", str(t / "stubs.o")])
        objects = [str(t / "stubs.o"), str(t / "link.o")]
        if calls:                                       # the language stub is only linked in on request
            run([tools["gcc"], *CFLAGS, "-c", str(HERE / "lang.S"), "-o", str(t / "lang.o")])
            objects.append(str(t / "lang.o"))
        if menu:                                        # ... and so are the menu's stubs
            run([tools["gcc"], *CFLAGS, "-c", str(HERE / "menu.S"), "-o", str(t / "menu.o")])
            objects.append(str(t / "menu.o"))
        if langmenu:                                    # ... and the list of languages
            (t / "language_names.h").write_text(language_header(), encoding="utf-8")
            run([tools["gcc"], *CFLAGS, "-I", str(t), "-c", str(HERE / "langmenu.c"), "-o", str(t / "langmenu_c.o")])
            run([tools["gcc"], *CFLAGS, "-c", str(HERE / "langmenu.S"), "-o", str(t / "langmenu_s.o")])
            objects += [str(t / "langmenu_s.o"), str(t / "langmenu_c.o")]
        run([tools["ld"], "-T", str(HERE / "cave.ld"), "--gc-sections", "-o", str(t / "cave.elf"), *objects])
        run([tools["objcopy"], "-O", "binary", str(t / "cave.elf"), str(t / "cave.bin")])
        syms = {}
        for line in run([tools["nm"], str(t / "cave.elf")]).splitlines():
            parts = line.split()
            if len(parts) == 3:
                syms[parts[2]] = int(parts[0], 16)
        cave = (t / "cave.bin").read_bytes()
    if not cave or len(cave) > CAVE_MAX:
        raise BuildError("cave is %d bytes, limit %d" % (len(cave), CAVE_MAX))
    needed = [h[2] for h in HOOKS] + [c[2] for c in calls] + ["link_tick", "link_fpga_tail"]
    if menu:
        needed += [MENU_BUILDER[2], "link_stub_wave_resume"] + sorted({c[2] for c in MENU_CALLS})
        needed += ["link_wave_build", "link_wave_name", "link_wave_icon"]
        # the stub's way back into stock's builder, written here so that it costs no register
        at = syms.get("link_stub_wave_resume", 0) - CAVE_ADDR
        if not 0 <= at <= len(cave) - 4 or cave[at:at + 4] != bytes.fromhex("aff30080"):
            raise BuildError("link_stub_wave_resume is not the nop.w placeholder")
        cave = cave[:at] + enc_bw(CAVE_ADDR + at, MENU_BUILDER_RESUME) + cave[at + 4:]
    if langmenu:
        needed += [LANGMENU_SITE[2], "link_stub_langpanel_resume", "link_language_panel"]
        at = syms.get("link_stub_langpanel_resume", 0) - CAVE_ADDR
        if not 0 <= at <= len(cave) - 4 or cave[at:at + 4] != bytes.fromhex("aff30080"):
            raise BuildError("link_stub_langpanel_resume is not the nop.w placeholder")
        cave = cave[:at] + enc_bw(CAVE_ADDR + at, LANGMENU_RESUME) + cave[at + 4:]
    for need in needed:
        if need not in syms:
            raise BuildError("symbol %s missing from the cave" % need)
        if not CAVE_ADDR <= syms[need] < CAVE_ADDR + len(cave):
            raise BuildError("%s is at %#x, outside the cave" % (need, syms[need]))
    return cave, syms


# --- assembling and self-checking the image -------------------------------------------------

def assemble(stock: bytes, cave: bytes, syms: dict[str, int], hooks, calls=(), menu=(), langmenu=False, texts=()) -> bytes:
    img = bytearray(stock)
    for addr, orig, stub, _ in hooks:
        off = addr - APP_BASE
        patch = enc_bw(addr, syms[stub]) + NOP * ((len(orig) - 4) // 2)
        if len(patch) != len(orig):
            raise BuildError("hook %#010x: cannot fill %d bytes" % (addr, len(orig)))
        img[off:off + len(orig)] = patch
    for addr, orig, stub in calls:
        off = addr - APP_BASE
        if len(orig) != 4:
            raise BuildError("call %#010x: a bl replaces exactly 4 bytes" % addr)
        img[off:off + 4] = enc_bl(addr, syms[stub])
    if menu:
        addr, _, stub = MENU_BUILDER
        img[addr - APP_BASE:addr - APP_BASE + 4] = enc_bw(addr, syms[stub])
        for addr, _, stub in MENU_CALLS:
            img[addr - APP_BASE:addr - APP_BASE + 4] = enc_bl(addr, syms[stub])
        for addr, value, _ in MENU_IMMEDIATES:
            img[addr - APP_BASE] = value + len(menu)
    if langmenu:
        addr, _, stub = LANGMENU_SITE
        img[addr - APP_BASE:addr - APP_BASE + 4] = enc_bw(addr, syms[stub])
        img[LANGMENU_LIMIT[0] - APP_BASE] = LANGMENU_LIMIT[2]
    for t in texts:
        struct.pack_into("<I", img, t["slot"] - APP_BASE, t["at"])
    img += b"\xff" * (CAVE_ADDR - APP_BASE - len(img))
    img += cave
    return bytes(img)


def self_check(stock: bytes, image: bytes, cave: bytes, syms: dict[str, int], hooks, calls=(), menu=(), langmenu=False, texts=()) -> list[int]:
    if image[CAVE_ADDR - APP_BASE:] != cave:
        raise BuildError("cave bytes are not where they should be")
    if set(image[len(stock):CAVE_ADDR - APP_BASE]) - {0xFF}:
        raise BuildError("padding is not all 0xFF")
    diff = [APP_BASE + i for i in range(len(stock)) if stock[i] != image[i]]
    allowed = {addr + i for addr, orig, _, _ in hooks for i in range(len(orig))}
    allowed |= {addr + i for addr, orig, _ in calls for i in range(len(orig))}
    if menu:
        allowed |= {addr + i for addr, orig, _ in (MENU_BUILDER,) + MENU_CALLS for i in range(len(orig))}
        allowed |= {addr for addr, _, _ in MENU_IMMEDIATES}
    if langmenu:
        allowed |= {LANGMENU_SITE[0] + i for i in range(4)} | {LANGMENU_LIMIT[0]}
    for t in texts:
        if allowed & {t["slot"] + i for i in range(4)}:
            raise BuildError("text slot %#010x is in something else this image changes" % t["slot"])
        allowed |= {t["slot"] + i for i in range(4)}
    stray = [a for a in diff if a not in allowed]
    if not diff or stray:
        raise BuildError("bytes changed outside the hooks: %s" % ", ".join("%#x" % a for a in stray[:8]))
    for addr, orig, stub, resume in hooks:
        off = addr - APP_BASE
        if dec_bw(addr, image[off:off + 4]) != syms[stub]:
            raise BuildError("hook %#010x does not branch to %s" % (addr, stub))
        if image[off + 4:off + len(orig)] != NOP * ((len(orig) - 4) // 2):
            raise BuildError("hook %#010x is not padded with nops" % addr)
        # The stub must hand control back to the instruction after the displaced ones.
        if struct.pack("<I", resume | 1) not in cave:
            raise BuildError("resume address %#x not found in the cave" % resume)
    for addr, orig, stub in calls:
        off = addr - APP_BASE
        if dec_bw(addr, image[off:off + 4], link=True) != syms[stub]:
            raise BuildError("call %#010x does not reach %s" % (addr, stub))
    if menu:
        addr, _, stub = MENU_BUILDER
        if dec_bw(addr, image[addr - APP_BASE:addr - APP_BASE + 4]) != syms[stub]:
            raise BuildError("the table builder does not branch to %s" % stub)
        back = syms["link_stub_wave_resume"]
        if dec_bw(back, image[back - APP_BASE:back - APP_BASE + 4]) != MENU_BUILDER_RESUME:
            raise BuildError("the stub does not go back to %#x" % MENU_BUILDER_RESUME)
        for addr, _, stub in MENU_CALLS:
            if dec_bw(addr, image[addr - APP_BASE:addr - APP_BASE + 4], link=True) != syms[stub]:
                raise BuildError("menu call %#010x does not reach %s" % (addr, stub))
        for addr, value, what in MENU_IMMEDIATES:
            if image[addr - APP_BASE] != value + len(menu) or image[addr - APP_BASE + 1] != stock[addr - APP_BASE + 1]:
                raise BuildError("menu immediate %#010x (%s) is not %#x" % (addr, what, value + len(menu)))
    if langmenu:
        addr, _, stub = LANGMENU_SITE
        if dec_bw(addr, image[addr - APP_BASE:addr - APP_BASE + 4]) != syms[stub]:
            raise BuildError("the Language case does not branch to %s" % stub)
        back = syms["link_stub_langpanel_resume"]
        if dec_bw(back, image[back - APP_BASE:back - APP_BASE + 4]) != LANGMENU_RESUME:
            raise BuildError("the language stub does not leave through %#x" % LANGMENU_RESUME)
        if image[LANGMENU_LIMIT[0] - APP_BASE] != LANGMENU_LIMIT[2]:
            raise BuildError("the last option of the Language setting is not %d" % LANGMENU_LIMIT[2])
        for name in LANGMENU_NAMES:
            if name.encode("utf-8") + b"\0" not in cave:
                raise BuildError("language name %s is not in the cave" % name)
    for t in texts:
        at = struct.unpack_from("<I", image, t["slot"] - APP_BASE)[0]
        raw = t["text"].encode("utf-8") + b"\0"
        if not CAVE_ADDR <= at < CAVE_ADDR + len(cave) or image[at - APP_BASE:at - APP_BASE + len(raw)] != raw:
            raise BuildError("slot %#010x does not point at %r" % (t["slot"], t["text"]))
    return diff


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the FNIRSI 2C53T stock-link image (stock V1.2.0 + USB HID link).")
    ap.add_argument("--stock", required=True, type=Path, help="APP_2C53T_V1.2.0_251015.bin (your own copy)")
    ap.add_argument("--out", required=True, type=Path, help="output image")
    ap.add_argument("--vid", type=lambda s: int(s, 0), default=0x1209, help="USB vendor id (default 0x1209)")
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=0x0001, help="USB product id (default 0x0001, test PID)")
    ap.add_argument("--without-scope-hook", action="store_true",
                    help="leave the fpga task untouched: link, memory read and meter stream only "
                         "(a first image to flash, with the SysTick hook as the only change)")
    ap.add_argument("--languages", action="store_true",
                    help="also make the six languages stock still carries selectable again: OK on the "
                         "second row of Settings > Language steps through them (4 more stock bytes changed)")
    ap.add_argument("--language-menu", action="store_true",
                    help="Settings > Language lists the eight languages stock still carries, one entry each, "
                         "in two pages of four (5 stock bytes changed). Instead of --languages")
    ap.add_argument("--texts", type=Path, action="append", metavar="FILE",
                    help="replace texts of stock by those of FILE (repeatable): lines of `SLOT WAS FONT WIDTH TEXT`, "
                         "see texts/. Four bytes of stock data per text, no code")
    ap.add_argument("--menu-wave", action="append", metavar="LABEL=SHAPE[:ARG][@ICON]",
                    help="add an entry to the generator's own list of waveforms, after stock's thirteen (repeatable). "
                         "SHAPE is one of fnlink.py's (pulse:25 and triangle:25 take a duty, harmonics:1:1,3:0.33 a list) or a "
                         "text/CSV file with one period; ICON is the stock waveform (0-12) whose picture it borrows")
    ap.add_argument("--menu-usb-wave", metavar="LABEL[@ICON]",
                    help="add a last entry that plays the waveform most recently sent with `fnlink.py wave` "
                         "(kept until power-off; stock's sine until one is sent)")
    args = ap.parse_args()
    hooks = HOOKS[:1] if args.without_scope_hook else HOOKS
    calls = LANGUAGE_CALLS if args.languages else ()
    langmenu = args.language_menu
    try:
        if langmenu and calls:
            raise BuildError("--language-menu and --languages are two ways of choosing the language: give one")
        menu = parse_menu(args.menu_wave, args.menu_usb_wave)
        stock = args.stock.read_bytes()
        validate_stock(stock, hooks, calls, menu, langmenu, bool(args.texts))
        texts = parse_texts(args.texts, stock)
        cave, syms = build_cave(find_tools(), args.vid, args.pid, calls, menu, langmenu)
        cave = place_texts(cave, texts)
        if len(cave) > CAVE_MAX:
            raise BuildError("cave of %d bytes with the texts: the limit is %d" % (len(cave), CAVE_MAX))
        image = assemble(stock, cave, syms, hooks, calls, menu, langmenu, texts)
        diff = self_check(stock, image, cave, syms, hooks, calls, menu, langmenu, texts)
    except (BuildError, OSError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    args.out.write_bytes(image)
    manifest = {
        "stock_sha256": STOCK_SHA256,
        "image_sha256": sha256(image),
        "image_len": len(image),
        "cave_addr": "%#010x" % CAVE_ADDR,
        "cave_len": len(cave),
        "cave_sha256": sha256(cave),
        "hooks": ["%#010x" % h[0] for h in hooks],
        "calls": ["%#010x" % c[0] for c in calls],
        "menu": [{"index": e["index"], "label": e["label"], "icon": e["icon"], "source": e["source"]} for e in menu],
        "menu_sites": (["%#010x" % MENU_BUILDER[0]] + ["%#010x" % c[0] for c in MENU_CALLS]
                       + ["%#010x" % i[0] for i in MENU_IMMEDIATES]) if menu else [],
        "language_menu": list(LANGMENU_NAMES) if langmenu else [],
        "language_menu_sites": ["%#010x" % LANGMENU_SITE[0], "%#010x" % LANGMENU_LIMIT[0]] if langmenu else [],
        "texts": [{"slot": "%#010x" % t["slot"], "was": "%#010x" % t["was"], "at": "%#010x" % t["at"], "font": t["font"],
                   "box": t["box"], "width": t["width"], "text": t["text"]} for t in texts],
        "changed_stock_bytes": ["%#010x" % a for a in diff],
        "usb_vid_pid": "%04x:%04x" % (args.vid, args.pid),
        "symbols": {k: "%#010x" % v for k, v in sorted(syms.items())},
    }
    args.out.with_suffix(".json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("stock   %s (%d bytes)" % (STOCK_SHA256, len(stock)))
    print("cave    %d bytes at %#010x (limit %d)" % (len(cave), CAVE_ADDR, CAVE_MAX))
    print("hooks   %s: %d stock bytes changed" % (", ".join("%#010x" % h[0] for h in hooks + calls), len(diff)))
    if langmenu:
        print("language %s" % ", ".join(LANGMENU_NAMES))
    for t in texts:
        print("text    %#010x: %s (%d of %d px)" % (t["slot"], t["text"], t["width"], t["box"]))
    for e in menu:
        print("menu    %d %-17s %s, picture of waveform %d" % (e["index"], e["label"], e["source"], e["icon"]))
    print("image   %s (%d bytes) -> %s" % (manifest["image_sha256"], len(image), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
