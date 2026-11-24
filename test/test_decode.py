#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Decoder checks against real captures (test/data/), no hardware and no emulator needed.

    python3 test/test_decode.py

The captures come from one retail FNIRSI 2C53T running stock V1.2.0 with stock-link v1
(2026-11-23 and 24). For the multimeter, every frame is decoded again and compared with what
was on the instrument at the time: a 10 kOhm resistor, a 9 V battery, fingers on the probes,
each function of the key cycle and, on the second day, the AC functions, a 1 uF capacitor, a
LED in the diode function and the HOLD key. For the oscilloscope, the instrument's own
generator (1 kHz square wave, 3.01 V peak to peak by its own on-screen measurement).
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
#    A blank display (while the range changes) is never consistent and is left out of the count.
with_unit = [(fn, fnlink.decode_meter_event(f, s)) for _, fn, f, s in everything
             if fnlink.decode_meter_frame(f)["unit"] and not fnlink.decode_meter_frame(f)["blank"]]
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

# ---------------------------------------------------------------- multimeter, second session

ac_idle = meter_lines("meter_ac_idle.txt")
ac_hand = meter_lines("meter_acv_hand.txt")
capacitor = meter_lines("meter_capacitor_1uf.txt")
diode = meter_lines("meter_diode_led.txt")
hold = meter_lines("meter_hold.txt")
second = ac_idle + ac_hand + capacitor + diode + hold
check("second session loaded", [len(x) for x in (ac_idle, ac_hand, capacitor, diode, hold)] == [120, 40, 300, 300, 60])
check("second session: no unknown annunciator", not any(fnlink.decode_meter_frame(f)["unknown_bits"] for _, _, f, _ in second))


def event(function, frame, state):
    return fnlink.decode_meter_event(frame, state, FUNCTION_CODES[function])


def stock_value_matches(d, state):
    """Stock's float is the decoded number, as the scaled value or as the raw count."""
    stock = struct.unpack_from("<f", state, 8)[0]
    return (abs(stock - d["value"]) <= 0.6 * 10 ** -state[12]
            or abs(abs(stock) - abs(d["value"]) * 10 ** state[12]) <= 1.0)


# 7. AC: nothing in the frame says "alternating" (the bytes are those of the DC function); the
#    function word does. Idle readings as on the screen that day.
for function, text, unit in (("AC V", "0.0000", ("V", "")), ("AC mA", "0.008", ("A", "m")), ("AC A", "0.0007", ("A", ""))):
    rows = [event(fn, f, s) for _, fn, f, s in ac_idle if fn == function]
    good = [d for d in rows if d["consistent"]]
    check("%s idle: 40 frames, consistent but for a blank one at most, all in %s" % (function, unit),
          len(rows) == 40 and len(good) >= 39 and all((d["unit"], d["prefix"]) == unit for d in good),
          "%d of %d" % (len(good), len(rows)))
    check("%s idle: the screen's %s is among them" % (function, text), any(d["text"].strip() == text for d in rows))
check("AC V with the probes shorted: 0 Hz", all(event(fn, f, s)["frequency_hz"] == 0 for _, fn, f, s in ac_idle if fn == "AC V"))
check("no frequency outside AC V", all(event(fn, f, s)["frequency_hz"] is None for _, fn, f, s in second if fn != "AC V"))

# 8. AC V with the probes in the hand: a couple of volts of mains hum, and the frequency stock
#    shows above the reading. It is the frame's last byte and, a frame later, state[18..19].
rows = [(f, s, event(fn, f, s)) for _, fn, f, s in ac_hand]
hum = [(f, s, d) for f, s, d in rows if d["value"] is not None and d["value"] > 1.0]
check("hand: 2.2 to 2.4 V", len(hum) >= 20 and all(2.2 <= d["value"] <= 2.45 and d["consistent"] for _, _, d in hum))
check("hand: frequency between 45 and 80 Hz once it has settled",
      all(45 <= d["frequency_hz"] <= 80 for _, _, d in hum[2:]), str([d["frequency_hz"] for _, _, d in hum]))
check("hand: the state's frequency is the last byte of this frame or of the one before",
      all(d["frequency_hz"] in (f[11], hum[i - 1][0][11]) for i, (f, s, d) in enumerate(hum) if i))

