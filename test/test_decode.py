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

# AC V at three frequencies set on the instrument's own generator (2026-11-27): 40 frames each, all
# with the leading 1 of the five-digit display, on the lowest range. The frame's last two bytes are
# the frequency, most significant first, and stock's state has the same number.
ac_gen = meter_lines("meter_acv_generator.txt")
check("AC V from the generator: 120 frames, all AC V, all with the leading 1 and the lowest-range bit",
      len(ac_gen) == 120 and {fn for _, fn, _, _ in ac_gen} == {"AC V"}
      and all(f[2] & 0x08 and f[8] == 0x82 for _, _, f, _ in ac_gen))
check("AC V from the generator: what was printed is what the frame and stock's state say",
      all(t == "%s V %dHz" % (event(fn, f, s)["text"].strip(), event(fn, f, s)["frequency_hz"]) and event(fn, f, s)["consistent"]
          for t, fn, f, s in ac_gen), str(ac_gen[0][0]))
check("AC V from the generator: 20, 400 and 1000 Hz in the frame's last two bytes, and five readings in all",
      [sorted({f[10] << 8 | f[11] for _, _, f, _ in ac_gen[k:k + 40]}) for k in (0, 40, 80)] == [[20], [400], [1000]]
      and sorted({event(fn, f, s)["text"].strip() for _, fn, f, s in ac_gen}) == ["1.0377", "1.0474", "1.0475", "1.0535", "1.0536"])
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
check("generator: the first entry of the menu is the sine, and an index past the menu has no name",
      fnlink.decode_generator(bytes.fromhex("01000000e8030000321e"))["waveform"] == "Sine Wave"
      and fnlink.decode_generator(bytes.fromhex("010d0000e8030000321e"))["waveform"] is None)
# the last six entries as the unit printed them (sessions/2026-11-25_generator_ch2_boxes/generator_waveforms.txt)
LAST_SIX = [("0107000050c30000321e", "Index Up"), ("0108000050c30000321e", "Index Decrease"), ("0109000050c30000321e", "Direct current"),
            ("010a000050c30000321e", "Multi-audio"), ("010b000050c30000321e", "Sink Pulse"), ("010c000050c30000321e", "Lorentz waves")]
check("generator: the last six entries carry the names the menu shows",
      all(fnlink.decode_generator(bytes.fromhex(raw))["waveform"] == name for raw, name in LAST_SIX))
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
      and "CH1 shown   40.2kHz" in text and "Vpp 114mV" in text and "MAX empty" in text and "arrows" not in text, text)
text = "\n".join(fnlink.status_lines(2, IDLE, SCOPE_STATE_STOPPED, STOPPED, bytes.fromhex("01040000b88800003214"), 0x02))
check("status: with the arrow keys' mode when it was read", "  arrows     move: left/right move the trace sideways" in text
      and "(byte 0x02; SELECT changes it)" in text, text)

# ---------------------------------------------------------------- the meter's functions
# The twelve functions of stock's meter menu, as read on the unit
# (sessions/2026-11-25_meter_functions/meter_functions.txt): name, state bytes.
METER_STATES = [
    ("Automatic", "5e000000000000000000000000010000ff0000000000000000000000000000000000c07f0000c07f0000c07f000000000000000000000000"),
    ("DC Voltage", "5e000000000101ff04e78c3b04000101010000000000000000000000000000003333bf41686686bf0000d0c1030403000000000000000000"),
    ("AC Voltage", "5e000000000102ffc1ca613f04000201010000000000000000000000000000002041a93f9949254400000000040204010000010000000000"),
    ("Continuity", "5e000000000203ff0000a04104020200020000000000000000000000000000000000c07f0000c07f0000c07f040404000000000000000000"),
    ("Diode", "5e000000000204ff0000a04104020101010000000000000000000000000000000000c07f0000c07f0000c07f040404000000000000000000"),
    ("Resistance", "5e000000000305ff0000a04104020102040000000000000000000000000000000000c07f0000c07f0000c07f040404000000000000000000"),
    ("Capacitance", "5e000000000406ff000000000300010005000000000000000000000000000000000000000000000000000000040404000000050505000000"),
    ("Temperature", "5e000000000507ff0000d04100000100090000000000000000000000000000000000d0410000504100000000030304000000090909000000"),
    ("Small DC current", "5e000000000608ff6f1203bc030001010b00000000000000000000000000000000000000000090c0000010c10404040000000a0a0a000000"),
    ("Small AC current", "5e000000000609ff6f12033c030002010b0000000000000000000000000000000000004100008040000000000404040000000a0a0a000000"),
    ("Large DC current", "5e00000000070aff17b751ba040001020c000000000000000000000000000000000000000000e1c3000061c40402020000000a0a0a000000"),
    ("Large AC current", "5e00000000070bff3480373a040002020c00000000000000000000000000000000002f440000c8c2000061c40202020000000a0a0a000000"),
]
states = [(name, bytes.fromhex(h)) for name, h in METER_STATES]
check("meter state: byte 6 names the function in the eleven that measure something",
      all(fnlink.METER_MEASURING[st[6]] == name for name, st in states[1:]) and states[0][1][6] == 0)
check("meter state: byte 5 is the menu entry, 0 to 7, two functions in four of them",
      [st[5] for _, st in states] == [0, 1, 1, 2, 2, 3, 4, 5, 6, 6, 7, 7])
check("meter state: byte 14 is 1 or 2 inside an entry, AC and continuity being 2",
      [st[14] for _, st in states] == [0, 1, 2, 2, 1, 1, 1, 1, 1, 2, 1, 2])
check("buzzer: stock's gate lets the chip's tone through in Automatic and Continuity only",
      [name for name, st in states if fnlink.buzzer_follows_meter_chip(1, st)] == ["Automatic", "Continuity"]
      and not any(fnlink.buzzer_follows_meter_chip(2, st) for _, st in states))
text = "\n".join(fnlink.status_lines(1, states[3][1], SCOPE_STATE_STOPPED, STOPPED, bytes.fromhex("01040000b88800003214")))
check("status: names the meter's function and says what the buzzer follows",
      "menu 2 (continuity and diode), measuring 3 (Continuity), variant 2" in text and "tone reaches the buzzer" in text
      and "state value OL Ohm" in text, text)
text = "\n".join(fnlink.status_lines(1, states[4][1], SCOPE_STATE_STOPPED, STOPPED, bytes.fromhex("01040000b88800003214")))
check("status: in Diode the chip's tone does not reach the buzzer", "measuring 4 (Diode), variant 1" in text
      and "tone does not reach the buzzer" in text, text)

# The boxes' units are state bytes 50 to 52, not 47 to 49 (which read 0 0 0 with the screen showing uA).
units = {name: [fnlink.STATS_UNITS.get(b[3]) for b in fnlink.decode_meter_stats(st)] for name, st in states}
check("meter boxes: uA in the current functions, as the screen showed in Small DC current",
      all(units[n] == ["uA"] * 3 for n in ("Small DC current", "Small AC current", "Large DC current", "Large AC current")), str(units))
