# What the stream carries: meter frames and scope records

How to turn the link's events into readings, volts and seconds. Everything here was
established on one retail unit (stock V1.2.0 + stock-link v1, 2026-11-23 and 24) by comparing
the bytes with what the instrument's own screen showed. `fnlink.py` implements it and
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
| 6, high nibble | `0x80` the diode function, `0x40` k, `0x20` n, `0x10` µ |
| 7 | `0x20` Ω, `0x10` F, `0x08` continuity, `0x04` M, `0x01` m |
| 8 | `0x02` V, `0x01` A, `0x20` °C, `0x80` lowest range (see below) |
| 9–11 | in AC V, byte 11 is the frequency in Hz that stock shows above the reading (seen up to 79; whether bytes 9–10 extend it is not known). In the other functions not understood: byte 11 drifts slowly and is not a checksum of the frame |

Digit segments: a `0x80`, f `0x40`, e `0x20`, b `0x08`, g `0x04`, c `0x02`, d `0x01`. Bit
`0x10` of digits 2 to 4 is the decimal point in front of that digit. When no point bit is
set and byte 8 bit 7 is, the reading is `x.xxxx` with the leading "1" bit as its first digit
(e.g. `1.0255`).

Readings seen: `9.827` kΩ on a 10 kΩ resistor, `9.201` V on a 9 V battery, `1.0255` MΩ
across fingers, `-0.006` mA, `23` °C, `0.L` / `.0L` for overload, `Auto` when the smart mode
detects nothing, `0.000` nF with open leads, `1.02` to `1.045` µF on a 1 µF capacitor,
`1.811` V across a LED in the diode function.

**AC has no annunciator.** In AC V, AC mA and AC A the frame's bytes are those of the DC
function; what says "alternating" is the function word. Seen: `0.0000` V with the probes
shorted, `0.008` mA and `0.0007` A with nothing in the current sockets, and 2.2 to 2.4 V of
mains hum with the open probes held in the hand. No AC source of known value was measured:
what is checked is the decoding, not the instrument's accuracy.

**Blank display.** While the range changes the display is empty for a frame or two, or shows
its decimal point alone (seen in capacitance). `fnlink` reports both as `blank`.

Not seen lit, so not decoded: °F, and capacitance prefixes other than n and µ. A frame with a
bit outside the table should be treated as not understood.

### Function word (checked)

| Byte 20 | Function | Byte 20 | Function |
|---|---|---|---|
| `0x00` | none sent yet: the chip powers up in Auto | `0x0A` | capacitance |
| `0x14` | Auto | `0x12` | temperature |
| `0x0C` | DC V | `0x11` | DC mA |
| `0x0D` | AC V | `0x16` | AC mA |
| `0x0B` | resistance | `0x10` | DC A |
| `0x17` | continuity | `0x15` | AC A |
| `0x0E` | diode | | |

Every key press also produces a kind 2 event: the chip's 10-byte echo (`AA 55` …) of the
order.

The function word is the order stock has just sent, so it changes one frame ahead of the
display.

### Stock's own state (checked unless noted)

Offsets are into the 40 state bytes (add `0x20001020` for the address).

| Offset | Content |
|---|---|
| 5 | position in the function menu: 0 Auto, 1 voltage, 2 continuity and diode, 3 resistance, 4 capacitance, 5 temperature, 6 mA, 7 A |
| 6 | what is being measured: 1 DC voltage, 2 AC voltage, 3 continuity, 4 diode, 5 resistance, 6 capacitance, 7 temperature, 8 DC mA, 9 AC mA, 10 DC A, 11 AC A (in Auto: what was detected) |
| 8–11 | the reading as a `float`. While stock converts it, this briefly holds the raw count (9827.0) instead of the scaled value (9.827) |
| 12 | decimals shown |
| 13 | what the screen shows: 0 a number, 1 "Auto" waiting, 2 overload, 3 blank |
| 14 | the variant inside the menu entry of offset 5: 1 or 2 (DC / AC, diode / continuity), 0 in Auto. Read in all twelve functions on 2026-11-25 |
| 16 | unit code: 1 V, 2 Ω, 3 kΩ, 4 MΩ, 5 nF, 6 µF, 9 °C, 11 mA, 12 A; `0xFF` none |
| 18–19 | in AC V: the frequency in Hz, 16 bits, a frame behind the frame's byte 11. Stale in the other functions |
| 20 | 1 while HOLD is on |
| 32–35, 36–39 | the MAX and MEAN boxes of the screen, as floats. MEAN is (MAX + MIN) / 2, not an average |

