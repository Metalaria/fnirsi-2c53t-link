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

def validate_stock(stock: bytes, hooks, calls=(), menu=()) -> None:
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
    for addr, want, what in STOCK_FACTS + (LANGUAGE_FACTS if calls else ()) + (MENU_FACTS if menu else ()):
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


def build_cave(tools: dict[str, str], vid: int, pid: int, calls=(), menu=()) -> tuple[bytes, dict[str, int]]:
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
    for need in needed:
        if need not in syms:
            raise BuildError("symbol %s missing from the cave" % need)
        if not CAVE_ADDR <= syms[need] < CAVE_ADDR + len(cave):
            raise BuildError("%s is at %#x, outside the cave" % (need, syms[need]))
    return cave, syms


# --- assembling and self-checking the image -------------------------------------------------

def assemble(stock: bytes, cave: bytes, syms: dict[str, int], hooks, calls=(), menu=()) -> bytes:
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
    img += b"\xff" * (CAVE_ADDR - APP_BASE - len(img))
    img += cave
    return bytes(img)


def self_check(stock: bytes, image: bytes, cave: bytes, syms: dict[str, int], hooks, calls=(), menu=()) -> list[int]:
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
    try:
        menu = parse_menu(args.menu_wave, args.menu_usb_wave)
        stock = args.stock.read_bytes()
        validate_stock(stock, hooks, calls, menu)
        cave, syms = build_cave(find_tools(), args.vid, args.pid, calls, menu)
        image = assemble(stock, cave, syms, hooks, calls, menu)
        diff = self_check(stock, image, cave, syms, hooks, calls, menu)
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
        "changed_stock_bytes": ["%#010x" % a for a in diff],
        "usb_vid_pid": "%04x:%04x" % (args.vid, args.pid),
        "symbols": {k: "%#010x" % v for k, v in sorted(syms.items())},
    }
    args.out.with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("stock   %s (%d bytes)" % (STOCK_SHA256, len(stock)))
    print("cave    %d bytes at %#010x (limit %d)" % (len(cave), CAVE_ADDR, CAVE_MAX))
    print("hooks   %s: %d stock bytes changed" % (", ".join("%#010x" % h[0] for h in hooks + calls), len(diff)))
    for e in menu:
        print("menu    %d %-17s %s, picture of waveform %d" % (e["index"], e["label"], e["source"], e["icon"]))
    print("image   %s (%d bytes) -> %s" % (manifest["image_sha256"], len(image), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