check("meter boxes: the reading's own unit in capacitance and temperature, V and mV in AC voltage",
      units["Capacitance"] == ["nF"] * 3 and units["Temperature"] == ["degC"] * 3 and units["AC Voltage"] == ["V", "mV", "mV"], str(units))
small = dict(states)["Small DC current"]
check("meter boxes: bytes 47 to 49 are not the unit", list(small[47:50]) == [0, 0, 0] and list(small[50:53]) == [10, 10, 10])
text = "\n".join(fnlink.status_lines(1, small, SCOPE_STATE_STOPPED, STOPPED, bytes.fromhex("01040000b88800003214")))
check("status: the boxes with their unit", "MEAN -4.5000 (unit code 10: uA)" in text, text)

# CH2 alone with the generator's square wave: scope state and stock's own measurements as read on 2026-11-25.
CH2_ONLY = bytes.fromhex("01000203b3000000000000000000000000000000021201010000000000000000000000000000000000000000000901000000ff0000000000")
st = fnlink.scope_settings(CH2_ONLY)
check("scope state: CH2 alone at 100 mV and 10 us per division",
      abs(st["channels"][1]["volts_per_div"] - 0.1) < 1e-12 and abs(st["seconds_per_div"] - 10e-6) < 1e-15, str(st))

# Coupling, probe setting and time base, from the states of sessions/2026-11-25_coupling_probe_time_bases.
STEP_DC = bytes.fromhex("01000203b40000000000000000000000000000000101000100000000ffff0000000000000000000000000000000801000001ff0000000000")
STEP_AC = bytes.fromhex("00000203b40000000000000000000000000000000101000100000000ffff0000000000000000000000000000000801000001ff0000000000")
check("scope state: CH1's coupling is byte 0, and the only byte that differs between DC and AC",
      [i for i in range(56) if STEP_DC[i] != STEP_AC[i]] == [0]
      and fnlink.scope_settings(STEP_DC)["channels"][0]["coupling"] == "DC"
      and fnlink.scope_settings(STEP_AC)["channels"][0]["coupling"] == "AC")
STEP_CH1_X10 = bytes.fromhex("01000203b40001000000000000000000000000000101000100000000faff0000000000000000000000000000000801000001ff0000000000")
STEP_CH2_X10 = bytes.fromhex("01000202b4000001000000000000000000000000021201010000000001000000000000000000000000000000000801000001ff0000000000")
check("scope state: the X10 setting of each channel is its own byte and multiplies its volts per division",
      [c["probe"] for c in fnlink.scope_settings(STEP_CH1_X10)["channels"]] == [10, 1]
      and [c["probe"] for c in fnlink.scope_settings(STEP_CH2_X10)["channels"]] == [1, 10]
      and abs(fnlink.scope_settings(STEP_CH1_X10)["channels"][0]["volts_per_div"] - 0.5) < 1e-12
      and abs(fnlink.scope_settings(STEP_CH2_X10)["channels"][1]["volts_per_div"] - 0.5) < 1e-12)
STEP_2US = bytes.fromhex("01000203b40000000000000000000000000000000101000100000000faff0000000000000000000000000000000701000001ff0000000000")
check("scope state: 2 us and 1 us per division are checked time bases, 500 ns is not",
      fnlink.scope_settings(STEP_2US)["time_base_checked"] and abs(fnlink.scope_settings(STEP_2US)["seconds_per_div"] - 2e-6) < 1e-15
      and fnlink.scope_settings(STEP_2US[:45] + b"\x06" + STEP_2US[46:])["time_base_checked"]
      and not fnlink.scope_settings(STEP_2US[:45] + b"\x05" + STEP_2US[46:])["time_base_checked"])
# the trigger edge: the same settings read on the unit with the rising and with the falling edge (2026-11-26)
EDGE_RISING = bytes.fromhex("01010202b40000010000000000000000000000000101000100000000ffff0000000000000000000000000000001301000001ff0000000000")
EDGE_FALLING = bytes.fromhex("01010202b40000010000000000000000000000000101000101000000ffff0000000000000000000000000000001301000000ff0000000000")
check("scope state: byte 24 is the trigger edge, 0 rising and 1 falling; anything else is not named",
      fnlink.scope_settings(EDGE_RISING)["trigger_edge"] == "rising" and fnlink.scope_settings(EDGE_FALLING)["trigger_edge"] == "falling"
      and fnlink.scope_settings(EDGE_RISING[:24] + b"\x02" + EDGE_RISING[25:])["trigger_edge"] is None
      and [i for i in range(56) if EDGE_RISING[i] != EDGE_FALLING[i]] == [24, 49])
text = "\n".join(fnlink.status_lines(2, IDLE, EDGE_FALLING, STOPPED, bytes.fromhex("0101000064000000321e")))
check("status: the scope line says the trigger's mode, edge and channel",
      "20ms/div, CH1 50mV/div X1 DC, CH2 500mV/div X10 DC, channels CH1, trigger Auto falling CH1" in text, text)
# the session of 2026-11-26: one setting switched at a time from the first state, both channels on
RE_BASE = bytes.fromhex("01010202b40000000000000000000000000000000302000100000000ffff0000000000000000000000000000000d01000001ff0000000000")
RE_STEPS = {  # what was set on the instrument: (the state read, the bytes that differ from the first)
    "trigger mode Normal": ("01010202b40000000000000000000000000000000302000000000000ffff0000000000000000000000000000000d01000001ff0000000000", [23]),
    "trigger mode Single": ("01010202b40000000000000000000000000000000302000200000000ffff0000000000000000000000000000000d00000001ff0000000000", [23, 46]),
    "trigger channel CH2": ("01010202b40000000000000000000000000000000302010100000000ffff0000000000000000000000000000000d01000001ff0000000000", [22]),
    "CH2 coupling AC": ("01000202b40000000000000000000000000000000302000100000000ffff0000000000000000000000000000000d01000001ff0000000000", [1]),
    "CH1 FFT on": ("01010202b40000000000010040390320000000000302000100000000ffff0000000000000000000000000000000d01000001ff0000000000", [10, 12, 13, 14, 15]),
    "CH2 FFT on": ("01010202b40000000000000100000000403903200302000100000000ffff0000000000000000000000000000000d01000001ff0000000000", [11, 16, 17, 18, 19]),
}
check("scope state: each setting switched on the unit moves the bytes the notes say and no other",
      all([i for i in range(56) if bytes.fromhex(h)[i] != RE_BASE[i]] == where for h, where in RE_STEPS.values()))
decoded = {name: fnlink.scope_settings(bytes.fromhex(h)) for name, (h, _) in RE_STEPS.items()}
first = fnlink.scope_settings(RE_BASE)
check("scope state: trigger mode (0 Normal, 1 Auto, 2 Single) and channel (0 CH1, 1 CH2)",
      (first["trigger_mode"], first["trigger_channel"]) == ("Auto", "CH1")
      and decoded["trigger mode Normal"]["trigger_mode"] == "Normal" and decoded["trigger mode Single"]["trigger_mode"] == "Single"
      and decoded["trigger channel CH2"]["trigger_channel"] == "CH2")