The MIN box is the next float, at offset 40: outside the 40 bytes the link sends (read it
with `fnlink.py read 0x20001048 4`). The three boxes scale on their own (9.193 V, 4.2962 V
and -600.60 mV at the same time); their decimals are at offsets 44–46 and their unit codes at
50–52, also outside: 0 mV, 1 V, 5 nF, 9 °C, 10 µA. V and mV are by the screen and by MEAN
being (MAX + MIN) / 2 only with MIN in millivolts; µA by a photograph of the screen in Small
DC current (2026-11-25), where the boxes are in µA while the reading is in mA; nF and °C
because the boxes carry there the code the reading has at offset 16. Offsets 47–49 are not
the unit, as an earlier version of this note said: they read 1 for a box in volts and 0 for
every other unit seen, and what they are is not established.

The frame's annunciators and the unit code at offset 16 come from different places. They
agree on more than 98% of frames that show something; the exceptions are the frame or two
around a change of function or of range. `fnlink.decode_meter_event` reports a reading as
`consistent` only when they agree, and takes the digits from the frame, which arrives whole,
never from the float.

### HOLD (checked)

HOLD does not reach the meter chip. Its frames go on with the live reading; stock stops
updating its state, and its screen keeps showing that frozen state. So while offset 20 is 1:

- the frame is the live reading, which the instrument's screen is **not** showing;
- the screen shows the state: the value at 8–11 written with the decimals at 12 and the unit
  at 16, or an overload when offset 13 is 2. `fnlink.held_screen` builds it.

Out of HOLD, the float written with those decimals is the display's text in every reading
where the two can be compared (455 across the captures of the second session), and offset 13
is 2 in every overload frame and in no other. In a capture with HOLD pressed half way
(`test/data/meter_hold.txt`) the state stays at -0.0005 V for 23 frames while the frame
moves between -0.0003 and 0.0005 V; the instrument's screen showed the frozen value.

## Status blocks (patch 3)

Event `0xB0` carries one block of stock's SRAM as it was when the report was built. Byte 2 of
the event is the kind; the layouts are in [`stock_v120_notes.md`](stock_v120_notes.md) and
`fnlink.py` decodes them (`decode_generator`, `decode_scope_measurements`,
`decode_meter_stats`).

