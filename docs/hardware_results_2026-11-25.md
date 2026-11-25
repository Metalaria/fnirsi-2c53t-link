# Third session on hardware, 2026-11-25: stock watched while it runs

Same unit, stock V1.2.0 with patch 3. Nothing was flashed. The aim was not this project's:
OpenScope-2C53T's reverse engineering keeps a list of things about stock that its authors could
only settle with a debug probe on a running, opened unit (`docs/re/parity2_static.md`, section
5, at their commit `7480998`). Several of them are plain reads of RAM, which the link does over
USB with the case closed. This is what came out. The captures are in
[`sessions/2026-11-25_openscope_parity/`](../sessions/2026-11-25_openscope_parity/README.md).
Addresses are given as they give them: `ms` is stock's state structure at `0x200000F8`.

## Method

- `fnlink.py snapshot`, new for this: a labelled set of RAM blocks saved after each key press
  of a fixed sequence. `fnlink.py watch`, also new: one small block read as fast as the link
  answers (2.0 ms a read), every change noted with the host's clock.
- Signal: the instrument's own generator ("Sawtooth wave", which is a triangle, 2 kHz, 3.0 V)
  into CH1. CH2 off, trigger AUTO, 200 us and 50 mV per division, probe setting X1.
- Host: macOS 13.7.8, Python 3.9, hidapi. Times are the host's; the instrument's clock was not
  read. The two tools refuse the settings/calibration page and the RAM tables it is loaded
  into, so nothing of the unit's calibration is in what was saved.
- One unit. Where a rule is said to hold, the number of snapshots it was checked on is given.

## The arrow keys have a mode, and SELECT changes it

The first three attempts at the zoom sequence captured something else: the arrow keys moved
the trace instead of changing the time base. Stock keeps what the arrow keys do in the low
nibble of `ms[0x354]` (`0x2000044C`):

| Value | Arrow keys | Source |
|---|---|---|
| 0 | left/right: time base `ms[0x2D]` (right is one step faster); up/down: range `ms[2]` (up is one step more volts per division) | code and unit |
| 1 | not looked at | |
| 2 | left/right: `ms[0x1A]`, the horizontal position, one screen column a press; up/down: `ms[0x1C]` | unit |
| 3 | cursors, reached only when they are on | code |

Code: the left/right handler tests the nibble at `0x0800B3A6`, and time base changes only at 0.
The nibble is advanced by `0x0800D38C`, the oscilloscope branch of the handler at `0x0800D2F8`,
which is entry 4 of the table of key handlers at `0x08046548`; from 2 it goes back to 0 unless
the cursors are on. Unit: one press of SELECT took the byte from `0x02` to `0x00` (watched with
`fnlink.py watch 0x2000044C:1`), after which right and left moved `ms[0x2D]` 13 -> 12 -> 13.
The order of that table matches the bit order of OpenScope's confirmed button map (entry 14,
PLAY/PAUSE, holds the RUN/STOP code they place at `0x0800BE5E`). `fnlink.py status` now prints
the mode.

## Stopped: what the display array holds

56 stopped snapshots in four sessions, all consistent with what follows
(`tools/paused_view_check.py` redoes the comparison).

The record does not change while stopped: the 1024 bytes at `ms+0x5B0` are identical in every
stopped snapshot of a session. `ms[0xDBC]` gets bit 15 set at STOP; its low 15 bits are the
anchor `T`. The tuple frozen at capture stays as described: `ms[0xDC0]` time base, `ms[0xDBE]`
mask, `ms[0xDC1]` range, `ms[0xDC3]` position, and `ms[0xDC8]`, which holds `ms[0x1A]` as it
was at capture.

**At the captured time base** the 301-byte array of CH1 (`ms+0x356`) is a slice of the record:

    array[c] = record[T - 150 - ms[0xDC8] - min(pan, 0) + c]
    first drawn column ms[0xDB4] = max(pan, 0),  width ms[0xDB6] = 301 - |pan|

where `pan` is `ms[0xDE8]` (a 64-bit signed number, repeated at `ms[0xDF0]`) divided by the
time of one column. Moving the trace right leaves the array alone and starts drawing later;
moving it left shifts the array. Either way only the 301 samples that were on the screen at
STOP are shown: the width shrinks, nothing more of the record comes into view. `ms[0xDE0]` was
0 in every snapshot. (42 snapshots, three records.)