check("scope state: CH2's coupling is byte 1 and the FFT flags bytes 10 and 11",
      [c["coupling"] for c in first["channels"]] == ["DC", "DC"] and decoded["CH2 coupling AC"]["channels"][1]["coupling"] == "AC"
      and [c["fft"] for c in first["channels"]] == [False, False]
      and [c["fft"] for c in decoded["CH1 FFT on"]["channels"]] == [True, False]
      and [c["fft"] for c in decoded["CH2 FFT on"]["channels"]] == [False, True])
text = "\n".join(fnlink.status_lines(2, IDLE, bytes.fromhex(RE_STEPS["CH2 FFT on"][0]), STOPPED, bytes.fromhex("0101000064000000321e")))
check("status: FFT and both couplings on the scope line", "CH2 50mV/div X1 DC, channels CH1+CH2, FFT CH2, trigger Auto rising CH1" in text, text)
# stock's twelve words of CH1 and CH2 as `status` read them with the oscilloscope stopped (2026-11-26)
WORDS_STOPPED = (struct.pack("<6I6i", 1000, 1000000, 5040, 4960, 504000, 496000, 304000, -12000, 316000, 316000, 208000, 148000)
                 + struct.pack("<6I6i", 846076, 1181, 5647, 4353, 611, 472, 2800, -3200, 6000, 4400, 800, -800))
m = fnlink.decode_scope_measurements(WORDS_STOPPED[:48])
check("measurements: words 9 to 11 under the names stock prints in front of them (Amp, Vrms, Avg)",
      (round(m["amp"], 6), round(m["vrms"], 6), round(m["avg"], 6)) == (0.316, 0.208, 0.148)
      and m["extra"] == [m["amp"], m["vrms"], m["avg"]] and round(fnlink.decode_scope_measurements(WORDS_STOPPED[48:])["avg"], 6) == -0.0008)
text = "\n".join(fnlink.status_lines(2, IDLE, bytes.fromhex("0101030009d3000000000000000000000000000001010001000000002e000000000000000000000000000000000d00000000ff0000000000"),
                                     WORDS_STOPPED + b"\0", bytes.fromhex("01010000e8030000321e")))
# ---- stock's FFT block, as read on the unit at three time bases (2026-11-26) -----------------
fft_file = Path(__file__).resolve().parent.parent / "sessions" / "2026-11-26_measurements_fft_pointer" / "fft_time_bases.txt"
fft_lines = fft_file.read_text().splitlines()
fft_states = [bytes.fromhex(x.split()[2]) for x in fft_lines if x.startswith("200000F8")]
fft_blocks = [bytes.fromhex(x.split()[2]) for x in fft_lines if x.startswith("20033940")]
fft_seen = []
for st, blk in zip(fft_states, fft_blocks):
    step = fnlink.fft_hz_per_index(fnlink.scope_settings(st)["seconds_per_div"])
    d = fnlink.decode_fft_block(blk)
    bars, mags = d["bars"], d["magnitudes"]
    fundamental = max(range(1, 64), key=lambda k: bars[k])
    scaled = all(int(mags[k] * 50 / max(mags)) == bars[k] for k in range(len(mags)))
    fft_seen.append((round(step, 1), fundamental, round(1000 / step), len(mags), scaled, bars))
check("FFT block: three captures with the row on, 128 bars and 80 floats each, every bar its float scaled to 50",
      len(fft_seen) == 3 and all(s[3] == 80 and s[4] and len(s[5]) == 128 and s[5][0] == 50 for s in fft_seen), str([s[:5] for s in fft_seen]))
check("FFT block: 488.3, 195.3 and 976.6 Hz an index at 200, 500 and 100 us per division, the 1 kHz fundamental at index 2, 5 and 1",
      [s[:3] for s in fft_seen] == [(488.3, 2, 2), (195.3, 5, 5), (976.6, 1, 1)], str([s[:3] for s in fft_seen]))
bars500 = fft_seen[1][5]
folded = [round((25000 - f) / 195.3125) for f in (23000, 21000, 19000, 17000, 15000, 13000)]
check("FFT block: at 500 us per division the square's harmonics above 12.5 kHz are where 25 kHz minus them falls (10, 20, 31, 41, 51, 61)",
      folded == [10, 20, 31, 41, 51, 61] and all(bars500[k] > bars500[k - 1] and bars500[k] > bars500[k + 1] for k in folded), str([bars500[k - 1:k + 2] for k in folded]))
# the same wave with CH1's probe setting at X10 (2026-11-26): the voltages carry the factor themselves
WORDS_X10 = struct.pack("<6I6i", 1000, 1000000, 5001, 4998, 500160, 499840, 3030400, -20800, 3051200, 3040000, 2108800, 1490400)
STATE_X10 = bytes.fromhex("01010200b4d301000000000000000000000000000101000100006affffff000010b6fdffffffffff00000000000d01000001ff0000000000")
mx = fnlink.decode_scope_measurements(WORDS_X10)
check("measurements at X10: words 6 to 11 are ten times those read at X1, with nothing to apply",
      fnlink.scope_settings(STATE_X10)["channels"][0]["probe"] == 10 and fnlink.scope_settings(STATE_X10)["channels"][0]["volts_per_div"] == 0.5
      and all(9.5 < a / b < 10.5 for a, b in ((mx["vmax"], 0.304), (mx["vpp"], 0.316), (mx["amp"], 0.316), (mx["vrms"], 0.208), (mx["avg"], 0.148))),
      str(mx))
check("scope: 10 ms per division is index 18", fnlink.seconds_per_div(18) == 0.01 and fnlink.seconds_per_div(17) == 0.005 and fnlink.seconds_per_div(19) == 0.02)
check("status: the three of them on the line of each channel",
      "Vmax 304mV, Vmin -12mV, Vpp 316mV, Amp 316mV, Vrms 208mV, Avg 148mV" in text and "STOP, 200us/div" in text
      and "channels CH1, trigger Auto rising CH1" in text, text)
text = "\n".join(fnlink.status_lines(2, IDLE, STEP_AC, STOPPED, bytes.fromhex("0100000050c30000321e")))
check("status: CH1's coupling on the scope line, and the first waveform of the menu by its name",
      "CH1 50mV/div X1 AC, CH2 100mV/div X1 AC, channels CH1" in text and "Sine Wave (index 0), 50kHz" in text, text)

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

# ---------------------------------------------------------------- snapshot and watch

check("range: ADDR:LEN in hex or decimal", fnlink.parse_range("0x2000013D:1") == (0x2000013D, 1) and fnlink.parse_range("536871168:0x38") == (0x20000100, 56))
for bad in ("0x20000100", "0x20000100:0", "x:4", "1:2:3"):
    try:
        fnlink.parse_range(bad)
        check("range: %r is refused" % bad, False)
    except fnlink.LinkError:
        check("range: %r is refused" % bad, True)