# 9. Capacitance: n with the probes apart, u (micro) on a 1 uF capacitor; stock's unit codes 5 and 6.
rows = [(s, event(fn, f, s)) for _, fn, f, s in capacitor]
nano = [d for _, d in rows if d["consistent"] and d["prefix"] == "n"]
micro = [(s, d) for s, d in rows if d["consistent"] and d["prefix"] == "u"]
check("capacitor: open probes read 0.000 nF", len(nano) >= 40 and all(d["text"].strip() == "0.000" and d["si"] == 0 for d in nano[:30]))
check("capacitor: 1 uF reads 1.00 to 1.05 uF", len(micro) >= 240 and all(1.00e-6 <= d["si"] <= 1.05e-6 for _, d in micro),
      "%d readings" % len(micro))
check("capacitor: stock's unit code is 6 for uF", all(s[16] == 6 for s, _ in micro))
blank = [d for _, d in rows if d["blank"]]
check("capacitor: the display with its point alone is blank and not consistent",
      len(blank) >= 2 and all(not d["consistent"] and d["value"] is None for d in blank), str(len(blank)))

# 10. Diode: bit 7 of byte 6, volts. Open probes, then a LED; every number is stock's own.
rows = [(f, s, event(fn, f, s)) for _, fn, f, s in diode]
check("diode: every frame lights byte 6 bit 7 and is consistent, in volts",
      all(f[6] & 0x80 and d["consistent"] and (d["unit"], d["prefix"]) == ("V", "") for f, _, d in rows))
led = [(s, d) for _, s, d in rows if d["value"] is not None]
check("diode: open probes show .0L", sum(1 for _, _, d in rows if d["text"].strip() == ".0L") >= 20)
check("diode: the LED settles at 1.811 V", len(led) >= 270 and sum(1 for _, d in led if d["text"].strip() == "1.811") >= 250,
      "%d numbers" % len(led))
check("diode: stock's float is the decoded number in every frame", all(stock_value_matches(d, s) for s, d in led))

# 11. HOLD: state[20] is 1, stock's state stops and the frames go on.
rows = [(s, event(fn, f, s)) for _, fn, f, s in hold]
held = [(s, d) for s, d in rows if d["hold"]]
check("hold: on for one stretch of 23 frames", len(held) == 23 and [d["hold"] for _, d in rows].count(True) == 23
      and "".join("1" if d["hold"] else "0" for _, d in rows).strip("0") == "1" * 23)
check("hold: the frozen screen is -0.0005 V throughout",
      all(d["held"] and d["held"]["text"] == "-0.0005" and (d["held"]["unit"], d["held"]["prefix"]) == ("V", "") for _, d in held))
check("hold: the frames keep changing meanwhile", len({d["text"] for _, d in held}) >= 5)
check("hold: nothing held outside it", all(d["held"] is None for _, d in rows if not d["hold"]))
check("hold: off in every other capture", not any(fnlink.decode_meter_event(f, s)["hold"] for _, _, f, s in everything + second[:-60]))

# 12. state[13] says what stock's screen shows: 2 for an overload and for nothing else, 0 for a
#     number. (A frame or two behind the display, like the rest of the state.)
overload = [s[13] for t, _, f, s in everything + second if fnlink.decode_meter_frame(f)["text"].strip() in ("0.L", ".0L", "0L")]
check("state[13] is 2 in at least 97%% of overload frames", len(overload) > 100 and overload.count(2) >= 0.97 * len(overload),
      "%d of %d" % (overload.count(2), len(overload)))
numbers = [s[13] for t, _, f, s in everything + second if fnlink.decode_meter_frame(f)["value"] is not None]
check("state[13] is 0 in at least 97%% of frames with a number", numbers.count(0) >= 0.97 * len(numbers),
      "%d of %d" % (numbers.count(0), len(numbers)))

# 13. held_screen on made-up states: an overload, a value too large for the display, a unit not known.
state = bytearray(hold[30][3])
check("held_screen: the capture's own state", fnlink.held_screen(bytes(state))["si"] == -0.0005)
state[13] = 2
check("held_screen: overload", fnlink.held_screen(bytes(state))["overload"] and fnlink.held_screen(bytes(state))["value"] is None)
state[13] = 3
check("held_screen: blank display", fnlink.held_screen(bytes(state)) is None)
state[13] = 0
struct.pack_into("<f", state, 8, 3.0)
check("held_screen: a raw count does not fit the display", fnlink.held_screen(bytes(state)) is None)
state[16] = 0x7F
check("held_screen: unknown unit", fnlink.held_screen(bytes(state)) is None)

# 14. meter_text
check("meter_text: diode", fnlink.meter_text(event("Diode", diode[150][2], diode[150][3])) == "1.811 V")
check("meter_text: micro", fnlink.meter_text(event("Cap", capacitor[200][2], capacitor[200][3])).endswith(" uF"))
check("meter_text: frequency", fnlink.meter_text(event("AC V", ac_hand[-1][2], ac_hand[-1][3])) == "2.349 V 49Hz")
check("meter_text: hold", fnlink.meter_text(event("DC V", hold[30][2], hold[30][3])) == "0.0003 V HOLD:-0.0005V",
      fnlink.meter_text(event("DC V", hold[30][2], hold[30][3])))

