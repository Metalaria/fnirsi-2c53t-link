#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Emulated end-to-end test of a stock-link image.

    python3 test/test_link_emu.py --stock APP_2C53T_V1.2.0_251015.bin --image APP_2C53T_V1.2.0_link.bin

Runs the patched image's real code -- the hooked SysTick handler, stock's USB interrupt
handler and usbd core, and the cave -- in Unicorn against a register-level model of the USB
device peripheral and a minimal host (usbemu.py). It first enumerates the UNPATCHED image as
a control: if the model were wrong, stock's own mass-storage device would not enumerate.

Needs: pip install unicorn. Exit status 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_link_image as builder  # noqa: E402
import fnlink  # noqa: E402
import usbemu  # noqa: E402
from usbemu import UsbEmu, Nak, Stall, UDEV, SYSTEM_MODE, X_TICK_COUNT  # noqa: E402

LINK_RAM = 0x20037000
LINK_RAM_LIMIT = 0x20037FE0
LINK_MAGIC = 0x4B4E4C33
STATE_BASE = 0x200000F8
CH1_BUF, CH2_BUF = 0x200006A8, 0x20000AA8
METER_TX_BUF = 0x20000005
TASK_STACK_TOP = 0x20035F00
EVENTS = (0x90, 0xA0, 0xA1, 0xB0)
STOCK_CLASS, STOCK_DESC = 0x20000030, 0x20000058
CAVE_ADDR = 0x080BE700
BOOT_HOLDOFF, SETTLE = 3000, 1000

STREAM_METER, STREAM_SCOPE, STREAM_STATUS = 1, 2, 4
REPORT_DESC = bytes.fromhex("0600ff0901a101150026ff00750895400901810295400901910 2c0".replace(" ", ""))

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("%s  %s%s" % ("ok  " if cond else "FAIL", name, (" -- " + detail) if detail and not cond else ""))


def make(image, ram):
    e = UsbEmu(image, ram)
    e.stock_usb_init()
    # A stand-in "factory" pattern below the application, so reads of 0x08006000 are testable.
    e.mu.mem_protect(0x08000000, 0x100000, 7)
    e.mu.mem_write(0x08006000, bytes((i * 7 + 3) & 0xFF for i in range(0x1000)))
    e.mu.mem_protect(0x08000000, 0x100000, 5)
    return e


def stock_disconnect(e):
    """The two writes stock does on every mode change and before saving settings."""
    e.cntr |= 2
    e.cfg |= 2


def stock_connect(e):
    """The two writes stock does when the USB Sharing screen is up."""
    e.cntr &= ~2
    e.cfg &= ~2


def poll_in(e):
    """One millisecond on the bus: a start of frame, then the host polls the IN endpoint."""
    e.sof()
    try:
        return e.host_in(1)
    except Nak:
        return None


def request(e, cmd, seq=0, length=0, addr=0, events=None, data=b""):
    """Send a request and return its response. Events queued ahead of it are appended to
    `events` if given, otherwise dropped."""
    rq = bytes([cmd, seq, length, 0]) + struct.pack("<I", addr) + data + bytes(56 - len(data))
    e.host_out(1, rq)
    for _ in range(100):
        try:
            rs = e.host_in(1)
        except Nak:
            e.sof()
            continue
        if rs[0] not in EVENTS:
            return rs
        if events is not None:
            events.append(rs)
    raise RuntimeError("no response")


def drain(e, limit=200):
    """Collect event reports until the device has nothing more to send."""
    out, idle = [], 0
    while idle < 3 and len(out) < limit:
        rs = poll_in(e)
        if rs is None:
            idle += 1
        else:
            idle = 0
            out.append(rs)
    return out


def feed_meter(e, frame, sof_every=1):
    """Deliver a frame from the meter chip one byte per millisecond, as on the wire."""
    for i, b in enumerate(frame):
        e.uart_byte(b)
        if (i + 1) % sof_every == 0:
            e.sof()


def task_view(e, snaps):
    """What must be identical between stock and the patched image after running the fpga
    task: its registers at every command boundary, all SRAM below the test stacks, the
    task's own live frame, and every peripheral write."""
    ram = bytes(e.mu.mem_read(0x20000000, 0x35000))
    frame = bytes(e.mu.mem_read(TASK_STACK_TOP - 0x38, 0x38))
    return snaps, ram, frame, dict(e.regs), e.spi_count


def read_mem(e, addr, n, seq=0):
    out = b""
    while len(out) < n:
        k = min(56, n - len(out))
        rs = request(e, 0x02, seq, k, addr + len(out))
        if rs[2] != 0 or rs[3] != k:
            raise RuntimeError("read failed at %08X: status %d n %d" % (addr + len(out), rs[2], rs[3]))
        out += rs[8:8 + k]
    return out


def language_ok(image, ram, cursor, language):
    """Run stock's Settings OK handler (0x0800BD60) on the Language row with the cursor on
    `cursor` and the language byte at `language`, up to the instruction after the one that
    stores the language. Calls into stock are skipped (there is no scheduler here); a call
    into the cave is followed. Returns the language stored, the registers stock still uses
    and every SRAM write."""
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
    from unicorn import arm_const as A
    mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    mu.mem_map(0x08000000, 0x100000)
    mu.mem_write(0x08007000, image)
    mu.mem_map(0x20000000, 0x40000)
    mu.mem_write(0x20000000, ram)
    for off, v in ((0xF68, 4), (0xF69, 1), (0xF6A, 0), (0xF6B, cursor), (0xF60, language)):
        mu.mem_write(STATE_BASE + off, bytes([v]))
    mu.reg_write(A.UC_ARM_REG_SP, 0x20036F00)
    mu.reg_write(A.UC_ARM_REG_LR, usbemu.RET_MAGIC | 1)
    writes, steps = [], [0]

    def code(mu, addr, size, _):
        steps[0] += 1
        if addr == 0x0800BEE0 or steps[0] > 5000:
            mu.emu_stop()
        elif size == 4 and addr < CAVE_ADDR:
            h1, h2 = struct.unpack("<HH", mu.mem_read(addr, 4))
            if (h1 & 0xF800) == 0xF000 and (h2 & 0xD000) == 0xD000:      # bl
                off = ((h1 & 0x3FF) << 12) | ((h2 & 0x7FF) << 1)
                sgn = (h1 >> 10) & 1
                i1 = (~(((h2 >> 13) & 1) ^ sgn)) & 1
                i2 = (~(((h2 >> 11) & 1) ^ sgn)) & 1
                off |= (sgn << 24) | (i1 << 23) | (i2 << 22)
                if sgn:
                    off -= 1 << 25
                if addr + 4 + off < CAVE_ADDR:                           # a call into stock: skipped
                    mu.reg_write(A.UC_ARM_REG_R0, 1)
                    mu.reg_write(A.UC_ARM_REG_PC, (addr + 4) | 1)

    mu.hook_add(UC_HOOK_CODE, code)
    mu.hook_add(UC_HOOK_MEM_WRITE, lambda mu, acc, addr, size, value, _: writes.append((addr, size)),
                begin=0x20000000, end=0x20035FFF)
    mu.emu_start(0x0800BD60 | 1, usbemu.RET_MAGIC, count=20000)
    at = mu.reg_read(A.UC_ARM_REG_PC)
    regs = tuple(mu.reg_read(r) for r in (A.UC_ARM_REG_R1, A.UC_ARM_REG_R2, A.UC_ARM_REG_R3, A.UC_ARM_REG_R4,
                                          A.UC_ARM_REG_R5, A.UC_ARM_REG_R6, A.UC_ARM_REG_R7, A.UC_ARM_REG_R8,
                                          A.UC_ARM_REG_R9, A.UC_ARM_REG_R10, A.UC_ARM_REG_R11, A.UC_ARM_REG_R12,
                                          A.UC_ARM_REG_SP))
    return at, mu.mem_read(STATE_BASE + 0xF60, 1)[0], regs, writes


def run_code(image, ram, entry, stops, state=(), regs=(), follow=(), limit=200000, skip=None, setup=None):
    """Run code of an image from `entry` until one of `stops` (or the return to the caller) on a
    copy of `ram`, with the bytes of stock's state structure in `state` ({offset: value}) and
    the registers in `regs` ({name: value}). A bl into stock is skipped, r0 untouched, unless
    its target is in `follow`; a bl into the cave is always followed. `stops` may map an
    address to a function called with the emulator before stopping there. `skip` maps the
    entry of a routine to a function called with the emulator when it is reached, from stock
    or from the cave, after which the routine returns at once. `setup` is called with the
    emulator before it starts. Returns the emulator, the address it stopped at and the list of
    SRAM writes below the test stack."""
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
    from unicorn import arm_const as A
    mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    mu.mem_map(0x08000000, 0x100000)
    mu.mem_write(0x08007000, image)
    mu.mem_map(0x20000000, 0x40000)
    mu.mem_write(0x20000000, ram)
    for off, v in dict(state).items():
        mu.mem_write(STATE_BASE + off, bytes([v & 0xFF]))
    mu.reg_write(A.UC_ARM_REG_SP, 0x20036F00)
    mu.reg_write(A.UC_ARM_REG_LR, usbemu.RET_MAGIC | 1)
    for name, v in dict(regs).items():
        mu.reg_write(getattr(A, "UC_ARM_REG_" + name.upper()), v)
    writes, steps, at = [], [0], [None]
    stops = stops if isinstance(stops, dict) else {a: None for a in stops}
    skip = skip or {}
    if setup:
        setup(mu)

    def code(mu, addr, size, _):
        steps[0] += 1
        if addr in skip:                                                 # seen with its arguments, not run
            skip[addr](mu)
            mu.reg_write(A.UC_ARM_REG_PC, mu.reg_read(A.UC_ARM_REG_LR) | 1)
        elif addr in stops or steps[0] > limit:
            at[0] = addr
            if stops.get(addr):
                stops[addr](mu)
            mu.emu_stop()
        elif size == 4 and addr < CAVE_ADDR:
            h1, h2 = struct.unpack("<HH", mu.mem_read(addr, 4))
            if (h1 & 0xF800) == 0xF000 and (h2 & 0xD000) == 0xD000:      # bl
                off = ((h1 & 0x3FF) << 12) | ((h2 & 0x7FF) << 1)
                sgn = (h1 >> 10) & 1
                off |= (sgn << 24) | (((~(((h2 >> 13) & 1) ^ sgn)) & 1) << 23) | (((~(((h2 >> 11) & 1) ^ sgn)) & 1) << 22)
                if sgn:
                    off -= 1 << 25
                target = addr + 4 + off
                if target in stops:                                      # a call to a stop: seen with its arguments
                    at[0] = target
                    if stops[target]:
                        stops[target](mu)
                    mu.emu_stop()
                elif target < CAVE_ADDR and target not in follow and target not in skip:   # a call into stock: skipped
                    mu.reg_write(A.UC_ARM_REG_PC, (addr + 4) | 1)

    mu.hook_add(UC_HOOK_CODE, code)
    mu.hook_add(UC_HOOK_MEM_WRITE, lambda mu, acc, addr, size, value, _: writes.append((addr, size, value)),
                begin=0x20000000, end=0x20035FFF)
    mu.emu_start(entry | 1, usbemu.RET_MAGIC, count=limit * 2)
    return mu, at[0] if at[0] is not None else mu.reg_read(A.UC_ARM_REG_PC), writes