check("private: the calibration page in flash and the tables in RAM, to the byte",
      [fnlink.private_region(a, n) is not None for a, n in ((0x08005FFF, 1), (0x08005FFF, 2), (0x08006FFF, 1), (0x08007000, 4),
                                                           (0x20000357, 1), (0x20000357, 2), (0x2000044A, 1), (0x2000044B, 1),
                                                           (fnlink.ARROWS_ADDR, 1), (0x2000044E, 602))]
      == [False, True, True, False, False, True, True, False, False, False])
check("arrows: the three modes seen on the unit, the channel bit, and a value never seen",
      fnlink.arrows_text(0x00).startswith("scale: left/right change the time base") and fnlink.arrows_text(0x02).startswith("move:")
      and fnlink.arrows_text(0x01).startswith("position: left/right move the trace sideways, up/down the channel's trace")
      and ", acting on CH2" in fnlink.arrows_text(0x80) and fnlink.arrows_text(0x07).startswith("mode 7 (unknown)"))
t0 = [(0.0, b"\x05"), (0.010, b"\x06"), (0.020, b"\x07"), (0.030, b"\x08"), (0.040, b"\x09")]
text = "\n".join(fnlink.watch_summary(t0, 400, 1.0, 1))
check("watch summary: a counter's period from its +1 steps", "changes   4" in text and "one count every 10.00 ms" in text and "4 of +1, 0 other" in text
      and "3 changes in 0.030 s: one every 10.00 ms" in text and "median 10.0 ms" in text, text)
wraps = [(0.01 * k, bytes([k % 3])) for k in range(8)]             # 0 1 2 0 1 2 0 1: a counter that wraps
text = "\n".join(fnlink.watch_summary(wraps, 500, 1.0, 1))
check("watch summary: a counter that wraps still gives its rate, not a plain-counter period",
      "6 changes in 0.060 s: one every 10.00 ms" in text and "counter" not in text, text)
text = "\n".join(fnlink.watch_summary([(0.0, b"\x00"), (0.5, b"\x0b"), (0.7, b"\x00")], 100, 1.0, 1))
check("watch summary: no period claimed for what is not a plain counter", "counter" not in text and "1 of +1" not in text and "changes   2" in text, text)
check("watch summary: nothing changed", fnlink.watch_summary([(0.0, b"\x00")], 10, 1.0, 1)[1] == "changes   0")

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

# ---- the generator's table (patch 4): what the host tool sends and what the instrument stores ----
shapes = {name: fnlink.wave_shape(name, 25.0, [(1, 1.0, 0.0), (3, 0.33, 0.0)]) for name in fnlink.WAVE_SHAPES}
check("wave: every built-in shape is 100 samples from 0 to 4094",
      all(len(v) == 100 and min(v) == 0 and max(v) == 4094 and all(isinstance(x, int) for x in v) for v in shapes.values()))
sine = fnlink.wave_shape("sine")
check("wave: the sine is stock's own table to within one count",
      sine[0] in (2047, 2048) and sine[25] == 4094 and sine[75] == 0 and abs(sine[10] - 3250) <= 1, str(sine[:3]))
check("wave: pulse and triangle follow the duty", sum(v == 4094 for v in fnlink.wave_shape("pulse", 25.0)) == 25
      and fnlink.wave_shape("triangle", 25.0).index(4094) == 25 and fnlink.wave_shape("triangle", 50.0).index(4094) == 50)
check("wave: what the instrument stores is stock's scaling, never above stock's top",
      fnlink.wave_stored([0, 4094, 9999, 2047], 30) == [20, 3741, 3741, 1880]
      and fnlink.wave_stored([4094], 10) == [1260] and fnlink.wave_stored([4094], 0) == [20] and fnlink.wave_stored([4094], 255) == [3741])
check("wave: any numbers are stretched from lowest to highest, a flat list stays at mid scale",
      fnlink.wave_normalise([-1.5] * 50 + [3.0] * 50) == [0] * 50 + [4094] * 50 and fnlink.wave_normalise([7] * 100) == [2047] * 100)
four = fnlink.wave_resample([0, 1, 0, -1])
check("wave: four points become a hundred, around the period", len(four) == 100 and four[0] == 0 and four[25] == 1
      and four[75] == -1 and abs(four[99] + 0.04) < 1e-9 and abs(four[12] - 0.48) < 1e-9)
check("wave: a file with comments, a header and mixed separators", fnlink.parse_wave_text(
    "# one period\nvolts;x\n0, 1.5\n-2e-1;3\n\n4 5\n") == [0.0, 1.5, -0.2, 3.0, 4.0, 5.0])
check("wave: harmonics as ORDER:LEVEL[:DEGREES]", fnlink.parse_harmonics("1:1, 3:0.33:90") == [(1, 1.0, 0.0), (3, 0.33, 90.0)])
for bad, fn in (("99 points", lambda: fnlink.wave_normalise([1] * 99)), ("NaN", lambda: fnlink.wave_normalise([float("nan")] * 100)),
                ("one point", lambda: fnlink.wave_resample([1])), ("a word among the numbers", lambda: fnlink.parse_wave_text("1\n2\n4 y")),
                ("harmonic 0", lambda: fnlink.parse_harmonics("0:1")), ("harmonic 26", lambda: fnlink.parse_harmonics("26:1")),
                ("a harmonic without a level", lambda: fnlink.parse_harmonics("3")),
                ("an unknown shape", lambda: fnlink.wave_shape("heart")), ("harmonics without a list", lambda: fnlink.wave_shape("harmonics"))):
    try:
        got = fn()
        refused = False
    except fnlink.LinkError:
        got, refused = None, True
    check("wave refused: " + bad, refused, repr(got))


class _Recorder:
    """A transport that answers WAVE as patch 4 does and keeps the table."""

    def __init__(self, features=3, amplitude=30):
        self.table, self.features, self.amplitude, self.sent, self.queue = [0] * 100, features, amplitude, [], []

    def write(self, rq):
        self.sent.append(bytes(rq))
        cmd, seq, n = rq[0], rq[1], rq[2]
        addr = struct.unpack_from("<I", rq, 4)[0]
        rs = bytearray(64)
        rs[0], rs[1] = cmd | 0x80, seq
        rs[4:8] = rq[4:8]
        if cmd == 1:
            rs[8:16] = b"2C53LINK"
            rs[16], rs[17], rs[18], rs[44], rs[3] = 1, 4 if self.features & 2 else 3, 3, self.features, 40
        elif cmd == 4:
            vals = struct.unpack_from("<%dH" % n, rq, 8)
            self.table[addr:addr + n] = fnlink.wave_stored(vals, self.amplitude)
            rs[8], rs[9], rs[3] = self.amplitude, 1, 2
        elif cmd == 2:
            mem = struct.pack("<100H", *self.table) if addr >= fnlink.WAVE_ADDR else bytes([1, 1, 0, 0, 0xE8, 3, 0, 0, 50, self.amplitude]) + bytes(200)
            base = fnlink.WAVE_ADDR if addr >= fnlink.WAVE_ADDR else fnlink.GENERATOR_ADDR
            rs[8:8 + n], rs[3] = mem[addr - base:addr - base + n], n
        self.queue.append(bytes(rs))

    def read(self, timeout_ms):
        return self.queue.pop(0) if self.queue else None