`ms[0xDD0]` and `ms[0xDD8]` (64-bit signed) are the times of the screen's left and right edges
from the trigger in units of 0.4 ns: -3,000,000 and +3,020,000 at 200 us per division, 150 and
151 columns of 20,000. They move with the pan and are not recomputed when the time base is
changed while stopped.

**Vertical range changed** (`ms[2]` 2 -> 3 -> 1 with `ms[0xDC1]` still 2), their closed form
holds exactly, with the result truncated:

    y = clamp(int(128 + pos_now + V[range_cap] / V[range_now] * (raw - 128 - pos_cap)), 28, 228)

301 of 301 columns at both k = 0.5 and k = 2.5; rounding instead of truncating gives 78 and 32
columns off by one.

**Zoomed in** (time base 13 -> 12, 11, 10, 9; `ms[0xE08]` = 500, 250, 100, 50): the array is,
byte for byte, the output of the 16-tap interpolator they decoded, run over the record with the
coefficient table at `0x080466E0`, factors 2, 4, 10 and 20. 301 of 301 columns at each of the
four. Column 150 sits at record position `T - ms[0xDC8]` (167.00, 167.00, 167.10, 167.10 for
T = 165 and a captured position of -2). The pointers `ms[0xDFC]` and `ms[0xE00]` were null in
every snapshot, these four included, and `ms[0xE04]` stayed 0: by the time the screen has been
redrawn there is no interpolated copy to find.

**Zoomed out** (time base 14, 15, 16, 17; `ms[0xE08]` = 2500, 5000, 10000, 25000): the array is
not touched. Only the drawn column and width change:

    width ms[0xDB6] = 301000 // ms[0xE08]     (120, 60, 30, 12)
    column ms[0xDB4] = 150 - width // 2       (90, 120, 135, 144)

So the trace is drawn narrower around the centre, again covering only the 301 samples of the
stopped screen. Which of them each drawn column takes is decided when drawing and is not in
RAM: a photograph of the screen would be needed for that.

`ms[0x1A]` is rescaled with the time base: -2 became -4, -8, -20, -40 zooming in and 0 zooming
out, and stayed 0 back at the captured time base, while the picture stayed where it was (the
pan is the separate number above).

## Timers

| Question | Their reading of the code | Measured |
|---|---|---|
| Period of the key scan | 10 ms, not proven | `ms[0x45]` (`0x2000013D`) counts 0 to 50 and wraps; 498 changes in 4.980 s: **10.00 ms** a call, 510 ms a cycle |
| Length of the key click | 11 scan ticks, about 110 ms | `0x20002D67` is 0 at rest; after a key it reads 1 to 10, one step every 10 ms, then 0. From the first 1 to the 0: 100.1, 101.0 and 99.0 ms in three presses, so **11 ticks from the key to the end** |
| Period of the auto-off handler | 1 s, not proven | `ms[0xF6C]` (`0x20001064`) with auto-off at 15 min: +29 in 28.999 s, **999.97 ms** a count |
| Does anything but a key reset it | no other writer; check live | Oscilloscope running on a signal, USB connected and enumerated: it counts on. One key press: back to 0 at once, and the next count comes on the old 1 s phase |
| `0x20002B20` | SysTick ticks per unit | 240,000 (1 ms at 240 MHz) |

With auto-off set to off (as the unit normally is) `ms[0xF6C]` stayed 0. Sound volume
`ms[0xF62]` was 10 and brightness `ms[0xF61]` 100 during all of this.

The same 1 s handler shows in the acquisition counters: `ms[0xDB2]` goes up by one about every
15 ms and is cleared every 1000 ms (ten clears, 999 to 1001 ms apart), its last value kept in
`ms[0xDB1]`: 66 to 71 a second, 67, 67, 66 repeating once settled, with one channel at 200 us
per division. `ms[0xDB0]` moves among 0, 1 and 2 within each acquisition.

## The meter's functions and the buzzer

Later the same day, for OpenScope-2C53T's pull request 102 (their continuity beep, moved to
the meter chip's own buzzer tone on PC3, as stock does it). Captures in
[`sessions/2026-11-25_meter_functions/`](../sessions/2026-11-25_meter_functions/README.md).

