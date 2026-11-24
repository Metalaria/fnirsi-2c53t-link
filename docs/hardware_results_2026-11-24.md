# Second session on hardware, 2026-11-24

Same unit, same image as the day before (stock V1.2.0 + stock-link v1, patch 2): nothing was
flashed. The aim was to close what the first session left open in the multimeter and to find
in RAM what the stream does not carry. Host: `fnlink.py` on macOS 13.7.8, Python 3.9, hidapi.

## Method

- `fnlink.py meter` captures of each function, with a note of what the screen showed and, for
  some, a photograph of it.
- Whole-RAM reads (`0x20000000`-`0x20037FFF`, 229,376 bytes) with one setting changed between
  two of them, and a photograph of the screen for each.
- For HOLD: a loop reading two RAM regions about four times a second while the key was
  pressed and released twice, keeping only the bytes that follow the key.

The RAM dumps are not in this repository and should not be published by anyone who repeats
this: they contain data of FNIRSI's firmware and the unit's calibration values as loaded.
The meter captures are in `test/data/` (frames and the 40 state bytes that travel with them).

## Multimeter

| | Result |
|---|---|
| AC V, AC mA, AC A | No AC annunciator in the frame; the function word tells. `0.0000` V shorted, `0.008` mA and `0.0007` A idle, as on the screen. 2.2 to 2.4 V with the open probes in the hand |
| AC frequency | Frame byte 11 and state bytes 18–19. 0 Hz shorted; 49 to 79 Hz of hum in the hand, 57 Hz in a photograph |
| Capacitance | µ is byte 6 `0x10`, stock's unit code 6. A 1 µF capacitor read 1.02 to 1.045 µF; the screen's MAX box read 1.0450uF |
| Diode | Byte 6 `0x80`, volts. `.0L` open; a LED read 1.811 V for 279 frames, each equal to stock's own value, and the same on the screen |
| HOLD | State byte 20. The meter chip is not told: frames go on live, stock's state and screen freeze |
| MAX / MEAN / MIN | Floats at state 32, 36 and 40, each with its own decimals and unit. MEAN is (MAX + MIN) / 2 |
| Screen status | State byte 13: 0 number, 1 Auto waiting, 2 overload, 3 blank |
| Blank display | While the range changes: empty, or the decimal point alone |

A capture of 300 frames with the capacitor held by hand has no inconsistent reading once the
value has settled. Moving a probe makes the meter change range, and for a frame or two the
display is blank or disagrees with stock's unit: that is what `consistent` is for.

Still not seen: °F, capacitance beyond µF, and a forward voltage of a silicon diode (only a
LED). No AC source of known value was measured.

Still assumed in what was found in RAM: the generator's waveform indices other than 1 and 4,
its first byte, the unit codes of the MAX / MEAN / MIN boxes, CH2's measurements, and three
of the twelve measurement words.

## Found in RAM, not in the stream

Details and addresses in [`stock_v120_notes.md`](stock_v120_notes.md).

- **Signal generator** settings at `0x20000F50`: waveform, frequency in Hz, duty, amplitude,
  and the 100-sample table of one period. Five dumps, each matching its photograph.
- **Oscilloscope**: stock's own measurements of CH1 (frequency, period, duty, pulse widths,
  Vmax, Vmin, Vpp) in three blocks at `0x20000148`, `0x200001A8` and `0x20000208`, and the
  running/stopped flag at scope state byte 46. Checked against a photograph of the stopped
  screen (40.23 kHz, 64.51 %, 114.0 mV). Time base index 8 is 5 µs per division.

## What it means for a host

- The multimeter is complete with the image already on the unit: every function of the key
  cycle decodes, and HOLD and the AC frequency are in the 40 state bytes each frame carries.
- During HOLD a host has to choose: the frame is the live reading, the state is what the
  instrument shows. `fnlink.py` prints both.
- The generator's settings, the scope's own measurements and the MIN box are not in the
  stream, and do not need to be: a host reads them with `READ` whenever it wants them, with
  the image already on the unit. `fnlink.py status` does it in four requests. This session
  read about 500 blocks a second that way for 39 s without an error.

## Patch 3 on the unit, the same afternoon

The status stream (README, "Protocol") was written after the sessions above, checked in the
emulator, and then flashed on the same unit over patch 2.

| | |
|---|---|
| Image | built twice (Ubuntu 24.04 under WSL, `gcc-arm-none-eabi 15:13.2.rel1-2`; and on the bench machine by applying the ten changed bytes and the cave to the official image), same sha256 `6bdff04c…30f1`, as in `reference/` |
| Emulator suite on the builder's machine | 149 checks, 0 failed |
| Flashing | `scripts/iap_flash.py flash` of OpenScope-2C53T from macOS, through the factory IAP drive: fourth update of this unit that way, fourth success |
| `fnlink.py info` | protocol 1, patch 3, "status streamed" |
| `fnlink.py verify` | 754,064 bytes read back in 27 s, identical to the image |
| Status stream, meter mode | kind 4 only; the MIN box empty (not a number) with the meter idle in Auto |
| Status stream, scope mode | kinds 1, 2 and 3. Generator `01 01 00 00 40 9C 00 00 28 0F`: Square Wave, 40 kHz, 40 %, 1.5 V, as set. CH1: 39996 Hz, 40.00 %, Vpp 171.8 mV, run byte 1 |
| …after pressing STOP | CH1: 40004 Hz, 38.70 %, Vpp 170.0 mV, run byte 0 |
| Status stream, generator mode | kind 1 only, the same ten bytes |
| Meter stream | unchanged, a frame every ~165 ms |
| The companion app, written against patch 2 | works unchanged: it subscribes with mask 3 and reads the rest with `READ` |
| USB Sharing, settings across a power cycle | as before |

The generator's duty cycle had only been seen at 50 %; here it is 40 (`0x28`) and the scope
measures 40.00 %. CH2 was off again: its block carries a frequency of noise and zeros.

The copy of the settings/calibration page meant to be taken just before the update was
skipped, so the page read afterwards has only the 2026-11-23 copy to be compared with, and
stock has saved settings since then. Compared by region (`fnlink.py cal-info`, written for
this):

| Region of the page | 2026-11-23 against after patch 3 |
|---|---|
| `0x000`-`0x037` settings | differ within `0x003`-`0x016` and `0x025`-`0x02E` |
| `0x038`-`0x12F` calibration table | identical |
| `0x130`-`0x1FF` after the structure | differ within `0x140`-`0x1E7` (168 bytes): `0xFF` before, zeros and RGB565 values now |
| the rest, and the bootloader flag page | identical |

The 168 bytes are not calibration and not written by the update: stock programs 512 bytes from
a staging buffer of which its structure fills 304, and the same 168 bytes were found in RAM at
`0x2002AD40`, inside that buffer (`stock_v120_notes.md`). 186 bytes differ in all, none of
them in the calibration table: it is the same on 2026-11-23 and after the update to patch 3.

## Corrections to the first session's notes

- `formats.md` said state bytes 32–39 "look like maximum and minimum". They are MAX and MEAN.
- The first `fnlink.py` marked every µF and every diode reading with `~` because it did not
  know either annunciator. The readings themselves were decoded correctly.