rec = _Recorder()
r = fnlink.Link(rec).wave(shapes["sinc"])
waves = [x for x in rec.sent if x[0] == 4]
check("wave: a table goes out as four requests of 28, 28, 28 and 16 samples at positions 0, 28, 56, 84",
      [(x[2], struct.unpack_from("<I", x, 4)[0]) for x in waves] == [(28, 0), (28, 28), (28, 56), (16, 84)]
      and r == {"amplitude": 30, "waveform_index": 1} and rec.table == fnlink.wave_stored(shapes["sinc"], 30))
check("wave: the tool writes, reads back and reports", fnlink.main(["wave", "gauss", "--invert"], transport=_Recorder(amplitude=20)) == 0)
old_patch = _Recorder(features=1)
check("wave: a link without the feature bit is told so and nothing is sent", fnlink.main(["wave", "sine"], transport=old_patch) == 2
      and not [x for x in old_patch.sent if x[0] == 4])
check("wave --show works on any patch", fnlink.main(["wave", "--show"], transport=_Recorder(features=1)) == 0)


# ---- keys: the log of what changes while the instrument is worked by hand ---------------------
class _Memory:
    """A transport that answers READ from a few regions of RAM, which change between rounds of
    reads as a script says: {round: [(address, byte), ...]}. Nothing else is answered."""

    def __init__(self, script):
        self.script, self.rounds, self.queue, self.requests = script, 0, [], []
        self.mem = {fnlink.GENERATOR_ADDR: bytearray(bytes.fromhex("01010000e8030000321e")),
                    fnlink.METER_STATE_ADDR: bytearray(56), fnlink.UI_ADDR: bytearray(bytes.fromhex("05640a00010004640300000000000000")),
                    fnlink.ARROWS_ADDR: bytearray(1), fnlink.SCOPE_STATE_ADDR: bytearray(56)}

    def write(self, rq):
        cmd, seq, n = rq[0], rq[1], rq[2]
        addr = struct.unpack_from("<I", rq, 4)[0]
        self.requests.append((cmd, addr, n))
        if addr == fnlink.GENERATOR_ADDR:               # the first block of a round: apply what the script has for it
            for at, value in self.script.get(self.rounds, []):
                base = max(b for b in self.mem if b <= at)
                self.mem[base][at - base] = value
            self.rounds += 1
        rs = bytearray(64)
        rs[0], rs[1], rs[3] = cmd | 0x80, seq, n
        rs[4:8] = rq[4:8]
        base = max(b for b in self.mem if b <= addr)
        rs[8:8 + n] = self.mem[base][addr - base:addr - base + n]
        self.queue.append(bytes(rs))

    def read(self, timeout_ms):
        return self.queue.pop(0) if self.queue else None


script = {1: [(fnlink.GENERATOR_ADDR + 1, 0)],                                            # up arrow: the previous waveform
          2: [(fnlink.METER_STATE_ADDR + 8, 0x7F), (fnlink.METER_STATE_ADDR + 5, 1)],     # a reading moves, and the menu entry
          3: [(fnlink.UI_ADDR + 8, 1), (fnlink.UI_ADDR + 9, 1), (fnlink.ARROWS_ADDR, 2)]}
mem = _Memory(script)
with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / "keys.txt"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fnlink.main(["keys", "--polls", "4", "--interval", "0", "--label", "test", "--out", str(log)], transport=mem)
    printed, saved = buf.getvalue().splitlines(), log.read_text().splitlines()
changes = [x.split(" s  ", 1)[1] for x in printed if " s  " in x]
check("keys: reads the five blocks each round, and nothing but reads", rc == 0 and mem.rounds == 5
      and {r[0] for r in mem.requests} == {2} and len(mem.requests) == 5 * 5, str(mem.requests[:6]))
check("keys: a change of waveform is a line with what the generator now says",
      any(x.startswith("generator [1] 01>00") and "Sine Wave (index 0), 1kHz, duty 50 %, 3.0 V" in x for x in changes), str(changes))
check("keys: of the meter's state only the menu bytes are reported, not the reading",
      [x for x in changes if x.startswith("meter")] == ["meter     [5] 00>01"], str(changes))
check("keys: the menus' cursors are given as offsets of stock's state, with the system mode when it changes",
      any(x.startswith("ui        [0xF68] 03>01 [0xF69] 00>01") and "system mode 1 (multimeter)" in x for x in changes)
      and any(x.startswith("arrows    [0] 00>02") and "move:" in x for x in changes), str(changes))
check("keys: the log saved is what was printed, the starting blocks first",
      saved[:len(printed) - 1] == printed[:-1] and printed[1].startswith("20000F50   10  01010000e8030000321e")
      and printed[0].startswith("keys      ") and printed[0].endswith("test") and len(changes) == 4, str(printed[:8]))
mem = _Memory({1: [(fnlink.UI_ADDR + 15, 0x2E)], 2: [(fnlink.UI_ADDR + 9, 1)]})
with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / "guided.txt"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fnlink.main(["keys", "--guide", "azul, abajo,menu corto", "--step", "0.05", "--interval", "0.005", "--out", str(log)], transport=mem)
    guided, saved = buf.getvalue().splitlines(), log.read_text()
asked = [x.split("press     ", 1)[1] for x in guided if "  press     " in x]
check("keys --guide: asks for each key in turn, ends by itself, and the bell stays out of the file",
      rc == 0 and [a.rstrip("\a") for a in asked] == ["azul", "abajo", "menu corto"] and all(a.endswith("\a") for a in asked)
      and "\a" not in saved and saved.count("  press     ") == 3 and guided[-2].startswith("end "), str(guided[-6:]))
check("keys: the byte of the ui block that moves by itself is left out, the cursor next to it is not",
      [x.split(" s  ", 1)[1] for x in guided if " s  ui" in x] == ["ui        [0xF69] 00>01"], str(guided))


class _Dropping(_Memory):
    """_Memory standing in for the HID transport: it stops answering after some requests, cannot be
    reopened twice, and then answers again, as the instrument does while a menu is up."""
    state = {}

    def __init__(self, script=None):
        if script is None:                               # the tool opening the link again
            _Dropping.state["opens"] += 1
            if _Dropping.state["opens"] <= 2:
                raise fnlink.LinkError("no 1209:0001 device")
            self.__dict__.update(_Dropping.state["first"].__dict__)
            self.queue = []
        else:
            super().__init__(script)
            _Dropping.state = {"opens": 0, "first": self, "writes": 0}

    def write(self, rq):
        _Dropping.state["writes"] += 1
        if 12 <= _Dropping.state["writes"] < 14 and _Dropping.state["opens"] == 0:
            raise OSError("not ready")
        super().write(rq)

    def close(self):
        pass


