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
EVENTS = (0x90, 0xA0, 0xA1)
STOCK_CLASS, STOCK_DESC = 0x20000030, 0x20000058
CAVE_ADDR = 0x080BE700
BOOT_HOLDOFF, SETTLE = 3000, 1000

STREAM_METER, STREAM_SCOPE = 1, 2
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


def request(e, cmd, seq=0, length=0, addr=0, events=None):
    """Send a request and return its response. Events queued ahead of it are appended to
    `events` if given, otherwise dropped."""
    rq = bytes([cmd, seq, length, 0]) + struct.pack("<I", addr) + bytes(56)
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True, type=Path)
    ap.add_argument("--image", required=True, type=Path)
    args = ap.parse_args()
    stock = args.stock.read_bytes()
    image = args.image.read_bytes()
    manifest = json.loads(args.image.with_suffix(".json").read_text())
    sym = {k: int(v, 16) for k, v in manifest["symbols"].items()}
    vid, pid = (int(x, 16) for x in manifest["usb_vid_pid"].split(":"))
    has_scope = "0x0803e49a" in manifest["hooks"]
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
    check("INFO: header", rs[0] == 0x81 and rs[1] == 0x5A and rs[2] == 0 and rs[3] == 36, rs[:8].hex())
    check("INFO: tag 2C53LINK, proto 1, patch 2", rs[8:16] == b"2C53LINK" and rs[16] == 1 and rs[17] == 2)
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
        check("fnlink read: refused range reported", fnlink.main(["read", "0x40005C40", "4"], transport=tr) == 2)
        check("fnlink verify: flash equals the image", fnlink.main(["verify", str(args.image)], transport=tr) == 0)
        other = Path(tmp) / "stock.bin"
        other.write_bytes(stock)
        check("fnlink verify: a different image is reported", fnlink.main(["verify", str(other)], transport=tr) == 1)

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
        if not (in_block or in_stack or addr in allowed_core):
            bad.append("%08X<-%X (pc %08X)" % (addr, value, pc))
    check("cave code writes only its block, its stack frames and the documented core fields (%d writes)" % n_cave,
          n_cave > 0 and not bad, ", ".join(bad[:6]))
    cal = [w for w in e.writes if 0x20000358 <= w[0] < 0x2000044C]
    check("nothing wrote stock's calibration tables in RAM", not cal, str(cal[:3]))

    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