**Stock's handler.** EXTI line 3 is PC3. The handler at `0x08009C10` returns unless
`ms[0xF68]` is 1 (multimeter); then it writes 10 to the buzzer's duty (TMR11 C1DT,
`0x40015434`) if `ms[0xF2D]` is 0, or if `ms[0xF2D]` and `ms[0xF36]` are both 2, and in
every case zeroes `0x20002D67`. `tools/exti3_gate.py` runs it in an emulator over 256
combinations of the three bytes: the duty is written in those five and no other. (code, emu)

**Which functions those are.** The three bytes in each function of the meter menu (unit):

| Function on screen | `ms[0xF2D]` | `ms[0xF2E]` | `ms[0xF36]` | Chip's tone reaches the buzzer |
|---|---|---|---|---|
| Automatic | 0 | 0 | 0 | yes |
| DC Voltage | 1 | 1 | 1 | no |
| AC Voltage | 1 | 2 | 2 | no |
| Continuity | 2 | 3 | 2 | yes |
| Diode | 2 | 4 | 1 | no |
| Resistance | 3 | 5 | 1 | no |
| Capacitance | 4 | 6 | 1 | no |
| Temperature | 5 | 7 | 1 | no |
| Small DC current | 6 | 8 | 1 | no |
| Small AC current | 6 | 9 | 2 | no |
| Large DC current | 7 | 10 | 1 | no |
| Large AC current | 7 | 11 | 2 | no |

The bytes were read; the last column is the handler's code applied to them. Only Continuity
was tried with the leads. `ms[0xF36]` is therefore the variant inside a menu entry (DC or AC,
diode or continuity), which is also how it picks the order sent to the meter chip.
`fnlink.py status` now names the function and says whether the tone gets through.

**How long the beep lasts.** The counter at `0x20002D67` is advanced in two places with the
same code (`0x08029D0C`, `0x08040508`): while the duty is not 0 it goes up by one, and at 11
the duty is set to 0 and the counter with it. It is the counter of the key click in the table
above. In Continuity, watched for 25 s with the leads shorted three times
(`tools/buzzer_tail_check.py` redoes this):

- leads open: 0 on every read;
- leads shorted: 0, with 131 single reads of 1, each 0 again on the next read and 10 ms or a
  multiple of it after the one before. The tick takes it to 1 and the next edge of the tone
  zeroes it. Never above 1;
- leads released: 1 to 10, one step every 10 ms, then 0: 101.0 and 100.0 ms from the first 1
  in the two releases captured.

So stock's continuity beep goes on for 11 ticks after the last edge of the chip's tone, 100
to 110 ms depending on where in the 10 ms the edge fell. The duty register itself cannot be
read with the link; that the buzzer is on is inferred from the counter moving.

## Generator menu, CH2 and the boxes

Three loose ends of earlier sessions, closed the same day. Captures in
[`sessions/2026-11-25_generator_ch2_boxes/`](../sessions/2026-11-25_generator_ch2_boxes/README.md).

**The generator's menu.** Each waveform selected in turn and its index read from
`0x20000F51`: 1 Square Wave, 2 Sawtooth wave, 3 Half-wave, 4 Full Wave, 5 Step Wave, 6 Reverse
Step Wave, 7 Index Up, 8 Index Decrease, 9 Direct Current, 10 Multi-audio, 11 Sink Pulse,
12 Lorentz waves. The labels are the screen's. The entry before Square Wave, index 0, was not
captured, and the shapes of 7 to 12 were not looked at.

**CH2 with a signal.** The generator's 50 kHz square wave on CH2, CH1 off:

- scope state: channel mask `ms[0x14]` = 2; `ms[0x15]` = `0x12` (it is `0x01` with CH1
  alone) and `ms[0x16]` = 1 (0 with CH1 alone); CH2's range `ms[3]` = 2, then 3 after one
  step up. `ms[0]` stayed 1 and `ms[1]` 0, so those two are not the channels' on flags;
- stock's own measurements of CH2 (the second twelve words of the block at `0x20000208`):
  50 kHz, 20 us, 49.39 %, 329 mV peak to peak at 50 mV per division; 345 mV at 100 mV per
  division. CH1, off, reads 0 Hz and 4 mV;