real_hid, fnlink.HidTransport = fnlink.HidTransport, _Dropping
try:
    drop = _Dropping({})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fnlink.main(["keys", "--guide", "ch1,menu corto,ch2,menu corto", "--step", "0.6", "--interval", "0.01"], transport=drop)
    dropped = [x.replace("\a", "") for x in buf.getvalue().splitlines()]
finally:
    fnlink.HidTransport = real_hid
lost = [i for i, x in enumerate(dropped) if "  link      lost" in x]
back = [i for i, x in enumerate(dropped) if "  link      back" in x]
between = [x.split("press     ", 1)[1] for x in dropped[lost[0]:back[0]] if "  press     " in x] if lost and back else []
check("keys: a link that goes away is picked up again, and the keys go on being asked for meanwhile",
      rc == 0 and len(lost) == 1 and len(back) == 1 and lost[0] < back[0] and between
      and [x.split("press     ", 1)[1] for x in dropped if "  press     " in x] == ["ch1", "menu corto", "ch2", "menu corto"], str(dropped))
check("keys: an unchanged block gives no line", fnlink.keys_changes("ui", b"\x01\x02", b"\x01\x02", None, 0xF60) == []
      and fnlink.keys_changes("meter", bytes(8), bytes([0, 0, 0, 0, 0, 0, 0, 9]), fnlink.KEYS_METER_BYTES, 0) == [])
for name, samples in (("101 samples", [0] * 101), ("a sample of 4095", [4095] * 100), ("a negative sample", [-1] + [0] * 99)):
    rec = _Recorder()
    try:
        fnlink.Link(rec).wave(samples)
        refused = False
    except fnlink.LinkError:
        refused = True
    check("wave refused before sending: " + name, refused and not rec.sent)

# ---- the generator's menu (patch 5): what the builder accepts ----------------------------------
# ---- tasks: FreeRTOS control blocks found in a block of RAM ----------------------------------
def made_up_heap():
    base = 0x20006000
    block = bytearray(0x400)

    def task(stack, size, name, priority, used_now, deepest):
        tcb = stack + size + 8
        struct.pack_into("<I", block, tcb - base, tcb - 8 - used_now)
        struct.pack_into("<II", block, tcb - base + 0x2C, priority, stack)
        block[tcb - base + 0x34:tcb - base + 0x34 + len(name)] = name
        for a in range(tcb - 8 - deepest, tcb - 8):
            block[a - base] = 0x5A
        return tcb

    task(0x20006010, 0x100, b"display", 1, 0x40, 0x90)
    task(0x20006180, 0x80, b"key", 4, 0x30, 0x30)
    struct.pack_into("<I", block, 0x300, 0x20006320)        # a pointer into the block that is no task
    return base, bytes(block)


heap_base, heap = made_up_heap()
found = fnlink.find_tasks(heap, heap_base)
check("tasks: both control blocks found and nothing else, with name, priority, stack and size",
      [(t["name"], t["priority"], t["stack"], t["size"]) for t in found]
      == [("display", 1, 0x20006010, 0x100), ("key", 4, 0x20006180, 0x80)], str(found))
check("tasks: what is in use now and what was never reached",
      [(t["in_use"], t["untouched"]) for t in found] == [(0x40, 0x100 - 0x90), (0x30, 0x80 - 0x30)], str(found))
check("tasks: an empty block has none", fnlink.find_tasks(bytes(0x400), heap_base) == [])


# The guided key logs kept under sessions/: what the notes say each key did, held against the logs.
def guided(name: str) -> list:
    """(key asked for, [(block, offset, old, new), ...]) for each prompt of the last run in a guided log."""
    text = (Path(__file__).resolve().parent.parent / "sessions" / "2026-11-26_keys" / name).read_text()
    out = []
    for line in text.split("\nkeys      ")[-1].split("\n"):
        m = re.match(r"\s*[\d.]+ s  (\w+)\s+(.*)", line)
        if not m:
            continue
        if m.group(1) == "press":
            out.append((m.group(2).strip(), []))
        elif m.group(1) == "link":
            out[-1][1].append(("link", m.group(2).split()[0], "", ""))
        else:
            for off, old, new in re.findall(r"\[(?:0x)?([0-9A-Fa-f]+)\] ([0-9a-f]{2})>([0-9a-f]{2})", m.group(2)):
                out[-1][1].append((m.group(1), off, old, new))
    return out


def first(changes: list, block: str, off: str):
    hits = [(o, n) for b, f, o, n in changes if b == block and f == off]
    return hits[0] if hits else None


g = guided("guided_multimeter.txt")
check("guided multimeter: twenty keys, and the ones that changed nothing",
      len(g) == 20 and [k for k, c in g if not c] == ["select", "trigger", "prm", "save", "save", "ch1", "ch2", "arriba", "abajo", "menu corto"])
check("guided multimeter: right and left step the menu entry and wrap both ways, 0 1 2 1 0 7 0 1",
      [first(c, "meter", "5") for k, c in g[:7]] == [("00", "01"), ("01", "02"), ("02", "01"), ("01", "00"), ("00", "07"), ("07", "00"), ("00", "01")]
      and [k for k, c in g[:7]] == ["derecha", "derecha", "izquierda", "izquierda", "izquierda", "derecha", "derecha"])
check("guided multimeter: DC to AC after MOVE, Automatic after AUTO, HOLD after the blue key",
      dict(g)["move"] == [("meter", "6", "01", "02"), ("meter", "14", "01", "02")] and first(dict(g)["auto"], "meter", "5") == ("01", "00")
      and dict(g)["azul"] == [("meter", "20", "00", "01")])
g = guided("guided_generator.txt")
check("guided generator: the level ms[0xF69] goes down with the blue key and up with MENU, and MENU does nothing at 0",
      [(k, first(c, "ui", "F69")) for k, c in g if k in ("azul", "menu corto")]
      == [("menu corto", None), ("menu corto", None), ("azul", ("00", "01")), ("azul", ("01", "02")), ("menu corto", ("02", "01")), ("menu corto", ("01", "00"))])
check("guided generator: up and down by level: the waveform, ms[0xF6B], the frequency; right and left logged nothing",
      [c[0] for k, c in g if k in ("arriba", "abajo")]
      == [("generator", "1", "10", "11"), ("generator", "1", "11", "10"), ("ui", "F6B", "00", "02"), ("ui", "F6B", "02", "00"),
          ("generator", "4", "d0", "d1"), ("generator", "4", "d1", "d0")]
      and all(not c for k, c in g if k in ("derecha", "izquierda")))
