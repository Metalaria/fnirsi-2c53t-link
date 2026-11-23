#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Decoder checks against real captures (test/data/), no hardware and no emulator needed.

    python3 test/test_decode.py

The captures come from one retail FNIRSI 2C53T running stock V1.2.0 with stock-link v1
(2026-11-23). For the multimeter, every frame is decoded again and compared with what was on
the instrument at the time: a 10 kOhm resistor, a 9 V battery, fingers on the probes, and
each function of the key cycle. For the oscilloscope, the instrument's own generator
(1 kHz square wave, 3.01 V peak to peak by its own on-screen measurement).
"""
from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import fnlink  # noqa: E402

DATA = HERE / "data"
FUNCTION_CODES = {name: code for code, name in fnlink.METER_FUNCTIONS.items()}
LINE = re.compile(r"^\s*([\d.]+) s\s+(.*?)\s+raw ((?:[0-9a-f]{2} ?){12})\s+state ([0-9a-f]{80})\s*$")

failures = 0
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures, checks
    checks += 1
    if not ok:
        failures += 1
        print("FAIL  %s%s" % (name, "  (" + detail + ")" if detail else ""))


def meter_lines(filename: str):
    """(text shown then, function name, frame, state) of every display frame in a capture."""
    out = []
    for line in (DATA / filename).read_text().splitlines():
        m = LINE.match(line)
        if not m:
            continue                                    # comments and echo lines
        head = m.group(2)
        function = next((n for n in sorted(FUNCTION_CODES, key=len, reverse=True) if head.endswith(n)), "func 0x00")
        text = head[: len(head) - len(function)].strip() if head.endswith(function) else head
        out.append((text, function, bytes.fromhex(m.group(3)), bytes.fromhex(m.group(4))))
    return out


def scope_record(filename: str):
    body = "".join(l for l in (DATA / filename).read_text().splitlines() if not l.startswith("#"))
    raw = bytes.fromhex(body)
    assert len(raw) == 56 + 1024, len(raw)
    return raw[:56], raw[56:]


# ---------------------------------------------------------------- multimeter

resistor = meter_lines("meter_resistor_10k.txt")
battery = meter_lines("meter_battery_9v.txt")
megaohm = meter_lines("meter_auto_megaohm.txt")
check("captures loaded", len(resistor) > 500 and len(battery) > 150 and len(megaohm) == 40,
      "%d %d %d" % (len(resistor), len(battery), len(megaohm)))

# 1. The digits decode to what fnlink printed on the day, for every frame.
for name, lines in (("resistor", resistor), ("battery", battery), ("megaohm", megaohm)):
    bad = [(t, fnlink.decode_meter_frame(f)["text"].strip()) for t, _, f, _ in lines
           if fnlink.decode_meter_frame(f)["text"].strip() != t]
    check("%s: every frame's digits decode as captured" % name, not bad, str(bad[:3]))

# 2. No frame lights an annunciator outside the documented set.
everything = resistor + battery + megaohm
check("no unknown annunciator in any captured frame",
      not any(fnlink.decode_meter_frame(f)["unknown_bits"] for _, _, f, _ in everything))

# 3. Per function, consistent readings carry the unit that function measures. The function word
#    is the order stock has just sent, so it changes a frame ahead of the display: the first
#    frame after a key press can still be the previous function's. Hence "nearly all".
EXPECTED_UNITS = {"DC V": {("V", "")}, "Ohm": {("Ohm", "k"), ("Ohm", "M")}, "Continuity": {("Ohm", "")},
                  "Temp": {("degC", "")}, "DC mA": {("A", "m")}, "DC A": {("A", "")}, "Cap": {("F", "n")}}
for function, units in EXPECTED_UNITS.items():
    seen = [(d["unit"], d["prefix"]) for d in (fnlink.decode_meter_event(f, s) for _, fn, f, s in everything
                                               if fn == function) if d["consistent"]]
    inside = sum(1 for u in seen if u in units)
    check("%s: at least 95%% of its consistent readings are in %s" % (function, sorted(units)),
          len(seen) >= 5 and inside >= 0.95 * len(seen), "%d of %d" % (inside, len(seen)))

# 4. The frame's annunciators and stock's own unit code agree on the great majority of frames;
#    the exceptions are the frame or two around a change of function, and Auto with nothing connected.
with_unit = [(fn, fnlink.decode_meter_event(f, s)) for _, fn, f, s in everything
             if fnlink.decode_meter_frame(f)["unit"]]
agree = sum(1 for _, d in with_unit if d["consistent"])
check("annunciators and firmware unit agree on >98% of frames with a unit",
      agree / len(with_unit) > 0.98, "%d of %d" % (agree, len(with_unit)))

# 5. Known readings.
def first(lines, function, text):
    """The first consistent reading with this function word and display text."""
    return next(d for d in (fnlink.decode_meter_event(f, s) for t, fn, f, s in lines if fn == function and t == text)
                if d["consistent"])

d = first(resistor, "Ohm", "9.831")
check("10 kOhm resistor, Ohm function: 9.831 kOhm", d["consistent"] and abs(d["si"] - 9831.0) < 1e-6, str(d["si"]))
d = first(resistor, "func 0x00", "9.824")
check("10 kOhm resistor, Auto: 9.824 kOhm", d["consistent"] and (d["unit"], d["prefix"]) == ("Ohm", "k"))
d = first(battery, "DC V", "9.201")
check("9 V battery, DC V: 9.201 V", d["consistent"] and abs(d["si"] - 9.201) < 1e-9, str(d["si"]))
d = first(megaohm, "func 0x00", "1.0255")
check("fingers on the probes: 1.0255 MOhm (leading 1, lowest range)", d["consistent"] and abs(d["si"] - 1.0255e6) < 1.0)
d = first(resistor, "DC mA", "-0.006")
check("DC mA: -0.006 mA", d["consistent"] and abs(d["si"] + 6e-6) < 1e-12, str(d["si"]))
d = first(resistor, "Continuity", "0.L")
check("continuity, open: OL in ohms with the continuity annunciator", d["continuity"] and d["value"] is None and d["unit"] == "Ohm")
d = first(resistor, "Temp", "23")
check("temperature: 23 degC", d["consistent"] and d["si"] == 23.0)

# 6. Stock's own value (a float in its state) matches the decoded digits once it has settled.
#    While stock is converting it the float briefly holds the raw count instead, so both are accepted.
mismatch = 0
for t, fn, frame, state in resistor + battery:
    d = fnlink.decode_meter_event(frame, state)
    if not d["consistent"] or d["value"] is None:
        continue
    stock = struct.unpack_from("<f", state, 8)[0]
    scaled = abs(stock - d["value"]) <= 0.6 * 10 ** -state[12]
    raw = abs(abs(stock) - abs(d["value"]) * 10 ** state[12]) <= 1.0
    # a display refresh and stock's conversion are not atomic: allow the previous reading too
    if not (scaled or raw):
        mismatch += 1
check("stock's float agrees with the decoded digits (scaled or raw count) on >90% of frames",
      mismatch < 0.1 * len(resistor + battery), "%d mismatches" % mismatch)

# ---------------------------------------------------------------- oscilloscope

state, data = scope_record("scope_ch1_1khz_square.hex")
s = fnlink.scope_settings(state)
check("CH1 record: settings decode", s is not None)
check("CH1 on, CH2 off", s["channels"][0]["enabled"] and not s["channels"][1]["enabled"])
check("500 mV/div on screen (50 mV at the input, X10)", abs(s["channels"][0]["volts_per_div"] - 0.5) < 1e-9
      and s["channels"][0]["probe"] == 10)
check("200 us/div, 8 us per sample", abs(s["seconds_per_div"] - 200e-6) < 1e-12 and abs(s["sample_period"] - 8e-6) < 1e-12)
m = fnlink.measure_record(s, 0, data)
check("Vpp within 0.1 V of the instrument's 3.01 V", abs(m["vpp"] - 3.01) < 0.1, "%.3f" % m["vpp"])
check("frequency within 0.5% of the instrument's 1.00 kHz", m["frequency"] and abs(m["frequency"] - 1000) < 5, str(m["frequency"]))
check("not clipped", not m["clipped"])

state1, data1 = scope_record("scope_pair_ch1_flat.hex")
state2, data2 = scope_record("scope_pair_ch2_clipped.hex")
s2 = fnlink.scope_settings(state2)
check("pair: both channels on, same settings in both headers", state1 == state2 and all(c["enabled"] for c in s2["channels"]))
check("pair: 2 ms/div", abs(s2["seconds_per_div"] - 2e-3) < 1e-12)
check("pair: CH1 flat, no frequency", fnlink.measure_record(s2, 0, data1)["frequency"] is None)
m2 = fnlink.measure_record(s2, 1, data2)
check("pair: CH2 clipped at both rails, still 1 kHz", m2["clipped"] and m2["frequency"] and abs(m2["frequency"] - 1000) < 10,
      str(m2["frequency"]))

check("ladders", fnlink.volts_per_div(0) == 0.01 and abs(fnlink.volts_per_div(5) - 0.5) < 1e-12
      and fnlink.volts_per_div(10) is None and abs(fnlink.seconds_per_div(13) - 200e-6) < 1e-15)

print("%d checks, %d failed" % (checks, failures))
sys.exit(1 if failures else 0)