- the record of CH2 at 100 mV per division, scaled with the same 25 counts per division as
  CH1, gives 344 mV peak to peak and 50.0 kHz (seen in the application that reads the link).

So CH2's range byte and its record scale are as CH1's, at the X1 probe setting. CH2's
position byte was 0 throughout and its probe byte was never X10: those two stay assumed.

**The units of the MAX, MEAN and MIN boxes.** In Small DC current with nothing connected the
screen showed the three boxes in uA (0.0000, -5.5000, -11.000) while the reading was in mA;
the state held those three numbers with bytes 47 to 49 at 0 and, in the capture of the twelve
functions, bytes 50 to 52 at 10. Across that capture bytes 50 to 52 follow one table (0 mV,
1 V, 5 nF, 9 degC, 10 uA, the last four being the reading's own codes at offset 16, where 11
is mA and 12 A), and bytes 47 to 49 are 1 only for a box in volts. The unit is therefore at
50 to 52; `fnlink.py status` took it from 47 to 49 until now and printed "mV?" for a box in
microamperes.

## Coupling, the X10 setting, 2 us and the first waveform

The last session of the day. Captures in
[`sessions/2026-11-25_coupling_probe_time_bases/`](../sessions/2026-11-25_coupling_probe_time_bases/README.md).
In each step `fnlink.py scope` measured the record with this repository's rules and
`fnlink.py status` printed stock's own measurement of the same wave.

**CH1's coupling is `ms[0]`**: 1 with DC, 0 with AC. Switching it changed that byte and no
other of the 56, and the trace dropped to straddle zero (stock's Vmax went from 317 mV to
170 mV). With CH2 alone on, `ms[0]` was still 1: it is not the channel's on flag, which is the
mask at `ms[0x14]`. CH2's coupling was not switched; `ms[1]` was 0 throughout.

**The X10 probe setting** is `ms[6]` for CH1 and `ms[7]` for CH2, and stock's own
measurements carry the factor:

| | record at X1 | stock at X1 | record at X10 | stock at X10 |
|---|---|---|---|---|
| CH1, peak to peak | 336 mV | 334 mV | 3.36 V | 3.35 V |
| CH2, peak to peak | 330 mV | 329 mV | 3.30 V | 3.29 V |

**2 us per division** (`ms[0x2D]` = 7): 50 kHz from the record, 50 kHz by stock. So 25
samples to a division holds from 2 us to 2 ms. At 1 us the record holds two periods of the
50 kHz wave and `scope` gives no frequency; counted by hand in the two saved records, the
rising edges are at samples 287 and 787, and 210 and 709: 500 and 499 samples for 20 us, so
the rule holds at 1 us too. The slow time bases were not captured: steps 10 to 12 were taken
at 5, 10 and 20 us instead of ms.

**The trigger edge was not found.** In the step meant to switch it, the bytes that changed
were CH1's range and the trigger level; nothing else in `ms[0x00..0x37]`.

**The generator's menu starts one step before Square Wave**, at index 0. Stock builds that
waveform from a 100-point table in flash, `0x0804D848`, scaled by the amplitude; the table is
a sine to within half a count. Its label on the screen was not noted.

## Patch 4 on the unit

Flashed in the afternoon over patch 3, from macOS through the factory IAP drive with
OpenScope's `scripts/iap_flash.py`: the fifth update of this unit that way. The image is the
one without `--languages` (`388bd8d7…773b`, as in `reference/APP_2C53T_V1.2.0_link_patch4.json`).

| | |
|---|---|
| `fnlink.py info` | protocol 1, patch 4, "the generator's table can be written" |
| `fnlink.py verify` | MATCH: the application area of the flash is exactly the image (754220 bytes) |
| Settings and calibration page | dumped before and after the flash: identical, byte for byte |
| Table before any write | Square Wave, 1000 Hz, 3.0 V: 100 samples, 20 to 3742, two levels |
| `wave sinc`, `wave harmonics --harmonics 1:1,3:0.33,5:0.2` | each written and read back identical, 20 to 3741 |
| On the instrument's own oscilloscope | the generator's output on CH1 shows the harmonics shape: a square with three ripples on each level (photograph) |
| After changing the frequency to 2000 Hz on the instrument | `wave --show` reads the harmonics table still there, 20 to 3741: a change of frequency keeps the waveform, as the code says |