g = guided("guided_oscilloscope.txt")
check("guided oscilloscope: SELECT takes the arrows' mode 0, 1, 2, 0",
      [c for k, c in g if k == "select"] == [[("arrows", "0", "00", "01")], [("arrows", "0", "01", "02")], [("arrows", "0", "02", "00")]])
check("guided oscilloscope: mode 0, right and left the time base, up and down the range",
      [(k, first(c, "scope", "45") or first(c, "scope", "2")) for k, c in g[0:4]]
      == [("derecha", ("09", "08")), ("izquierda", ("08", "09")), ("arriba", ("02", "03")), ("abajo", ("03", "02"))])
check("guided oscilloscope: mode 1, right and left the horizontal position, up and down CH1's position with the trigger level",
      [(k, first(c, "scope", "26") or first(c, "scope", "4"), first(c, "scope", "28")) for k, c in g[5:9]]
      == [("derecha", ("00", "01"), None), ("izquierda", ("01", "00"), None), ("arriba", ("b1", "b2"), ("f7", "f8")), ("abajo", ("b2", "b1"), ("f8", "f7"))])
check("guided oscilloscope: mode 2, right and left the horizontal position, up and down the trigger level alone",
      [(k, first(c, "scope", "26") or first(c, "scope", "28"), first(c, "scope", "4")) for k, c in g[10:14]]
      == [("derecha", ("00", "01"), None), ("izquierda", ("01", "00"), None), ("arriba", ("f7", "f8"), None), ("abajo", ("f8", "f7"), None)])
check("guided oscilloscope: MOVE logged nothing; CH1, CH2, TRIGGER and PRM lose the link and MENU brings it back",
      [(k, [x[:2] for x in c]) for k, c in g[15:25]]
      == [("move", []), ("move", [])] + [(k, [("link", "lost")]) if k != "menu corto" else (k, [("link", "back")])
                                         for k in ("ch1", "menu corto", "ch2", "menu corto", "trigger", "menu corto", "prm", "menu corto")])
check("guided oscilloscope: the blue key stops and runs, and AUTO's steps with CH1 alone",
      [c for k, c in g if k == "azul"] == [[("scope", "46", "01", "00")], [("scope", "46", "00", "01")]]
      and [n for k, c in g[27:] for b, f, o, n in c if f == "47"] == ["01", "02", "03", "11", "12", "13", "ff", "00"])

g = guided("guided_generator_digit.txt")
check("guided digit: at the third level left and right move the digit, the high nibble of ms[0xF6B]",
      [(k, first(c, "ui", "F6B")) for k, c in g if k in ("izquierda", "derecha")]
      == [("izquierda", ("00", "10")), ("izquierda", ("10", "20")), ("derecha", ("20", "10")), ("derecha", ("10", "00"))])
check("guided digit: up and down change the frequency by 10, by 100 and by 1 according to the digit",
      [c for k, c in g if k in ("arriba", "abajo")]
      == [[("generator", "4", "e8", "f2")], [("generator", "4", "f2", "e8")],
          [("generator", "4", "e8", "4c"), ("generator", "5", "03", "04")], [("generator", "4", "4c", "e8"), ("generator", "5", "04", "03")],
          [("generator", "4", "e8", "e9")], [("generator", "4", "e9", "e8")]])
g = guided("guided_multimeter_move.txt")
check("guided MOVE: it switches the variant inside the entry: DC and AC voltage, continuity and diode",
      [c for k, c in g if k == "move"]
      == [[("meter", "6", "01", "02"), ("meter", "14", "01", "02")], [("meter", "6", "02", "01"), ("meter", "14", "02", "01")],
          [("meter", "6", "03", "04"), ("meter", "14", "02", "01")], [("meter", "6", "04", "03"), ("meter", "14", "01", "02")]]
      and first(dict(g)["auto"], "meter", "5") == ("02", "00") and all(not c for k, c in g if k == "espera"))
g = guided("guided_oscilloscope_ch2.txt")
check("guided CH2: with both channels on MOVE switches bit 7 of the arrows' byte, in each of the three modes",
      [c for k, c in g if k == "move"]
      == [[("arrows", "0", a, b)] for a, b in (("00", "80"), ("80", "00"), ("01", "81"), ("81", "01"), ("02", "82"), ("82", "02"))]
      and fnlink.arrows_text(0x81).endswith("acting on CH2 (byte 0x81; SELECT changes it)"))
check("guided CH2: up and down act on the channel of that bit: range and position of CH1, then of CH2",
      [(k, c[0][1:]) for k, c in g if k in ("arriba", "abajo")][:8]
      == [("arriba", ("2", "02", "03")), ("abajo", ("2", "03", "02")), ("arriba", ("3", "00", "01")), ("abajo", ("3", "01", "00")),
          ("arriba", ("4", "b6", "b7")), ("abajo", ("4", "b7", "b6")), ("arriba", ("5", "d3", "d4")), ("abajo", ("5", "d4", "d3"))])
check("guided CH2: the trigger level follows CH1, the trigger's channel, and not CH2; in mode 2 it moves with either",
      [first(c, "scope", "28") for k, c in g if k in ("arriba", "abajo")]
      == [("01", "db"), ("db", "00"), None, None, ("00", "01"), ("01", "00"), None, None, ("00", "01"), ("01", "00"), ("00", "01"), ("01", "00")])
check("guided CH2: AUTO with both channels, the steps and the range of CH1 going up to 6",
      [n for k, c in g[23:] for b, f, o, n in c if f == "47"] == ["01", "02", "03", "11", "21", "32", "f3", "ff", "00"]
      and [n for k, c in g[23:] for b, f, o, n in c if f == "2"] == ["00", "01", "02", "03", "04", "05", "06"]
      and first(g[23][1], "scope", "49") == ("00", "11"))


# cal-check and cal-ram: a made-up page, never a unit's. CH1's values are 1000 + 10 i and CH2's 2000 + 10 i,
# with three of them spoilt.
made = bytearray(b"\xFF" * fnlink.CAL_LEN)
made[0] = 0x55
ch1 = [1000 + 10 * i for i in range(60)]
ch2 = [2000 + 10 * i for i in range(60)]
ch1[30] = 0xDC5B            # an upper above 12 bits: bank 0 range 0
ch2[14] = 0                 # a baseline: bank 1 range 4
ch2[44] = 1                 # its upper, below nothing but that baseline: still a pair in order
ch2[59] = 0xFFFF            # the last word's high half
for i in range(60):
    struct.pack_into("<I", made, 0x038 + 4 * i, ch2[i] << 16 | ch1[i])
made[0x128:0x130] = bytes([0xFF, 1, 2, 3, 4, 5, 6, 7])
lines = fnlink.cal_check_lines(bytes(made))
check("cal-check: the signature, the last word, and each spoilt value by its place",
      lines[0].startswith("signature    0x55") and lines[1] == "last word    CH2's half is 0xFFFF"
      and "CH1          59 of 60 values are 1 to 4095" in lines and "  above 4095 1: upper bank 0 range 0" in lines
      and "CH2          58 of 60 values are 1 to 4095" in lines and "  0          1: baseline bank 1 range 4" in lines
      and "  0xFFFF     1: upper bank 2 range 9" in lines, "\n".join(lines))
