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
import struct
import sys
import time
import zlib
from pathlib import Path

VID, PID = 0x1209, 0x0001
REPORT_LEN = 64
PAYLOAD_MAX = 56
CMD_INFO, CMD_READ, CMD_STREAM = 0x01, 0x02, 0x03
EVT_METER, EVT_SCOPE_HEADER, EVT_SCOPE_DATA, EVT_STATUS = 0x90, 0xA0, 0xA1, 0xB0
EVENTS = (EVT_METER, EVT_SCOPE_HEADER, EVT_SCOPE_DATA, EVT_STATUS)
STREAM_METER, STREAM_SCOPE, STREAM_STATUS = 0x01, 0x02, 0x04
FEATURE_STATUS = 0x01                   # INFO feature bit: the link has the status stream (patch 3)
# Sub-kinds of a status event: which block of stock's SRAM it carries.
STATUS_GENERATOR, STATUS_SCOPE_CH1, STATUS_SCOPE_CH2, STATUS_METER = 1, 2, 3, 4
STATUS = {0: "ok", 1: "unknown command", 2: "address range not readable", 3: "bad length"}

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
# were read on the unit one by one on 2026-11-25 (sessions/2026-11-25_generator_ch2_boxes). The entry
# before "Square Wave", index 0, has not been seen named.
GENERATOR_WAVEFORMS = {1: "Square Wave", 2: "Sawtooth wave", 3: "Half-wave", 4: "Full Wave", 5: "Step Wave",
                       6: "Reverse Step Wave", 7: "Index Up", 8: "Index Decrease", 9: "Direct Current",
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

    "extra" are the three words not identified (the first looks like a second amplitude).
    Checked for CH1 against a photograph of the screen; CH2's block follows CH1's and was only
    seen with the channel off (a frequency and its period, the rest zero)."""
    if len(words) < 4 * SCOPE_MEASURED_WORDS:
        raise LinkError("measurement block too short")
    w = struct.unpack_from("<6I6i", words)             # the voltages are signed: Vmin goes below zero
    return {"frequency_hz": w[0], "period_s": w[1] * 1e-9, "duty_positive_percent": w[2] / 100.0,
            "duty_negative_percent": w[3] / 100.0, "width_positive_s": w[4] * 1e-9, "width_negative_s": w[5] * 1e-9,
            "vmax": w[6] * 1e-6, "vmin": w[7] * 1e-6, "vpp": w[8] * 1e-6, "extra": [x * 1e-6 for x in w[9:12]]}


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
SAMPLES_PER_DIV = 25        # samples per horizontal division (checked from 5 us to 2 ms per division)
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
    factor and the sample value of zero volts. CH2's range byte was checked on hardware at X1
    (2026-11-25); its position and probe bytes are assumed to mirror CH1's."""
    t = seconds_per_div(state[45])
    v = [volts_per_div(state[2]), volts_per_div(state[3])]
    if t is None or None in v:
        return None
    channels = []
    for ch in (0, 1):
        probe = 10 if state[6 + ch] == 1 else 1
        position = state[4 + ch] - 256 if state[4 + ch] > 127 else state[4 + ch]
        channels.append({"enabled": bool(state[20] & (1 << ch)), "volts_per_div": v[ch] * probe,
                         "probe": probe, "zero": 128 + position})
    level = int.from_bytes(state[28:30], "little", signed=True)
    return {"channels": channels, "seconds_per_div": t, "sample_period": t / SAMPLES_PER_DIV,
            "time_base_checked": 11 <= state[45] <= 16, "trigger_level": 128 + level}


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

    def _request(self, cmd: int, length: int = 0, addr: int = 0, timeout_ms: int = 1000) -> bytes:
        self.seq = (self.seq + 1) & 0xFF
        rq = bytes([cmd, self.seq, length, 0]) + struct.pack("<I", addr)
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
        return d

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
    return 0


def cmd_read(link: Link, args) -> int:
    data = link.read(args.addr, args.length)
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        print("%08X  %-47s  %s" % (args.addr + off, row.hex(" "),
                                   "".join(chr(c) if 32 <= c < 127 else "." for c in row)))
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
# unit: SELECT took it from 2 to 0). 0 and 2 were watched on the unit, 3 is from the code alone,
# 1 has not been looked at. Bit 7 is the channel the keys act on when both are shown (code).
# In mode 2 up/down change ms[0x1C], which OpenScope-2C53T's map of the structure calls the
# trigger level.
ARROWS_ADDR = 0x2000044C
ARROWS = {0: "scale: left/right change the time base, up/down the volts per division",
          1: "mode 1 (not looked at)",
          2: "move: left/right move the trace sideways, up/down the trigger level",
          3: "cursors"}


def arrows_text(value: int) -> str:
    return "%s%s (byte 0x%02X; SELECT changes it)" % (
        ARROWS.get(value & 0x0F, "mode %d (unknown)" % (value & 0x0F)), ", acting on CH2" if value & 0x80 else "", value)


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
        "settings not understood" if s is None else "%s/div, CH1 %s/div X%d, CH2 %s/div X%d, channels %s" % (
            _eng(s["seconds_per_div"], "s"), _eng(s["channels"][0]["volts_per_div"], "V"), s["channels"][0]["probe"],
            _eng(s["channels"][1]["volts_per_div"], "V"), s["channels"][1]["probe"],
            "+".join("CH%d" % (i + 1) for i, c in enumerate(s["channels"]) if c["enabled"]) or "none"),
        "   state %s" % scope.hex()))
    if arrows is not None:
        out.append("  arrows     %s" % arrows_text(arrows))
    for ch in (0, 1):
        block = measured[48 * ch:48 * ch + 48]
        m = decode_scope_measurements(block)
        out.append("  CH%d shown   %s, period %s, duty +%.2f %% / -%.2f %%, width +%s / -%s, Vmax %s, Vmin %s, Vpp %s" % (
            ch + 1, _eng(m["frequency_hz"], "Hz"), _eng(m["period_s"], "s"), m["duty_positive_percent"],
            m["duty_negative_percent"], _eng(m["width_positive_s"], "s"), _eng(m["width_negative_s"], "s"),
            _eng(m["vmax"], "V"), _eng(m["vmin"], "V"), _eng(m["vpp"], "V")))
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
    p = sub.add_parser("dump-cal", help="save the 4 KB settings/calibration page (0x08006000)")
    p.add_argument("out", type=Path)
    p.set_defaults(fn=cmd_dump_cal)
    p = sub.add_parser("cal-info", help="describe a saved dump-cal file, or compare two (no device needed)")
    p.add_argument("files", type=Path, nargs="+", metavar="FILE")
    p.set_defaults(fn=cmd_cal_info, offline=True)
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