| Kind | Sent in | Payload |
|---|---|---|
| 1 | scope and generator modes | 10 bytes from `0x20000F50`: the generator's settings |
| 2 | scope mode | 48 bytes from `0x20000208` (CH1's measurements as shown), then the byte at `0x20000126` (1 running, 0 stopped) |
| 3 | scope mode | 48 bytes from `0x20000238` (CH2's), then the same byte |
| 4 | meter mode | 16 bytes from `0x20001048`: meter state 40 to 55, i.e. the MIN box, the decimals and unit codes of the three boxes |

One event goes out every 100 ms, the kinds of the current mode in turn, so each block is
refreshed every 100 to 300 ms. A stopped oscilloscope sends no records; kinds 2 and 3 are how
a host tells that from silence. The same blocks can be read on request with `READ`, which is
all patch 2 offers.

## The generator's table (patch 4)

`0x20000F5A`, 100 samples of 16 bits, one period: what the DAC is given, in order, over and
over. Stock's own values run from 20 to 3742 at 3.0 V (ram: five dumps). `READ` returns it on
any patch; `WAVE` stores into it from patch 4, scaled as stock scales its sine:

    stored = min(sample, 4094) * min(amplitude, 30) // 33 + 20

with `amplitude` the byte at `0x20000F59` (tenths of a volt). So a full-scale sample is 3741
at 3.0 V and 1260 at 1.0 V, and nothing stored is ever above what stock stores. (code and
emulation; on the unit at 3.0 V, where two waveforms read back as 20 to 3741)

With patch 5 and an image built with `--menu-usb-wave`, `WAVE` also keeps the samples as
sent, and the added entry of the menu that INFO names plays them, rescaled whenever the
instrument rebuilds its table, until power-off. They count only once positions 0 to 99 have
arrived in order.

The last sample is at `0x20001020`, which is also where the 40 bytes of "meter state" that a
meter event carries begin: the first two of those bytes are this sample, not meter state.

## Oscilloscope

A record is one acquisition of one channel as stock has it after applying its calibration:
1024 samples of 8 bits. It travels as one `0xA0` header (56 bytes of stock's scope state,
from `0x200000F8`) followed by 19 `0xA1` data events.

### Scope state bytes

| Offset | Content | Evidence |
|---|---|---|
| 0 | coupling of CH1: 1 DC, 0 AC. It does not say whether the channel is on (that is offset 20) | switched on the unit, 2026-11-25: the only byte that changed, and the trace dropped to straddle zero. CH2's coupling is not located (offset 1 was 0 throughout and was not switched) |
| 2, 3 | volts per division of CH1, CH2 as an index: 0 = 10 mV, then 1-2-5 steps to 9 = 10 V | CH1 checked at 0, 2 and 5. CH2 at 3: 344 mV peak to peak from the record against 345 mV by stock's own measurement (2026-11-25); 2 was the step before it |
| 4, 5 | vertical position of CH1, CH2, signed: zero volts is sample value 128 + position | CH1 checked in three sessions; CH2 assumed |
| 6, 7 | probe setting of CH1, CH2: 0 = X1, 1 = X10 | both checked both ways (2026-11-25): at X10 the record scaled ten times larger gives 3.36 V and 3.30 V where stock's own measurement gives 3.35 V and 3.29 V |
| 20 | channels on: bit 0 CH1, bit 1 CH2 | checked (1, 2 and 3) |
| 21 | low nibble: trigger source, 1 CH1, 2 CH2. High nibble: 1 with CH2 alone | assumed; seen `0x01` with CH1 alone and `0x12` with CH2 alone |
| 22 | 0 with CH1 alone, 1 with CH2 alone | seen; not established (the selected channel?) |
| 28–29 | trigger level, signed 16 bits, in counts from the middle (128) | inferred: it changed by 2 when an arrow key was pressed with the arrows not on the scales |
| 45 | time per division as an index: 0 = 10 ns, then 1-2-5 steps | checked at 8 (5 µs, photograph of the screen) and 13 (200 µs); 11 and 16 fit 50 µs and 2 ms |
| 46 | 1 running, 0 stopped (the RUN/STOP key) | checked: three RAM dumps running, one stopped |

### Scales

- **Vertical: 25 counts per division.** `volts = (sample - zero) / 25 * volts_per_div * probe`.
  The generator's square wave measured 3.01 V peak to peak on the instrument's screen and
  spans 150–152 counts at 500 mV per division.
- **Horizontal: 25 samples per division**, so the sample period is the time base over 25.
  The 1 kHz wave is 125 samples long at 200 µs per division and 12.5 at 2 ms. At 5, 10 and
  20 µs per division the frequency computed from the record with this rule agreed with the
  instrument's own measurement (generator at 40 kHz on 2026-11-24 for 10 µs, at 50 kHz on
  2026-11-25 for the three; compared on the screen of the application that uses this format,
  the figures of the second day were not written down). At 2 µs per division `fnlink.py
  scope` got 50 kHz from the record and stock's own measurement was 50 kHz (2026-11-25). At
  1 µs the record holds two periods of that wave, too few for `scope` to give a frequency,
  but its rising edges are 500 and 499 samples apart in the two records taken: 20 µs at
  40 ns a sample. Below 1 µs and above 2 ms per division this was not checked; the fastest
  time bases cannot have 25 real samples per division.
- The screen is 12 by 8 divisions: 300 samples wide, 200 counts tall, centred on 128.

### Things to know

- The first sample of every record is a filler byte of the acquisition, not a measurement.
- Records are not aligned to the trigger. Stock aligns when it draws; whoever displays a
  record has to find the edge.
- A wave that leaves the converter's range sits exactly on 5 and 244.
- With both channels on, each acquisition gives a CH1 record and a CH2 record 1–2 ms apart.
- Coupling and trigger edge are somewhere in the state bytes and have not been located.
- Stock computes its own measurements (frequency, duty, Vmax, Vmin, Vpp) and keeps them in
  RAM just after the scope state. The link does not send them; see `stock_v120_notes.md`.