check("cal-check: the pairs in order, and the eight bytes after the sixty words",
      "  upper above its baseline in 30 of 30 pairs" in lines and lines[-1] == "after them   7 of the 8 bytes at 0x128-0x12F are not 0xFF", "\n".join(lines))
check("cal-check: no value of the table in what it prints",
      not any(str(v) in "\n".join(lines) or "%X" % v in "\n".join(lines).upper().replace("0XFFFF", "") for v in (1010, 2010, 0xDC5B, 1590)), "\n".join(lines))
mirror = list(ch1) + list(ch2)
mirror[30] = 0xDC5B & 0x0FFF
mirror[60 + 3] = 7
lines = fnlink.cal_ram_lines(bytes(made), struct.pack("<120H", *mirror))
check("cal-ram: what RAM has against the page: the same, the low 12 bits, or something else, by place",
      lines == ["CH1          59 of 60 values in RAM are the page's", "  its low 12 bits 1: upper bank 0 range 0",
                "CH2          59 of 60 values in RAM are the page's", "  something else  1: baseline bank 0 range 3"], "\n".join(lines))
mem = _Memory({})
mem.mem[fnlink.CAL_RAM_ADDR] = bytearray(struct.pack("<120H", *mirror))
with tempfile.TemporaryDirectory() as tmp:
    dump = Path(tmp) / "made_up_page.bin"
    dump.write_bytes(bytes(made))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fnlink.main(["cal-ram", str(dump)], transport=mem)
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = fnlink.main(["cal-check", str(dump)])
check("cal-ram: reads the 240 bytes twice and nothing else, and prints the comparison",
      rc == 0 and {r[0] for r in mem.requests} == {2} and sum(r[2] for r in mem.requests) == 480
      and all(fnlink.CAL_RAM_ADDR <= r[1] < fnlink.CAL_RAM_ADDR + 240 for r in mem.requests)
      and buf.getvalue().splitlines()[1:5] == lines, buf.getvalue())
check("cal-check: the command, on a file, without a device",
      rc2 == 0 and "signature    0x55 (first byte of the page)" in buf2.getvalue().splitlines()
      and buf2.getvalue().rstrip().endswith("no value of the table is printed)"), buf2.getvalue())
try:
    fnlink.cal_ram_lines(bytes(made), bytes(100))
    check("cal-ram: a copy of the wrong size is refused", False)
except fnlink.LinkError:
    check("cal-ram: a copy of the wrong size is refused", True)


import build_link_image as builder  # noqa: E402

# ---- --texts: where the builder puts the texts -------------------------------------------------
placed = [{"text": "Tensión continua"}, {"text": "Diodo"}, {"text": "Tensión continua"}]
with_texts = builder.place_texts(b"\x70\x47" * 5, placed)
check("texts: placed after the code, one copy of each, NUL-terminated, the cave still a multiple of 4",
      with_texts == b"\x70\x47" * 5 + "Tensión continua".encode("utf-8") + b"\0Diodo\0" + b"\0" * 2
      and [t["at"] - builder.CAVE_ADDR for t in placed] == [10, 28, 10], str(placed))

# ---- --menu-names: a C literal for a name that is not ASCII -------------------------------------
check("menu names: non-ASCII as hex escapes, and the literal closed before a hex digit that follows one",
      builder.c_literal("R\u00e1faga de 10") == "R\\xC3\\xA1\"\"faga de 10" and builder.c_literal("Onda USB") == "Onda USB"
      and builder.c_literal("a\u00f1o") == "a\\xC3\\xB1o", builder.c_literal("R\u00e1faga de 10"))
named = builder.parse_menu(["Gauss Pulse=gauss@12"], "USB Wave@0")
builder.parse_menu_names([], named, b"")
check("menu names: none given, no entry has any and the header is as before",
      all(not e.get("names") for e in named) and "MENU_TRANSLATED" not in builder.menu_header(named))
named[0]["names"] = {"es": "Campana de Gauss"}
named[1]["names"] = {}
header = builder.menu_header(named)
check("menu names: the header has a row of pointers for each of the eight languages, null where there is no name",
      "#define MENU_TRANSLATED 1" in header and header.count("{ 0, 0 }") == 7 and '{ "Campana de Gauss", 0 }' in header, header[-400:])

menu = builder.parse_menu(["Triangle 25/75=triangle:25@2", "Gauss Pulse=gauss@12", "Sum=harmonics:1:1,3:0.33"], "USB Wave@0")
check("menu: entries take the indexes after stock's thirteen, in order, the USB one last",
      [(m["index"], m["label"], m["icon"], m["source"]) for m in menu] == [
          (13, "Triangle 25/75", 2, "triangle:25"), (14, "Gauss Pulse", 12, "gauss"), (15, "Sum", 0, "harmonics:1:1,3:0.33"),
          (16, "USB Wave", 0, "usb")])
check("menu: a shape is the one fnlink sends, its argument included", menu[0]["samples"] == fnlink.wave_shape("triangle", 25.0)
      and menu[1]["samples"] == fnlink.wave_shape("gauss") and menu[3]["samples"] is None
      and menu[2]["samples"] == fnlink.wave_shape("harmonics", harmonics=[(1, 1.0, 0.0), (3, 0.33, 0.0)]))
header = builder.menu_header(menu)
check("menu: the generated header has the counts, the names and one table per built-in entry",
      "#define MENU_EXTRA      4u" in header and "#define MENU_BAKED      3" in header and "#define MENU_USB_INDEX  16u" in header
      and '"Triangle 25/75", "Gauss Pulse", "Sum", "USB Wave"' in header and header.count("    { ") == 3)
check("menu: with only the USB entry there is still a (dummy) table to compile",
      "menu_wave_data[1][100]" in builder.menu_header(builder.parse_menu(None, "USB")))
check("menu: no option, no entries", builder.parse_menu(None, None) == [])
for bad, waves, usb in (("no shape", ["Label"], None), ("an unknown shape", ["A=heart"], None), ("a label of 18 characters", ["A" * 18 + "=sine"], None),
                        ("an empty label", ["=sine"], None), ("a label that is not ASCII", ["Señal=sine"], None), ("a quote in a label", ['A"=sine'], None),
                        ("a picture past stock's thirteen", ["A=sine@13"], None), ("a picture that is not a number", ["A=sine@x"], None),
                        ("nine entries", ["A%d=sine" % k for k in range(9)], None), ("eight and the USB one", ["A%d=sine" % k for k in range(8)], "USB"),
                        ("a bad USB label", None, "USB\\")):
    try:
        builder.parse_menu(waves, usb)
        refused = False
    except builder.BuildError:
        refused = True
    check("menu refused: " + bad, refused)

print("%d checks, %d failed" % (checks, failures))
sys.exit(1 if failures else 0)
