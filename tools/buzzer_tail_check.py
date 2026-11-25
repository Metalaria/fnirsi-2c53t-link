#!/usr/bin/env python3
"""What the buzzer's tick counter did in a `fnlink.py watch 0x20002D67:1` capture.

Stock advances that counter every 10 ms while the buzzer is on and turns the buzzer off at 11.
A key press sets the buzzer on once; in Continuity every edge of the meter chip's tone zeroes
the counter again. So a capture shows, for each beep, how long the buzzer stayed on after the
last thing that restarted it.

    python tools/buzzer_tail_check.py sessions/2026-11-25_meter_functions/continuity_counter.txt
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def changes(path: Path):
    rows = []
    for line in path.read_text().splitlines():
        m = re.match(r"\s+([\d.]+) ms\s+([0-9a-f]{2})\s", line)
        if m:
            rows.append((float(m.group(1)), int(m.group(2), 16)))
    return rows


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    rows = changes(Path(sys.argv[1]))
    tails, singles, i = [], [], 0
    while i < len(rows):
        t, v = rows[i]
        if v == 1 and i + 1 < len(rows) and rows[i + 1][1] == 0:
            singles.append(t)                       # the tick took it to 1, something zeroed it at once
        elif v == 1:
            j = i
            while j < len(rows) and rows[j][1] != 0:
                j += 1
            if j < len(rows):
                tails.append((t, rows[j][0], [x for _, x in rows[i:j]]))
            i = j
        i += 1
    print("%d changes" % len(rows))
    print("single reads of 1, each 0 again on the next read: %d" % len(singles))
    if len(singles) > 1:
        gaps = [b - a for a, b in zip(singles, singles[1:]) if b - a < 200]
        off = max(abs(g - 10 * round(g / 10)) for g in gaps)
        print("  %d of them less than 200 ms after the one before, never more than %.1f ms off a multiple of 10 ms" % (
            len(gaps), off))
    ok = True
    for start, end, values in tails:
        counted = values == list(range(1, len(values) + 1))
        ok = ok and counted and len(values) == 10
        print("countdown from %.1f ms: %s, 0 again %.1f ms after the first 1%s" % (
            start, " ".join(str(v) for v in values), end - start, "" if counted else "   NOT one by one"))
    highest = max((v for t, v in rows if not any(a <= t <= b for a, b, _ in tails)), default=0)
    print("highest value outside a countdown: %d" % highest)
    return 0 if ok and highest <= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