REGS = ("r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "r12", "sp")


def regs_of(mu, names=REGS):
    from unicorn import arm_const as A
    return tuple(mu.reg_read(getattr(A, "UC_ARM_REG_" + n.upper())) for n in names)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True, type=Path)
    ap.add_argument("--image", required=True, type=Path)
    args = ap.parse_args()
    stock = args.stock.read_bytes()
    image = args.image.read_bytes()
    manifest = json.loads(args.image.with_suffix(".json").read_text(encoding="utf-8"))
    sym = {k: int(v, 16) for k, v in manifest["symbols"].items()}
    vid, pid = (int(x, 16) for x in manifest["usb_vid_pid"].split(":"))
    has_scope = "0x0803e49a" in manifest["hooks"]
    has_languages = "0x0800beca" in manifest.get("calls", [])
    menu = manifest.get("menu", [])
    # The waveform under which WAVE stores into the playing table: any, unless the image has a
    # "USB" entry in the menu, in which case only that one (patch 6).
    usb_index = next((m["index"] for m in menu if m["source"] == "usb"), 0)
    PLAYS = usb_index or 1
    print("image: %s, hooks %s" % (args.image.name, ", ".join(manifest["hooks"])))
    ram = usbemu.initial_ram(stock)
    check("patched image leaves stock's initial RAM unchanged", usbemu.initial_ram(image) == ram)

    # ---- control: the unpatched image, as stock's USB Sharing would run it ----------------
    e = make(stock, ram)
    check("stock: disconnected after init", not e.connected())
    e.w8(SYSTEM_MODE, 7)
    e.w32(X_TICK_COUNT, 10000)
    e.tick(50)
    stock_connect(e)
    info = e.enumerate()
    d = info["device"]
    check("stock: enumerates as 2e3c:5720 rev 0200",
          struct.unpack_from("<HHH", d, 8) == (0x2E3C, 0x5720, 0x0200), d.hex())
    check("stock: product string 'AT32 Mass Storage'", info["strings"].get(2) == "AT32 Mass Storage")
    check("stock: mass-storage interface", info["config"][14:17] == bytes([0x08, 0x06, 0x50]))
    check("stock: GET_MAX_LUN = 0", e.control(0xA1, 0xFE, 0, 0, 1) == b"\x00")

    # ---- the patched image -----------------------------------------------------------------
    e = make(image, ram)
    e.audit = True
    e.w8(SYSTEM_MODE, 2)

    e.tick(BOOT_HOLDOFF - 1)
    check("boot hold-off: still disconnected, stock tables", not e.connected()
          and e.r32(UDEV + 4) == STOCK_CLASS and e.r32(UDEV + 8) == STOCK_DESC)
    e.tick(SETTLE - 1)
    check("settle: still disconnected one tick before", not e.connected())
    e.tick(5)
    check("link connects in a measurement mode", e.connected())
    check("core points at the link tables",
          e.r32(UDEV + 4) == sym["link_class_table"] and e.r32(UDEV + 8) == sym["link_desc_table"])
    check("state block initialised", e.r32(LINK_RAM) == LINK_MAGIC and e.r32(LINK_RAM + 8) == 1)

    info = e.enumerate()
    d = info["device"]
    check("link: device descriptor vid:pid", struct.unpack_from("<HH", d, 8) == (vid, pid), d.hex())
    check("link: one configuration, class per interface", d[4] == 0 and d[17] == 1 and d[7] == 64)
    c = info["config"]
    check("link: configuration is 41 bytes, HID interface, 2 endpoints",
          len(c) == 41 and c[9 + 4] == 2 and c[9 + 5] == 3, c.hex())
    check("link: interrupt IN 0x81 / OUT 0x01, 64 bytes, 1 ms",
          c[27:34] == bytes([7, 5, 0x81, 3, 64, 0, 1]) and c[34:41] == bytes([7, 5, 0x01, 3, 64, 0, 1]))
    check("link: strings", info["strings"].get(1) == "FNIRSI" and info["strings"].get(2) == "2C53T stock-link",
          repr(info["strings"]))
    check("link: serial string comes from stock's function", 3 in info["strings"])
    check("link: configured", e.r8(UDEV + 0x224) == 3)

    # What a HID host does next.
    e.control(0x21, 0x0A, 0, 0, 0)                                   # SET_IDLE
    rd = e.control(0x81, 6, 0x2200, 0, 255)
    check("link: report descriptor", rd == REPORT_DESC, rd.hex())
    check("link: report descriptor length matches HID descriptor", c[25] == len(rd))
    check("link: HID descriptor via GET_DESCRIPTOR", e.control(0x81, 6, 0x2100, 0, 9) == c[18:27])
    check("link: GET_PROTOCOL = report", e.control(0xA1, 0x03, 0, 0, 1) == b"\x01")
    try:
        e.control(0xA1, 0x01, 0x0100, 0, 64)                         # GET_REPORT: not offered
        check("link: GET_REPORT stalls", False)
    except Stall:
        check("link: GET_REPORT stalls", True)
    info2 = e.enumerate()                                            # recovers from the stall
    check("link: re-enumerates after a stalled request", info2["device"] == d)

    # INFO
    rs = request(e, 0x01, seq=0x5A)
    check("INFO: header", rs[0] == 0x81 and rs[1] == 0x5A and rs[2] == 0 and rs[3] == 44, rs[:8].hex())
    check("INFO: tag 2C53LINK, proto 1, patch 6", rs[8:16] == b"2C53LINK" and rs[16] == 1 and rs[17] == 6)
    check("INFO: reports system mode, not subscribed", rs[18] == 2 and rs[19] == 0)
    check("INFO: reports tick count", struct.unpack_from("<I", rs, 20)[0] == e.r32(X_TICK_COUNT))

    # READ
    a = 0x0802A990
    check("READ: flash matches the image", read_mem(e, a, 200) == image[a - 0x08007000:a - 0x08007000 + 200])
    check("READ: the hook itself reads back as patched", read_mem(e, 0x0802A994, 6) != stock[0x23994:0x2399A])
    page = read_mem(e, 0x08006000, 4096)
    want = bytes((i * 7 + 3) & 0xFF for i in range(0x1000))
    check("READ: whole 4 KB settings page, byte exact", page == want, "crc %08X" % zlib.crc32(page))
    pattern = bytes((i * 5 + 1) & 0xFF for i in range(242))
    e.audit = False
    e.mu.mem_write(0x20000358, pattern)
    e.audit = True
    check("READ: SRAM", read_mem(e, 0x20000358, 242) == pattern)
    check("READ: last bytes of flash and SRAM",
          len(read_mem(e, 0x08100000 - 56, 56)) == 56 and len(read_mem(e, 0x20038000 - 56, 56)) == 56)
    for name, ln, ad, st in (("peripheral address", 4, 0x40005C40, 2), ("below flash", 4, 0x07FFFFFE, 2),
                             ("past end of flash", 8, 0x080FFFFC, 2), ("past end of SRAM", 2, 0x20037FFF, 2),
                             ("address wrap", 56, 0xFFFFFFF0, 2), ("system memory", 4, 0x1FFFF7E8, 2),
                             ("length 0", 0, 0x20000000, 3), ("length 57", 57, 0x20000000, 3)):
        rs = request(e, 0x02, 1, ln, ad)
        check("READ refused: " + name, rs[2] == st and rs[3] == 0 and rs[8:] == bytes(56), rs[:12].hex())
    rs = request(e, 0x7F, 9)
    check("unknown command answered with an error", rs[0] == 0xFF and rs[1] == 9 and rs[2] == 1)

    # strict request/response pairing
    e.host_out(1, bytes([1, 1]) + bytes(62))
    try:
        e.host_out(1, bytes([1, 2]) + bytes(62))
        check("second request is NAKed until the response is read", False)
    except Nak:
        check("second request is NAKed until the response is read", True)
    check("...and the pending response is intact", e.host_in(1)[1] == 1)
    check("...and the channel carries on", request(e, 0x01, 3)[1] == 3)
    e.host_out(1, bytes([1, 4]))                                     # short report
    check("short report answered with a length error", e.host_in(1)[2] == 3)

    # events with the link class installed
    for _ in range(20):
        e.sof()
    e.istr_flags |= 0x0800
    e.irq()                                                          # suspend
    e.istr_flags |= 0x1000
    e.irq()                                                          # wake-up
    check("SOF / suspend / wake-up handled", e.r8(UDEV + 0x224) == 3 and request(e, 0x01, 7)[1] == 7)

    # bus reset with a response pending
    e.host_out(1, bytes([1, 8]) + bytes(62))
    info3 = e.enumerate()
    check("bus reset with a response pending: clean re-enumeration",
          info3["device"] == d and request(e, 0x01, 0x11)[1] == 0x11)

    # ---- WAVE (patch 4): the generator's table, and no other byte ----------------------------
    GEN, TABLE, POINTS = 0x20000F50, 0x20000F5A, 100
    SENTINEL = 0x0ABC

    def table():
        return list(struct.unpack("<%dH" % POINTS, e.mu.mem_read(TABLE, 2 * POINTS)))

    def set_generator(waveform, amplitude, fill=True):
        e.audit = False
        e.mu.mem_write(GEN, bytes([1, waveform, 0, 0]) + struct.pack("<I", 1000) + bytes([50, amplitude]))
        if fill:
            e.mu.mem_write(TABLE, struct.pack("<%dH" % POINTS, *([SENTINEL] * POINTS)))
        e.audit = True

    def wave(first, samples, seq=0x70, count=None):
        return request(e, 0x04, seq, len(samples) if count is None else count, first,
                       data=struct.pack("<%dH" % len(samples), *samples))

    set_generator(PLAYS, 30)
    around = (bytes(e.mu.mem_read(GEN - 64, 64 + 10)), bytes(e.mu.mem_read(TABLE + 2 * POINTS, 64)))
    n_writes = len(e.writes)
    part = [0, 1, 33, 2047, 4094, 4095, 0xFFFF] + [100 * k for k in range(21)]
    rs = wave(0, part, seq=0x71)
    check("WAVE: answer header, the amplitude used and stock's waveform",
          rs[0] == 0x84 and rs[1] == 0x71 and rs[2] == 0 and rs[3] == 2 and rs[8] == 30 and rs[9] == PLAYS
          and struct.unpack_from("<I", rs, 4)[0] == 0, rs[:12].hex())
    check("WAVE: 28 samples stored with stock's scaling, values above 4094 held at 4094",
          table()[:28] == fnlink.wave_stored(part, 30) and table()[4:7] == [3741, 3741, 3741] and table()[0] == 20,
          str(table()[:8]))
    check("WAVE: the rest of the table is untouched", table()[28:] == [SENTINEL] * 72)
    ramp = [k * 4094 // 99 for k in range(POINTS)]
    answers = [wave(first, ramp[first:first + 28], seq=0x72 + first // 28) for first in range(0, POINTS, 28)]
    check("WAVE: the whole table in four requests", all(r[2] == 0 and r[3] == 2 for r in answers)
          and table() == fnlink.wave_stored(ramp, 30) and min(table()) == 20 and max(table()) == 3741)
    check("WAVE: the last sample is the last two bytes of the table", wave(99, [4094], seq=0x76)[2] == 0
          and e.r16(TABLE + 198) == 3741)
    done = table()
    for name, first, samples, count, st in (
            ("position 100", 100, [1], None, 2), ("two samples from position 99", 99, [1, 2], None, 2),
            ("28 samples from position 73", 73, [7] * 28, None, 2), ("position 0xFFFFFFFF", 0xFFFFFFFF, [1], None, 2),
            ("an address instead of a position", TABLE, [1], None, 2), ("position 0x80000000", 0x80000000, [1] * 28, None, 2),
            ("no samples", 0, [], None, 3), ("29 samples", 0, [1] * 28, 29, 3), ("255 samples", 0, [1] * 28, 255, 3)):
        rs = wave(first, samples, seq=0x77, count=count)
        check("WAVE refused: " + name, rs[0] == 0x84 and rs[2] == st and rs[3] == 0 and rs[8:] == bytes(56), rs[:12].hex())
    e.host_out(1, bytes([4, 0x78, 5, 0]) + struct.pack("<I", 0) + struct.pack("<H", 4094))
    check("WAVE refused: a report shorter than the samples it announces", e.host_in(1)[2] == 3)
    check("WAVE: nothing was stored by any refused request", table() == done)
    check("WAVE: the generator's settings and the bytes after the table are as they were",
          around == (bytes(e.mu.mem_read(GEN - 64, 64 + 10)), bytes(e.mu.mem_read(TABLE + 2 * POINTS, 64))))
    stray = ["%08X (pc %08X)" % (a, pc) for a, size, _, pc in e.writes[n_writes:]
             if pc >= CAVE_ADDR and 0x20000000 <= a < 0x20035000 and not (TABLE <= a and a + size <= TABLE + 2 * POINTS)
             and a not in (UDEV + 4, UDEV + 8, UDEV + 0x17, UDEV + 0x117, UDEV + 0x26, UDEV + 0x28, UDEV + 0x220,
                           UDEV + 0x13A, UDEV + 0x13C, UDEV + 0x140)]
    stored = [a for a, size, _, pc in e.writes[n_writes:] if pc >= CAVE_ADDR and TABLE <= a < TABLE + 2 * POINTS]
    check("WAVE: every store into stock's SRAM is 16 bits inside the table (%d stores)" % len(stored),
          not stray and len(stored) == 28 + POINTS + 1 and all(a % 2 == 0 for a in stored), ", ".join(stray[:6]))
    for ampl, used in ((10, 10), (1, 1), (0, 0), (31, 30), (200, 30)):
        set_generator(PLAYS, ampl)
        rs = wave(0, ramp[:28], seq=0x79)
        check("WAVE: amplitude %d on the instrument -> scaled by %d" % (ampl, used),
              rs[8] == used and rs[9] == PLAYS and table()[:28] == fnlink.wave_stored(ramp[:28], used)
              and max(table()[:28]) <= 20 + 4094 * 30 // 33)
    if usb_index:
        set_generator(2, 30)
        answers = [wave(first, ramp[first:first + 28]) for first in range(0, POINTS, 28)]
        check("WAVE: with a USB entry in the menu and another waveform selected, the playing table is left alone",
              all(r[2] == 0 and r[3] == 2 and r[9] == 2 for r in answers) and table() == [SENTINEL] * POINTS)
    set_generator(PLAYS, 30, fill=False)
    for first in range(0, POINTS, 28):
        wave(first, ramp[first:first + 28])
    check("WAVE: ...and written at once while the entry that plays it is selected", table() == fnlink.wave_stored(ramp, 30))
    set_generator(1, 30, fill=False)
    e.audit = False
    try:
        e.call(0x08029E14)                              # stock's own table builder, as after a key
        rebuilt = table()
    except Exception as ex:
        rebuilt = str(ex)
    e.audit = True
    check("WAVE: stock's builder rewrites the table when a setting changes (square: 20 and 3742)",
          isinstance(rebuilt, list) and set(rebuilt) == {20, 3742} and rebuilt != fnlink.wave_stored(ramp, 30), str(rebuilt)[:80])

    # ---- multimeter stream: stock's real USART2 handler, fed real frames ----------------------
    f_5v = bytes.fromhex("5AA5C6F7EBEB0F000200014E")          # 5.008 V
    f_mains = bytes.fromhex("5AA5A5ADED9F0F0002000031")       # 228.3 V
    f_low = bytes.fromhex("5AA54ECE8FEA0F008200017F")         # 1.4978 V
    e.audit = False
    e.mu.mem_write(METER_TX_BUF, bytes.fromhex("AA55050C000000000011"))   # last command: DC volts
    e.audit = True
    feed_meter(e, f_5v)
    check("meter: nothing is sent before the host subscribes", drain(e) == [])
    check("meter: frames are counted even when not subscribed", struct.unpack_from("<I", request(e, 1), 32)[0] == 1)
    rs = request(e, 0x03, 7, STREAM_METER)
    check("STREAM: subscribe to the meter", rs[0] == 0x83 and rs[2] == 0 and rs[8] == STREAM_METER)
    check("STREAM: unknown bits refused", request(e, 0x03, 8, 0x80)[2] == 3)
    feed_meter(e, f_5v)
    ev = drain(e)
    check("meter: one event per frame", len(ev) == 1 and ev[0][0] == 0x90 and ev[0][2] == 1, str([x[:4].hex() for x in ev]))
    check("meter: the 12 bytes are the frame", ev and ev[0][8:20] == f_5v)
    check("meter: function word and mode ride along", ev and ev[0][20] == 0x0C and ev[0][21] == 2)
    check("meter: stock's meter state rides along", ev and ev[0][24:64] == bytes(e.mu.mem_read(0x20001020, 40)))
    feed_meter(e, f_mains, sof_every=2)                       # the poll misses every other index
    ev = drain(e)
    check("meter: frame caught when the poll skips indices", len(ev) == 1 and ev[0][8:20] == f_mains)
    seq0 = ev[0][1] if ev else 0
    for b in f_low[:11]:
        e.uart_byte(b)
        e.sof()
        e.sof()                                               # polls faster than bytes arrive
    check("meter: no event until the last byte", drain(e) == [])
    e.uart_byte(f_low[11])
    ev = drain(e)
    check("meter: event right after the last byte", len(ev) == 1 and ev[0][8:20] == f_low and ev[0][1] == (seq0 + 1) & 0xFF)
    echo = bytes.fromhex("AA55050C000000AA0011")              # the chip's acknowledgement
    feed_meter(e, echo)
    ev = drain(e)
    check("meter: a 10-byte echo is reported as such", len(ev) == 1 and ev[0][2] == 2 and ev[0][8:18] == echo)
    feed_meter(e, bytes.fromhex("00FF5A00AA00"))              # noise and broken headers
    check("meter: noise produces no event", drain(e) == [])
    feed_meter(e, f_5v)
    ev = drain(e)
    check("meter: recovers after noise", len(ev) == 1 and ev[0][8:20] == f_5v)
    for fr in (f_5v, f_mains, f_low):                         # host not reading: keep the newest
        for b in fr:
            e.uart_byte(b)
            e.istr_flags |= 0x0200
            e.irq()
    ev = drain(e)
    check("meter: an unread event is replaced by the newest frame", [x[8:20] for x in ev][-1:] == [f_low] and len(ev) <= 2)
    feed_meter(e, f_mains)
    ahead = []
    rs = request(e, 0x02, 0x33, 16, 0x08007000, events=ahead)
    check("meter: a request is answered while streaming", rs[0] == 0x82 and rs[1] == 0x33 and rs[8:24] == image[:16])
    check("meter: and the event is delivered too, ahead of the response",
          [x[8:20] for x in ahead + drain(e)] == [f_mains])
    request(e, 0x03, 9, 0)
    feed_meter(e, f_5v)
    check("meter: silent again after unsubscribing", drain(e) == [])

    # ---- status stream: blocks of stock's SRAM that are in neither stream -------------------
    GENERATOR, MEASURED, RUN_FLAG, METER_EXT = 0x20000F50, 0x20000208, 0x20000126, 0x20001048
    gen_block = bytes.fromhex("0102000050c300003214")          # entry 2, 50 kHz, 50 %, 2.0 V
    meas_ch1 = struct.pack("<6I6i", 50176, 19929, 5050, 4950, 10065, 9864, 210000, -10000, 220000, 218000, 118000, 100000)
    meas_ch2 = struct.pack("<12I", 76, 13157894, *([0] * 10))
    meter_ext = bytes.fromhex("662616c4040402010100010100000000")
    e.audit = False
    e.mu.mem_write(GENERATOR, gen_block)
    e.mu.mem_write(MEASURED, meas_ch1 + meas_ch2)
    e.mu.mem_write(METER_EXT, meter_ext)
    e.w8(RUN_FLAG, 1)
    e.audit = True

    def status_events(ms):
        """Run the bus for `ms` milliseconds with the tick counter moving; the status events seen."""
        out = []
        for _ in range(ms):
            e.audit = False
            e.w32(X_TICK_COUNT, e.r32(X_TICK_COUNT) + 1)
            e.audit = True
            rs = poll_in(e)
            if rs is not None and rs[0] == 0xB0:
                out.append(rs)
        return out

    check("INFO: 44 bytes, feature bits 0 and 1 say the status stream and WAVE exist, bit 2 only with a menu",
          (lambda r: r[3] == 44 and r[44] == (7 if menu else 3) and r[45] == len(menu) and r[16] == 1 and r[17] == 6)(request(e, 1, 0x60)))
    check("status: nothing is sent before the host subscribes", status_events(350) == [])
    rs = request(e, 0x03, 0x61, STREAM_STATUS)
    check("STREAM: subscribe to the status blocks", rs[2] == 0 and rs[8] == STREAM_STATUS)
    check("STREAM: all three streams accepted, a fourth bit refused", request(e, 0x03, 0x62, 7)[8] == 7 and request(e, 0x03, 0x63, 8)[2] == 3)
    request(e, 0x03, 0x64, STREAM_STATUS)
    ev = status_events(650)                                    # system mode 2: generator, CH1, CH2 in turn
    kinds = [x[2] for x in ev]
    check("status, scope mode: one block every 100 ms, generator / CH1 / CH2 in turn",
          6 <= len(ev) <= 7 and kinds[:6] in ([1, 2, 3, 1, 2, 3], [2, 3, 1, 2, 3, 1], [3, 1, 2, 3, 1, 2]), str(kinds))
    by_kind = {x[2]: x for x in ev}
    check("status: the generator block is stock's ten bytes", by_kind[1][3] == 10 and by_kind[1][8:18] == gen_block and by_kind[1][18:] == bytes(46))
    check("status: CH1's measurements with the run flag after them", by_kind[2][3] == 49 and by_kind[2][8:56] == meas_ch1 and by_kind[2][56] == 1)
    check("status: CH2's measurements with the run flag after them", by_kind[3][3] == 49 and by_kind[3][8:56] == meas_ch2 and by_kind[3][56] == 1)
    check("status: sequence numbers count up, ticks ride along",
          [x[1] for x in ev] == [(ev[0][1] + i) & 0xFF for i in range(len(ev))]
          and all(0 < struct.unpack_from("<I", b, 4)[0] - struct.unpack_from("<I", a, 4)[0] <= 110 for a, b in zip(ev, ev[1:])))
    e.audit = False
    e.w8(RUN_FLAG, 0)                                          # the user presses STOP
    e.audit = True
    ev = status_events(350)
    check("status: a stopped scope is reported as stopped", [x[56] for x in ev if x[2] in (2, 3)] and all(x[56] == 0 for x in ev if x[2] in (2, 3)))
    e.audit = False
    e.w8(SYSTEM_MODE, 3)                                       # (the mode byte alone: the link stays up)
    e.audit = True
    ev = status_events(350)
    check("status, generator mode: only the generator block", len(ev) >= 3 and {x[2] for x in ev} == {1}, str([x[2] for x in ev]))
    e.audit = False
    e.w8(SYSTEM_MODE, 1)
    e.audit = True
    ev = status_events(350)
    check("status, meter mode: only the rest of the meter's state", len(ev) >= 3 and {x[2] for x in ev} == {4}
          and ev[0][3] == 16 and ev[0][8:24] == meter_ext, str([x[2] for x in ev]))
    e.audit = False
    e.w8(SYSTEM_MODE, 2)
    e.audit = True
    request(e, 0x03, 0x65, STREAM_METER | STREAM_STATUS)       # both small streams together
    seen = []
    for i, b in enumerate(f_5v * 3):
        e.uart_byte(b)
        e.audit = False
        e.w32(X_TICK_COUNT, e.r32(X_TICK_COUNT) + 10)
        e.audit = True
        rs = poll_in(e)
        if rs is not None:
            seen.append(rs[0])
    seen += [x[0] for x in drain(e)]
    check("status and meter together: three meter events and status blocks between them",
          seen.count(0x90) == 3 and seen.count(0xB0) >= 3, str([hex(x) for x in seen]))
    ahead = []
    rs = request(e, 0x02, 0x66, 10, GENERATOR, events=ahead)
    check("status: a READ is still answered while it streams", rs[1] == 0x66 and rs[8:18] == gen_block)
    e.audit = False
    e.w32(X_TICK_COUNT, 0xFFFFFFC0)                            # the tick counter about to wrap (49.7 days)
    e.audit = True
    request(e, 0x03, 0x67, STREAM_STATUS)
    ev = status_events(400)
    check("status: keeps its pace across the tick counter's wrap", 3 <= len(ev) <= 5, str(len(ev)))
    request(e, 0x03, 0x68, 0)
    check("status: silent again after unsubscribing", status_events(350) == [])
    e.audit = False
    e.w32(X_TICK_COUNT, 20000)
    e.audit = True

    # ---- oscilloscope stream: stock's real fpga task, fed by a model of the FPGA ---------------
    def run_task(emu, ops, key):
        emu.spi_count = 0
        emu.spi_miso = lambda n: (n * key + 3) & 0xFF
        return emu.run_fpga_task(ops, TASK_STACK_TOP)

    ops = [1, 2, 4, 5, 3, 6, 7, 8, 9, 4, 5]
    ref = make(stock, ram)
    ref_view = task_view(ref, run_task(ref, ops, 7))
    check("scope model: stock reads 1024 samples per channel",
          bytes(ref.mu.mem_read(CH1_BUF, 4)) != bytes(4) and bytes(ref.mu.mem_read(CH2_BUF, 4)) != bytes(4))
    quiet = make(image, ram)
    check("fpga task: patched image behaves exactly like stock (link idle)",
          task_view(quiet, run_task(quiet, ops, 7)) == ref_view)
    check("fpga task: nothing captured while not subscribed", quiet.r32(LINK_RAM) != LINK_MAGIC)

    if has_scope:
        e.audit = False
        e.mu.mem_write(STATE_BASE, bytes((i * 3 + 1) & 0xFF for i in range(56)))
        e.audit = True
        run_task(e, [4, 5], 5)
        check("scope: nothing captured before the host subscribes", struct.unpack_from("<I", request(e, 1), 36)[0] == 0)
        request(e, 0x03, 1, STREAM_SCOPE)
        before = bytes(e.mu.mem_read(0x20000000, 0x35000))
        snaps = run_task(e, [1, 4, 5, 8], 11)
        after = bytes(e.mu.mem_read(0x20000000, 0x35000))
        ref2 = make(stock, ram)
        ref2.mu.mem_write(0x20000000, before)
        snaps_ref = run_task(ref2, [1, 4, 5, 8], 11)
        check("fpga task: registers identical to stock while capturing", snaps == snaps_ref)
        check("fpga task: stock's SRAM identical to stock while capturing",
              after == bytes(ref2.mu.mem_read(0x20000000, 0x35000)))
        ch1, ch2 = bytes(e.mu.mem_read(CH1_BUF, 1024)), bytes(e.mu.mem_read(CH2_BUF, 1024))
        state56 = bytes(e.mu.mem_read(STATE_BASE, 56))
        asm = fnlink.ScopeAssembler()
        reports = drain(e)
        recs = [r for r in (asm.feed(x) for x in reports) if r]
        check("scope: two records, 20 reports each", len(reports) == 40 and len(recs) == 2, "%d reports" % len(reports))
        by_ch = {r["channel"]: r for r in recs}
        check("scope: CH1 record is stock's buffer, byte exact", 0 in by_ch and by_ch[0]["data"] == ch1)
        check("scope: CH2 record is stock's buffer, byte exact", 1 in by_ch and by_ch[1]["data"] == ch2)
        check("scope: each record carries the scope state", all(r["state"] == state56 for r in recs))
        check("scope: a record is sent once", drain(e) == [])

        run_task(e, [4], 13)
        old = bytes(e.mu.mem_read(CH1_BUF, 1024))
        first = [poll_in(e) for _ in range(5)]                    # header + 4 chunks on their way
        run_task(e, [4], 17)                                      # a newer acquisition lands meanwhile
        newer = bytes(e.mu.mem_read(CH1_BUF, 1024))
        rest = drain(e)
        recs = [r for r in (asm.feed(x) for x in first + rest) if r]
        check("scope: a record being sent is never overwritten", len(recs) == 1 and recs[0]["data"] == old and old != newer)
        check("scope: the skipped acquisition is counted", struct.unpack_from("<I", request(e, 1), 40)[0] == 1)
        run_task(e, [4], 19)
        recs = [r for r in (asm.feed(x) for x in drain(e)) if r]
        check("scope: the next acquisition flows again", len(recs) == 1 and recs[0]["data"] == bytes(e.mu.mem_read(CH1_BUF, 1024)))

        request(e, 0x03, 2, STREAM_METER | STREAM_SCOPE)
        run_task(e, [4, 5], 23)
        sent = 0
        got_meter = []
        pending_bytes = list(f_5v + f_mains)
        while True:
            if pending_bytes:
                e.uart_byte(pending_bytes.pop(0))
            rs = poll_in(e)
            if rs is None and not pending_bytes:
                break
            if rs is not None and rs[0] == 0x90:
                got_meter.append(rs[8:20])
            elif rs is not None:
                sent += 1
        check("both streams together: meter frames are interleaved, nothing lost",
              got_meter == [f_5v, f_mains] and sent == 40, "%d scope reports, %d meter" % (sent, len(got_meter)))
        run_task(e, [4], 29)
        [poll_in(e) for _ in range(3)]
        info4 = e.enumerate()                                     # host resets mid-record
        check("re-enumeration mid-record: session starts unsubscribed", request(e, 1)[19] == 0 and drain(e) == [])
        request(e, 0x03, 3, STREAM_SCOPE)
        run_task(e, [5], 31)
        recs = [r for r in (asm.feed(x) for x in drain(e)) if r]
        check("...and streams again after subscribing", len(recs) == 1 and recs[0]["channel"] == 1)

    else:
        request(e, 0x03, 1, STREAM_SCOPE)
        run_task(e, [4, 5], 11)
        check("no scope hook: subscribing captures nothing", drain(e) == [] and struct.unpack_from("<I", request(e, 1), 36)[0] == 0)

    # stock drops the bus (mode change, or before it saves settings) while we are linked
    check("subscription is active before the mode change", e.r8(LINK_RAM + 32) == STREAM_SCOPE)

    e.w8(SYSTEM_MODE, 1)
    stock_disconnect(e)
    e.tick(SETTLE - 1)
    check("after stock disconnects: stays off the bus for the whole settle time", not e.connected())
    check("after stock disconnects: subscription dropped, no more copying", e.r8(LINK_RAM + 32) == 0)
    e.tick(5)
    check("...then reconnects", e.connected() and e.r32(LINK_RAM + 8) == 2)
    e.enumerate()
    check("...and answers", request(e, 0x01, 0x21)[18] == 1)

    # USB Sharing: stock sets mode 7, draws the screen, then connects. A request that arrived
    # but was not yet serviced must not reach stock's mass-storage class.
    e.host_out(1, bytes([1, 0x22]) + bytes(62), run_irq=False)
    check("a transfer is pending when the mode changes", e._istr() & 0x8000)
    e.w8(SYSTEM_MODE, 7)
    e.tick(1)
    check("mode 7: bus released within one tick", not e.connected())
    check("mode 7: pending transfer discarded", e._istr() == 0, "ISTR %04X" % e._istr())
    e.irq()                                                          # the latched interrupt
    check("mode 7: stock tables restored", e.r32(UDEV + 4) == STOCK_CLASS and e.r32(UDEV + 8) == STOCK_DESC)
    e.tick(3 * SETTLE)
    check("mode 7: the link never connects on its own", not e.connected())
    stock_connect(e)
    e.tick(3 * SETTLE)
    check("mode 7: stock's connection is left alone", e.connected() and e.r32(UDEV + 4) == STOCK_CLASS)
    ms = e.enumerate()
    check("mode 7: enumerates as stock mass storage",
          struct.unpack_from("<HH", ms["device"], 8) == (0x2E3C, 0x5720) and ms["strings"].get(2) == "AT32 Mass Storage")
    check("mode 7: GET_MAX_LUN = 0", e.control(0xA1, 0xFE, 0, 0, 1) == b"\x00")

    # leaving USB Sharing for the scope: stock disconnects on the way
    e.w8(SYSTEM_MODE, 2)
    stock_disconnect(e)
    e.tick(SETTLE + 5)
    check("back to a measurement mode: link returns", e.connected() and e.r32(UDEV + 4) == sym["link_class_table"])
    e.enumerate()
    check("...and answers", request(e, 0x01, 0x31)[18] == 2)

    # other modes (settings and unknown ones): hands off
    for mode in (0, 4, 5, 6, 8, 9, 0xFF):
        e.w8(SYSTEM_MODE, 2)
        stock_disconnect(e)
        e.tick(SETTLE + 5)
        e.w8(SYSTEM_MODE, mode)
        e.tick(1)
        ok = not e.connected() and e.r32(UDEV + 4) == STOCK_CLASS
        e.tick(2 * SETTLE)
        check("mode %d: released and left alone" % mode, ok and not e.connected())

    # stock using the bus in a measurement mode (not expected): never touched
    e.w8(SYSTEM_MODE, 3)
    stock_connect(e)
    e.tick(3 * SETTLE)
    check("bus already in use in a measurement mode: tables untouched", e.r32(UDEV + 4) == STOCK_CLASS)

    # stale state block after a warm reset
    e2 = make(image, ram)
    e2.mu.mem_write(LINK_RAM, bytes(e.mu.mem_read(LINK_RAM, 0x100)))
    e2.mu.mem_write(LINK_RAM + 4, struct.pack("<H", 999))            # settle nearly expired
    e2.w8(SYSTEM_MODE, 1)
    e2.tick(BOOT_HOLDOFF - 1)
    check("warm reset with stale state: boot hold-off still applies", not e2.connected())
    e2.tick(SETTLE + 5)
    e2.enumerate()
    check("...then links normally", request(e2, 0x01, 0x41)[1] == 0x41)
    e3 = make(image, ram)
    e3.mu.mem_write(LINK_RAM, bytes((i * 31 + 7) & 0xFF for i in range(0x100)))   # garbage
    e3.w8(SYSTEM_MODE, 2)
    e3.tick(BOOT_HOLDOFF + SETTLE + 5)
    e3.enumerate()
    check("garbage in the state block at power-on: reinitialised", request(e3, 0x01, 0x51)[1] == 0x51)

    # ---- the host tool, driven through the emulated device ----------------------------------
    import tempfile

    class EmuTransport:
        """fnlink transport backed by the emulated device. `script` is called once per
        millisecond of bus time and may feed the instrument (meter bytes, acquisitions)."""

        def __init__(self, emu, script=None):
            self.emu = emu
            self.script = script

        def write(self, report):
            self.emu.host_out(1, report)

        def read(self, timeout_ms):
            for _ in range(timeout_ms):
                if self.script:
                    self.script(self.emu)
                rs = poll_in(self.emu)
                if rs is not None:
                    return rs
            return None

    e.w8(SYSTEM_MODE, 2)
    stock_disconnect(e)
    e.tick(SETTLE + 5)
    e.enumerate()
    tr = EmuTransport(e)
    with tempfile.TemporaryDirectory() as tmp:
        cal = Path(tmp) / "cal.bin"
        rc = fnlink.main(["dump-cal", str(cal)], transport=tr)
        check("fnlink dump-cal: saves the exact page", rc == 0 and cal.read_bytes() == want)
        check("fnlink dump-cal: refuses to overwrite a backup", fnlink.main(["dump-cal", str(cal)], transport=tr) == 2)
        check("fnlink info", fnlink.main(["info"], transport=tr) == 0)
        e.audit = False
        generator_before = bytes(e.mu.mem_read(0x20000F50, 10))
        e.mu.mem_write(0x20000F50, bytes([1, PLAYS, 0, 0]) + struct.pack("<I", 2000) + bytes([50, 25]))
        e.audit = True
        tab = lambda: list(struct.unpack("<100H", e.mu.mem_read(0x20000F5A, 200)))
        check("fnlink wave: a built-in shape is stored and read back",
              fnlink.main(["wave", "sinc"], transport=tr) == 0 and tab() == fnlink.wave_stored(fnlink.wave_shape("sinc"), 25))
        check("fnlink wave: --invert", fnlink.main(["wave", "ramp", "--invert"], transport=tr) == 0
              and tab() == fnlink.wave_stored(fnlink.wave_shape("ramp-down"), 25))
        pts = Path(tmp) / "points.csv"
        pts.write_text("# one period\nvolts\n0\n1\n0\n-1\n")
        four = fnlink.wave_normalise(fnlink.wave_resample([0, 1, 0, -1]))
        check("fnlink wave: a file with four points, resampled and stretched",
              fnlink.main(["wave", "--file", str(pts)], transport=tr) == 0 and tab() == fnlink.wave_stored(four, 25)
              and four[0] == 2047 and four[25] == 4094 and four[75] == 0)
        shown = Path(tmp) / "table.txt"
        check("fnlink wave --show: reads the table and writes nothing",
              fnlink.main(["wave", "--show", "--out", str(shown)], transport=tr) == 0
              and [int(x) for x in shown.read_text().split()] == tab() == fnlink.wave_stored(four, 25))
        check("fnlink wave: a shape and a file together, or neither, is refused before anything is sent",
              fnlink.main(["wave"], transport=tr) == 2 and fnlink.main(["wave", "sine", "--file", str(pts)], transport=tr) == 2
              and tab() == fnlink.wave_stored(four, 25))
        e.audit = False
        e.mu.mem_write(0x20000F50, generator_before)
        e.audit = True
        check("fnlink read: refused range reported", fnlink.main(["read", "0x40005C40", "4"], transport=tr) == 2)
        check("fnlink verify: flash equals the image", fnlink.main(["verify", str(args.image)], transport=tr) == 0)
        other = Path(tmp) / "stock.bin"
        other.write_bytes(stock)
        check("fnlink verify: a different image is reported", fnlink.main(["verify", str(other)], transport=tr) == 1)

        def moving_clock(emu):
            emu.audit = False
            emu.w32(X_TICK_COUNT, emu.r32(X_TICK_COUNT) + 1)
            emu.audit = True

        tr_status = EmuTransport(e, moving_clock)
        check("fnlink status: the four blocks by READ", fnlink.main(["status"], transport=tr_status) == 0)

        # snapshot: labelled blocks into a text file, a pointer followed, the calibration refused
        notes = Path(tmp) / "session.txt"
        pointer_at, target = 0x20030000, 0x20030100
        e.audit = False
        e.w32(pointer_at, target)
        e.w32(pointer_at + 4, 0)
        for k in range(24):
            e.w8(target - 8 + k, 0xA0 + k)
        e.audit = True
        rc = fnlink.main(["snapshot", "step one", "0x200000F8:56", "0x20000EA8:96", "--follow", "0x%X:8:16" % pointer_at,
                          "--follow", "0x%X:8:16" % (pointer_at + 4), "--out", str(notes)], transport=tr)
        text = notes.read_text() if notes.exists() else ""
        rows = dict((x[:8], x.split()[2]) for x in text.splitlines() if len(x) > 9 and x[8] == " " and x[:2] in ("20", "08"))
        check("fnlink snapshot: every block as it is in the instrument", rc == 0 and text.startswith("snapshot  step one")
              and rows.get("200000F8") == bytes(e.r8(0x200000F8 + k) for k in range(56)).hex()
              and rows.get("20000EA8") == bytes(e.r8(0x20000EA8 + k) for k in range(96)).hex(), text[:300])
        check("fnlink snapshot: a pointer followed with the bytes before its target, a null one left alone",
              rows.get("%08X" % (target - 8)) == bytes(0xA0 + k for k in range(24)).hex()
              and "follow    %08X -> %08X" % (pointer_at, target) in text and "-> 00000000, not a RAM address: nothing read" in text, text[-400:])
        check("fnlink snapshot: a second one is added to the same file",
              fnlink.main(["snapshot", "step two", "0x200000F8:56", "--out", str(notes)], transport=tr) == 0
              and notes.read_text().count("snapshot  ") == 2)
        before = notes.read_text()
        check("fnlink snapshot: the calibration is refused, in RAM and in flash, and nothing is written",
              fnlink.main(["snapshot", "no", "0x20000350:64", "--out", str(notes)], transport=tr) == 2
              and fnlink.main(["snapshot", "no", "0x08006000:16", "--out", str(notes)], transport=tr) == 2
              and fnlink.main(["watch", "0x20000400:4", "--reads", "3"], transport=tr) == 2 and notes.read_text() == before)
        check("fnlink snapshot: the display arrays right after the tables are not refused",
              fnlink.main(["snapshot", "arrays", "0x2000044E:602", "--out", str(notes)], transport=tr) == 0)
        seen = Path(tmp) / "watch.txt"
        rc = fnlink.main(["watch", "0x%X:4" % X_TICK_COUNT, "--reads", "40", "--label", "the tick", "--out", str(seen)], transport=tr_status)
        text = seen.read_text() if seen.exists() else ""
        check("fnlink watch: a value that moves is reported change by change", rc == 0 and "the tick" in text
              and text.count(" ms  ") >= 20 and "changes   " in text, text[:300])
        check("fnlink watch: more than one report is refused", fnlink.main(["watch", "0x20000100:57", "--reads", "2"], transport=tr) == 2)
        check("fnlink status --events: the same from the status stream", fnlink.main(["status", "--events"], transport=tr_status) == 0)
        got = fnlink.status_from_events(fnlink.Link(tr_status), 2, 2000)
        check("fnlink status_from_events: generator and both channels in scope mode",
              set(got) == {1, 2, 3} and got[1] == gen_block and got[2][:48] == meas_ch1 and got[2][48] == e.r8(RUN_FLAG), str(sorted(got)))
        check("fnlink: the link is left unsubscribed afterwards", request(e, 0x01, 0x70)[19] == 0)

        wire = list(f_5v + f_mains + f_low)
        tr_meter = EmuTransport(e, lambda emu: emu.uart_byte(wire.pop(0)) if wire else None)
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fnlink.main(["meter", "--count", "3", "--timeout", "50"], transport=tr_meter)
        lines = buf.getvalue().splitlines()
        check("fnlink meter: prints the three readings",
              rc == 0 and [ln.split()[2] for ln in lines] == ["5.008", "228.3", "1.4978"], buf.getvalue())
        check("fnlink meter: names the function", all("DC V" in ln for ln in lines))
        check("fnlink meter: unsubscribes when done", request(e, 1)[19] == 0)

        if has_scope:
            outdir = Path(tmp) / "captures"
            todo = [[4, 5], [4, 5]]

            def acquire(emu):
                # One acquisition of both channels once subscribed, the next when both are saved.
                saved = len(list(outdir.glob("*.bin"))) if outdir.exists() else 0
                if todo and emu.r8(LINK_RAM + 32) & STREAM_SCOPE and saved == 2 * (2 - len(todo)):
                    run_task(emu, todo.pop(0), 37 + len(todo))

            with contextlib.redirect_stdout(io.StringIO()):
                rc = fnlink.main(["scope", str(outdir), "--count", "4", "--timeout", "50"], transport=EmuTransport(e, acquire))
            files = sorted(outdir.glob("*.bin"))
            check("fnlink scope: saves four records of 56 + 1024 bytes",
                  rc == 0 and len(files) == 4 and all(f.stat().st_size == 1080 for f in files))
            check("fnlink scope: last CH2 file is stock's buffer",
                  files and files[-1].read_bytes()[56:] == bytes(e.mu.mem_read(CH2_BUF, 1024)))

    for h, want in (("5AA5C6F7EBEB0F000200014E", "5.008"), ("5AA546DECF4F0E0002000182", "4.994"),
                    ("5AA54ECE8FEA0F008200017F", "1.4978"), ("5AA5A5ADED9F0F0002000031", "228.3"),
                    ("5AA5E42E6325070000000000", "Auto")):
        got = fnlink.decode_meter_frame(bytes.fromhex(h))["text"]
        check("decoder: documented frame reads %s" % want, got == want, got)

    # ---- fpga task, many states: the patched task must match stock step for step ------------
    import random

    def fuzzed(img, seed, subscribe):
        emu = make(img, ram)
        rnd = random.Random(seed)
        # Mostly small values, as real indices and flags are; wild ones crash stock itself.
        emu.mu.mem_write(STATE_BASE, bytes(rnd.randrange(256) if rnd.random() < 0.15 else rnd.randrange(21)
                                           for _ in range(0x60)))
        emu.mu.mem_write(STATE_BASE + 0x352, bytes(rnd.choice((0, 1, 2, 0xFF)) for _ in range(4)))
        emu.mu.mem_write(STATE_BASE + 0xDB0, bytes(rnd.randrange(256) for _ in range(16)))
        if subscribe:                                   # as if a host had subscribed to the scope
            emu.mu.mem_write(LINK_RAM, struct.pack("<I", LINK_MAGIC) + bytes(28) + bytes([STREAM_SCOPE, 0, 0, 0, 0, 0xFF]))
        todo = [rnd.choice((1, 2, 3, 4, 4, 5, 5, 6, 7, 8, 9)) for _ in range(8)]
        try:
            view = task_view(emu, run_task(emu, todo, 2 * seed + 1))
        except Exception as ex:                         # a state stock itself cannot survive
            view = "stopped: %s" % str(ex)[:40]
        return view, emu, todo

    differing, captured, survived = [], 0, 0
    for seed in range(80):
        ref_v, _, todo = fuzzed(stock, seed, False)
        for sub in (False, True):
            v, emu, _ = fuzzed(image, seed, sub)
            if v != ref_v:
                differing.append((seed, sub))
            if sub and not isinstance(v, str):
                captured += emu.r32(LINK_RAM + 20)
        survived += not isinstance(ref_v, str)
    check("fpga task: identical to stock over 80 random scope states (%d ran to the end)" % survived,
          not differing and survived >= 30, str(differing[:5]))
    check("fpga task: records were captured during those runs" if has_scope else "no scope hook: nothing captured",
          (captured > 0) == has_scope, str(captured))

    # ---- languages: the tables and fonts of the stock image itself ----------------------------
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import language_tables
    lines, tables_ok = language_tables.report(language_tables.Image(stock))
    check("language tables: eight valid rows in every table, 70 strings a language",
          tables_ok and sum(" 70 strings" in x and "0 bad pointers" in x for x in lines) == 8, "; ".join(lines[1:]))
    check("language tables: every character has a glyph (Spanish in all six fonts from 15 to 28 px)",
          any(x.startswith("Spanish") and x.endswith("every font has them all") for x in lines), "; ".join(lines))

    # ---- --languages: stock's Settings OK handler with and without the call ------------------
    stops, outcome, regs_differ, writes_differ = set(), {}, [], []
    for cursor in (0, 1):
        for language in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 255):
            ref = language_ok(stock, ram, cursor, language)
            got = language_ok(image, ram, cursor, language)
            stops |= {ref[0], got[0]}
            outcome[cursor, language] = (ref[1], got[1])
            if ref[2] != got[2]:
                regs_differ.append((cursor, language))
            if ref[3] != got[3]:
                writes_differ.append((cursor, language))
    check("language: the handler reaches the store in both images", stops == {0x0800BEE0}, str(sorted(stops)))
    check("language: stock stores the row plus one", all(v[0] == c + 1 for (c, _), v in outcome.items()))
    if has_languages:
        cycle = {1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8, 8: 2, 0: 2, 9: 2, 255: 2}
        check("language: the first row still gives Chinese", all(outcome[0, lang][1] == 1 for lang in cycle))
        check("language: the second row steps English ... Korean and back to English",
              all(outcome[1, lang][1] == nxt for lang, nxt in cycle.items()), str({k: v[1] for k, v in outcome.items() if k[0] == 1}))
        check("language: never a value outside 1..8", all(1 <= v[1] <= 8 for v in outcome.values()))
    else:
        check("no language call in this image: the handler stores what stock stores", all(v[0] == v[1] for v in outcome.values()))
    check("language: r1-r12 and sp after the store are stock's, in all 22 cases", not regs_differ, str(regs_differ[:5]))
    check("language: the same SRAM writes as stock, in all 22 cases", not writes_differ, str(writes_differ[:5]))

    # ---- the generator's menu (patch 5): stock's own code with and without the added entries ----
    BUILDER, LIST, TABLE_AT, GEN_AT = 0x08029E14, 0x0801579C, 0x20000F5A, 0x20000F50
    E_BYTES = len(menu)
    LAST = 12 + E_BYTES
    callee = ("r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "sp")
    seed = {"r4": 0x44444444, "r5": 0x55555555, "r6": 0x66666666, "r7": 0x77777777, "r8": 0x88888888,
            "r9": 0x99999999, "r10": 0xAAAAAAAA, "r11": 0xBBBBBBBB}

    def table_of(mu):
        return list(struct.unpack("<100H", mu.mem_read(TABLE_AT, 200)))

    def build(img, index, duty, ampl, ram_now=ram):
        mu, at, writes = run_code(img, ram_now, BUILDER, (), {0xE58: 1, 0xE59: index, 0xE60: duty, 0xE61: ampl}, seed)
        return table_of(mu), at, regs_of(mu, callee), [w for w in writes if not 0x20035000 <= w[0]]

    same, unbalanced, strays = [], [], []
    for index in list(range(0, 13)) + [13 + E_BYTES, 40, 255]:
        for duty, ampl in ((50, 30), (20, 10), (80, 1), (50, 0)):
            ref, got = build(stock, index, duty, ampl), build(image, index, duty, ampl)
            if ref != got:
                same.append((index, duty, ampl))
            if got[1] != usbemu.RET_MAGIC or got[2] != tuple(seed[r] for r in callee[:-1]) + (0x20036F00,):
                unbalanced.append((index, duty, ampl))
    check("menu: stock's builder gives the same table, registers and writes for every stock waveform and for an index past the list (%d cases)" % (16 * 4),
          not same and not unbalanced, "%s %s" % (same[:4], unbalanced[:4]))
    if menu:
        import build_link_image as builder_module
        entries = builder_module.parse_menu(["%s=%s@%d" % (m["label"], m["source"], m["icon"]) for m in menu if m["source"] != "usb"],
                                            next(("%s@%d" % (m["label"], m["icon"]) for m in menu if m["source"] == "usb"), None))
        sine = list(struct.unpack("<100H", stock[0x0804D848 - 0x08007000:0x0804D848 - 0x08007000 + 200]))
        check("menu: the manifest describes %d added entries, indexes 13 to %d" % (len(menu), LAST),
              [m["index"] for m in menu] == list(range(13, 13 + len(menu))) == [x["index"] for x in entries])
        wrong = []
        for x in entries:
            for ampl in (30, 17, 1, 0, 200):
                want = fnlink.wave_stored(x["samples"] if x["samples"] else sine, ampl)
                tab, at, kept, writes = build(image, x["index"], 50, ampl)
                outside = [w for w in writes if not (TABLE_AT <= w[0] and w[0] + w[1] <= TABLE_AT + 200) and w[0] < 0x20035000]
                if tab != want or at != usbemu.RET_MAGIC or kept != tuple(seed[r] for r in callee[:-1]) + (0x20036F00,) or outside:
                    wrong.append((x["index"], ampl, at, len(outside)))
        check("menu: each added entry builds its own table with stock's scaling, writes only the table and returns with stock's registers",
              not wrong, str(wrong[:4]))
        check("menu: an added entry never stores above stock's own top",
              max(max(build(image, x["index"], 50, 255)[0]) for x in entries) <= 3742)
        garbage = bytearray(ram)
        garbage[LINK_RAM - 0x20000000:LINK_RAM - 0x20000000 + 0xA00] = bytes((i * 37 + 11) & 0xFF for i in range(0xA00))
        usb_entry = next((x for x in entries if x["samples"] is None), None)
        if usb_entry:
            check("menu: at start-up, with the link's SRAM not yet initialised, the USB entry plays stock's sine",
                  build(image, usb_entry["index"], 50, 30, bytes(garbage))[0] == fnlink.wave_stored(sine, 30))

        # the list: stock's own drawing routine, its text and picture calls recorded
        def names(img, language, first, index):
            """The six name pointers stock's list passes to its text routine, in order."""
            got = []
            from unicorn import arm_const as A

            def grab(mu):
                got.append(struct.unpack("<I", mu.mem_read(mu.reg_read(A.UC_ARM_REG_SP), 4))[0])
            # the text routine is not run: its call is observed at the bl and skipped
            mu = run_names(img, language, first, index, grab)
            return got, mu

        def run_names(img, language, first, index, grab):
            from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE
            from unicorn import arm_const as A
            mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            mu.mem_map(0x08000000, 0x100000)
            mu.mem_write(0x08007000, img)
            mu.mem_map(0x20000000, 0x40000)
            mu.mem_write(0x20000000, ram)
            for off, v in ((0xF60, language), (0xF6A, first), (0xE59, index)):
                mu.mem_write(STATE_BASE + off, bytes([v]))
            mu.reg_write(A.UC_ARM_REG_SP, 0x20036F00)
            mu.reg_write(A.UC_ARM_REG_LR, usbemu.RET_MAGIC | 1)
            steps = [0]

            def code(mu, addr, size, _):
                steps[0] += 1
                if steps[0] > 20000:
                    mu.emu_stop()
                elif size == 4 and addr < CAVE_ADDR:
                    h1, h2 = struct.unpack("<HH", mu.mem_read(addr, 4))
                    if (h1 & 0xF800) == 0xF000 and (h2 & 0xD000) == 0xD000:
                        off = ((h1 & 0x3FF) << 12) | ((h2 & 0x7FF) << 1)
                        sgn = (h1 >> 10) & 1
                        off |= (sgn << 24) | (((~(((h2 >> 13) & 1) ^ sgn)) & 1) << 23) | (((~(((h2 >> 11) & 1) ^ sgn)) & 1) << 22)
                        if sgn:
                            off -= 1 << 25
                        target = addr + 4 + off
                        if target < CAVE_ADDR:
                            if target == 0x0800F154:
                                grab(mu)
                            mu.reg_write(A.UC_ARM_REG_PC, (addr + 4) | 1)
            mu.hook_add(UC_HOOK_CODE, code)
            mu.emu_start(LIST | 1, usbemu.RET_MAGIC, count=40000)
            return mu

        def cstring(at):
            raw = image[at - 0x08007000:at - 0x08007000 + 100]
            return raw.split(b"\0", 1)[0].decode("utf-8", "replace")

        CODES = ("zh", "en", "ru", "ja", "es", "pt", "de", "ko")         # --menu-names: a name of its own in a language

        differ, rows_wrong, not_returned = [], [], []
        renamed = {int(t["slot"], 16): int(t["at"], 16) for t in manifest.get("texts", [])}     # --texts, below
        for language in range(1, 9):
            for first in range(0, 8 + E_BYTES):
                ref, mu_ref = names(stock, language, first, first)
                got, mu_got = names(image, language, first, first)
                from unicorn import arm_const as A
                if mu_got.reg_read(A.UC_ARM_REG_PC) != usbemu.RET_MAGIC or len(got) != 6:
                    not_returned.append((language, first))
                    continue
                for k in range(6):
                    row = first + k
                    if row < 13:
                        if got[k] != renamed.get(0x0804C61C + 52 * (language - 1) + 4 * row, ref[k]):
                            differ.append((language, row))
                    elif cstring(got[k]) != menu[row - 13].get("names", {}).get(CODES[language - 1], menu[row - 13]["label"]):
                        rows_wrong.append((language, row, cstring(got[k])))
        check("menu: stock's list returns and asks for six names in every language and at every scroll position", not not_returned, str(not_returned[:4]))
        check("menu: rows 1 to 13 get exactly the name stock gives them (or the one --texts puts in its slot), in all eight languages",
              not differ, str(differ[:4]))
        check("menu: rows 14 to %d get the added names (%s)" % (LAST + 1, ", ".join(m["label"] for m in menu)), not rows_wrong, str(rows_wrong[:4]))

        # the pictures: the three places that turn the index into a file number
        def picture(img, entry, base_reg, index, on):
            seen = []
            from unicorn import arm_const as A
            mu, at, _ = run_code(img, ram, entry, {0x080073B4: lambda mu: seen.append(mu.reg_read(A.UC_ARM_REG_R2))},
                                 {0xE58: on, 0xE59: index}, {base_reg: STATE_BASE}, limit=3000)
            return seen[0] if seen else None

        sites = ((0x080108E6, "r0", lambda i, on: (0x5A if on else 0x67) + i), (0x080127B8, "r4", lambda i, on: 0x17 + i - 13 * on),
                 (0x0801F8E0, "r0", lambda i, on: 0x17 + i - 13 * on))
        pics_same, pics_wrong = [], []
        for entry, reg, number in sites:
            for on in (0, 1):
                for index in range(0, 13):
                    if picture(image, entry, reg, index, on) != picture(stock, entry, reg, index, on) or picture(image, entry, reg, index, on) != number(index, on):
                        pics_same.append((hex(entry), index, on))
                for m in menu:
                    if picture(image, entry, reg, m["index"], on) != number(m["icon"], on):
                        pics_wrong.append((hex(entry), m["index"], on, picture(image, entry, reg, m["index"], on)))
        check("menu: the three picture sites ask for stock's file for every stock waveform", not pics_same, str(pics_same[:4]))
        check("menu: for an added entry they ask for the picture of the waveform it borrows, never a file past stock's thirteen",
              not pics_wrong, str(pics_wrong[:4]))

        # the keys: next / previous waveform, as stock's handlers do it, the builder included
        def key(img, handler, index, first):
            mu, at, _ = run_code(img, ram, handler, (), {0xF68: 3, 0xF69: 0, 0xF6A: first, 0xF6B: 0, 0xE58: 1, 0xE59: index, 0xE60: 50, 0xE61: 30},
                                 limit=60000)
            return mu.mem_read(STATE_BASE + 0xE59, 1)[0], mu.mem_read(STATE_BASE + 0xF6A, 1)[0], table_of(mu), at

        walk, index, first, lost = [], 0, 0, []
        for _ in range(LAST + 4):
            before = index
            index, first, tab, at = key(image, 0x0800A900, index, first)
            walk.append(index)
            if not first <= index <= first + 5 or at != usbemu.RET_MAGIC:
                lost.append((index, first, hex(at)))
            x = next((x for x in entries if x["index"] == index), None)
            # (a key that does not move the cursor does not rebuild the table, in stock either)
            if x and index != before and tab != fnlink.wave_stored(x["samples"] if x["samples"] else sine, 30):
                lost.append(("table", index))
        check("menu: the next-waveform key walks 1 ... %d and stops there, the cursor always among the six rows shown, each added entry built on arrival" % LAST,
              walk == list(range(1, LAST + 1)) + [LAST] * 4 and not lost, "%s %s" % (walk, lost[:4]))
        back = []
        for _ in range(LAST + 3):
            index, first, tab, at = key(image, 0x0800A148, index, first)
            back.append(index)
            if not first <= index <= first + 5 or at != usbemu.RET_MAGIC:
                lost.append((index, first, hex(at)))
        check("menu: the previous-waveform key walks back to 0", back == list(range(LAST - 1, -1, -1)) + [0] * 3 and not lost, "%s %s" % (back, lost[:4]))
        ref_walk, index, first = [], 0, 0
        for _ in range(15):
            index, first, _, _ = key(stock, 0x0800A900, index, first)
            ref_walk.append(index)
        check("menu: (stock, for reference: the same key stops at 12)", ref_walk == list(range(1, 13)) + [12] * 3, str(ref_walk))

        # the first row shown when the list opens: four places, each min(index, first row of the last page)
        opens = ((0x0800C2A6, 0x0800C2B4, "r5"), (0x0800D90E, 0x0800D91C, "r4"), (0x0800E5E6, 0x0800E602, "r4"), (0x0802DFFC, 0x0802E00A, "r10"))
        bad_open = []
        for start, stop, reg in opens:
            for index in range(0, LAST + 1):
                for img, top in ((image, 7 + E_BYTES), (stock, 7)):
                    mu, at, _ = run_code(img, ram, start, (stop,), {0xE59: index, 0xF6A: 99}, {reg: STATE_BASE, "r0": 0}, limit=50)
                    if at != stop or mu.mem_read(STATE_BASE + 0xF6A, 1)[0] != min(index, top):
                        bad_open.append((hex(start), index, img is image))
        check("menu: opening the list shows min(index, %d) as its first row (stock: 7), at the four places that set it" % (7 + E_BYTES),
              not bad_open, str(bad_open[:4]))

        # WAVE and the USB entry, through the emulated link
        if usb_entry:
            e.audit = False
            e.w8(SYSTEM_MODE, 3)
            stock_disconnect(e)
            e.tick(SETTLE + 5)
            e.enumerate()
            e.mu.mem_write(GEN_AT, bytes([1, 1, 0, 0]) + struct.pack("<I", 1000) + bytes([50, 30]))
            e.audit = True
            rs = request(e, 0x01, 0x30)
            names_at = struct.unpack_from("<I", rs, 48)[0]
            labels = []
            for k in range(rs[45]):
                at = struct.unpack("<I", read_mem(e, names_at + 4 * k, 4))[0]
                labels.append(read_mem(e, at, 24).split(b"\0", 1)[0].decode())
            check("menu: INFO gives the count, the USB entry and where the names are; READ returns them",
                  rs[44] == 7 and rs[45] == len(menu) and rs[46] == usb_entry["index"] and labels == [m["label"] for m in menu], str(labels))

            def gen_set(index, ampl):
                e.audit = False
                e.mu.mem_write(GEN_AT, bytes([1, index, 0, 0]) + struct.pack("<I", 1000) + bytes([50, ampl]))
                e.audit = True

            def tab_now():
                return list(struct.unpack("<100H", e.mu.mem_read(TABLE_AT, 200)))

            def send(samples, order=(0, 28, 56, 84)):
                for first in order:
                    part = samples[first:first + 28]
                    request(e, 0x04, 0x31, len(part), first, data=struct.pack("<%dH" % len(part), *part))

            gen_set(usb_entry["index"], 30)
            e.call(BUILDER)
            earlier = fnlink.wave_normalise(fnlink.wave_resample([0, 1, 0, -1]))
            check("menu: the USB entry plays the last waveform fnlink sent earlier in this run, at today's amplitude",
                  tab_now() == fnlink.wave_stored(earlier, 30))
            sent = fnlink.wave_shape("sinc")
            gen_set(1, 30)
            e.call(BUILDER)
            send(sent)
            check("menu: with Square Wave selected, WAVE leaves the square that is playing as it is", set(tab_now()) == {20, 3742})
            gen_set(usb_entry["index"], 30)
            e.call(BUILDER)
            check("menu: selecting the USB entry plays what was sent", tab_now() == fnlink.wave_stored(sent, 30))
            gen_set(usb_entry["index"], 12)
            e.call(BUILDER)
            check("menu: ...and a change of amplitude rescales it instead of losing it", tab_now() == fnlink.wave_stored(sent, 12))
            other = fnlink.wave_shape("stairs")
            gen_set(usb_entry["index"], 30)
            request(e, 0x04, 0x32, 28, 0, data=struct.pack("<28H", *other[:28]))
            e.call(BUILDER)
            check("menu: a waveform only partly received does not count: stock's sine until it is whole",
                  tab_now() == fnlink.wave_stored(sine, 30))
            send(other, order=(0, 56, 28, 84))
            e.call(BUILDER)
            check("menu: nor does one received out of order", tab_now() == fnlink.wave_stored(sine, 30))
            send(other)
            e.call(BUILDER)
            check("menu: received whole and in order, it is the USB entry's waveform", tab_now() == fnlink.wave_stored(other, 30))
            for x in entries:
                gen_set(x["index"], 30)
                e.call(BUILDER)
                if x["samples"] and tab_now() != fnlink.wave_stored(x["samples"], 30):
                    check("menu: entry %d through the emulated instrument" % x["index"], False)
            tr5 = type("T", (), {"write": lambda self, r: e.host_out(1, r), "read": lambda self, t: next((x for x in (poll_in(e) for _ in range(t)) if x is not None), None)})()
            gen_set(1, 30)
            e.call(BUILDER)
            check("menu: fnlink info and wave name the entries; a waveform sent under Square Wave is stored, not played",
                  fnlink.main(["info"], transport=tr5) == 0 and fnlink.main(["wave", "gauss"], transport=tr5) == 0
                  and set(tab_now()) == {20, 3742} and fnlink.main(["wave", "--show"], transport=tr5) == 0)
            gen_set(usb_entry["index"], 30)
            e.call(BUILDER)
            check("menu: what fnlink sent is what the USB entry plays", tab_now() == fnlink.wave_stored(fnlink.wave_shape("gauss"), 30))
            check("menu: sent while the USB entry is selected, a waveform plays at once and fnlink reads it back",
                  fnlink.main(["wave", "stairs"], transport=tr5) == 0 and tab_now() == fnlink.wave_stored(fnlink.wave_shape("stairs"), 30))
    else:
        check("no menu in this image: the builder, the list, the keys and the pictures are stock's bytes",
              all(image[a - 0x08007000:a - 0x08007000 + 4] == stock[a - 0x08007000:a - 0x08007000 + 4]
                  for a in (0x08029E14, 0x080157E6, 0x080108EA, 0x080127BC, 0x0801F8EE, 0x0800A9C0, 0x0800C2AA, 0x0802E000)))

    # ---- --language-menu: Settings > Language with one entry per language ---------------------
    # Stock's own key handlers and its routine for the options of a setting, run on the menu's
    # state bytes. The pictures and the text are not drawn here (no file system, no fonts set
    # up): the two routines are recorded with their arguments. What a row off the cursor is
    # filled with is painted for real, into a band of pixels placed outside stock's SRAM.
    langmenu = manifest.get("language_menu", [])
    OPTIONS, PICTURE, TEXT = 0x0801CF50, 0x08039F6C, 0x0800F154
    BAND_AT, BAND_PIXELS, BAND_ROOM = 0x20008350, 0x60000000, 0x40000
    key_handlers = [h & ~1 for h in struct.unpack_from("<16I", stock, 0x08046548 - 0x08007000)]
    KEY_PREVIOUS, KEY_NEXT, KEY_OK = key_handlers[10], key_handlers[11], key_handlers[14]

    def menu_bytes(mu):
        return tuple(mu.mem_read(STATE_BASE + o, 1)[0] for o in (0xF68, 0xF69, 0xF6A, 0xF6B, 0xF60))

    def press(img, handler, row, cursor, language=2, inside=1):
        """One key handler on the settings menu: (where it ended, the menu's bytes, SRAM written).
        A handler that goes on to a peripheral ends there, in both images alike."""
        from unicorn import UcError
        seen = {}

        def keep(mu):
            seen["mu"] = mu

        try:
            mu, at, writes = run_code(img, ram, handler, (), {0xF68: 4, 0xF69: inside, 0xF6A: row, 0xF6B: cursor, 0xF60: language},
                                      limit=30000, setup=keep)
        except UcError as ex:
            return "stopped: %s" % ex, menu_bytes(seen["mu"]), []
        return at, menu_bytes(mu), sorted({w[0] for w in writes})

    def cstring(mu, at):
        return bytes(mu.mem_read(at, 64)).split(b"\0")[0]

    def paint(img, state, band=(118, 58, 60, 150)):
        """Stock's options routine over one band: (where it ended, the calls, the pixels, the
        callee-saved registers, SRAM writes, (lowest sp at a call of the two routines, deepest
        stack byte written), bytes past the band)."""
        from unicorn import UC_HOOK_MEM_WRITE
        from unicorn import arm_const as A
        calls, deepest, at_calls = [], [0x20036F00], [0x20036F00]

        def picture(mu):
            at_calls[0] = min(at_calls[0], mu.reg_read(A.UC_ARM_REG_SP))
            calls.append(("picture", cstring(mu, mu.reg_read(A.UC_ARM_REG_R0)).decode("latin1"),
                          mu.reg_read(A.UC_ARM_REG_R1), mu.reg_read(A.UC_ARM_REG_R2)))

        def text(mu):
            at_calls[0] = min(at_calls[0], mu.reg_read(A.UC_ARM_REG_SP))
            a = struct.unpack("<4I", mu.mem_read(mu.reg_read(A.UC_ARM_REG_SP), 16))
            calls.append(("text",) + tuple(mu.reg_read(getattr(A, "UC_ARM_REG_R%d" % i)) for i in range(4))
                         + (cstring(mu, a[0]).decode("utf-8"),) + a[1:])

        def setup(mu):
            mu.mem_map(BAND_PIXELS, BAND_ROOM)
            mu.mem_write(BAND_AT, struct.pack("<HHHHI", *band, BAND_PIXELS))
            mu.hook_add(UC_HOOK_MEM_WRITE, lambda mu, acc, addr, size, value, _: deepest.__setitem__(0, min(deepest[0], addr)),
                        begin=0x20036000, end=0x20036F00)

        mu, at, writes = run_code(img, ram, OPTIONS, (), state, seed, skip={PICTURE: picture, TEXT: text},
                                  setup=setup, limit=3000000)
        used = 2 * band[2] * band[3]
        return (at, calls, bytes(mu.mem_read(BAND_PIXELS, used)), regs_of(mu, callee), writes, (at_calls[0], deepest[0]),
                any(mu.mem_read(BAND_PIXELS + used, BAND_ROOM - used)))

    # the keys: every handler over every setting and every cursor position, against stock. Where
    # the list goes on, the key must do what stock's does on its one step, from the first row
    # to the second: the same end, the same writes, the cursor one further.
    odd = []
    step = press(stock, KEY_NEXT, 0, 0)
    for handler in key_handlers:
        for row in range(7):
            for cursor in range(8):
                ref, got = press(stock, handler, row, cursor), press(image, handler, row, cursor)
                if langmenu and handler == KEY_NEXT and row == 0 and 1 <= cursor <= 6:
                    if ref[1][3] != cursor or ref[2]:
                        odd.append(("stock moved", row, cursor))
                    ref = (step[0], step[1][:3] + (cursor + 1,) + step[1][4:], step[2])
                if has_languages and handler == KEY_OK and row == 0:
                    continue                                 # --languages: its own section above
                if got != ref:
                    odd.append(("%08X" % handler, row, cursor, got[1]))
    check("stock: the next-option key steps from the first language to the second and writes the cursor",
          step[1] == (4, 1, 0, 1, 2) and STATE_BASE + 0xF6B in step[2], str(step))
    check("language menu: the next-option key goes on to the eighth entry, every other key and setting as stock"
          if langmenu else "no language menu: all sixteen key handlers as stock over the settings menu", not odd, str(odd[:6]))
    walk, cursor = [], 0
    for _ in range(9):
        cursor = press(image, KEY_NEXT, 0, cursor)[1][3]
        walk.append(cursor)
    for _ in range(9):
        cursor = press(image, KEY_PREVIOUS, 0, cursor)[1][3]
        walk.append(cursor)
    last = 7 if langmenu else 1
    check("language menu: the cursor walks down to entry %d, stays, and back up to 0" % last,
          walk == [min(i, last) for i in range(1, 10)] + [max(last - i, 0) for i in range(1, 10)], str(walk))
    if not has_languages:
        stored = [press(image, KEY_OK, 0, cursor, language)[1][4] for cursor in range(last + 1) for language in (1, 2, 5, 8)]
        check("language menu: OK stores the entry under the cursor plus one, whatever the language was",
              stored == [cursor + 1 for cursor in range(last + 1) for _ in range(4)], str(stored))
    opened = [press(image, KEY_OK, 0, 0, language, inside=0)[1] for language in (1, 5, 8)]
    check("language menu: opening the options puts the cursor on the first entry and keeps the language",
          opened == [(4, 1, 0, 0, language) for language in (1, 5, 8)], str(opened))

    # the other six settings: the routine paints what stock paints
    differ = []
    for row in range(1, 7):
        for inside in (0, 1):
            for cursor in range(4):
                state = {0xF68: 4, 0xF69: inside, 0xF6A: row, 0xF6B: cursor, 0xF60: 5, 0xF64: 1, 0xF65: 2}
                ref, got = paint(stock, state), paint(image, state)
                if ref != got or got[0] != usbemu.RET_MAGIC:
                    differ.append((row, inside, cursor))
    check("options routine: the six other settings painted exactly as stock (calls, pixels, registers, writes)",
          not differ, str(differ[:6]))

    if langmenu:
        NAMES = ("中文", "English", "Русский", "にほんご", "Español", "Português", "Deutsch", "한국어")
        check("language menu: the manifest lists the eight names", tuple(langmenu) == NAMES, str(langmenu))
        glyphs = language_tables.font_characters(language_tables.Image(stock), language_tables.FONT_CMAPS[18])
        check("language menu: the 18 px font has a glyph for every character of every name",
              all(ord(c) in glyphs for n in NAMES for c in n))
        P = "3:/System file/%d.jpg"
        wrong, unlike, frames, strays = [], [], set(), []
        cases = [(1, cursor, language) for cursor in range(8) for language in range(1, 9)]
        cases += [(0, 0, language) for language in range(1, 9)]
        cases += [(1, 3, 0), (1, 4, 9), (0, 0, 0), (0, 0, 9), (0, 0, 255), (1, 8, 2), (1, 200, 6)]
        for inside, cursor, language in cases:
            shown = cursor if inside else language - 1
            first = 4 if 4 <= shown < 8 else 0
            want = []
            for slot in range(4):
                entry, y = first + slot, 61 + 36 * slot
                chosen = language == entry + 1
                if inside and cursor == entry:
                    want += [("picture", P % 72, 121, y), ("picture", P % (75 if chosen else 78), 166 if chosen else 165, y + 8)]
                    colour = 0x055F
                else:
                    want += [("picture", P % (74 if chosen else 77), 166, y + 10)]
                    colour = 0xFFFF
                want += [("text", 182, y, 92, 34, NAMES[entry], 0x20001114, colour, 1)]
            got = paint(image, {0xF68: 4, 0xF69: inside, 0xF6A: 0, 0xF6B: cursor, 0xF60: language})
            if got[1] != want:
                wrong.append((inside, cursor, language))
            if got[0] != usbemu.RET_MAGIC or got[3] != tuple(seed[r] for r in callee[:-1]) + (0x20036F00,) or got[4] or got[6]:
                strays.append((inside, cursor, language))
            # stock's own four rows (Auto Shutdown) with the cursor and the mark in the same slots
            slot_chosen = language - 1 - first
            ref = paint(stock, {0xF68: 4, 0xF69: inside, 0xF6A: 3, 0xF6B: (cursor - first) & 0xFF if inside else 0,
                                0xF60: 2, 0xF65: slot_chosen if 0 <= slot_chosen < 4 else 9})
            same_calls = [c[:5] + c[6:] if c[0] == "text" else c for c in ref[1]] == [c[:5] + c[6:] if c[0] == "text" else c for c in got[1]]
            if not same_calls or ref[2] != got[2]:
                unlike.append((inside, cursor, language))
            frames.add((ref[5][0] - got[5][0], ref[5][1] - got[5][1]))
        check("language menu: four entries of the right page, with stock's pictures, places, font and colours, in all %d states" % len(cases),
              not wrong, str(wrong[:6]))
        check("language menu: the same pictures, text boxes and pixels as stock's own four-row panel in the same state",
              not unlike, str(unlike[:6]))
        check("language menu: leaves through stock's exit with r4-r11 and sp intact, no SRAM write, nothing past the band",
              not strays, str(strays[:6]))
        # stock's text routine alone takes 112 bytes below its caller, the picture routine 48 and more
        check("language menu: at the calls of stock's routines, %d bytes more stack than stock's own panel; %d in its own leaf"
              % (max(f[0] for f in frames), max(f[1] for f in frames)),
              max(f[0] for f in frames) <= 64 and max(f[1] for f in frames) <= 96, str(sorted(frames)))
        bands = []
        for band in ((0, 0, 320, 240), (150, 80, 100, 70), (0, 0, 320, 61), (300, 100, 20, 140), (130, 93, 3, 5)):
            got = paint(image, {0xF68: 4, 0xF69: 1, 0xF6A: 0, 0xF6B: 5, 0xF60: 5}, band)
            ref = paint(stock, {0xF68: 4, 0xF69: 1, 0xF6A: 3, 0xF6B: 1, 0xF60: 2, 0xF65: 0}, band)
            if got[2] != ref[2] or got[6] or got[4] or got[0] != usbemu.RET_MAGIC:
                bands.append(band)
        check("language menu: clipped to the band as stock clips its own rows (whole screen, partial, none)", not bands, str(bands))
        changed = {int(a, 16) for a in manifest["changed_stock_bytes"]}
        check("language menu: its five stock bytes are the Language case's first instruction and the limit of that setting",
              {0x0801CF7E, 0x0801CF7F, 0x0801CF80, 0x0801CF81, 0x0800B1D8} <= changed
              and image[0x0800B1D8 - 0x08007000:0x0800B1DF - 0x08007000] == bytes([7, 1, 3, 3, 0, 0, 1]))
    else:
        got = paint(image, {0xF68: 4, 0xF69: 1, 0xF6A: 0, 0xF6B: 0, 0xF60: 2})
        check("no language menu: the Language options are stock's two rows",
              got == paint(stock, {0xF68: 4, 0xF69: 1, 0xF6A: 0, 0xF6B: 0, 0xF60: 2})
              and [c[5] for c in got[1] if c[0] == "text"] == ["中文", "English"], str(got[1]))

    # ---- --texts: texts of stock replaced by pointing their slots at the cave ------------------
    # The multimeter's title is the one routine that reads its table (0x0804C43C, twelve
    # functions for each of eight languages): run for every language and function, with the
    # text routine recorded, it must ask for stock's text everywhere but in the slots replaced.
    texts = manifest.get("texts", [])
    TITLE, TITLES = 0x08015C08, 0x0804C43C

    def title(img, language, function):
        from unicorn import arm_const as A
        calls = []

        def text(mu):
            a = struct.unpack("<4I", mu.mem_read(mu.reg_read(A.UC_ARM_REG_SP), 16))
            calls.append(tuple(mu.reg_read(getattr(A, "UC_ARM_REG_R%d" % i)) for i in range(4))
                         + (cstring(mu, a[0]).decode("utf-8"),) + a[1:])

        mu, at, writes = run_code(img, ram, TITLE, (), {0xF60: language, 0xF2E: function}, seed,
                                  skip={PICTURE: lambda mu: None, TEXT: text})
        return at, calls, regs_of(mu, callee), writes

    replaced = {int(t["slot"], 16): t["text"] for t in texts}
    wrong = []
    for language in range(1, 9):
        for function in range(12):
            ref, got = title(stock, language, function), title(image, language, function)
            slot = TITLES + 48 * (language - 1) + 4 * function
            want = replaced.get(slot, ref[1][0][4])
            if (got[0], got[2], got[3]) != (ref[0], ref[2], ref[3]) or len(got[1]) != 1 \
                    or got[1][0] != ref[1][0][:4] + (want,) + ref[1][0][5:] or got[1][0][:4] != (52, 0, 216, 29):
                wrong.append((language, function, got[1]))
    check("multimeter title: %s in its 216 px box, every other language and function as stock"
          % ("the %d texts replaced" % len([a for a in replaced if TITLES <= a < TITLES + 384]) if texts else "stock's texts"),
          not wrong, str(wrong[:4]))
    # Stock's text routine itself, run to the end with its fonts (their descriptors are in the
    # image's initialised data): what it paints on a whole-screen band, as the set of pixels.
    FONT_AT = {15: 0x200010B4, 16: 0x200010E4, 18: 0x20001114, 20: 0x20001144, 25: 0x20001174, 28: 0x200011A4}
    TEXT_X, TEXT_Y, SCRATCH = 40, 60, BAND_PIXELS + 0x30000

    class Everything:
        def __contains__(self, _):
            return True

    def draw(text, px, width, height):
        from unicorn import arm_const as A

        def setup(mu):
            mu.mem_map(BAND_PIXELS, BAND_ROOM)
            mu.mem_write(BAND_AT, struct.pack("<HHHHI", 0, 0, 320, 240, BAND_PIXELS))
            mu.mem_write(SCRATCH, text.encode("utf-8") + b"\0")
            mu.mem_write(mu.reg_read(A.UC_ARM_REG_SP), struct.pack("<4I", SCRATCH, FONT_AT[px], 0xFFFF, 1))

        mu, at, writes = run_code(image, ram, TEXT, (), (), {"r0": TEXT_X, "r1": TEXT_Y, "r2": width, "r3": height},
                                  follow=Everything(), setup=setup, limit=3000000)
        pixels = mu.mem_read(BAND_PIXELS, 2 * 320 * 240)
        return at, {(i % 320, i // 320) for i in range(320 * 240) if pixels[2 * i] or pixels[2 * i + 1]}

    def whole(text, px, width, height):
        """Is the text drawn whole and inside its box? Every line the builder expects, drawn
        alone, must be a line of what the routine draws for the text, and nothing may be
        painted outside the box."""
        at, ink = draw(text, px, width, height)
        lines = builder.text_lines(stock, px, width, text)
        inside = all(TEXT_X <= x < TEXT_X + width and TEXT_Y <= y < TEXT_Y + height for x, y in ink)
        rows = sorted({y for _, y in ink})
        alone = [draw(line.strip(), px, width, px)[1] for line in lines]
        # the lines are stacked a line height apart: split what was painted by those bands
        top = TEXT_Y + (height - len(lines) * px) // 2
        split = [{(x, y - top - k * px) for x, y in ink if top + k * px <= y < top + (k + 1) * px} for k in range(len(lines))]
        ref = [{(x, y - TEXT_Y) for x, y in a} for a in alone]
        return at == usbemu.RET_MAGIC and bool(ink) and inside and split == ref, (inside, len(ink), [len(a) for a in alone], rows[:1], rows[-1:])

    ok_one, why_one = whole("Corriente alterna mA", 28, 216, 29)
    cut = draw("Corriente continua mA", 28, 216, 29)[1]
    full = draw("Corriente continua mA", 28, 320 - TEXT_X, 29)[1]
    check("stock's text routine, really run: 215 px is drawn whole in the title's 216 x 29 box; 232 px is not, and what it loses is the end",
          ok_one and builder.text_width(stock, 28, "Corriente alterna mA") == 215 and builder.text_width(stock, 28, "Corriente continua mA") == 232
          and len({(x, y) for x, y in cut if TEXT_Y <= y < TEXT_Y + 29}) < len(full), str(why_one))
    ok_two, why_two = whole("Subida exponencial", 18, 100, 36)
    check("stock's text routine: a text of two words too wide for 100 px is drawn in two lines, broken at the space, inside a box 36 high",
          ok_two and builder.text_lines(stock, 18, 100, "Subida exponencial") == ["Subida ", "exponencial"], str(why_two))
    spill = draw("Amplitud", 18, 54, 22)[1]
    check("stock's text routine: a word one pixel too wide (55 in 54) loses its last letter to a line below the box, as on the unit",
          builder.text_width(stock, 18, "Amplitud") == 55 and any(y >= TEXT_Y + 22 for _, y in spill)
          and not any(y >= TEXT_Y + 22 for _, y in draw("Amplit.", 18, 54, 22)[1]))
    own_names = sorted({name for e in manifest.get("menu", []) for name in e.get("names", {}).values()})
    if own_names:
        drawn = [(name, whole(name, 18, 100, 36)) for name in own_names]
        check("menu names: stock's own routine draws each of the %d names given for a language whole, inside a row of the list" % len(own_names),
              all(ok for _, (ok, _) in drawn), str([(name, why) for name, (ok, why) in drawn if not ok][:3]))
    if texts:
        bad = []
        for t in texts:
            slot, at, raw = int(t["slot"], 16), int(t["at"], 16), t["text"].encode("utf-8") + b"\0"
            here = struct.unpack_from("<I", image, slot - 0x08007000)[0]
            if here != at or not CAVE_ADDR <= at < CAVE_ADDR + manifest["cave_len"] or image[at - 0x08007000:at - 0x08007000 + len(raw)] != raw \
                    or struct.unpack_from("<I", stock, slot - 0x08007000)[0] != int(t["was"], 16) \
                    or builder.text_lines(stock, t["font"], t["box"][0], t["text"]) != t["lines"] \
                    or len(t["lines"]) * t["font"] > t["box"][1]:
                bad.append(t["slot"])
        check("texts: each of the %d slots points at its text in the cave, laid out as the manifest says" % len(texts),
              not bad, str(bad))
        drawn = [(t["text"], whole(t["text"], t["font"], *t["box"])) for t in texts]
        check("texts: stock's own routine draws each of the %d whole and inside its box" % len(texts),
              all(ok for _, (ok, _) in drawn), str([(text, why) for text, (ok, why) in drawn if not ok][:3]))
        changed = {int(a, 16) for a in manifest["changed_stock_bytes"]}
        check("texts: nothing of stock changed for them but bytes of those pointers",
              all(any(slot <= a < slot + 4 for a in changed) for slot in replaced))

    # ---- audit of every SRAM write made by cave code ---------------------------------------
    allowed_core = {UDEV + 4, UDEV + 8,                              # class / descriptor pointers
                    UDEV + 0x17, UDEV + 0x117,                       # endpoint 0 stall flags
                    UDEV + 0x26, UDEV + 0x28, UDEV + 0x220,          # endpoint 0 data stage
                    UDEV + 0x13A, UDEV + 0x13C, UDEV + 0x140}        # OUT endpoint 1 receive
    bad = []
    n_cave = 0
    for addr, size, value, pc in e.writes:
        if pc < CAVE_ADDR:
            continue
        n_cave += 1
        in_block = LINK_RAM <= addr and addr + size <= LINK_RAM_LIMIT
        in_stack = 0x20036000 <= addr < usbemu.STACK_TOP or 0x20035000 <= addr < TASK_STACK_TOP
        in_table = 0x20000F5A <= addr and addr + size <= 0x20000F5A + 200       # WAVE, patch 4
        if not (in_block or in_stack or in_table or addr in allowed_core):
            bad.append("%08X<-%X (pc %08X)" % (addr, value, pc))
    check("cave code writes only its block, its stack frames, the documented core fields and the generator's table (%d writes)" % n_cave,
          n_cave > 0 and not bad, ", ".join(bad[:6]))
    cal = [w for w in e.writes if 0x20000358 <= w[0] < 0x2000044C]
    check("nothing wrote stock's calibration tables in RAM", not cal, str(cal[:3]))

    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
