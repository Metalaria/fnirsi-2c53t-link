#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host side of stock-link v1: talk to a FNIRSI 2C53T running the stock-link image.

    python fnlink.py info
    python fnlink.py dump-cal  cal_0x08006000.bin
    python fnlink.py verify    APP_2C53T_V1.2.0_link.bin
    python fnlink.py read      0x20001060 16
    python fnlink.py meter                      # live multimeter display, Ctrl-C to stop
    python fnlink.py scope     captures/        # save oscilloscope records, Ctrl-C to stop

Needs the hidapi bindings:  python -m pip install hidapi
The instrument must be in a measurement mode (multimeter, oscilloscope or generator) with
"USB Sharing" off; the link takes a few seconds to appear after power-on or a mode change.

Nothing here writes to the instrument's memory. One 64-byte report each way:
  request   cmd, seq, len, 0, addr(le32)
  response  cmd|0x80, seq, status, n, addr(le32), payload[n]
  event     type, seq, kind/channel, n, tick or offset(le32), payload[n]   (after `STREAM`)
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import struct
import sys
import time
import zlib
from pathlib import Path

VID, PID = 0x1209, 0x0001
REPORT_LEN = 64
PAYLOAD_MAX = 56
CMD_INFO, CMD_READ, CMD_STREAM, CMD_WAVE = 0x01, 0x02, 0x03, 0x04
EVT_METER, EVT_SCOPE_HEADER, EVT_SCOPE_DATA, EVT_STATUS = 0x90, 0xA0, 0xA1, 0xB0
EVENTS = (EVT_METER, EVT_SCOPE_HEADER, EVT_SCOPE_DATA, EVT_STATUS)
STREAM_METER, STREAM_SCOPE, STREAM_STATUS = 0x01, 0x02, 0x04
FEATURE_STATUS = 0x01                   # INFO feature bit: the link has the status stream (patch 3)
FEATURE_WAVE = 0x02                     # INFO feature bit: the link can store the generator's table (patch 4)
FEATURE_MENU = 0x04                     # INFO feature bit: entries were added to the generator's menu (patch 5)
STOCK_WAVEFORMS = 13                    # entries 0..12 of the generator's list are stock's
# Sub-kinds of a status event: which block of stock's SRAM it carries.
STATUS_GENERATOR, STATUS_SCOPE_CH1, STATUS_SCOPE_CH2, STATUS_METER = 1, 2, 3, 4
STATUS = {0: "ok", 1: "unknown command", 2: "address range not readable", 3: "bad length"}

# The signal generator's table (code: stock plays these 100 samples through a circular DMA into the
# DAC and rewrites them at power-on and when the waveform, the duty or the amplitude is changed, not
# when the frequency is). Patch 4 stores samples there, scaled as stock scales its own.
WAVE_ADDR = 0x20000F5A                  # state[0xE62]
WAVE_POINTS = 100
WAVE_FULL_SCALE = 4094                  # top of stock's own sine table
WAVE_CHUNK = PAYLOAD_MAX // 2           # samples of 16 bits in one request
WAVE_AMPLITUDE_MAX = 30                 # tenths of a volt: stock's menu stops at 3.0 V

APP_BASE = 0x08007000
CAL_ADDR, CAL_LEN = 0x08006000, 0x1000
SCOPE_RECORD_LEN = 1024
MODES = {1: "multimeter", 2: "oscilloscope", 3: "signal generator", 7: "USB Sharing"}

# Command word stock last sent to the meter chip (docs/board_revisions.md in OpenScope).
METER_FUNCTIONS = {
    0x14: "Auto", 0x0C: "DC V", 0x0D: "AC V", 0x0B: "Ohm", 0x0E: "Diode", 0x17: "Continuity",
    0x12: "Temp", 0x11: "DC mA", 0x16: "AC mA", 0x10: "DC A", 0x15: "AC A", 0x0A: "Cap",
}

# Stock's own meter state (unit, 2026-11-25: read in each of the twelve functions).
# state[5] is the entry of the function menu, state[6] what is being measured and
# state[14] the variant inside the entry: 1 or 2 (DC / AC, diode / continuity), 0 in Auto.
METER_MENU = {0: "Automatic", 1: "voltage", 2: "continuity and diode", 3: "resistance", 4: "capacitance",
              5: "temperature", 6: "small current", 7: "large current"}
METER_MEASURING = {0: "nothing yet", 1: "DC Voltage", 2: "AC Voltage", 3: "Continuity", 4: "Diode", 5: "Resistance",
                   6: "Capacitance", 7: "Temperature", 8: "Small DC current", 9: "Small AC current",
                   10: "Large DC current", 11: "Large AC current"}


def buzzer_follows_meter_chip(mode: int, state: bytes) -> bool:
    """Whether stock lets the meter chip's own buzzer tone through to the buzzer.

    Stock's EXTI3 handler (0x08009C10) turns the buzzer on at each edge of that tone only in the
    multimeter (system mode 1) and when the menu entry is 0, or when the entry and the variant
    are both 2. On the unit those are Automatic and Continuity; Diode (2, 1) is not."""
    return mode == 1 and (state[5] == 0 or (state[5] == 2 and state[14] == 2))


# Seven-segment font of the meter chip's display bytes. Segment bits, read off stock's own
# digit table: a=0x80 f=0x40 e=0x20 b=0x08 g=0x04 c=0x02 d=0x01; 0x10 is not part of the glyph.
SEG_A, SEG_F, SEG_E, SEG_B, SEG_G, SEG_C, SEG_D, SEG_EXTRA = 0x80, 0x40, 0x20, 0x08, 0x04, 0x02, 0x01, 0x10
_GLYPHS = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg", "6": "acdefg",
    "7": "abc", "8": "abcdefg", "9": "abcdfg", "A": "abcefg", "u": "cde", "t": "defg", "o": "cdeg",
    "L": "def", "-": "g", " ": "", "C": "adef", "P": "abefg", "E": "adefg", "r": "eg",
}
_SEG = {"a": SEG_A, "b": SEG_B, "c": SEG_C, "d": SEG_D, "e": SEG_E, "f": SEG_F, "g": SEG_G}
SEGMENT_FONT = {sum(_SEG[s] for s in segs): ch for ch, segs in _GLYPHS.items()}


class LinkError(RuntimeError):
    pass


def decode_meter_frame(frame: bytes) -> dict:
    """Display content of a 12-byte `5A A5` frame, as text.

    Bytes 2..6 carry four seven-segment digits, each split across two bytes (high nibble of
    one, low nibble of the next). Byte 2 bit 4 is the minus sign and bit 3 a leading "1".
    Bit 4 of digits 2..4 is the decimal point in front of that digit. With no explicit point,
    byte 8 bit 7 marks the lowest range, shown as x.xxxx. The unit comes from the annunciator
    bits (see frame_unit)."""
    if len(frame) < 12 or frame[0] != 0x5A or frame[1] != 0xA5:
        raise LinkError("not a meter data frame: %s" % frame.hex())
    masks = [(frame[2 + i] & 0xF0) | (frame[3 + i] & 0x0F) for i in range(4)]
    digits = [SEGMENT_FONT.get(m & ~SEG_EXTRA, "?") for m in masks]
    points = [False] + [bool(m & SEG_EXTRA) for m in masks[1:]]
    negative, leading_one = bool(frame[2] & 0x10), bool(frame[2] & 0x08)
    numeric = all(d.isdigit() for d in digits)
    body = "".join(("." if p else "") + d for d, p in zip(digits, points))
    if numeric and not any(points) and frame[8] & 0x80:
        body = ("1" if leading_one else "0") + "." + body       # lowest range: x.xxxx
    elif leading_one:
        body = "1" + body
    text = ("-" if negative else "") + body
    value = None
    if "".join(digits).strip().isdigit():               # blank leading digits are fine ("  23")
        try:
            value = float(text)
        except ValueError:
            value = None
    unit, prefix, continuity, unknown = frame_unit(frame)
    # While the range changes the display can be empty, or show its decimal point alone.
    blank = not text.strip(" .-")
    return {"text": text, "value": value, "blank": blank, "digits": "".join(digits), "masks": masks, "negative": negative,
            "leading_one": leading_one, "flags": frame[7], "range": frame[8], "tail": frame[9:12],
            "unit": unit, "prefix": prefix, "continuity": continuity, "unknown_bits": unknown}


# Annunciators of the display frame, established on a real unit (docs/formats.md): the high
# nibble of byte 6 and bytes 7 and 8. Anything else lit is reported as unknown.
_B6_KNOWN, _B7_KNOWN, _B8_KNOWN = 0xF0, 0x3D, 0xA3


def frame_unit(frame: bytes) -> tuple:
    """(unit, prefix, continuity, unknown bits lit) from a display frame's annunciators.

    Units are ASCII: "V", "A", "Ohm", "F", "degC" or "" when none is lit; the micro prefix is
    "u". Byte 6 bit 7 is lit in the diode function and carries no unit of its own (the reading
    is in volts)."""
    b6, b7, b8 = frame[6] & 0xF0, frame[7], frame[8]
    unknown = bool(b6 & ~_B6_KNOWN & 0xFF) or bool(b7 & ~_B7_KNOWN & 0xFF) or bool(b8 & ~_B8_KNOWN & 0xFF)
    unit = "V" if b8 & 0x02 else "A" if b8 & 0x01 else "Ohm" if b7 & 0x20 else "F" if b7 & 0x10 else \
        "degC" if b8 & 0x20 else ""
    prefix = "k" if b6 & 0x40 else "n" if b6 & 0x20 else "u" if b6 & 0x10 else "M" if b7 & 0x04 else \
        "m" if b7 & 0x01 else ""
    return unit, prefix, bool(b7 & 0x08), unknown


# Unit code in stock's own meter state (state[16], i.e. 0x20001030), seen on a real unit.
FIRMWARE_UNITS = {1: ("V", ""), 2: ("Ohm", ""), 3: ("Ohm", "k"), 4: ("Ohm", "M"), 5: ("F", "n"), 6: ("F", "u"),
                  9: ("degC", ""), 11: ("A", "m"), 12: ("A", "")}
PREFIX_SCALE = {"": 1.0, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6}
FUNCTION_AC_V = 0x0D
DISPLAY_COUNTS = 19999      # the display holds 4 1/2 digits
# state[13]: what stock's own screen is showing
SCREEN_NUMBER, SCREEN_OVERLOAD = 0, 2


