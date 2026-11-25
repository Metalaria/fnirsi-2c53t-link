#!/usr/bin/env python3
"""When does stock's EXTI3 handler turn the buzzer on?

EXTI line 3 is PC3, which carries the meter chip's own buzzer tone (OpenScope-2C53T #94, #102).
This runs stock's handler at 0x08009C10 in an emulator for every combination of the three
state bytes it tests and prints what it wrote: the buzzer duty (TMR11 C1DT, 0x40015434) and
the buzzer's tick counter (0x20002D67).

    python tools/exti3_gate.py --stock APP_2C53T_V1.2.0_251015.bin

Needs your own copy of FNIRSI's V1.2.0 image and the unicorn package. Nothing of the firmware
is in this file.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS
from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_LR

IMAGE_BASE = 0x08007000
HANDLER = 0x08009C10
STATE = 0x200000F8                 # "ms" in OpenScope's notes
DUTY, COUNTER, PENDING = 0x40015434, 0x20002D67, 0x40010414
RETURN_TO = 0x08000100             # an address outside the image, where the run stops
DUTY_BEFORE, COUNTER_BEFORE = 0x55, 0x77


def run(image: bytes, system_mode: int, menu: int, variant: int):
    mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    mu.mem_map(0x08000000, 0x100000)
    mu.mem_write(IMAGE_BASE, image)
    mu.mem_map(0x20000000, 0x40000)
    mu.mem_map(0x40010000, 0x10000)
    mu.mem_write(PENDING, struct.pack("<I", 0x08))        # line 3 pending
    mu.mem_write(DUTY, struct.pack("<I", DUTY_BEFORE))
    mu.mem_write(COUNTER, bytes([COUNTER_BEFORE]))
    mu.mem_write(STATE + 0xF68, bytes([system_mode]))
    mu.mem_write(STATE + 0xF2D, bytes([menu]))
    mu.mem_write(STATE + 0xF36, bytes([variant]))
    mu.reg_write(UC_ARM_REG_SP, 0x20030000)
    mu.reg_write(UC_ARM_REG_LR, RETURN_TO | 1)
    mu.emu_start(HANDLER | 1, RETURN_TO, count=200)
    return struct.unpack("<I", mu.mem_read(DUTY, 4))[0], mu.mem_read(COUNTER, 1)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stock", required=True, help="FNIRSI's APP_2C53T_V1.2.0_251015.bin")
    args = ap.parse_args()
    image = Path(args.stock).read_bytes()
    beeps = []
    for system_mode in range(0, 8):
        for menu in range(0, 8):
            for variant in range(0, 4):
                duty, counter = run(image, system_mode, menu, variant)
                if duty not in (DUTY_BEFORE, 10) or counter not in (COUNTER_BEFORE, 0):
                    print("unexpected: mode %d menu %d variant %d -> duty 0x%X counter 0x%X" % (
                        system_mode, menu, variant, duty, counter))
                    return 1
                if duty == 10:
                    beeps.append((system_mode, menu, variant))
                if system_mode in (1, 2) and menu <= 2:
                    print("ms[0xF68]=%d ms[0xF2D]=%d ms[0xF36]=%d: duty %s, counter %s" % (
                        system_mode, menu, variant, "written 10" if duty == 10 else "untouched",
                        "zeroed" if counter == 0 else "untouched"))
    print("of 256 combinations (system mode 0-7, menu 0-7, variant 0-3) the duty is written in %d:" % len(beeps))
    print("  " + ", ".join("(%d, %d, %d)" % b for b in beeps))
    expected = [(1, 0, v) for v in range(4)] + [(1, 2, 2)]
    print("as read: system mode 1 and (menu 0, or menu 2 with variant 2): %s" % ("yes" if beeps == expected else "NO"))
    return 0 if beeps == expected else 1


if __name__ == "__main__":
    sys.exit(main())