# ---------------------------------------------------------------- stock's RAM, read on request
# A few bytes each, read from the unit on 2026-11-24 with a photograph of its screen next to them.

g = fnlink.decode_generator(bytes.fromhex("01010000983a0000321e"))
check("generator: Square, 15 kHz, 50 %, 3.0 V", (g["waveform"], g["frequency_hz"], g["duty_percent"], g["amplitude_v"])
      == ("Square Wave", 15000, 50, 3.0), str(g))
g = fnlink.decode_generator(bytes.fromhex("01040000b88800003214"))
check("generator: Full Wave, 35 kHz, 2.0 V", (g["waveform"], g["frequency_hz"], g["amplitude_v"]) == ("Full Wave", 35000, 2.0), str(g))
check("generator: an entry of the menu that was not photographed has no name",
      fnlink.decode_generator(bytes.fromhex("01070000e8030000321e"))["waveform"] is None)
g = fnlink.decode_generator(bytes.fromhex("0102000050c300003214"))
check("generator: the triangle on the scope was entry 2, 50 kHz, 2.0 V", (g["waveform_index"], g["waveform"], g["frequency_hz"], g["amplitude_v"])
      == (2, "Sawtooth wave", 50000, 2.0), str(g))

# a second stopped screen, a 50 kHz triangle: 50.18KHz, 50.50%, 220.0mV. Its Vmin is below zero.
TRIANGLE = struct.pack("<6I6i", 50176, 19929, 5050, 4950, 10065, 9864, 210000, -10000, 220000, 218000, 118000, 100000)
check("scope measurements: read from the unit as unsigned words, Vmin was 4294957296", struct.unpack_from("<I", TRIANGLE, 28)[0] == 4294957296)
m = fnlink.decode_scope_measurements(TRIANGLE)
check("scope measurements: 50176 Hz, 50.50 %, 220.0 mV, Vmin -10 mV", m["frequency_hz"] == 50176 and abs(m["duty_positive_percent"] - 50.5) < 1e-9
      and abs(m["vpp"] - 0.22) < 1e-9 and abs(m["vmin"] + 0.01) < 1e-9 and abs(m["vmax"] - m["vmin"] - m["vpp"]) < 1e-9, str(m))

# the stopped screen read 40.23KHz, 64.51% and 114.0mV
STOPPED = bytes.fromhex("289d00001761000033190000dd0d0000a33e000074220000b02403006067010050bd010080b5010070880200d0780200"
                        "4c00000006c6c800" + "00" * 40)
m = fnlink.decode_scope_measurements(STOPPED[:48])
check("scope measurements: 40232 Hz, 64.51 %, 114.0 mV peak to peak", m["frequency_hz"] == 40232
      and abs(m["duty_positive_percent"] - 64.51) < 1e-9 and abs(m["vpp"] - 0.114) < 1e-9, str(m))
check("scope measurements: period is 1/frequency and the two widths add up to it",
      abs(m["period_s"] - 1 / 40232) < 1e-9 and abs(m["width_positive_s"] + m["width_negative_s"] - m["period_s"]) < 2e-9)
check("scope measurements: Vpp is Vmax - Vmin, duties add up to 100 %", abs(m["vmax"] - m["vmin"] - m["vpp"]) < 1e-9
      and abs(m["duty_positive_percent"] + m["duty_negative_percent"] - 100) < 0.011)
m2 = fnlink.decode_scope_measurements(STOPPED[48:])
check("scope measurements: CH2 off shows a frequency and its period only", m2["frequency_hz"] == 76
      and abs(m2["period_s"] - 1 / 76) < 1e-7 and m2["vpp"] == 0)

METER_STATE = bytes.fromhex("14000000000101ff87161341030001010100000000000000000000000000000087161341787a8940662616c4040402010100010100000000")
boxes = fnlink.decode_meter_stats(METER_STATE)
check("meter boxes: MAX 9.193 V, MEAN 4.2962 V, MIN -600.6 with its own unit",
      [b[0] for b in boxes] == ["MAX", "MEAN", "MIN"] and abs(boxes[0][1] - 9.193) < 1e-3 and abs(boxes[1][1] - 4.2962) < 1e-4
      and abs(boxes[2][1] + 600.6) < 0.01 and [b[2] for b in boxes] == [4, 4, 2] and [b[3] for b in boxes] == [1, 1, 0], str(boxes))