What the photograph also shows: stock's own measurements on that shape read 1.20 kHz and a
duty of 60.71 %, while the period on its screen is about five divisions of 200 us. Its
frequency measurement is not reliable on a waveform with ripple; the record is.

Not checked on the unit: that a change of duty or amplitude brings stock's waveform back
(code and emulation only), and the image built with `--languages`, which has not been flashed.

## Patch 5 on the unit

Flashed in the evening over patch 4, the same way (sixth update of this unit), built with
five menu entries: `Triangle 25/75=triangle:25@2`, `Gauss Pulse=gauss@12`, `Burst 10=burst@10`,
`AM 80%=am@10` and the USB entry `USB Wave@0` (`3d27625e…d8b4`, as in
`reference/APP_2C53T_V1.2.0_link_patch5_menu.json`).

| | |
|---|---|
| `fnlink.py info` | protocol 1, patch 5, the five entries listed as 14 to 18 with their names |
| `fnlink.py verify` | MATCH (755540 bytes) |
| Settings and calibration page | the calibration table is identical before and after; 68 bytes differ after it, where stock keeps its settings |
| The list on the instrument | the five entries follow Lorentz waves, each name on one line of its row; the cursor stays in view down to the last (photograph, with Burst 10 selected) |
| Pictures | each added entry shows the picture of the stock waveform it borrows; Burst 10 and AM 80% both borrowed Multi-audio's and so looked the same |
| `wave sinc` with Gauss Pulse selected, 3.0 V | written and read back, 20 to 3741; the tool reports entry 18 as its place in the menu |
| `wave sinc` with USB Wave selected, 2.0 V | 20 to 2501: the USB entry at the amplitude set on the instrument |
| On its own oscilloscope | the sinc, 50 kHz, four divisions of 5 us apart (photograph) |

What went wrong, and it was the design and not the unit: the third `wave sinc` was sent with
Square Wave selected. Patch 5 writes the playing table whatever is selected, so the
instrument went on showing "Square Wave" and playing a sinc until a generator setting was
changed. Patch 6 stores such a waveform for the USB entry only.

Not checked on the unit with patch 5: a power cycle with an added entry selected.

## Patch 6 on the unit

Flashed later the same evening over patch 5 (seventh update), built with the same five
entries, AM 80% now borrowing Sink Pulse's picture (`d34097a8…83ec`, as in
`reference/APP_2C53T_V1.2.0_link_patch6_menu.json`).

| | |
|---|---|
| `fnlink.py info` | protocol 1, patch 6, the five entries |
| `fnlink.py verify` | MATCH (755556 bytes) |
| Settings and calibration page | against the dump taken after patch 5: the calibration table is identical; 7 bytes of settings differ and 206 in the scratch area after the table, which stock fills with whatever its staging buffer held (the same area that went from `0xFF` to other values on 2026-11-24) |
| `wave sinc` with Square Wave selected | "stored ... not playing": kept for entry 18, and the instrument's oscilloscope goes on showing the square |
| `wave gauss` with USB Wave selected | written and read back, 20 to 3741, and the bell on the oscilloscope at once (photograph) |
| Back on Square Wave | the square again; `wave --show` reads its table as two levels, 20 and 3742 |
| The list | Burst 10 and AM 80% now show different pictures |
| After a restart | about a minute after power-on (`uptime 72.0 s`) the generator was on Gauss Pulse with its table in place, 20 to 3741. Whether that entry had been selected before the restart was not noted |

The square on the oscilloscope has a peak after each edge that settles onto the level
(photograph: 10X probe, channel set to X1). Its table is exactly two values, so that shape
is made after the DAC; an over-compensated probe draws it. Not pursued further.

## Not answered

- What needs a write to the instrument's memory (the 20M limit, the per-channel "FFT" flag)
  or a read of peripheral registers (TMR10, TMR11, GPIO): the link reads flash and RAM only,
  and the one thing it writes, from patch 4, is the generator's table.
- The idle counter with USB disconnected: without USB there is no link.
- Whether the unit really powers off at 900 counts with USB connected: not waited for.
- The zoom-out sampling rule (above), mode 1 of the arrow keys, and all of this with both
  channels on or at a captured time base below 4, where their notes expect the record to be
  interleaved.