def held_screen(state: bytes):
    """What stock's screen shows while HOLD is on, from the 40 state bytes: a number or an
    overload in a known unit, as {"text", "value", "unit", "prefix", "overload", "si"}.

    HOLD does not reach the meter chip: its frames go on with the live reading while stock stops
    updating its state, and the screen keeps showing that frozen state. None when the state is
    not a plain number or an overload (a blank display, "Auto", a value the display cannot hold)."""
    if len(state) < 21 or state[16] not in FIRMWARE_UNITS or state[13] not in (SCREEN_NUMBER, SCREEN_OVERLOAD):
        return None
    unit, prefix = FIRMWARE_UNITS[state[16]]
    if state[13] == SCREEN_OVERLOAD:
        return {"text": "OL", "value": None, "unit": unit, "prefix": prefix, "overload": True, "si": None}
    decimals = state[12]
    value = struct.unpack_from("<f", state, 8)[0]
    if decimals > 4 or value != value or abs(value) * 10 ** decimals > DISPLAY_COUNTS + 0.5:
        return None
    text = "%.*f" % (decimals, value)
    return {"text": text, "value": float(text), "unit": unit, "prefix": prefix, "overload": False,
            "si": float(text) * PREFIX_SCALE[prefix]}


def decode_meter_event(frame: bytes, state: bytes, function: int | None = None) -> dict:
    """A display frame plus the 40 state bytes that travel with it, and optionally the function
    word of the same event.

    Adds:
      "si"            the reading in base units, or None
      "consistent"    the frame's annunciators and stock's own unit code agree, no unknown
                      annunciator is lit and the display is not blank. Around a change of function
                      or of range one of the two is still the old one for a frame or two; such
                      readings should not be trusted.
      "hold"          HOLD is on: the frame is the live reading, the instrument's screen is frozen
      "held"          what that frozen screen shows (see held_screen), or None
      "frequency_hz"  in AC V (function 0x0D) the frequency stock shows above the reading; None
                      in any other function, where the state's number is stale."""
    d = decode_meter_frame(frame)
    firmware = FIRMWARE_UNITS.get(state[16]) if len(state) > 16 else None
    d["firmware_unit_code"] = state[16] if len(state) > 16 else None
    d["consistent"] = (bool(d["unit"]) and not d["unknown_bits"] and not d["blank"]
                       and firmware == (d["unit"], d["prefix"]))
    d["si"] = d["value"] * PREFIX_SCALE[d["prefix"]] if d["value"] is not None and d["unit"] else None
    d["hold"] = len(state) > 20 and state[20] == 1
    d["held"] = held_screen(state) if d["hold"] else None
    d["frequency_hz"] = (state[18] | state[19] << 8) if function == FUNCTION_AC_V and len(state) > 19 else None
    return d


# ---------------------------------------------------------------- stock's RAM, read on request
#
# Things stock keeps in RAM that the stream does not carry (docs/stock_v120_notes.md). They are
# read with READ, which the link answers from the USB interrupt: each block is one instant.

METER_STATE_ADDR, METER_STATE_LEN = 0x20001020, 56
SCOPE_STATE_ADDR, SCOPE_STATE_LEN = 0x200000F8, 56
SCOPE_MEASURED_ADDR = 0x20000208            # what the screen shows; 12 words for CH1, then 12 for CH2
SCOPE_MEASURED_WORDS = 12
GENERATOR_ADDR, GENERATOR_LEN = 0x20000F50, 10
# The generator's waveform, as its position in the menu (0 to 12). Names as the menu shows them: 1 to 12
# were read on the unit one by one on 2026-11-25 (sessions/2026-11-25_generator_ch2_boxes). Index 0 is one
# step before "Square Wave" in the menu; stock builds it from a 100-point table at 0x0804D848 that is a sine,
# and its name is the first of stock's own table of thirteen waveform names per language (code). Spelled as
# in that table.
GENERATOR_WAVEFORMS = {0: "Sine Wave", 1: "Square Wave", 2: "Sawtooth wave", 3: "Half-wave", 4: "Full Wave", 5: "Step Wave",
                       6: "Reverse Step Wave", 7: "Index Up", 8: "Index Decrease", 9: "Direct current",
                       10: "Multi-audio", 11: "Sink Pulse", 12: "Lorentz waves"}
# Unit code of the MAX / MEAN / MIN boxes, state bytes 50 to 52. V and mV: by the screen and by
# MEAN = (MAX + MIN) / 2 holding only with MIN in millivolts. uA: by a photograph of the screen in Small DC
# current. nF and degC: the boxes carry the code the reading itself has in those functions (state byte 16).
STATS_UNITS = {0: "mV", 1: "V", 5: "nF", 9: "degC", 10: "uA"}


def decode_generator(block: bytes) -> dict:
    """The signal generator's settings from the 10 bytes at GENERATOR_ADDR.

    "flag0" is 0 or 1 and changes while the generator screen is being operated; what it means is not
    established."""
    if len(block) < GENERATOR_LEN:
        raise LinkError("generator block too short")
    return {"flag0": block[0], "waveform_index": block[1], "waveform": GENERATOR_WAVEFORMS.get(block[1]),
            "frequency_hz": struct.unpack_from("<I", block, 4)[0], "duty_percent": block[8],
            "amplitude_v": block[9] / 10.0}


def decode_scope_measurements(words: bytes) -> dict:
    """One channel's twelve 32-bit words of stock's own measurements, in SI units.

    Words 0 to 8 were checked for CH1 against photographs of the screen. "amp", "vrms" and
    "avg" are words 9 to 11 under the names stock prints in front of them (its code prints
    word i after label i); on 2026-11-26 stopped screens showed 316.0, 208.0 and 148.0 mV
    under those labels and the words read 316000, 208000 and 148000, though no photograph and
    reading were of the same acquisition. With the probe setting at X10 the six voltages
    already carry the factor (words 6 to 8 on 2026-11-25, 9 to 11 on 2026-11-26). "extra" is
    the same three words, as before."""
    if len(words) < 4 * SCOPE_MEASURED_WORDS:
        raise LinkError("measurement block too short")
    w = struct.unpack_from("<6I6i", words)             # the voltages are signed: Vmin goes below zero
    return {"frequency_hz": w[0], "period_s": w[1] * 1e-9, "duty_positive_percent": w[2] / 100.0,
            "duty_negative_percent": w[3] / 100.0, "width_positive_s": w[4] * 1e-9, "width_negative_s": w[5] * 1e-9,
            "vmax": w[6] * 1e-6, "vmin": w[7] * 1e-6, "vpp": w[8] * 1e-6,
            "amp": w[9] * 1e-6, "vrms": w[10] * 1e-6, "avg": w[11] * 1e-6, "extra": [x * 1e-6 for x in w[9:12]]}


FFT_POINTS = 128            # indexes of the spectrum stock keeps while a channel's "FFT:" row is on


def fft_hz_per_index(seconds_per_div: float) -> float:
    """The frequency step between two indexes of stock's FFT block at a time base.

    Read on the unit at 100, 200 and 500 us per division with the generator's 1 kHz square
    (2026-11-26): the fundamental sits at index 1, 2 and 5, and at 500 us the harmonics above
    12.5 kHz come back folded where this rule puts them. That is what 128 points two of the
    record's sample periods apart give: 25 samples to a division, so an index is
    25 / (2 * 128 * seconds per division) hertz. Not tried at other time bases."""
    return SAMPLES_PER_DIV / (2.0 * FFT_POINTS * seconds_per_div)


