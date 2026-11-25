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

def validate_stock(stock: bytes, hooks, calls=()) -> None:
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
    for addr, want, what in STOCK_FACTS + (LANGUAGE_FACTS if calls else ()):
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


def build_cave(tools: dict[str, str], vid: int, pid: int, calls=()) -> tuple[bytes, dict[str, int]]:
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        defs = ["-DLINK_USB_VID=%#x" % vid, "-DLINK_USB_PID=%#x" % pid]
        run([tools["gcc"], *CFLAGS, *defs, "-c", str(HERE / "link.c"), "-o", str(t / "link.o")])
        run([tools["gcc"], *CFLAGS, "-c", str(HERE / "stubs.S"), "-o", str(t / "stubs.o")])
        objects = [str(t / "stubs.o"), str(t / "link.o")]
        if calls:                                       # the language stub is only linked in on request
            run([tools["gcc"], *CFLAGS, "-c", str(HERE / "lang.S"), "-o", str(t / "lang.o")])
            objects.append(str(t / "lang.o"))
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
    for need in [h[2] for h in HOOKS] + [c[2] for c in calls] + ["link_tick", "link_fpga_tail"]:
        if need not in syms:
            raise BuildError("symbol %s missing from the cave" % need)
        if not CAVE_ADDR <= syms[need] < CAVE_ADDR + len(cave):
            raise BuildError("%s is at %#x, outside the cave" % (need, syms[need]))
    return cave, syms


# --- assembling and self-checking the image -------------------------------------------------

def assemble(stock: bytes, cave: bytes, syms: dict[str, int], hooks, calls=()) -> bytes:
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
    img += b"\xff" * (CAVE_ADDR - APP_BASE - len(img))
    img += cave
    return bytes(img)


def self_check(stock: bytes, image: bytes, cave: bytes, syms: dict[str, int], hooks, calls=()) -> list[int]:
    if image[CAVE_ADDR - APP_BASE:] != cave:
        raise BuildError("cave bytes are not where they should be")
    if set(image[len(stock):CAVE_ADDR - APP_BASE]) - {0xFF}:
        raise BuildError("padding is not all 0xFF")
    diff = [APP_BASE + i for i in range(len(stock)) if stock[i] != image[i]]
    allowed = {addr + i for addr, orig, _, _ in hooks for i in range(len(orig))}
    allowed |= {addr + i for addr, orig, _ in calls for i in range(len(orig))}
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
    args = ap.parse_args()
    hooks = HOOKS[:1] if args.without_scope_hook else HOOKS
    calls = LANGUAGE_CALLS if args.languages else ()
    try:
        stock = args.stock.read_bytes()
        validate_stock(stock, hooks, calls)
        cave, syms = build_cave(find_tools(), args.vid, args.pid, calls)
        image = assemble(stock, cave, syms, hooks, calls)
        diff = self_check(stock, image, cave, syms, hooks, calls)
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
        "changed_stock_bytes": ["%#010x" % a for a in diff],
        "usb_vid_pid": "%04x:%04x" % (args.vid, args.pid),
        "symbols": {k: "%#010x" % v for k, v in sorted(syms.items())},
    }
    args.out.with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("stock   %s (%d bytes)" % (STOCK_SHA256, len(stock)))
    print("cave    %d bytes at %#010x (limit %d)" % (len(cave), CAVE_ADDR, CAVE_MAX))
    print("hooks   %s: %d stock bytes changed" % (", ".join("%#010x" % h[0] for h in hooks + calls), len(diff)))
    print("image   %s (%d bytes) -> %s" % (manifest["image_sha256"], len(image), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
