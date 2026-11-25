#!/usr/bin/env python3
"""Check what stock draws while the oscilloscope is stopped against the record it captured.

Reads a text file of `fnlink.py snapshot` blocks taken while stepping the instrument (see
sessions/2026-11-25_openscope_parity/README.md) and compares, for every stopped snapshot, the
301-byte display array of CH1 with the raw record, under the rules OpenScope-2C53T's
docs/re/parity2_static.md derives from stock's code:

  pan      at the captured time base the array is a slice of the record, moved by the pan the
           instrument keeps at ms[0xDE8] (in the units of ms[0xDD0]/ms[0xDD8], the screen's edges)
  vertical y = clamp(128 + pos_now + V[range_cap] / V[range_now] * (raw - 128 - pos_cap), 28, 228)
  zoom in  the array is the output of stock's 16-tap interpolator (table at 0x080466E0)

The interpolator's coefficients are read from a stock V1.2.0 image given with --stock: none of
FNIRSI's firmware is in this repository. Without it that one check is skipped.
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

MS = 0x200000F8                       # stock's state structure
DISPLAY, RECORD = 0x2000044E, 0x200006A8
VOLTS = (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)      # mV per division by range index
FACTORS = (2, 4, 5, 8, 10, 20, 25, 40, 50, 80)                    # the interpolator's sets, in table order
APP_BASE, KERNEL_TABLE = 0x08007000, 0x080466E0


def load(path: Path) -> list:
    snaps, cur = [], None
    for line in path.read_text().splitlines():
        if line.startswith("snapshot  "):
            cur = {"label": line[10:], "blocks": {}}
            snaps.append(cur)
        elif line.startswith("watch"):
            cur = None
        elif cur is not None and re.match(r"^[0-9A-F]{8} +\d+  [0-9a-f]+$", line):
            addr, _, data = line.split()
            cur["blocks"][int(addr, 16)] = bytes.fromhex(data)
    return [s for s in snaps if DISPLAY in s["blocks"] and RECORD in s["blocks"]]


def field(snap: dict, offset: int, fmt: str):
    """One field of the state structure, by its offset from 0x200000F8."""
    size = struct.calcsize(fmt)
    for addr, data in snap["blocks"].items():
        if addr <= MS + offset and MS + offset + size <= addr + len(data):
            return struct.unpack_from(fmt, data, MS + offset - addr)[0]
    raise KeyError("ms[0x%X] is not in snapshot %r" % (offset, snap["label"]))


def kernel(stock: bytes, record: bytes, factor: int) -> bytes:
    """out[n*factor + p] = clamp((sum c[set + p][k] * in[n + k]) >> 15, 0, 255), k = 0..15."""
    first = sum(f for f in FACTORS if f < factor)
    taps = [struct.unpack_from("<16i", stock, KERNEL_TABLE - APP_BASE + 64 * (first + p)) for p in range(factor)]
    out = bytearray()
    for n in range(len(record) - 16):
        for c in taps:
            out.append(max(0, min(255, sum(c[k] * record[n + k] for k in range(16)) >> 15)))
    return bytes(out)


def check(path: Path, stock: bytes | None) -> int:
    snaps = load(path)
    stopped = [s for s in snaps if field(s, 0x2E, "B") == 0]
    if not stopped:
        print("%s: no stopped snapshot" % path)
        return 1
    record = stopped[0]["blocks"][RECORD]
    anchor = field(stopped[0], 0xDBC, "<H") & 0x7FFF
    bad = 0
    # (one screen column in the time units of the edges: 301 columns between them)
    column = (field(stopped[0], 0xDD8, "<q") - field(stopped[0], 0xDD0, "<q")) // 301
    print("%s: %d stopped snapshots, trigger anchor %d, captured at time base %d, range %d, position %d, horizontal %d" % (
        path.name, len(stopped), anchor, field(stopped[0], 0xDC0, "B"), field(stopped[0], 0xDC1, "B"),
        field(stopped[0], 0xDC3, "b"), field(stopped[0], 0xDC8, "<h")))
    for s in stopped:
        shown = s["blocks"][DISPLAY][:301]
        ratio = field(s, 0xE08, "<q")
        h_now, h_cap = field(s, 0x1A, "<h"), field(s, 0xDC8, "<h")
        col, width = field(s, 0xDB4, "<H"), field(s, 0xDB6, "<H")
        k = VOLTS[field(s, 0xDC1, "B")] / VOLTS[field(s, 2, "B")]
        pos_now, pos_cap = field(s, 4, "b"), field(s, 0xDC3, "b")
        what = "time base %2d, ratio %5d, range %d, horizontal %4d, drawn from column %3d, %3d wide" % (
            field(s, 0x2D, "B"), ratio, field(s, 2, "B"), h_now, col, width)
        if s["blocks"][RECORD] != record:
            verdict = "DIFFERS: the record changed while stopped"
        elif ratio == 1000:
            pan = field(s, 0xDE8, "<q") // column
            start = anchor - 150 - h_cap - min(pan, 0)
            want = bytes(max(28, min(228, int(128 + pos_now + k * (record[start + c] - 128 - pos_cap)))) for c in range(301))
            n = 301 if pan >= 0 else width
            ok = (shown[:n] == want[:n] and col == max(0, pan) and width == 301 - abs(pan))
            verdict = ("ok       array = record[%d ..] rescaled by %.2f, pan %d" % (start, k, pan)) if ok else "DIFFERS  from the pan and vertical rules"
        elif ratio < 1000:
            if stock is None:
                verdict = "skipped  (zoom in: needs --stock)"
            else:
                factor = 1000 // ratio
                at = kernel(stock, record, factor).find(shown)
                verdict = ("ok       array = interpolator output [%d ..], column 150 at record position %.2f" % (at, (at + 150) / factor + 7)
                           if at >= 0 else "DIFFERS  from the 16-tap interpolator")
        else:
            first = next(x for x in stopped if field(x, 0xE08, "<q") == 1000)
            ok = shown == first["blocks"][DISPLAY][:301] and width == 301 * 1000 // ratio and col == 150 - width // 2
            verdict = "ok       array left as it was; only the column and the width change" if ok else "DIFFERS  from: array untouched, width = 301000 // ratio, column = 150 - width // 2"
        bad += verdict.startswith("DIFFERS")
        print("  %-14s %s: %s" % (s["label"], what, verdict))
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("captures", type=Path, nargs="+")
    ap.add_argument("--stock", type=Path, help="stock V1.2.0 application image, for the interpolator's coefficients")
    args = ap.parse_args()
    stock = args.stock.read_bytes() if args.stock else None
    return 1 if sum(check(p, stock) for p in args.captures) else 0


if __name__ == "__main__":
    sys.exit(main())