check("meter boxes: MEAN is (MAX + MIN) / 2 once MIN is put in volts", abs((boxes[0][1] + boxes[2][1] / 1000) / 2 - boxes[1][1]) < 1e-3)
IDLE = bytes.fromhex("13050000000000ff0000000004000101010000000000000000000000000000000000c07f0000c07f0000c07f040402010100010100000000")
check("meter boxes: empty when stock holds no number", all(b[1] is None for b in fnlink.decode_meter_stats(IDLE)))

SCOPE_STATE_STOPPED = bytes.fromhex("01000200ccd40000000000000000000000000000010100010000e7ff16000000e05ef8ffffffffff00000000000800000001ff0000000000")
SCOPE_STATE_RUNNING = bytes.fromhex("01000200ccd40000000000000000000000000000010100010000e7ff16000000e05ef8ffffffffff00000000000801000001ff0000000000")
check("scope state: byte 46 is the only difference between stopped and running",
      [i for i in range(56) if SCOPE_STATE_STOPPED[i] != SCOPE_STATE_RUNNING[i]] == [46]
      and (SCOPE_STATE_STOPPED[46], SCOPE_STATE_RUNNING[46]) == (0, 1))
check("scope state: 5 us per division, 50 mV per division", abs(fnlink.scope_settings(SCOPE_STATE_STOPPED)["seconds_per_div"] - 5e-6) < 1e-12
      and abs(fnlink.scope_settings(SCOPE_STATE_STOPPED)["channels"][0]["volts_per_div"] - 0.05) < 1e-12)
lines = fnlink.status_lines(2, IDLE, SCOPE_STATE_STOPPED, STOPPED, bytes.fromhex("01040000b88800003214"))
text = "\n".join(lines)
check("status: says what it read", "STOP" in text and "Full Wave (index 4), 35kHz, duty 50 %, 2.0 V" in text
      and "CH1 shown   40.2kHz" in text and "Vpp 114mV" in text and "MAX empty" in text, text)

# ---------------------------------------------------------------- the settings/calibration page
# Made-up pages only: a real one holds a unit's calibration and has no place in a repository.

def made_up_page(settings=0x11, table=0x22, scratch=0xFF):
    page = bytearray([0xFF]) * 4096
    page[0x000:0x038] = bytes([settings]) * 0x38
    page[0x038:0x130] = bytes([table]) * 0xF8
    page[0x130:0x200] = bytes([scratch]) * 0xD0
    page[0x800:0x804] = b"flag"
    return bytes(page)

first = made_up_page()
later = made_up_page(settings=0x33, scratch=0x00)             # stock saved settings in between
check("cal table: 248 bytes at 0x038", len(first[fnlink.CAL_TABLE]) == 248 and fnlink.CAL_TABLE.start == 0x38)
check("cal table: same fingerprint although settings and scratch changed",
      fnlink.cal_table_sha256(first) == fnlink.cal_table_sha256(later) and first != later)
check("cal differences: settings and scratch, not the table",
      fnlink.cal_differences(first, later) == [("settings", 0x38), ("scratch after the table", 0xD0)], str(fnlink.cal_differences(first, later)))
other = made_up_page(table=0x23)
check("cal differences: a changed table is named", fnlink.cal_differences(first, other) == [("calibration table", 248)]
      and fnlink.cal_table_sha256(first) != fnlink.cal_table_sha256(other))
check("cal differences: identical pages", fnlink.cal_differences(first, first) == [])
check("cal regions cover the page once", [r[1] for r in fnlink.CAL_REGIONS] + [0x1000] == [0] + [r[2] for r in fnlink.CAL_REGIONS])
try:
    fnlink.cal_table_sha256(first[:100])
    check("cal table: a file of another size is refused", False)
except fnlink.LinkError:
    check("cal table: a file of another size is refused", True)
import io, contextlib, tempfile
with tempfile.TemporaryDirectory() as tmp:
    fa, fb, fc = (Path(tmp) / n for n in ("a.bin", "b.bin", "c.bin"))
    fa.write_bytes(first); fb.write_bytes(later); fc.write_bytes(other)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc_same = fnlink.main(["cal-info", str(fa), str(fb)])
        rc_diff = fnlink.main(["cal-info", str(fa), str(fc)])
        rc_one = fnlink.main(["cal-info", str(fa)])
    text = out.getvalue()
    check("cal-info: runs without a device and says where two dumps differ",
          (rc_same, rc_diff, rc_one) == (0, 1, 0) and "the calibration table is identical in both" in text
          and "the calibration tables are not the same" in text and "scratch after the table" in text, text[-400:])

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