def decode_fft_block(block: bytes) -> dict:
    """The block a scope state's bytes 12-15 (CH1) or 16-19 (CH2) point at while that channel's
    FFT is on: 128 bytes, the height of each bar stock draws (0 to 50, the largest magnitude
    being 50), then 32-bit floats, the magnitudes those heights come from. Indexes above 64
    mirror those below. "magnitudes" has as many floats as the block read holds (the unit was
    never read past the 80th). A block read over the link while the instrument runs is several
    requests a few milliseconds apart, not one instant."""
    if len(block) < FFT_POINTS:
        raise LinkError("FFT block too short")
    n = min(FFT_POINTS, (len(block) - FFT_POINTS) // 4)
    return {"bars": list(block[:FFT_POINTS]), "magnitudes": list(struct.unpack_from("<%df" % n, block, FFT_POINTS))}


def decode_meter_stats(state: bytes) -> list:
    """The MAX, MEAN and MIN boxes from 56 bytes of meter state: [(name, value, decimals, unit code)].

    Each box scales on its own. A value that is not a number means the box is empty. The unit code is
    state byte 50 + box (STATS_UNITS). Bytes 47 to 49 are not it: they were 1 for a box in volts and 0
    for every other unit seen, and what they are is not established."""
    if len(state) < 53:
        raise LinkError("meter state too short for the statistics")
    values = struct.unpack_from("<3f", state, 32)
    return [(name, None if v != v else v, state[44 + i], state[50 + i])
            for i, (name, v) in enumerate(zip(("MAX", "MEAN", "MIN"), values))]


# ---------------------------------------------------------------- oscilloscope records

COUNTS_PER_DIV = 25         # sample counts per vertical division
SAMPLES_PER_DIV = 25        # samples per horizontal division (checked at ten time bases from 1 us to 20 ms)
_STEPS = (1, 2, 5)


def volts_per_div(index: int):
    """Input sensitivity of index 0..9: 10 mV, 20 mV, 50 mV ... 10 V. None outside."""
    return _STEPS[index % 3] * 10 ** (index // 3) * 0.01 if 0 <= index <= 9 else None


def seconds_per_div(index: int):
    """Time base of index 0..: 10 ns, 20 ns, 50 ns ... None for an absurd index."""
    return _STEPS[index % 3] * 10 ** (index // 3) * 10e-9 if 0 <= index <= 32 else None


def scope_settings(state: bytes):
    """The 56 scope state bytes of a record header decoded (docs/formats.md), or None.

    Per channel: enabled, volts per division as displayed (probe setting applied), probe
    factor, the sample value of zero volts, the coupling and whether its FFT is on. Range
    and probe bytes of both channels were checked on hardware (2026-11-25); CH2's position
    byte is assumed to mirror CH1's. The rest was read on the unit on 2026-11-26, each
    setting switched and back: the couplings are bytes 0 and 1 (1 DC, 0 AC), the FFT flags
    bytes 10 and 11, the trigger's channel byte 22 (0 CH1, 1 CH2), its mode byte 23
    (0 Normal, 1 Auto, 2 Single) and its edge byte 24 (0 rising, 1 falling). A value outside
    those gives None."""
    t = seconds_per_div(state[45])
    v = [volts_per_div(state[2]), volts_per_div(state[3])]
    if t is None or None in v:
        return None
    channels = []
    for ch in (0, 1):
        probe = 10 if state[6 + ch] == 1 else 1
        position = state[4 + ch] - 256 if state[4 + ch] > 127 else state[4 + ch]
        channels.append({"enabled": bool(state[20] & (1 << ch)), "volts_per_div": v[ch] * probe,
                         "probe": probe, "zero": 128 + position, "coupling": {0: "AC", 1: "DC"}.get(state[ch]),
                         "fft": {0: False, 1: True}.get(state[10 + ch])})
    level = int.from_bytes(state[28:30], "little", signed=True)
    return {"channels": channels, "seconds_per_div": t, "sample_period": t / SAMPLES_PER_DIV,
            "time_base_checked": 6 <= state[45] <= 16, "trigger_level": 128 + level,
            "trigger_edge": {0: "rising", 1: "falling"}.get(state[24]),
            "trigger_mode": {0: "Normal", 1: "Auto", 2: "Single"}.get(state[23]),
            "trigger_channel": {0: "CH1", 1: "CH2"}.get(state[22])}


def measure_record(settings: dict, channel: int, data: bytes) -> dict:
    """Peak-to-peak volts and frequency of a record, the way the instrument's screen reports them.

    The first sample of a record is a filler byte and is ignored. The frequency comes from the
    rising crossings of the middle level (None with fewer than two whole periods)."""
    samples = data[1:]
    lo, hi = min(samples), max(samples)
    ch = settings["channels"][channel]
    to_volts = lambda m: (m - ch["zero"]) / COUNTS_PER_DIV * ch["volts_per_div"]
    frequency = None
    if hi - lo >= 8:
        mid, hyst = (hi + lo) / 2, max(2, (hi - lo) / 8)
        rising, below = [], False
        for i, m in enumerate(samples):
            if m <= mid - hyst:
                below = True
            elif below and m >= mid + hyst:
                below = False
                rising.append(i)
        if len(rising) >= 3:
            period = (rising[-1] - rising[0]) / (len(rising) - 1)
            frequency = 1.0 / (period * settings["sample_period"])
    return {"min": to_volts(lo), "max": to_volts(hi), "vpp": to_volts(hi) - to_volts(lo),
            "frequency": frequency, "clipped": lo <= 5 or hi >= 244}


def _eng(value: float, unit: str) -> str:
    """1234.5 -> '1.23k', 0.0002 -> '200u' (three significant figures) followed by the unit."""
    if value == 0:
        return "0" + unit
    for scale, prefix in ((1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n")):
        if abs(value) >= scale * 0.9995:
            return "%.3g%s%s" % (value / scale, prefix, unit)
    return "%.3g%s" % (value, unit)


def wave_stored(samples, amplitude: int) -> list:
    """What the instrument stores for these samples (0..4094) at an amplitude in tenths of a volt:
    stock's own scaling, sample * amplitude / 33 + 20, which the patch applies."""
    amplitude = min(amplitude, WAVE_AMPLITUDE_MAX)
    return [min(v, WAVE_FULL_SCALE) * amplitude // 33 + 20 for v in samples]


def wave_normalise(values) -> list:
    """Any 100 numbers as samples: the lowest becomes 0 and the highest 4094. A flat list stays at
    mid scale."""
    values = [float(v) for v in values]
    if len(values) != WAVE_POINTS:
        raise LinkError("a waveform is %d points, got %d" % (WAVE_POINTS, len(values)))
    if any(math.isnan(v) or math.isinf(v) for v in values):
        raise LinkError("a waveform cannot hold NaN or infinity")
    lo, hi = min(values), max(values)
    if hi == lo:
        return [WAVE_FULL_SCALE // 2] * WAVE_POINTS
    return [int(round((v - lo) / (hi - lo) * WAVE_FULL_SCALE)) for v in values]


def wave_resample(values, points: int = WAVE_POINTS) -> list:
    """One period given as any number of points (two at least), as `points` of them, by linear
    interpolation around the period: the last point is followed by the first."""
    values = [float(v) for v in values]
    n = len(values)
    if n < 2:
        raise LinkError("a waveform needs two points at least")
    if n == points:
        return values
    out = []
    for i in range(points):
        x = i * n / points
        k = int(x)
        out.append(values[k % n] + (values[(k + 1) % n] - values[k % n]) * (x - k))
    return out


def parse_wave_text(text: str) -> list:
    """Numbers from a text file: one per line or separated by commas, semicolons or spaces. `#`
    starts a comment. A line that begins with a word (a CSV header) is skipped."""
    values = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or re.match(r"[A-Za-z_]", line):
            continue
        for tok in re.split(r"[;,\s]+", line):
            if tok:
                try:
                    values.append(float(tok))
                except ValueError:
                    raise LinkError("not a number in the waveform file: %r" % tok) from None
    return values


def parse_harmonics(text: str) -> list:
    """`1:1,3:0.33,5:0.2` or `1:1:0,2:0.5:90` -> [(order, amplitude, phase in degrees)]."""
    out = []
    for item in text.split(","):
        parts = item.strip().split(":")
        try:
            order, level = int(parts[0]), float(parts[1])
            phase = float(parts[2]) if len(parts) > 2 else 0.0
        except (ValueError, IndexError):
            raise LinkError("a harmonic is ORDER:LEVEL or ORDER:LEVEL:DEGREES, got %r" % item) from None
        if not 1 <= order <= WAVE_POINTS // 4 or len(parts) > 3:
            raise LinkError("harmonic order %d: 1 to %d fit in %d points" % (order, WAVE_POINTS // 4, WAVE_POINTS))
        out.append((order, level, phase))
    return out


def wave_shape(name: str, duty: float = 50.0, harmonics=None) -> list:
    """One period of a named shape as 100 samples (0..4094). `duty` is the high part of `pulse`
    and the rising part of `triangle`, in percent."""
    n = WAVE_POINTS
    d = min(max(duty, 1.0), 99.0) / 100.0
    t = [i / n for i in range(n)]
    if name == "sine":
        v = [math.sin(2 * math.pi * x) for x in t]
    elif name == "triangle":
        v = [x / d if x < d else (1 - x) / (1 - d) for x in t]
    elif name == "ramp":
        v = t
    elif name == "ramp-down":
        v = [1 - x for x in t]
    elif name == "pulse":
        v = [1.0 if x < d else 0.0 for x in t]
    elif name == "sinc":
        v = [1.0 if x == 0.5 else math.sin(12 * math.pi * (x - 0.5)) / (12 * math.pi * (x - 0.5)) for x in t]
    elif name == "gauss":
        v = [math.exp(-((x - 0.5) / 0.12) ** 2) for x in t]
    elif name == "exp-decay":
        v = [math.exp(-5 * x) for x in t]
    elif name == "exp-rise":
        v = [1 - math.exp(-5 * x) for x in t]
    elif name == "half-sine":
        v = [max(0.0, math.sin(2 * math.pi * x)) for x in t]
    elif name == "abs-sine":
        v = [abs(math.sin(2 * math.pi * x)) for x in t]
    elif name == "stairs":
        v = [int(x * 8) / 7 for x in t]
    elif name == "burst":
        v = [math.sin(2 * math.pi * 10 * x) if x < 0.5 else 0.0 for x in t]
    elif name == "am":
        v = [(1 + 0.8 * math.sin(2 * math.pi * x)) * math.sin(2 * math.pi * 10 * x) for x in t]
    elif name == "harmonics":
        if not harmonics:
            raise LinkError("harmonics needs --harmonics, for instance 1:1,3:0.33,5:0.2")
        v = [sum(a * math.sin(2 * math.pi * k * x + math.radians(ph)) for k, a, ph in harmonics) for x in t]
    else:
        raise LinkError("unknown shape %r: one of %s" % (name, ", ".join(WAVE_SHAPES)))
    return wave_normalise(v)


WAVE_SHAPES = ("sine", "triangle", "ramp", "ramp-down", "pulse", "sinc", "gauss", "exp-decay", "exp-rise",
               "half-sine", "abs-sine", "stairs", "burst", "am", "harmonics")


class HidTransport:
    """64-byte reports over hidapi."""

    def __init__(self, vid: int = VID, pid: int = PID):
        try:
            import hid
        except ImportError as e:
            raise LinkError("hidapi bindings missing: python -m pip install hidapi") from e
        found = hid.enumerate(vid, pid)
        if not found:
            raise LinkError("no %04x:%04x device. Is the instrument in a measurement mode, with "
                            "USB Sharing off, and has it had a few seconds since the last mode change?" % (vid, pid))
        self.serial = found[0].get("serial_number") or ""
        self.dev = hid.device()
        self.dev.open_path(found[0]["path"])

    def write(self, report: bytes) -> None:
        # hidapi wants the report id first; this device has none, so it is 0.
        if self.dev.write(b"\x00" + report) < 0:
            raise LinkError("write failed: %s" % self.dev.error())

    def read(self, timeout_ms: int) -> bytes | None:
        data = bytes(self.dev.read(REPORT_LEN, timeout_ms))
        return data if len(data) == REPORT_LEN else None

    def close(self) -> None:
        self.dev.close()


class Link:
    def __init__(self, transport, on_event=None):
        self.t = transport
        self.seq = 0
        self.on_event = on_event

    def _request(self, cmd: int, length: int = 0, addr: int = 0, timeout_ms: int = 1000, data: bytes = b"") -> bytes:
        self.seq = (self.seq + 1) & 0xFF
        rq = bytes([cmd, self.seq, length, 0]) + struct.pack("<I", addr) + data
        if len(rq) > REPORT_LEN:
            raise LinkError("request does not fit in a report")
        self.t.write(rq + bytes(REPORT_LEN - len(rq)))
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            rs = self.t.read(timeout_ms)
            if rs is None:
                raise LinkError("no response within %d ms" % timeout_ms)
            if rs[0] in EVENTS:                     # events may arrive ahead of the response
                if self.on_event:
                    self.on_event(rs)
                if time.monotonic() > deadline:
                    raise LinkError("no response within %d ms (only events)" % timeout_ms)
                continue
            break
        if rs[0] != (cmd | 0x80) or rs[1] != self.seq:
            raise LinkError("out-of-step response: %s" % rs[:8].hex())
        if rs[2] != 0:
            raise LinkError("device refused (%s) at 0x%08X" % (STATUS.get(rs[2], rs[2]), addr))
        if struct.unpack_from("<I", rs, 4)[0] != addr:
            raise LinkError("response is for another address")
        return rs[8:8 + rs[3]]

    def info(self) -> dict:
        p = self._request(CMD_INFO)
        if len(p) < 24 or p[:8] != b"2C53LINK":
            raise LinkError("not a stock-link device: %s" % p.hex())
        ticks, links, requests = struct.unpack_from("<III", p, 12)
        d = {"protocol": p[8], "patch": p[9], "mode": p[10], "stream": p[11],
             "ticks": ticks, "links": links, "requests": requests}
        if len(p) >= 36:
            d["meter_frames"], d["scope_records"], d["scope_skipped"] = struct.unpack_from("<III", p, 24)
        d["features"] = p[36] if len(p) >= 37 else 0
        # patch 5: how many entries the image added to the generator's menu, which of them (if any)
        # plays what `wave` sent, and where the pointers to their names are
        d["menu_count"], d["menu_usb_index"] = (p[37], p[38]) if len(p) >= 44 else (0, 0)
        d["menu_names_addr"] = struct.unpack_from("<I", p, 40)[0] if len(p) >= 44 else 0
        return d

    def menu_names(self, info: dict) -> dict:
        """{index: label} of the entries the image added to the generator's menu, read from the image."""
        names = {}
        if not info.get("features", 0) & FEATURE_MENU or not info.get("menu_count"):
            return names
        pointers = struct.unpack("<%dI" % info["menu_count"], self.read(info["menu_names_addr"], 4 * info["menu_count"]))
        for k, at in enumerate(pointers):
            text = self.read(at, 24)
            names[STOCK_WAVEFORMS + k] = text.split(b"\0", 1)[0].decode("ascii", "replace")
        return names

    def read(self, addr: int, length: int, progress=None) -> bytes:
        out = bytearray()
        while len(out) < length:
            n = min(PAYLOAD_MAX, length - len(out))
            chunk = self._request(CMD_READ, n, addr + len(out))
            if len(chunk) != n:
                raise LinkError("short read at 0x%08X" % (addr + len(out)))
            out += chunk
            if progress:
                progress(len(out), length)
        return bytes(out)

    def read_twice(self, addr: int, length: int, progress=None) -> bytes:
        """Read a region two times and insist on identical data."""
        a = self.read(addr, length, progress)
        b = self.read(addr, length, progress)
        if a != b:
            raise LinkError("two reads of 0x%08X differ: not saving anything" % addr)
        return a

    def stream(self, mask: int) -> int:
        p = self._request(CMD_STREAM, mask)
        return p[0]

    def wave(self, samples) -> dict:
        """Store one period (100 samples, 0..4094) in the generator's table. Returns the amplitude
        the instrument scaled them by (tenths of a volt) and the waveform stock will go back to."""
        samples = [int(v) for v in samples]
        if len(samples) != WAVE_POINTS or min(samples) < 0 or max(samples) > WAVE_FULL_SCALE:
            raise LinkError("a waveform is %d samples from 0 to %d" % (WAVE_POINTS, WAVE_FULL_SCALE))
        seen = set()
        for first in range(0, WAVE_POINTS, WAVE_CHUNK):
            part = samples[first:first + WAVE_CHUNK]
            p = self._request(CMD_WAVE, len(part), first, data=struct.pack("<%dH" % len(part), *part))
            if len(p) != 2:
                raise LinkError("unexpected answer to a waveform request")
            seen.add((p[0], p[1]))
        if len(seen) != 1:
            raise LinkError("the amplitude or the waveform was changed on the instrument while the table was "
                            "being written: write it again")
        amplitude, waveform = seen.pop()
        return {"amplitude": amplitude, "waveform_index": waveform}

    def events(self, timeout_ms: int = 500):
        """Yield event reports until the transport stays silent for timeout_ms."""
        while True:
            rs = self.t.read(timeout_ms)
            if rs is None:
                return
            if rs[0] in EVENTS:
                yield rs


class ScopeAssembler:
    """Rebuilds 1024-sample records from header + data events; drops incomplete ones."""

    def __init__(self):
        self.partial = {}

    def feed(self, rs: bytes):
        ch, seq = rs[2], rs[1]
        if rs[0] == EVT_SCOPE_HEADER:
            self.partial[ch] = {"channel": ch, "seq": seq, "tick": struct.unpack_from("<I", rs, 4)[0],
                                "state": rs[8:8 + rs[3]], "data": bytearray()}
            return None
        rec = self.partial.get(ch)
        off = struct.unpack_from("<I", rs, 4)[0]
        if rec is None or rec["seq"] != seq or off != len(rec["data"]):
            self.partial.pop(ch, None)                  # lost a report: wait for the next header
            return None
        rec["data"] += rs[8:8 + rs[3]]
        if len(rec["data"]) < SCOPE_RECORD_LEN:
            return None
        del self.partial[ch]
        rec["data"] = bytes(rec["data"])
        return rec


def _progress(done: int, total: int) -> None:
    if not sys.stderr.isatty():
        return
    if done == total or done % (PAYLOAD_MAX * 64) == 0:
        sys.stderr.write("\r  %d / %d bytes" % (done, total))
        if done == total:
            sys.stderr.write("\n")
        sys.stderr.flush()


def cmd_info(link: Link, args) -> int:
    i = link.info()
    print("stock-link   protocol %d, patch %d" % (i["protocol"], i["patch"]))
    print("system mode  %d (%s)" % (i["mode"], MODES.get(i["mode"], "unknown")))
    print("uptime       %.1f s" % (i["ticks"] / 1000.0))
    print("link count   %d, requests %d" % (i["links"], i["requests"]))
    if "meter_frames" in i:
        print("seen         %d meter frames, %d scope records (%d skipped)"
              % (i["meter_frames"], i["scope_records"], i["scope_skipped"]))
    print("status       %s" % ("streamed (subscribe with bit 2)" if i["features"] & FEATURE_STATUS
                               else "not streamed by this patch: read on request"))
    print("waveforms    %s" % ("the generator's table can be written (wave)" if i["features"] & FEATURE_WAVE
                               else "not with this patch: the link is read-only"))
    for index, label in link.menu_names(i).items():
        print("menu         entry %d of the generator's list: %s%s" % (
            index + 1, label, " (plays what `wave` sent last)" if index == i["menu_usb_index"] else ""))
    return 0


def wave_text_plot(stored: list, rows: int = 8, cols: int = 50) -> list:
    """A small picture of one period, for the terminal."""
    lo, hi = min(stored), max(stored)
    span = max(hi - lo, 1)
    picked = [stored[i * len(stored) // cols] for i in range(cols)]
    level = [min(rows - 1, (v - lo) * rows // span) for v in picked]
    return ["  " + "".join("#" if level[c] == r else " " for c in range(cols)).rstrip() for r in range(rows - 1, -1, -1)]


def cmd_wave(link: Link, args) -> int:
    info = link.info()
    added = link.menu_names(info)
    if args.show:
        table = list(struct.unpack("<%dH" % WAVE_POINTS, link.read(WAVE_ADDR, 2 * WAVE_POINTS)))
        g = decode_generator(link.read(GENERATOR_ADDR, GENERATOR_LEN))
        print("generator    %s, %d Hz, amplitude %.1f V" % (g["waveform"] or added.get(g["waveform_index"]) or "waveform %d" % g["waveform_index"],
                                                            g["frequency_hz"], g["amplitude_v"]))
        print("table        %d samples, %d to %d" % (len(table), min(table), max(table)))
        print("\n".join(wave_text_plot(table)))
        if args.out:
            args.out.write_text("".join("%d\n" % v for v in table))
            print("saved        %s" % args.out)
        return 0
    if not info["features"] & FEATURE_WAVE:
        raise LinkError("this patch (%d) cannot write the generator's table: it takes patch 4" % info["patch"])
    if (args.shape is None) == (args.file is None):
        raise LinkError("give a shape or --file, one of the two")
    if args.file is not None:
        values = parse_wave_text(args.file.read_text())
        samples = wave_normalise(wave_resample(values))
        what = "%s (%d points)" % (args.file.name, len(values))
    else:
        samples = wave_shape(args.shape, args.duty, parse_harmonics(args.harmonics) if args.harmonics else None)
        what = args.shape
    if args.invert:
        samples = [WAVE_FULL_SCALE - v for v in samples]
    r = link.wave(samples)
    stock = GENERATOR_WAVEFORMS.get(r["waveform_index"]) or added.get(r["waveform_index"]) or "waveform %d" % r["waveform_index"]
    usb = info.get("menu_usb_index", 0)
    usb_name = added.get(usb, "?")
    # From patch 6, with a "USB" entry in the menu the playing table is only written while that entry is selected.
    if usb and info["patch"] >= 6 and r["waveform_index"] != usb:
        print("stored       %s: %d samples, kept as entry %d of the instrument's list, \"%s\", until power-off"
              % (what, WAVE_POINTS, usb + 1, usb_name))
        print("not playing  the instrument is on %s, which is left as it is. Select \"%s\" on it to play this one."
              % (stock, usb_name))
        print("\n".join(wave_text_plot(wave_stored(samples, r["amplitude"] or WAVE_AMPLITUDE_MAX))))
        return 0
    want = wave_stored(samples, r["amplitude"])
    got = list(struct.unpack("<%dH" % WAVE_POINTS, link.read(WAVE_ADDR, 2 * WAVE_POINTS)))
    if got != want:
        raise LinkError("the table read back differs from what was sent in %d of %d samples: a generator setting "
                        "may have been changed meanwhile" % (sum(a != b for a, b in zip(got, want)), WAVE_POINTS))
    print("written      %s: %d samples, read back identical (%d to %d in the table)" % (what, WAVE_POINTS, min(got), max(got)))
    print("amplitude    %.1f V, the one set on the instrument" % (r["amplitude"] / 10.0))
    if r["amplitude"] == 0:
        print("             at 0 V the output is flat: raise the amplitude on the instrument and write again")
    if usb and r["waveform_index"] == usb:
        print("until        another one is sent or the instrument is switched off: it is entry %d of its list, \"%s\", "
              "which is the one selected." % (usb + 1, usb_name))
    else:
        print("until        the waveform, the duty or the amplitude is changed on the instrument, or it is switched "
              "off: then it plays %s again. Its screen keeps showing %s meanwhile." % (stock, stock))
        if usb:
            print("in the menu  entry %d of the instrument's list, \"%s\", plays it from now until power-off."
                  % (usb + 1, usb_name))
    print("\n".join(wave_text_plot(got)))
    return 0


def cmd_read(link: Link, args) -> int:
    data = link.read(args.addr, args.length)
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        print("%08X  %-47s  %s" % (args.addr + off, row.hex(" "),
                                   "".join(chr(c) if 32 <= c < 127 else "." for c in row)))
    return 0


# ---- tasks: stock's FreeRTOS tasks and how much of each stack was ever used --------------------
#
# Stock V1.2.0 creates its tasks once, at start-up, from a heap that starts zeroed: each task's
# stack is allocated just before its control block, and the part of a stack that was never
# reached is still zero. Reading the heap therefore says how close each task has come to the
# end of its stack since power-on, which is the question to ask after adding code that runs
# in one of them.

TASK_HEAP = (0x20006000, 0x2000)        # V1.2.0: every stack and control block is in here
TCB_PRIORITY, TCB_STACK, TCB_NAME, TCB_NAME_LEN = 0x2C, 0x30, 0x34, 16
HEAP_BLOCK_HEADER = 8                   # heap_4: between the end of the stack and the control block


def find_tasks(block: bytes, base: int) -> list:
    """The FreeRTOS control blocks in a block of RAM read at `base`: the saved top of stack
    first, then, at fixed offsets, the priority, the start of the stack and the name."""
    tasks = []
    for off in range(0, len(block) - TCB_NAME - TCB_NAME_LEN, 4):
        top, = struct.unpack_from("<I", block, off)
        priority, stack = struct.unpack_from("<II", block, off + TCB_PRIORITY)
        name = block[off + TCB_NAME:off + TCB_NAME + TCB_NAME_LEN]
        text = name.split(b"\0")[0]
        end = base + off - HEAP_BLOCK_HEADER
        if not (base <= stack < top <= end and priority < 32 and len(text) >= 2
                and all(0x20 < c < 0x7F for c in text) and len(text) < TCB_NAME_LEN):
            continue
        untouched = 0                   # zeros from the bottom up, but never into what is in use now
        while stack + untouched < top and block[stack - base + untouched] == 0:
            untouched += 1
        tasks.append({"name": text.decode("ascii"), "priority": priority, "tcb": base + off, "stack": stack,
                      "size": end - stack, "in_use": end - top, "untouched": untouched})
    return tasks


def cmd_tasks(link: Link, args) -> int:
    base, length = TASK_HEAP
    tasks = find_tasks(link.read(base, length), base)
    if not tasks:
        print("no task found in 0x%08X..0x%08X: not stock V1.2.0's heap?" % (base, base + length))
        return 1
    print("task          prio  stack at    size  in use now  deepest ever  never reached")
    for t in tasks:
        print("%-12s  %4d  0x%08X  %4d  %10d  %12d  %13d" % (
            t["name"], t["priority"], t["stack"], t["size"], t["in_use"], t["size"] - t["untouched"], t["untouched"]))
    print("bytes; 'never reached' counts the zeros left at the bottom of each stack since power-on")
    return 0


# ---- snapshot and watch: stock's RAM for someone else to read ---------------------------------
#
# Both exist to answer questions about stock that otherwise need a debug probe on an opened unit:
# `snapshot` saves a labelled set of RAM blocks at one moment (one per step of a procedure), `watch`
# follows one small block and says when it changes. Their output is meant to be passed on, so they
# refuse the two places that hold a unit's calibration unless told otherwise.

RAM_START, RAM_END = 0x20000000, 0x20038000
# (name, first address, end) of what is not to be shared: the settings/calibration page with the
# bootloader's flag page, and the RAM tables stock loads the calibration into and saves it from.
# The RAM tables are the ones stock's settings saver reads (0x20000358-0x2000044A). The bytes
# right after them are not calibration: 0x2000044C says what the arrow keys do (ARROWS_ADDR) and
# the display arrays start at 0x2000044E.
PRIVATE_REGIONS = (("the settings/calibration page in flash", CAL_ADDR, CAL_ADDR + CAL_LEN),
                   ("stock's calibration tables in RAM", 0x20000358, 0x2000044B))


def parse_range(text: str) -> tuple:
    """'ADDR:LEN' -> (addr, length), both as Python writes numbers (0x2000013D:1)."""
    try:
        addr, length = (int(x, 0) for x in text.split(":"))
    except ValueError:
        raise LinkError("a range is ADDR:LEN, for example 0x200000F8:56 (got %r)" % text) from None
    if length < 1 or addr < 0:
        raise LinkError("a range needs a length of at least 1 (got %r)" % text)
    return addr, length


def private_region(addr: int, length: int):
    """Name of the calibration region a block overlaps, or None."""
    for name, start, end in PRIVATE_REGIONS:
        if addr < end and addr + length > start:
            return name
    return None


def _shareable(addr: int, length: int, allow: bool) -> None:
    name = private_region(addr, length)
    if name and not allow:
        raise LinkError("0x%08X:%d overlaps %s, which is this unit's own and not for sharing; "
                        "--allow-calibration reads it anyway" % (addr, length, name))


def _block_line(addr: int, data: bytes) -> str:
    return "%08X %4d  %s" % (addr, len(data), data.hex())


def snapshot_lines(link: Link, label: str, ranges: list, follows: list, allow: bool = False) -> list:
    """The text of one snapshot. `ranges` are (addr, length); `follows` are (addr, before, after):
    the 32-bit pointer at addr is read and, if it points into RAM, so are `before` bytes ahead of
    its target and `after` from it (a heap block with its header, for one)."""
    for addr, length in ranges:
        _shareable(addr, length, allow)
    for addr, _, _ in follows:
        _shareable(addr, 4, allow)
    info = link.info()
    out = ["snapshot  %s" % label,
           "host      %s" % time.strftime("%Y-%m-%dT%H:%M:%S"),
           "link      patch %d, mode %d (%s), tick %d" % (info["patch"], info["mode"], MODES.get(info["mode"], "unknown"), info["ticks"])]
    for addr, length in ranges:
        out.append(_block_line(addr, link.read(addr, length)))
    for addr, before, after in follows:
        target = struct.unpack("<I", link.read(addr, 4))[0]
        if RAM_START + before <= target and target + after <= RAM_END:
            _shareable(target - before, before + after, allow)
            out.append("follow    %08X -> %08X, %d bytes before it and %d from it" % (addr, target, before, after))
            out.append(_block_line(target - before, link.read(target - before, before + after)))
        else:
            out.append("follow    %08X -> %08X, not a RAM address: nothing read" % (addr, target))
    out.append("")
    return out


def cmd_snapshot(link: Link, args) -> int:
    follows = []
    for text in args.follow or []:
        try:
            follows.append(tuple(int(x, 0) for x in text.split(":")))
            if len(follows[-1]) != 3 or min(follows[-1]) < 0:
                raise ValueError
        except ValueError:
            raise LinkError("--follow takes ADDR:BEFORE:AFTER, for example 0x20000EF4:8:64 (got %r)" % text) from None
    lines = snapshot_lines(link, args.label, [parse_range(r) for r in args.ranges], follows, args.allow_calibration)
    if args.out:
        with args.out.open("a") as f:
            f.write("\n".join(lines) + "\n")
        blocks = sum(1 for x in lines if len(x) > 9 and x[8] == " " and all(c in "0123456789ABCDEF" for c in x[:8]))
        print("snapshot %r: %d blocks added to %s" % (args.label, blocks, args.out))
    else:
        print("\n".join(lines))
    return 0


def watch_summary(samples: list, reads: int, elapsed: float, length: int) -> list:
    """What `watch` says at the end. `samples` are (seconds, bytes), one per change, the first being
    the value found at the start."""
    out = ["reads     %d in %.2f s (%.2f ms each: no change shorter than that is seen)" % (reads, elapsed, 1000.0 * elapsed / max(reads, 1)),
           "changes   %d" % (len(samples) - 1)]
    if len(samples) > 2:
        gaps = sorted(1000.0 * (b[0] - a[0]) for a, b in zip(samples[1:], samples[2:]))
        out.append("interval  median %.1f ms, shortest %.1f, longest %.1f (between one change and the next)" % (
            gaps[len(gaps) // 2], gaps[0], gaps[-1]))
        span = samples[-1][0] - samples[1][0]
        out.append("rate      %d changes in %.3f s: one every %.2f ms if none was missed" % (len(samples) - 2, span, 1000.0 * span / (len(samples) - 2)))
    if length <= 4 and len(samples) > 1:
        values = [int.from_bytes(v, "little") for _, v in samples]
        ones = sum(1 for a, b in zip(values, values[1:]) if b - a == 1)
        out.append("steps     %d of +1, %d other (as an unsigned little-endian number)" % (ones, len(values) - 1 - ones))
        span = samples[-1][0] - samples[1][0]
        if ones >= 2 and ones == len(values) - 1 and span > 0:
            out.append("counter   +%d in %.3f s: one count every %.2f ms" % (ones - 1, span, 1000.0 * span / (ones - 1)))
    return out


def cmd_watch(link: Link, args) -> int:
    """Read one block of at most 56 bytes over and over, as fast as the link answers, and report
    every change with the host's clock."""
    addr, length = parse_range(args.range)
    if length > PAYLOAD_MAX:
        raise LinkError("watch follows at most %d bytes (one report)" % PAYLOAD_MAX)
    _shareable(addr, length, args.allow_calibration)
    lines, samples, reads = [], [], 0
    start = time.monotonic()
    last = None
    try:
        while True:
            value = link.read(addr, length)
            now = time.monotonic() - start
            reads += 1
            if value != last:
                samples.append((now, value))
                lines.append("%10.1f ms  %s%s" % (1000.0 * now, value.hex(), "  = %d" % int.from_bytes(value, "little") if length <= 4 else ""))
                if not args.out:
                    print(lines[-1], flush=True)
                last = value
            if (args.reads and reads >= args.reads) or (not args.reads and now >= args.seconds):
                break
    except KeyboardInterrupt:
        pass
    summary = ["watch     0x%08X:%d" % (addr, length)] + watch_summary(samples, reads, time.monotonic() - start, length)
    if args.out:
        with args.out.open("a") as f:
            f.write("\n".join(["watch     0x%08X:%d  %s  %s" % (addr, length, args.label, time.strftime("%Y-%m-%dT%H:%M:%S"))]
                              + lines + summary[1:] + [""]) + "\n")
        print("%d changes added to %s" % (len(samples) - 1, args.out))
    print("\n".join(summary))
    return 0


# What the arrow keys do in the oscilloscope: the low nibble of one byte of stock's state, which
# the SELECT key steps through (code: its handler, entry 4 of the key table at 0x08046548, adds one;
# unit: SELECT took it 0, 1, 2, 0). 0, 1 and 2 were watched on the unit one key at a time, with
# CH1 alone (sessions/2026-11-26_keys/guided_oscilloscope.txt); 3 is from the code alone. Bit 7 is
# the channel up/down act on when both are shown (code; unit: MOVE switches it, and with it set the
# range and the position that changed were CH2's, guided_oscilloscope_ch2.txt). In mode 1 up/down change the channel's
# position and ms[0x1C] with it; in mode 2 they change ms[0x1C] alone, which OpenScope-2C53T's map
# of the structure calls the trigger level.
ARROWS_ADDR = 0x2000044C
ARROWS = {0: "scale: left/right change the time base, up/down the volts per division",
          1: "position: left/right move the trace sideways, up/down the channel's trace up and down",
          2: "move: left/right move the trace sideways, up/down the trigger level",
          3: "cursors"}


def arrows_text(value: int) -> str:
    return "%s%s (byte 0x%02X; SELECT changes it)" % (
        ARROWS.get(value & 0x0F, "mode %d (unknown)" % (value & 0x0F)), ", acting on CH2" if value & 0x80 else "", value)


# ---- keys: what the instrument's state does while its keys are pressed ------------------------
# Nothing here presses a key: the tool reads a few small blocks of stock's RAM over and over and
# prints what changed, so that someone working the instrument by hand gets the whole sequence in
# one file. A line typed on the computer goes into the log as a note, between the changes; with
# --guide the tool itself says which key to press next and writes that down, so nothing is typed.
UI_ADDR, UI_LEN = 0x20001058, 16        # ms[0xF60..0xF6F]: language, sound, system mode (0xF68), the menus' cursors
KEYS_METER_BYTES = (5, 6, 14, 20)       # menu entry, function, variant, HOLD: the rest of the meter state moves with the reading
KEYS_UI_BYTES = tuple(range(12))        # ms[0xF6F] moves about once a second with no key pressed (seen on the unit)
# The oscilloscope's cursors, from the code alone until a log says otherwise: ms[0x23A] is which
# cursors are on (SELECT only reaches the arrows' mode 3 when it is not zero, 0x0800D39E; the right
# arrow in that mode branches on its values 1, 2 and 3, 0x0800B4E2), four signed 16-bit positions
# at ms[0x232..0x239] (the same handler takes one from 0x232, 0x234 or 0x238), and floats from
# ms[0x23C] that 0x08025FC0 writes from the positions. The block ends where stock's copy of the
# calibration tables begins (ms[0x260]) and holds none of it.
CURSORS_ADDR, CURSORS_LEN = 0x20000328, 48      # ms[0x230..0x25F]
KEYS_BLOCKS = (("generator", GENERATOR_ADDR, GENERATOR_LEN, None, 0),
               ("meter", METER_STATE_ADDR, METER_STATE_LEN, KEYS_METER_BYTES, 0),
               ("ui", UI_ADDR, UI_LEN, KEYS_UI_BYTES, 0xF60),
               ("arrows", ARROWS_ADDR, 1, None, 0),
               ("scope", SCOPE_STATE_ADDR, 56, None, 0),
               ("cursors", CURSORS_ADDR, CURSORS_LEN, None, 0x230))


def keys_changes(name: str, old: bytes, new: bytes, only, base: int) -> list:
    """[(offset as written, old byte, new byte)] of the bytes of one block that changed; `only`
    limits it to some offsets, `base` is added to the offset written (the ui block is given as
    offsets into stock's state structure)."""
    return [(base + i, old[i], new[i]) for i in range(min(len(old), len(new)))
            if old[i] != new[i] and (only is None or i in only)]


def keys_line(now: float, name: str, changed: list, block: bytes) -> str:
    """One line of the log for the changes of one block, with what the block says when it is known."""
    text = " ".join("[%s] %02x>%02x" % ("0x%X" % o if o > 99 else str(o), a, b) for o, a, b in changed)
    if name == "generator":
        g = decode_generator(block)
        text += "   %s (index %d), %s, duty %d %%, %.1f V" % (g["waveform"] or "?", g["waveform_index"],
                                                             _eng(g["frequency_hz"], "Hz"), g["duty_percent"], g["amplitude_v"])
    elif name == "ui" and any(o == 0xF68 for o, _, _ in changed):
        text += "   system mode %d (%s)" % (block[8], MODES.get(block[8], "unknown"))
    elif name == "arrows":
        text += "   " + arrows_text(block[0])
    return "%9.3f s  %-9s %s" % (now, name, text)


def _typed_line():
    """A line typed on the keyboard since the last call, without waiting; None when there is none."""
    try:
        import select
        if select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            return line.strip() if line else None
    except (OSError, ValueError, ImportError):
        pass
    return None


def cmd_keys(link: Link, args) -> int:
    """Log every change of the generator's settings, the meter's menu bytes, the menus' cursors,
    the arrows' mode, the scope state and the scope's cursors while the instrument is worked by hand."""
    out = []

    def say(line: str) -> None:
        out.append(line.replace("\a", ""))
        print(line, flush=True)

    def read_all() -> dict:
        return {name: link.read(addr, length) for name, addr, length, _, _ in KEYS_BLOCKS}

    say("keys      %s  %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), args.label))
    last = read_all()
    for name, addr, length, _, _ in KEYS_BLOCKS:
        say("%08X  %3d  %s" % (addr, length, last[name].hex()))
    guide = [k.strip() for k in (args.guide or "").split(",") if k.strip()]
    if guide:
        say("            %d keys, one every %g s: press each one once when its line appears; it ends by itself" % (len(guide), args.step))
    else:
        say("            a line typed here and Enter is kept as a note; Ctrl-C ends")
    start, polls, asked = time.monotonic(), 0, [0]

    def guide_done() -> bool:
        """Ask for the keys that are due; True once the last one has had its time."""
        elapsed = time.monotonic() - start
        while asked[0] < len(guide) and elapsed >= asked[0] * args.step:
            say("%9.3f s  press     %s\a" % (elapsed, guide[asked[0]]))          # the bell is for the terminal only
            asked[0] += 1
        return bool(guide) and asked[0] == len(guide) and elapsed >= len(guide) * args.step

    try:
        while not (args.polls and polls >= args.polls) and not (args.seconds and time.monotonic() - start >= args.seconds):
            if guide_done():
                break
            note = _typed_line()
            if note:
                say("%9.3f s  note      %s" % (time.monotonic() - start, note))
            try:
                now_blocks = read_all()
            except (LinkError, OSError, ValueError) as e:
                if not isinstance(link.t, HidTransport):
                    raise
                say("%9.3f s  link      lost (%s)" % (time.monotonic() - start, e))
                now_blocks = None
                while now_blocks is None:                # the link goes away while a menu or the mode screen is up
                    time.sleep(0.5)
                    if guide_done():                     # the keys go on being asked for meanwhile: one of them closes that screen
                        break
                    try:
                        try:
                            link.t.close()
                        except (OSError, ValueError):
                            pass
                        link.t = HidTransport()
                        now_blocks = read_all()
                    except (LinkError, OSError, ValueError):
                        now_blocks = None
                if now_blocks is None:
                    break
                say("%9.3f s  link      back" % (time.monotonic() - start))
            polls += 1
            now = time.monotonic() - start
            for name, _, _, only, base in KEYS_BLOCKS:
                changed = keys_changes(name, last[name], now_blocks[name], only, base)
                if changed:
                    say(keys_line(now, name, changed, now_blocks[name]))
            last = now_blocks
            if args.interval:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    say("end       %.1f s, %d reads of the %d blocks" % (time.monotonic() - start, polls, len(KEYS_BLOCKS)))
    if args.out:
        with args.out.open("a") as f:
            f.write("\n".join(out) + "\n\n")
        print("added to %s" % args.out)
    return 0


def status_lines(mode: int, meter: bytes, scope: bytes, measured: bytes, generator: bytes, arrows: int | None = None) -> list:
    """What `status` prints, from the four RAM blocks (and the arrow keys' mode, when given).
    Stock only updates a block in its own mode: the others show what was there when the instrument
    last left that mode."""
    out = ["system mode  %d (%s)" % (mode, MODES.get(mode, "unknown"))]
    g = decode_generator(generator)
    out.append("generator    %s (index %d), %s, duty %d %%, %.1f V, byte0 %d   raw %s" % (
        g["waveform"] or "?", g["waveform_index"], _eng(g["frequency_hz"], "Hz"), g["duty_percent"],
        g["amplitude_v"], g["flag0"], generator.hex()))
    s = scope_settings(scope)
    out.append("scope        %s, %s%s" % (
        "RUN" if scope[46] == 1 else "STOP" if scope[46] == 0 else "run byte %d" % scope[46],
        "settings not understood" if s is None else "%s/div, CH1 %s/div X%d %s, CH2 %s/div X%d %s, channels %s%s, trigger %s %s %s" % (
            _eng(s["seconds_per_div"], "s"), _eng(s["channels"][0]["volts_per_div"], "V"), s["channels"][0]["probe"],
            s["channels"][0]["coupling"] or "coupling byte %d" % scope[0],
            _eng(s["channels"][1]["volts_per_div"], "V"), s["channels"][1]["probe"],
            s["channels"][1]["coupling"] or "coupling byte %d" % scope[1],
            "+".join("CH%d" % (i + 1) for i, c in enumerate(s["channels"]) if c["enabled"]) or "none",
            "".join(", FFT CH%d" % (i + 1) for i, c in enumerate(s["channels"]) if c["fft"]),
            s["trigger_mode"] or "mode byte %d" % scope[23], s["trigger_edge"] or "edge byte %d" % scope[24],
            s["trigger_channel"] or "channel byte %d" % scope[22]),
        "   state %s" % scope.hex()))
    if arrows is not None:
        out.append("  arrows     %s" % arrows_text(arrows))
    for ch in (0, 1):
        block = measured[48 * ch:48 * ch + 48]
        m = decode_scope_measurements(block)
        out.append("  CH%d shown   %s, period %s, duty +%.2f %% / -%.2f %%, width +%s / -%s, Vmax %s, Vmin %s, Vpp %s, Amp %s, Vrms %s, Avg %s" % (
            ch + 1, _eng(m["frequency_hz"], "Hz"), _eng(m["period_s"], "s"), m["duty_positive_percent"],
            m["duty_negative_percent"], _eng(m["width_positive_s"], "s"), _eng(m["width_negative_s"], "s"),
            _eng(m["vmax"], "V"), _eng(m["vmin"], "V"), _eng(m["vpp"], "V"),
            _eng(m["amp"], "V"), _eng(m["vrms"], "V"), _eng(m["avg"], "V")))
        out.append("     words    %s" % " ".join(str(x) for x in struct.unpack_from("<6I6i", block)))
    held = held_screen(meter)
    out.append("meter        menu %d (%s), measuring %d (%s), variant %d, screen byte %d, unit code %d, HOLD %s%s" % (
        meter[5], METER_MENU.get(meter[5], "not known"), meter[6], METER_MEASURING.get(meter[6], "not known"),
        meter[14], meter[13], meter[16], "on" if meter[20] == 1 else "off",
        "" if held is None else ", state value %s %s%s" % (held["text"], held["prefix"], held["unit"])))
    out.append("  buzzer     the meter chip's tone %s (stock's gate on menu and variant)" % (
        "reaches the buzzer" if buzzer_follows_meter_chip(mode, meter) else "does not reach the buzzer"))
    out.append("  boxes      %s" % ", ".join(
        "%s %s (unit code %d%s)" % (name, "empty" if v is None else "%.*f" % (min(dec, 6), v), code,
                                    ": " + STATS_UNITS[code] if code in STATS_UNITS else "")
        for name, v, dec, code in decode_meter_stats(meter)))
    out.append("     state    %s" % meter.hex())
    return out


def status_from_events(link: Link, mode: int, timeout_ms: int) -> dict:
    """One status event of each kind the link sends in this mode, by sub-kind.

    The generator and scope kinds come in the scope mode, the generator alone in the generator
    mode, the meter kind in the meter mode."""
    wanted = {1: {STATUS_METER}, 2: {STATUS_GENERATOR, STATUS_SCOPE_CH1, STATUS_SCOPE_CH2},
              3: {STATUS_GENERATOR}}.get(mode, set())
    got = {}
    link.stream(STREAM_STATUS)
    try:
        deadline = time.monotonic() + timeout_ms / 1000.0
        for rs in link.events(timeout_ms):
            if rs[0] == EVT_STATUS:
                got[rs[2]] = rs[8:8 + rs[3]]
            if wanted <= set(got) or time.monotonic() > deadline:
                break
    finally:
        link.stream(0)
    return got


def cmd_status(link: Link, args) -> int:
    """Read and print the generator's settings, the scope's own measurements and the meter's
    statistics boxes: the things stock keeps in RAM and neither stream carries. With --events it
    takes what it can from the status stream (patch 3) instead of reading it on request."""
    shown = None
    if args.events:
        info = link.info()
        if not info["features"] & FEATURE_STATUS:
            raise LinkError("this patch (%d) has no status stream: run without --events" % info["patch"])
        ev = status_from_events(link, info["mode"], 2000)
        for kind, name in ((STATUS_GENERATOR, "generator"), (STATUS_SCOPE_CH1, "scope CH1"),
                           (STATUS_SCOPE_CH2, "scope CH2"), (STATUS_METER, "meter state 40..55")):
            if kind in ev:
                print("status event %d  %-18s %s" % (kind, name, ev[kind].hex()))
        if STATUS_GENERATOR in ev:
            g = decode_generator(ev[STATUS_GENERATOR])
            print("generator    %s (index %d), %s, duty %d %%, %.1f V" % (
                g["waveform"] or "?", g["waveform_index"], _eng(g["frequency_hz"], "Hz"), g["duty_percent"], g["amplitude_v"]))
        for ch, kind in ((1, STATUS_SCOPE_CH1), (2, STATUS_SCOPE_CH2)):
            if kind in ev:
                m = decode_scope_measurements(ev[kind])
                print("  CH%d shown   %s, duty +%.2f %%, Vmax %s, Vmin %s, Vpp %s, %s" % (
                    ch, _eng(m["frequency_hz"], "Hz"), m["duty_positive_percent"], _eng(m["vmax"], "V"),
                    _eng(m["vmin"], "V"), _eng(m["vpp"], "V"), "RUN" if ev[kind][48] == 1 else "STOP"))
        return 0 if ev else 1
    try:
        while True:
            mode = link.info()["mode"]
            lines = status_lines(mode, link.read(METER_STATE_ADDR, METER_STATE_LEN),
                                 link.read(SCOPE_STATE_ADDR, SCOPE_STATE_LEN),
                                 link.read(SCOPE_MEASURED_ADDR, 96), link.read(GENERATOR_ADDR, GENERATOR_LEN),
                                 link.read(ARROWS_ADDR, 1)[0])
            if lines != shown:
                if shown is not None:
                    print()
                print("\n".join(lines))
                sys.stdout.flush()
                shown = lines
            if not args.watch:
                return 0
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


# The 4 KB that dump-cal saves, by what stock keeps where (docs/stock_v120_notes.md). Only the
# calibration table is the unit's own and constant: stock rewrites the settings whenever it saves
# them, and programs 512 bytes each time although its structure is 304, so what follows the table
# is whatever its scratch buffer held.
CAL_REGIONS = (("settings", 0x000, 0x038), ("calibration table", 0x038, 0x130), ("scratch after the table", 0x130, 0x200),
               ("never programmed", 0x200, 0x800), ("bootloader flag page", 0x800, 0x1000))
CAL_TABLE = slice(0x038, 0x130)


def cal_table_sha256(page: bytes) -> str:
    """sha256 of the calibration table alone: the fingerprint to compare two dumps of one unit by.

    The sha256 of the whole page changes every time stock saves a setting."""
    if len(page) != CAL_LEN:
        raise LinkError("a settings/calibration page is %d bytes, this is %d" % (CAL_LEN, len(page)))
    return hashlib.sha256(page[CAL_TABLE]).hexdigest()


def cal_differences(a: bytes, b: bytes) -> list:
    """[(region name, bytes that differ)] for two dumps of the page, regions with a difference only."""
    if len(a) != CAL_LEN or len(b) != CAL_LEN:
        raise LinkError("a settings/calibration page is %d bytes" % CAL_LEN)
    out = []
    for name, start, end in CAL_REGIONS:
        n = sum(1 for i in range(start, end) if a[i] != b[i])
        if n:
            out.append((name, n))
    return out


def _cal_summary(page: bytes) -> bool:
    """Print what identifies a dump; False when the calibration table reads as erased."""
    table = page[CAL_TABLE]
    blank = all(x == 0xFF for x in table)
    print("crc32    %08X" % zlib.crc32(page))
    print("sha256   %s  (whole page: changes whenever stock saves a setting)" % hashlib.sha256(page).hexdigest())
    print("table    %s  (calibration table alone, 0x038-0x12F: compare dumps by this)" % cal_table_sha256(page))
    print("non-FF   %d bytes in the page, %d of %d in the table, first byte 0x%02X" % (
        sum(1 for x in page if x != 0xFF), sum(1 for x in table if x != 0xFF), len(table), page[0]))
    if blank:
        print("WARNING  the calibration table reads as erased: this is not a usable calibration backup")
    return not blank


def cmd_dump_cal(link: Link, args) -> int:
    if args.out.exists():
        raise LinkError("%s already exists: refusing to overwrite a calibration backup" % args.out)
    page = link.read_twice(CAL_ADDR, CAL_LEN, _progress)
    args.out.write_bytes(page)
    print("saved    %s (%d bytes)" % (args.out, len(page)))
    return 0 if _cal_summary(page) else 1


def cmd_cal_info(args) -> int:
    """Describe a saved dump of the page or, given two, say where they differ. No device needed."""
    pages = [p.read_bytes() for p in args.files]
    for path, page in zip(args.files, pages):
        print(path)
        if len(page) != CAL_LEN:
            raise LinkError("%s is %d bytes, not a %d-byte page" % (path, len(page), CAL_LEN))
        _cal_summary(page)
    if len(pages) == 1:
        return 0
    if len(pages) != 2:
        raise LinkError("give one dump to describe or two to compare")
    differences = cal_differences(pages[0], pages[1])
    same_table = all(name != "calibration table" for name, _ in differences)
    for name, n in differences:
        print("differ   %-24s %d bytes" % (name, n))
    if not differences:
        print("SAME     the two dumps are identical")
    elif same_table:
        print("SAME     the calibration table is identical in both; what differs is outside it")
    else:
        print("DIFFERS  the calibration tables are not the same")
    return 0 if same_table else 1


# The calibration table as OpenScope-2C53T lays it out (its factory_cal notes and the discussion of
# its PR #70; not checked here against stock's code): 60 words from 0x038, CH1 in the low half of
# each word and CH2 in the high half, words 0-29 the baselines and 30-59 the uppers, each three
# banks of ten ranges. Stock's copy in RAM, by the same notes: CH1's 60 values and then CH2's.
# These commands answer what that discussion asks of a unit and never print a value of the table.
CAL_WORDS, CAL_BANKS, CAL_RANGES = 60, 3, 10
CAL_RAM_ADDR = 0x20000358


def cal_halves(page: bytes) -> tuple:
    """([CH1's 60 values], [CH2's 60 values]) of a dump of the page."""
    if len(page) != CAL_LEN:
        raise LinkError("a settings/calibration page is %d bytes, this is %d" % (CAL_LEN, len(page)))
    words = struct.unpack_from("<%dI" % CAL_WORDS, page, CAL_TABLE.start)
    return [w & 0xFFFF for w in words], [w >> 16 for w in words]


def _cal_place(i: int) -> str:
    return "%s bank %d range %d" % ("baseline" if i < 30 else "upper", (i % 30) // CAL_RANGES, i % CAL_RANGES)


def cal_check_lines(page: bytes) -> list:
    """What can be said of a dump's table without giving any of it: counts and places only."""
    ch1, ch2 = cal_halves(page)
    lines = ["signature    0x%02X (first byte of the page)" % page[0],
             "last word    CH2's half is %s" % ("0" if ch2[-1] == 0 else "0xFFFF" if ch2[-1] == 0xFFFF else "neither 0 nor 0xFFFF")]
    for name, values in (("CH1", ch1), ("CH2", ch2)):
        kinds = (("0", [i for i, v in enumerate(values) if v == 0]),
                 ("0xFFFF", [i for i, v in enumerate(values) if v == 0xFFFF]),
                 ("above 4095", [i for i, v in enumerate(values) if 4095 < v < 0xFFFF]))
        bad = sum(len(where) for _, where in kinds)
        lines.append("%s          %d of %d values are 1 to 4095" % (name, CAL_WORDS - bad, CAL_WORDS))
        for kind, where in kinds:
            if where:
                lines.append("  %-10s %d: %s" % (kind, len(where), "; ".join(_cal_place(i) for i in where)))
        low = [i for i in range(30) if values[30 + i] <= values[i]]
        lines.append("  upper above its baseline in %d of 30 pairs%s" % (
            30 - len(low), "" if not low else "; not in: " + "; ".join("bank %d range %d" % (i // CAL_RANGES, i % CAL_RANGES) for i in low)))
    tail = page[CAL_TABLE.start + 4 * CAL_WORDS:CAL_TABLE.stop]
    lines.append("after them   %d of the %d bytes at 0x%03X-0x%03X are not 0xFF" % (
        sum(1 for x in tail if x != 0xFF), len(tail), CAL_TABLE.start + 4 * CAL_WORDS, CAL_TABLE.stop - 1))
    return lines


def cal_ram_lines(page: bytes, ram: bytes) -> list:
    """How stock's copy of the table in RAM compares with a dump of the page: counts and places only."""
    if len(ram) != 4 * CAL_WORDS:
        raise LinkError("stock's copy is %d bytes, this is %d" % (4 * CAL_WORDS, len(ram)))
    copy = struct.unpack("<%dH" % (2 * CAL_WORDS), ram)
    lines = []
    for name, values, mirror in zip(("CH1", "CH2"), cal_halves(page), (copy[:CAL_WORDS], copy[CAL_WORDS:])):
        same = [i for i in range(CAL_WORDS) if mirror[i] == values[i]]
        low12 = [i for i in range(CAL_WORDS) if mirror[i] != values[i] and mirror[i] == values[i] & 0x0FFF]
        other = [i for i in range(CAL_WORDS) if i not in same and i not in low12]
        lines.append("%s          %d of %d values in RAM are the page's" % (name, len(same), CAL_WORDS))
        for kind, where in (("its low 12 bits", low12), ("something else", other)):
            if where:
                lines.append("  %-15s %d: %s" % (kind, len(where), "; ".join(_cal_place(i) for i in where)))
    return lines


def cmd_cal_check(args) -> int:
    """Answer, for a saved dump, what OpenScope-2C53T's calibration work asks of a unit. No device needed."""
    print(args.file)
    print("\n".join(cal_check_lines(args.file.read_bytes())))
    print("             (counts and places; no value of the table is printed)")
    return 0


def cmd_cal_ram(link: Link, args) -> int:
    """Compare stock's copy of the table in RAM, as it is now, with a saved dump of the page."""
    page = args.file.read_bytes()
    cal_halves(page)
    ram = link.read_twice(CAL_RAM_ADDR, 4 * CAL_WORDS)
    print("read         %d bytes from 0x%08X, twice, the same both times" % (len(ram), CAL_RAM_ADDR))
    print("\n".join(cal_ram_lines(page, ram)))
    print("             (counts and places; no value of the table is printed)")
    return 0


def cmd_verify(link: Link, args) -> int:
    image = args.image.read_bytes()
    t0 = time.time()
    flash = link.read(APP_BASE, len(image), _progress)
    print("read     %d bytes in %.0f s" % (len(flash), time.time() - t0))
    print("image    %s" % hashlib.sha256(image).hexdigest())
    print("flash    %s" % hashlib.sha256(flash).hexdigest())
    if flash == image:
        print("MATCH    the application area of the MCU flash is exactly this image")
        return 0
    bad = [i for i in range(len(image)) if image[i] != flash[i]]
    print("DIFFERS  %d bytes, first at 0x%08X" % (len(bad), APP_BASE + bad[0]))
    return 1


def meter_text(d: dict) -> str:
    """One reading of decode_meter_event as text: "1.811 V", "2.349 V 49Hz", "0.0003 V HOLD:-0.0005V".

    A trailing '~' marks a reading that is not consistent (see decode_meter_event)."""
    unit = (d["prefix"] + d["unit"]) if d["unit"] else ""
    out = d["text"].strip() + (" " + unit if unit else "")
    if d["frequency_hz"] is not None:
        out += " %dHz" % d["frequency_hz"]
    if d["hold"]:
        h = d["held"]
        out += " HOLD:" + (h["text"] + h["prefix"] + h["unit"] if h else "?")
    if unit and not d["consistent"]:
        out += " ~"
    return out


def cmd_meter(link: Link, args) -> int:
    """Print every meter frame: the display digits, the function and the raw bytes."""
    link.stream(STREAM_METER)
    count = 0
    try:
        for rs in link.events(args.timeout):
            if rs[0] != EVT_METER:
                continue
            frame, func = rs[8:20], rs[20]
            tick = struct.unpack_from("<I", rs, 4)[0]
            if rs[2] == 1:
                d = decode_meter_event(frame, rs[24:64], func)
                print("%9.3f s  %-14s %-10s raw %s  state %s" % (
                    tick / 1000.0, meter_text(d), METER_FUNCTIONS.get(func, "func 0x%02X" % func),
                    frame.hex(" "), rs[24:64].hex()))
            else:
                print("%9.3f s  echo    %-10s raw %s" % (
                    tick / 1000.0, METER_FUNCTIONS.get(func, "func 0x%02X" % func), frame[:10].hex(" ")))
            count += 1
            if args.count and count >= args.count:
                break
    except KeyboardInterrupt:
        pass
    finally:
        link.stream(0)
    return 0 if count else 1


def cmd_scope(link: Link, args) -> int:
    """Save oscilloscope records as raw files: 56 bytes of scope state, then 1024 samples."""
    args.outdir.mkdir(parents=True, exist_ok=True)
    asm = ScopeAssembler()
    link.stream(STREAM_SCOPE)
    count = 0
    try:
        for rs in link.events(args.timeout):
            if rs[0] not in (EVT_SCOPE_HEADER, EVT_SCOPE_DATA):
                continue
            rec = asm.feed(rs)
            if rec is None:
                continue
            name = args.outdir / ("scope_%06d_ch%d_t%d.bin" % (count, rec["channel"] + 1, rec["tick"]))
            name.write_bytes(rec["state"] + rec["data"])
            d = rec["data"]
            s = scope_settings(rec["state"])
            if s is None:
                summary = "settings not understood"
            else:
                m, ch = measure_record(s, rec["channel"], d), s["channels"][rec["channel"]]
                summary = "%s/div X%d  %s/div  Vpp %s%s%s" % (
                    _eng(ch["volts_per_div"], "V"), ch["probe"], _eng(s["seconds_per_div"], "s"), _eng(m["vpp"], "V"),
                    "  " + _eng(m["frequency"], "Hz") if m["frequency"] else "", "  CLIPPED" if m["clipped"] else "")
            print("%9.3f s  CH%d  seq %3d  min %3d  max %3d  %s  -> %s" % (
                rec["tick"] / 1000.0, rec["channel"] + 1, rec["seq"], min(d[1:]), max(d[1:]), summary, name.name))
            count += 1
            if args.count and count >= args.count:
                break
    except KeyboardInterrupt:
        pass
    finally:
        link.stream(0)
    return 0 if count else 1


def main(argv=None, transport=None) -> int:
    ap = argparse.ArgumentParser(description="FNIRSI 2C53T stock-link host tool.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="identify the device and show its mode").set_defaults(fn=cmd_info)
    p = sub.add_parser("read", help="hex dump of MCU flash or SRAM")
    p.add_argument("addr", type=lambda s: int(s, 0))
    p.add_argument("length", type=lambda s: int(s, 0))
    p.set_defaults(fn=cmd_read)
    sub.add_parser("tasks", help="stock's tasks and how much of each stack was ever used since power-on (read-only)"
                   ).set_defaults(fn=cmd_tasks)
    p = sub.add_parser("wave", help="store one period of a waveform in the signal generator's table (patch 4), or show the table")
    p.add_argument("shape", nargs="?", choices=WAVE_SHAPES, help="a built-in shape")
    p.add_argument("--file", type=Path, help="one period as numbers in a text or CSV file (any count from 2: resampled to 100, "
                                             "lowest to highest stretched over the full amplitude)")
    p.add_argument("--duty", type=float, default=50.0, help="pulse: high part; triangle: rising part; in percent (default 50)")
    p.add_argument("--harmonics", help="for the shape `harmonics`: ORDER:LEVEL[:DEGREES],... for instance 1:1,3:0.33,5:0.2")
    p.add_argument("--invert", action="store_true", help="upside down")
    p.add_argument("--show", action="store_true", help="read the table that is being played and draw it; writes nothing")
    p.add_argument("--out", type=Path, help="with --show: save the 100 values to this file")
    p.set_defaults(fn=cmd_wave)
    p = sub.add_parser("status", help="generator settings, the scope's own measurements and the meter's "
                                      "MAX/MEAN/MIN, read from stock's RAM")
    p.add_argument("--watch", type=float, default=0, metavar="SECONDS",
                   help="keep reading at this interval and print again whenever something changes")
    p.add_argument("--events", action="store_true",
                   help="take the blocks from the link's status stream (patch 3) instead of reading them")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("snapshot", help="save a labelled set of memory blocks, to pass on (one per step of a procedure)")
    p.add_argument("label")
    p.add_argument("ranges", nargs="+", metavar="ADDR:LEN")
    p.add_argument("--follow", action="append", metavar="ADDR:BEFORE:AFTER",
                   help="read the pointer at ADDR and, if it points into RAM, BEFORE bytes ahead of its target and AFTER from it")
    p.add_argument("--out", type=Path, help="add the snapshot to this text file instead of printing it")
    p.add_argument("--allow-calibration", action="store_true", help="do not refuse the blocks that hold this unit's calibration")
    p.set_defaults(fn=cmd_snapshot)
    p = sub.add_parser("watch", help="read one small block over and over and report every change with its time")
    p.add_argument("range", metavar="ADDR:LEN")
    p.add_argument("--seconds", type=float, default=10.0, help="how long (default 10)")
    p.add_argument("--reads", type=int, default=0, help="stop after this many reads instead")
    p.add_argument("--label", default="", help="a word about what was being done, for the file")
    p.add_argument("--out", type=Path, help="add every change to this text file and print only the summary")
    p.add_argument("--allow-calibration", action="store_true", help="do not refuse the blocks that hold this unit's calibration")
    p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("keys", help="log what stock's state does while the instrument's keys are pressed by hand "
                                    "(read-only); lines typed meanwhile are kept as notes")
    p.add_argument("--label", default="", help="a word about what is being done, for the first line")
    p.add_argument("--out", type=Path, help="add the log to this text file as well")
    p.add_argument("--interval", type=float, default=0.05, help="seconds between reads (default 0.05)")
    p.add_argument("--seconds", type=float, default=0, help="stop after this long (default: until Ctrl-C)")
    p.add_argument("--polls", type=int, default=0, help="stop after this many reads")
    p.add_argument("--guide", metavar="KEY,KEY,...", help="say which key to press next, one after another, and write it in the log")
    p.add_argument("--step", type=float, default=5.0, help="with --guide: seconds given to each key (default 5)")
    p.set_defaults(fn=cmd_keys)
    p = sub.add_parser("dump-cal", help="save the 4 KB settings/calibration page (0x08006000)")
    p.add_argument("out", type=Path)
    p.set_defaults(fn=cmd_dump_cal)
    p = sub.add_parser("cal-info", help="describe a saved dump-cal file, or compare two (no device needed)")
    p.add_argument("files", type=Path, nargs="+", metavar="FILE")
    p.set_defaults(fn=cmd_cal_info, offline=True)
    p = sub.add_parser("cal-check", help="for a saved dump-cal file: the signature and how many values of the table are out of "
                                         "range, with their places and without the values (no device needed)")
    p.add_argument("file", type=Path, metavar="FILE")
    p.set_defaults(fn=cmd_cal_check, offline=True)
    p = sub.add_parser("cal-ram", help="compare stock's copy of the calibration table in RAM with a saved dump-cal file; "
                                       "prints counts, not values")
    p.add_argument("file", type=Path, metavar="FILE")
    p.set_defaults(fn=cmd_cal_ram)
    p = sub.add_parser("verify", help="compare the MCU flash with an image file")
    p.add_argument("image", type=Path)
    p.set_defaults(fn=cmd_verify)
    for name, fn, text in (("meter", cmd_meter, "print multimeter frames as they arrive"),
                           ("scope", cmd_scope, "save oscilloscope records as they arrive")):
        p = sub.add_parser(name, help=text)
        if name == "scope":
            p.add_argument("outdir", type=Path)
        p.add_argument("--count", type=int, default=0, help="stop after this many (default: until Ctrl-C)")
        p.add_argument("--timeout", type=int, default=2000, help="give up after this many ms of silence")
        p.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    try:
        if getattr(args, "offline", False):
            return args.fn(args)
        t = transport or HidTransport()
        try:
            return args.fn(Link(t), args)
        finally:
            if transport is None:
                t.close()
    except (LinkError, OSError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
