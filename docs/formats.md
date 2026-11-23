# What the stream carries: meter frames and scope records

How to turn the link's events into readings, volts and seconds. Everything here was
established on one retail unit (stock V1.2.0 + stock-link v1, 2026-11-23) by comparing the
bytes with what the instrument's own screen showed. `fnlink.py` implements it and
`test/test_decode.py` checks it against the captures in `test/data/`.

Evidence labels: **checked** (compared with the screen or with a known input), **assumed**
(follows the pattern of a checked field, not compared itself).

## Multimeter

The meter is a separate chip that sends the main processor a 12-byte frame with its display
about six times a second (one every ~165 ms). The link forwards every frame as event `0x90`,
kind 1, with these bytes:

| Event bytes | Content |
|---|---|
| 8–19 | the frame (`5A A5` …) |
| 20 | last order stock sent the chip, i.e. the selected function |
| 21 | system mode |
| 24–63 | 40 bytes of stock's meter state, from `0x20001020` |

### The display frame (checked)

| Frame bytes | Content |
|---|---|
| 0–1 | `5A A5` |
| 2–6 | four seven-segment digits, each split across two bytes: digit *i* (0–3) is the high nibble of byte 2+*i* and the low nibble of byte 3+*i* |
| 2, low nibble side | `0x10` minus sign, `0x08` a leading "1" (4½ digits) |
| 6, high nibble | `0x40` k, `0x20` n |
| 7 | `0x20` Ω, `0x10` F, `0x08` continuity, `0x04` M, `0x01` m |
| 8 | `0x02` V, `0x01` A, `0x20` °C, `0x80` lowest range (see below) |
| 9–11 | not understood; byte 11 drifts slowly and is not a checksum of the frame |

Digit segments: a `0x80`, f `0x40`, e `0x20`, b `0x08`, g `0x04`, c `0x02`, d `0x01`. Bit
`0x10` of digits 2 to 4 is the decimal point in front of that digit. When no point bit is
set and byte 8 bit 7 is, the reading is `x.xxxx` with the leading "1" bit as its first digit
(e.g. `1.0255`).

Readings seen: `9.827` kΩ on a 10 kΩ resistor, `9.201` V on a 9 V battery, `1.0255` MΩ
across fingers, `-0.006` mA, `23` °C, `0.L` / `.0L` for overload, `Auto` when the smart mode
detects nothing.

Not seen lit, so not decoded: the AC annunciator, µ, °F, diode, and capacitance prefixes
other than n. The n prefix itself was only seen with open leads (`0.000`) and was not
compared with the screen. A frame with a bit outside the table should be treated as not
understood.

### Function word (checked)

| Byte 20 | Function | Byte 20 | Function |
|---|---|---|---|
| `0x00` | none sent yet: the chip powers up in Auto | `0x0A` | capacitance |
| `0x14` | Auto | `0x12` | temperature |
| `0x0C` | DC V | `0x11` | DC mA |
| `0x0B` | resistance | `0x10` | DC A |
| `0x17` | continuity | | |

`0x0D` AC V, `0x0E` diode, `0x16` AC mA and `0x15` AC A are the codes OpenScope-2C53T
documents; they were not reached in these captures. Every key press also produces a kind 2
event: the chip's 10-byte echo (`AA 55` …) of the order.

The function word is the order stock has just sent, so it changes one frame ahead of the
display.

### Stock's own state (checked unless noted)

Offsets are into the 40 state bytes (add `0x20001020` for the address).

| Offset | Content |
|---|---|
| 5 | selected function as an index: 0 Auto, 1 DC V, 2 continuity, 3 resistance, 4 capacitance, 5 temperature, 6 DC mA, 7 DC A |
| 6 | what is being measured: 1 voltage, 3 continuity, 5 resistance, 6 capacitance, 7 temperature, 8 mA, 10 A (in Auto: what was detected) |
| 8–11 | the reading as a `float`. While stock converts it, this briefly holds the raw count (9827.0) instead of the scaled value (9.827) |
| 12 | decimals shown |
| 16 | unit code: 1 V, 2 Ω, 3 kΩ, 4 MΩ, 5 capacitance, 9 °C, 11 mA, 12 A; `0xFF` none |
| 32–39 | two more floats that track the reading (looks like maximum and minimum; assumed) |

The frame's annunciators and the unit code at offset 16 come from different places. They
agree on more than 98% of frames; the exceptions are the frame or two around a change of
function. `fnlink.decode_meter_event` reports a reading as `consistent` only when they agree,
and takes the digits from the frame, which arrives whole, never from the float.

## Oscilloscope

A record is one acquisition of one channel as stock has it after applying its calibration:
1024 samples of 8 bits. It travels as one `0xA0` header (56 bytes of stock's scope state,
from `0x200000F8`) followed by 19 `0xA1` data events.

### Scope state bytes

| Offset | Content | Evidence |
|---|---|---|
| 2, 3 | volts per division of CH1, CH2 as an index: 0 = 10 mV, then 1-2-5 steps to 9 = 10 V | checked at 0, 2 and 5 |
| 4, 5 | vertical position of CH1, CH2, signed: zero volts is sample value 128 + position | CH1 checked in three sessions; CH2 assumed |
| 6, 7 | probe setting of CH1, CH2: 0 = X1, 1 = X10 | CH1 checked both ways; CH2 assumed |
| 20 | channels on: bit 0 CH1, bit 1 CH2 | checked (1 and 3) |
| 21 | trigger source: 1 CH1, 2 CH2 | assumed |
| 28–29 | trigger level, signed 16 bits, in counts from the middle (128) | inferred: it changed by 2 when an arrow key was pressed with the arrows not on the scales |
| 45 | time per division as an index: 0 = 10 ns, then 1-2-5 steps | checked at 13 (200 µs); 11 and 16 fit 50 µs and 2 ms |

### Scales

- **Vertical: 25 counts per division.** `volts = (sample - zero) / 25 * volts_per_div * probe`.
  The generator's square wave measured 3.01 V peak to peak on the instrument's screen and
  spans 150–152 counts at 500 mV per division.
- **Horizontal: 25 samples per division**, so the sample period is the time base over 25.
  The 1 kHz wave is 125 samples long at 200 µs per division and 12.5 at 2 ms. Outside 50 µs
  to 2 ms per division this was not checked; the fastest time bases cannot have 25 real
  samples per division.
- The screen is 12 by 8 divisions: 300 samples wide, 200 counts tall, centred on 128.

### Things to know

- The first sample of every record is a filler byte of the acquisition, not a measurement.
- Records are not aligned to the trigger. Stock aligns when it draws; whoever displays a
  record has to find the edge.
- A wave that leaves the converter's range sits exactly on 5 and 244.
- With both channels on, each acquisition gives a CH1 record and a CH2 record 1–2 ms apart.
- Coupling and trigger edge are somewhere in the state bytes and have not been located.
