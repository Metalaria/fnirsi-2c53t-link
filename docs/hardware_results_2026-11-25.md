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
| 1 | left/right: `ms[0x1A]`, the horizontal position; up/down: the channel's position `ms[4]`, and `ms[0x1C]` by the same amount | unit, 2026-11-26, with CH1 alone |
| 2 | left/right: `ms[0x1A]`, the horizontal position, one screen column a press; up/down: `ms[0x1C]` alone | unit |
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

## Patch 6 with `--languages` on the unit

The same image built with `--languages` as well (`cef60d62…51a5`, as in
`reference/APP_2C53T_V1.2.0_link_patch6_menu_languages.json`), flashed over patch 6 (eighth
update). It is the first time the language call has run on hardware.

| | |
|---|---|
| `fnlink.py info` | protocol 1, patch 6, the five entries |
| `fnlink.py verify` | MATCH (755576 bytes) |
| Settings and calibration page | against the dump taken after patch 6: the calibration table is identical; 1 byte of settings differs and 207 in the scratch area after the table |
| Settings > Language | OK on the second row steps through the languages; the instrument was left in Spanish and works in it |

What it does not give is a list: the panel goes on showing its two rows, 中文 and English
(by the code, neither is marked while a third language is in use), and the language is chosen
by counting presses. The owner's verdict was that each language should have its own entry;
`--language-menu` was written after this.

## `--language-menu` on the unit, 2026-11-26

Patch 6 with the five entries and `--language-menu` (`25127f01…3643`, as in
`reference/APP_2C53T_V1.2.0_link_patch6_menu_langmenu.json`), flashed over the image above
(ninth update).

| | |
|---|---|
| `fnlink.py info` | protocol 1, patch 6, the five entries |
| `fnlink.py verify` | MATCH (756028 bytes) |
| Settings and calibration page | against the dump taken after the image above: the calibration table is identical (`9cbf9bf5…ff00d`); 1 byte of settings differs and 207 in the scratch area |
| The instrument | starts in Spanish, as it was left, and measures |

What the owner reported is not about the menu but about the language: in Spanish the
multimeter's title is cut. A video shows "Voltaje de Corriente" over a DC voltage; the
full names of the two voltages differ only in the word that does not fit, and the four
currents are in the same case (and the Spanish for "small" is in the wrong gender). The
cause and the measurements are in `stock_v120_notes.md`, "Texts that do not fit";
`--texts` was written after this. Still to come from this session: `fnlink.py tasks` after
using the menu.

## `--texts` on the unit, 2026-11-26

The same image with `--texts texts/es.txt`, when that file had the six titles of the
multimeter and nothing else (`3560264c…06e2`, 756136 bytes), flashed over the one above
(tenth update).

| | |
|---|---|
| `fnlink.py info` | protocol 1, patch 6, the five entries |
| `fnlink.py verify` | MATCH (756136 bytes) |
| Settings and calibration page | against the dump taken after the image above: the calibration table is identical; 207 bytes differ in the scratch area and none in the settings |
| The multimeter in Spanish | the owner finds nothing left to report in it |
| The language menu | nothing reported against it either |

What he reports now is in the generator, in Spanish: the third setting reads "Amplitu",
and the sawtooth is called "Onda diente", which he doubts. Both are stock's: the first is
a word one pixel wider than its box (see `stock_v120_notes.md`), the second a translation
cut short by its author. `texts/es.txt` has since gained that label and six waveform
names; that image has not been flashed.

## The thirteen texts on the unit, 2026-11-26

The image built from `texts/es.txt` with its thirteen texts (`4e7de994…7097`, 756236 bytes,
as in `reference/APP_2C53T_V1.2.0_link_patch6_menu_langmenu_es.json` at that commit),
flashed over the one above (eleventh update).

| | |
|---|---|
| `fnlink.py info` | protocol 1, patch 6, the five entries |
| `fnlink.py verify` | MATCH (756236 bytes) |
| Settings and calibration page | against the dump taken after the image above: the calibration table is identical; 1 byte of settings and 103 of the scratch area differ |
| The generator in Spanish | the owner's only remark is that an added entry still says "Gauss Pulse" |
| An added entry and power-off | the step was: select Gauss Pulse, switch off and on. `wave --show` then says `Gauss Pulse, 1000 Hz, amplitude 3.0 V` and draws the bell (20 to 3741) |
| The square that did not look square (patch 6, above) | it was the probe: with its compensation adjusted the square is square on the instrument; the table reads 20 to 3742, two levels |

The added entries have one name each, whatever the language, because that is all the
builder took. `--menu-names` was written after this; the fonts turned out to have á, í, ó,
ñ and ü but no é or ú.

## The trigger edge and the slow time bases, 2026-11-26

Captures in
[`sessions/2026-11-26_trigger_edge_slow_time_bases/`](../sessions/2026-11-26_trigger_edge_slow_time_bases/README.md).

**The trigger edge is `ms[0x18]`: 0 rising, 1 falling.** It was found in two steps, the
first of which looked in the wrong place. This time the edge did change: in
the snapshot taken with the falling edge the drawn arrays are in the opposite phase and
stock's positive and negative measurements have changed places, and in the third
both are back. Of the 3796 bytes read, 385 are the same in the first and third snapshots
and different in the second. All but one are samples, drawn points and those measurements
(the positive and negative duty and width, 49.60 and 50.40 %, 4.96 and 5.04 ms). The one is `ms[0xDB8]` (4, 12, 4), and the code says it is not the edge: a
16-bit counter that `0x0804067E` increments and the `fpga` task compares with a value from
the time base and zeroes (`0x0803E55A`, `0x0803FF76`). So the edge is in neither block. What
was not read is `ms[0x00..0x37]`, left out because the session of the 25th had found nothing
else changing there, when the edge had not in fact been switched, and the calibration
tables. OpenScope's `STATE_STRUCTURE.md` has it at `ms[0x18]`, 0 rising and 1 falling; the
`status` lines of this session, taken with the rising edge, show 0 there.

A `status` taken afterwards with the falling edge, at the same 20 ms per division, settles
it: the 56 bytes differ from those read with the rising edge in two places, `ms[0x18]`
(0, now 1) and `ms[0x31]` (1, now 0). The second is not the edge: in the 85 readings of
the earlier sessions kept here, none taken with the edge switched to falling, `ms[0x18]` is
0 in all and `ms[0x31]` is 0 in 53 and 1 in 32. `fnlink.py status` now names the edge.

**5 and 20 ms per division.** With the generator's square wave at 100 Hz, `fnlink.py scope`
gets 100 Hz from each of the two records saved at 5 ms and at 20 ms per division, so 25
samples to a division holds there as well (at 20 ms a period is 12.5 samples). Stock's own
measurement says 100 Hz at 5 ms and 104 Hz at 20 ms.

## The tasks' stacks with the language menu, 2026-11-26

`fnlink.py tasks` on the unit, whose flash had last been verified as the image with
`--language-menu` and the thirteen texts (`4e7de994…7097`). The step before the command
was: open Settings > Language, go through its eight entries, go through the generator's
list, return to the multimeter.

| Task | Priority | Stack | In use | Deepest since power-on | Never reached |
|---|---|---|---|---|---|
| `display` | 1 | 1536 | 284 | 817 | 719 |
| `key` | 4 | 512 | 276 | 276 | 236 |
| `osc` | 2 | 1024 | 276 | 780 | 244 |
| `fpga` | 3 | 512 | 324 | 324 | 188 |
| `dvom_TX` | 2 | 256 | 140 | 140 | 116 |
| `dvom_RX` | 3 | 512 | 332 | 396 | 116 |
| `IDLE` | 0 | 512 | 84 | 84 | 428 |

Bytes. With the language panel in use, 719 of the `display` task's 1536 bytes were never
reached; the dumps of 2026-11-24, with patch 3 and no panel, had 710. The figures are those
of one power-on and of what was done in it: on the 24th `dvom_RX` had 21 bytes never
reached and `key` 76.

## `--menu-names` on the unit, 2026-11-26

The image with the thirteen texts and a Spanish name for each of the five added entries
(`3cc7e0c8…5308`, 756504 bytes, as in
`reference/APP_2C53T_V1.2.0_link_patch6_menu_langmenu_es.json` at that commit) is the twelfth update. Its
flashing was not logged; these are the readings taken before the session below.

| | |
|---|---|
| `fnlink.py verify` | MATCH (756504 bytes) |
| Settings and calibration page | against the dump taken after the image of the thirteen texts: the calibration table is identical (`9cbf9bf5…ff00d`); 9 bytes of settings and 75 of the scratch area differ |
| The list of waveforms in Spanish | not reported |

## Trigger mode and channel, CH2's coupling, the FFT and the AUTO key, 2026-11-26

With the image above. `fnlink.py snapshot 0x200000F8:56` after each step on the
instrument, both channels on, and one `fnlink.py watch` of the same bytes through a press
of AUTO. The captures are in
[`sessions/2026-11-26_trigger_coupling_fft_auto/`](../sessions/2026-11-26_trigger_coupling_fft_auto/README.md).

| Step on the instrument | Bytes that differ from the starting state |
|---|---|
| trigger mode Normal (it was Auto) | `ms[0x17]`: 1 to 0 |
| trigger mode Single | `ms[0x17]`: 2; `ms[0x2E]` (running): 1 to 0 |
| trigger mode Auto again | none |
| trigger channel CH2 | `ms[0x16]`: 0 to 1 |
| trigger channel CH1 again | none |
| CH2's coupling AC | `ms[0x01]`: 1 to 0 |
| CH2's coupling DC again | none |
| CH1's FFT on | `ms[0x0A]`: 0 to 1; `ms[0x0C..0x0F]`: 0 to `0x20033940` |
| CH1's FFT off, CH2's on | `ms[0x0B]`: 0 to 1; `ms[0x10..0x13]`: 0 to `0x20033940` |
| both off | none |

So `ms[0x17]` is the trigger's mode with 0 Normal, 1 Auto, 2 Single. OpenScope's
`STATE_STRUCTURE.md` (at their commit `7480998`) has the byte as the trigger run mode with
0 AUTO and 1 NORMAL: the first two are the other way round on this unit. `ms[0x16]`,
"active channel selector" there, is what the trigger menu's channel row sets. `ms[0x01]`
as CH2's coupling and `ms[0x0A]`, `ms[0x0B]` as the rows the menu labels "FFT:" are what
their `parity2_static.md` derives from the menu's code (`ms[ch-1]`, `ms[ch+9]`); here they
were read changing. `ms[0x15]` was `0x02` in all eleven readings, with both channels on and
the trigger on either: the earlier sessions had `0x01` with CH1 alone and `0x12` with CH2
alone. What `0x20033940` points at was not read.

**AUTO.** Watched every 10 ms: 832 readings that differ from the one before in 8.3 s. At
the key `ms[0x31]` goes from `0x01` to `0x11`, `ms[0x2F]` to 1, both couplings and both
ranges to 0, CH1's position to 0 and the time base from 13 to 4; then `ms[0x2F]` goes
through `02`, `03`, `11`, `21`, `22`, `32`, `F3`, `FF` and is 0 again 5.76 s after the key,
when the trigger level is written (`0xFFFF` before, `0x002E` after). On the way CH1's
range steps 0, 1, 2 and later 3, the time base returns to 13, CH2's coupling returns to 1
at `22` and CH1's at `F3`. `ms[0x32]` counts 10 ms ticks from 0 after most of those steps
and, after the last, up to `0xFF`, where it stays. The table of times and the code that
saves and restores the couplings through `ms[0x31]` are in `stock_v120_notes.md`,
"Oscilloscope". This explains the `ms[0x31]` of the section on the trigger edge: it is
what the couplings were at the last AUTO.

`fnlink.py status` and the scope settings it decodes now give the trigger's mode and
channel, CH2's coupling and the FFT rows.

The owner also reports that the link works from the Android application; no capture of
that is kept here.

## `texts/es-english-rows.txt` on the unit, and back, 2026-11-26

The image with the eighteen texts of the two-row tables in Spanish (`537feee8…0b09`,
756620 bytes, as in
`reference/APP_2C53T_V1.2.0_link_patch6_menu_langmenu_es_english_rows.json` at that commit), flashed over
the one above (thirteenth update). Before it, `fnlink.py verify` gave MATCH for the image
above once more.

| | |
|---|---|
| `fnlink.py info` | protocol 1, patch 6, the five entries |
| `fnlink.py verify` | MATCH (756620 bytes) |
| Settings and calibration page | against a dump taken just before flashing: the calibration table is identical (`9cbf9bf5…ff00d`); 85 bytes differ in the scratch area and none in the settings |
| `fnlink.py status` in the oscilloscope | `RUN, 200us/div, CH1 100mV/div X1 DC, CH2 10mV/div X1 DC, channels CH1+CH2, trigger Auto rising CH1`, and the measurements of both channels |
| The panels, the bar, the boxes and the measurements in Spanish | not reported |

The owner then flashed the previous image again (`3cc7e0c8…5308`; fourteenth update;
`fnlink.py verify` gave MATCH for it later) and, after the readings of the next section,
this one once more (fifteenth update; MATCH, 756620 bytes). Why he went back and forth is
not recorded. With the previous image two photographs of the oscilloscope show stock's
English labels ("Freq:", "Duty+:", "Vp-p:", "Wid-:"); with this one, three show "Frec:",
"Ciclo+:", "Anc+:", "Anc-:", "Amp:", "Vef:", "Vp-p:" and "Med:" each followed by its
value, whole and clear of its neighbours. The panels, the bar and the multimeter's boxes
are still not reported. A photograph of the oscilloscope's "Parameter Measurement" menu
shows its grid of buttons ("Freq", "Wid+", ..., "Vrms", "Avg", "A-on", "A-off") as
stock has it: those words are not strings of the image, so `--texts` does not reach them.

## The measurements against a stopped screen, and the FFT pointer, 2026-11-26

After that. The captures are in
[`sessions/2026-11-26_measurements_fft_pointer/`](../sessions/2026-11-26_measurements_fft_pointer/README.md).

**Measurements.** The generator's 1 kHz square on CH1 at 100 mV per division. With the
oscilloscope stopped, CH1's shown block reads 1000, 1000000, 5040, 4960, 504000, 496000,
308000, -4000, 312000, 304000, 208000, 148000, and a photograph of that stopped screen
shows "Freq:1.00KHz", "Duty+:50.40%" and "Vp-p:312.0mV": words 0, 2 and 8. The screen
showed three measurements for CH1 and four for CH2, none of them Amp, Vrms or Avg, so words
9 to 11 are still not compared with a screen. For what they are worth: a square from 0 to
304 mV with a duty of 50.4 % has an RMS value of 216 mV and a mean of 153 mV, and words 10
and 11 read 208 mV and 148 mV.

**`ms[0x31]`.** It reads 0 in these snapshots; it was `0x11` at the end of the watch of the
AUTO key. Between the two the unit was reflashed twice, which restarts it.

**The FFT pointer.** With CH1's "FFT:" row on, `ms[0x0C..0x0F]` held `0x20033940` again,
and the 56 bytes read there begin `32 01 1d 02 01 00 08 02 01 00 04 02 01 00 02 02`. What
they are was not identified. No instruction or literal of the image holds that address.

## Words 9 to 11 on the screen, and the FFT block, 2026-11-26

With the image above on the unit again, the same wave, and eight measurements chosen for
CH1 in the "Parameter Measurement" menu. The captures and what each photograph shows are in
the same session directory.

**Amp, Vrms, Avg.** `fnlink.py status`, stopped, CH1 alone, reads 316000, 208000 and
148000 in CH1's words 9, 10 and 11. Stopped screens photographed in the same minutes
show 304.0 and 316.0 mV under the label of word 9, 212.0 and 208.0 mV under
that of word 10, and 148.0 and 144.0 mV under that of word 11; running, 316.0, 209.2 and
146.4 mV. The earlier readings of the same wave had 304000, 208000 to 209500 and 147833 to
148000. So the three words carry what the screen prints there, as the code says, but none
of these photographs is of the acquisition a reading was taken from: each differs from the
reading in some other measurement. `fnlink.py` now gives the three words those names.

For CH2 one pair is of the same acquisition: the screen, stopped, showed "Ciclo+:56.47%",
"Anc-:472.0nS" and "Vp-p:6.00mV", CH2 was then switched off, and the reading has 5647, 472
and 6000 in CH2's words 2, 5 and 8. Its words 0 and 1 read 846076 and 1181 where that screen
had shown "Frec:923.3KHz": with the channel off those two are not what was last shown,
which fits what was seen before with a channel off.

**The FFT block.** 448 bytes at `0x20033940`, running, CH1 alone. With CH1's FFT on they
are 128 bytes followed by 32-bit floats (80 of them fit in what was read). The floats are
a spectrum by index: with the 1 kHz square the largest are index 0 (6847), 2 (4133), 6
(1219), 10 (617) and 14 (354), with smaller ones between; with the probe off the generator
index 0 is 116 and the rest are 40 at most. Byte *k* is float *k* times 50 over the
largest float, without its fraction: in all 80 pairs of the square's reading (50, 0, 30,
2, 1, 0, 8, ...), and in the first 38 of the other, whose floats from the 39th on come
from a later read of the block. The 128 bytes of the square are nearly a mirror image
about index 64 (30 at 2 and 32 at 126, 8 at 6 and 11 at 122, 4 at 10 and 6 at 118); a
block is eight reads a few milliseconds apart with the instrument acquiring, so its two
ends are not of the same instant. With the FFT off the 448 bytes are zero but for three
16-bit values `0x3A29` (offsets 136, 436 and 438). A photograph with the FFT on shows the
spectrum drawn as bars in the upper left of the grid. Not established: how many floats
there are, which samples the transform takes, and the frequency of an index. The square's
odd harmonics are 4 to 4.2 indexes apart, which is what a 128-point transform of every
second sample of the record would give at this time base (62.5 kHz, 488 Hz an index), and
that reading has not been tested at another time base.

## A channel's panel in Spanish, a screen equal to a reading, and the oscilloscope's menu, 2026-11-26

Photographs taken with the same image on the unit, the instrument in Spanish.

- **A channel's panel.** "Canal:", "Sonda:", "Acoplamiento:" and "FFT:" with their values
  (ON, X1, DC, OFF) in their buttons, whole, as painted in emulation. The trigger's panel,
  the bar with "Simple" and the multimeter's boxes are still not reported.
- **A stopped screen with the values of a reading.** It shows "Frec:1.00KHz",
  "Ciclo+:50.40%", "Anc+:504.0uS", "Anc-:496.0uS", "Amp:316.0mV", "Vef:208.0mV",
  "Vp-p:316.0mV" and "Med:148.0mV": the eight words the `status` of the section above read
  for those measurements (1000, 5040, 504000, 496000, 316000, 208000, 316000, 148000). The
  photograph is of another acquisition, taken some three quarters of an hour after that
  reading; a `status` meant to go with it was run with the instrument in the multimeter,
  where both blocks read zero. Another stopped screen shows 320.0 mV peak to peak, 316.0,
  212.0 and 148.0 mV; a running one 317.2, 315.4, 208.3 and 146.3 mV.
- **The oscilloscope's menu.** Its title and its list are in Spanish, and three entries
  are broken in the middle of a word: "Configuraci / ón CH1", "Configuraci / ón CH2" and
  "Configuraci / ón de dispa", the end of the last one missing. The cause is in
  `stock_v120_notes.md`, "Texts that do not fit". `texts/es.txt` has since gained three
  texts for those entries; that image (`98b5d1b7…f2d9`) is the one of the next section.
- Three snapshots meant for the FFT block at three time bases saved one block each, not
  two: the pointer was not followed. The file was not passed on, so why is not known.

## The oscilloscope's menu in Spanish on the unit, 2026-11-26

The image with the three texts of the oscilloscope's menu added to the one above
(`98b5d1b7…f2d9`, 756668 bytes, as in
`reference/APP_2C53T_V1.2.0_link_patch6_menu_langmenu_es_english_rows.json`), flashed over
it (sixteenth update). Before it, `fnlink.py verify` gave MATCH for the image above.

| | |
|---|---|
| `fnlink.py verify` | MATCH (756668 bytes) |
| Settings and calibration page | against a dump taken just before flashing: the calibration table is identical (`9cbf9bf5…ff00d`); 2 bytes of settings and 67 of the scratch area differ |
| The oscilloscope's menu | three photographs: "Ajustes de / CH1", "Ajustes de / CH2" and "Ajustes de / disparo", each whole in its two lines, under the three entries that were already whole |
| A channel's panel | CH1's and CH2's: "Canal:", "Sonda:", "Acoplamiento:", "FFT:" and their values, whole |
| The trigger's panel | "Modo:" with Auto, "Flanco:" with the picture of the edge, "Canal:" with CH1, whole |
| The bar with "Simple", the multimeter's MEDIA | not reported |

The file of the three snapshots that had not followed the FFT's pointer was passed on: in
all three the FFT row was off and the time base was 200 µs per division, so they say
nothing about the FFT. They are kept in the session directory with a note of what they do
hold: stopped, `ms[0x1A..0x1B]` read 0, -63 and -13 and `ms[0x20..0x27]` 0, -1260000 and
-260000, 20000 times the other in both.

## The FFT's indexes, the arrows' mode 1 and the horizontal position, 2026-11-26

With the image above. The captures are in the same session directory
(`fft_time_bases.txt`, `status_fft_on.txt`, `snapshots_arrows_mode_1.txt`).

**What frequency an index of the FFT block is.** CH1 alone with the generator's 1 kHz
square, CH1's FFT on, running; 448 bytes of the block at each of three time bases, the
screen's "H=" read before each and `ms[0x2D]` agreeing (13, 14, 12).

| Time per division | Largest bars after index 0 | 25 / (256 × time per division) |
|---|---|---|
| 200 µs | index 2 (30), 6 (8), 10 (4), 14 (2) | 488.3 Hz: 1, 3, 5, 7 kHz at 2.05, 6.1, 10.2, 14.3 |
| 500 µs | index 5 (28), 15 (6), 26 (3) | 195.3 Hz: 1, 3, 5 kHz at 5.1, 15.4, 25.6 |
| 100 µs | index 1 (30), 3 (9), 5 (5), 7 (3), 9 (2) | 976.6 Hz: 1, 3, 5, 7, 9 kHz at 1.0, 3.1, 5.1, 7.2, 9.2 |

At 500 µs there are more bars than those: 2, 2, 2, 3, 3, 3 at indexes 10, 20, 31, 41, 51
and 61, each above its neighbours. They are where 23, 21, 19, 17, 15 and 13 kHz fall once
folded about 12.5 kHz, half of 25 kHz. So the block is 128 points taken 2 × (time per
division / 25) apart, twice the record's sample period, and the square's harmonics above
half that rate are not removed first. In the three readings byte *k* is float *k* times 50
over the largest, truncated, in all 80 pairs. `fnlink.py` has the rule
(`fft_hz_per_index`, `decode_fft_block`) and its tests read these captures.

**The arrows' mode 1.** A first attempt failed twice over: the FFT row was off, and the
time base would not change. `status` said why: the arrows were in mode 1, which the notes
had as "not looked at". With the keys that change the time base in mode 0, `ms[0x2D]`
stayed at 13 and `ms[0x1A..0x1B]` went through -25, -63 and -13; a photograph shows the
top bar with two small arrows where earlier ones, in mode 0, show "V" and "H". After
SELECT, `status` says mode 0 and the time base changed.

**The horizontal position.** `ms[0x20..0x27]`, a signed 64-bit number, is `ms[0x1A..0x1B]`
times a tenth of the time per division in nanoseconds in every reading of today (-25 and
-500000, -63 and -1260000, -13 and -260000, -11 and -220000 at 200 µs; -4 and -200000 at
500 µs; -20 and -200000 at 100 µs) and in those of the earlier sessions where it is not
zero, over sixty readings in all from 10 µs to 500 µs per division. From 500 µs to 100 µs the
nanoseconds stayed and the other followed. One reading kept in the tests, stopped at 5 µs
on 2026-11-24, has -25 with -500000 and does not fit.

## The voltages at X10, 10 ms per division, and the multimeter's keys, 2026-11-26

With the image above; `status_probe_x10.txt` and `ten_ms_per_division.txt` in the same
session directory.

- **Words 9 to 11 at X10.** With CH1's probe setting at X10 and the same 1 kHz square,
  `status` reads 3030400, -20800, 3051200, 3040000, 2108800 and 1490400 in words 6 to 11,
  where the readings at X1 had 304000 to 308000, -12000 to -666, 304666 to 317125, 304000
  to 316000, 208000 to 209500 and 146125 to 148000. All six carry the factor.
- **10 ms per division.** Two records, 100 Hz from each with 25 samples to a division. The
  procedure had the generator set to 100 Hz before them; no reading of the generator was
  taken at that moment. An earlier attempt was at 10 µs per division by mistake and shows a
  flat trace.
- **The horizontal position after AUTO.** That `status` has -150 in `ms[0x1A..0x1B]` and
  -150000 in `ms[0x20..0x27]` at 200 µs per division: the pair of 10 µs per division, where
  the time base had been, not of 200 µs. `ms[0x31]` is 1 in it, so AUTO had been pressed
  since the unit was started. The relation of the section above is therefore not kept by
  whatever AUTO does to the time base; `formats.md` says so now.
- **The multimeter's keys**, as the owner describes them on the unit: a short press of MENU
  shows nothing; holding it for a few seconds lets him change between modes; the up and
  down arrows do nothing; SELECT does nothing; the right arrow changes what is measured
  (voltage, continuity, resistance, capacitance, ...); the blue key confirms. The left
  arrow, the screen where the mode is changed and the generator's keys are still to be
  described.

## The mode screen, the generator's keys and its list in Spanish, 2026-11-26

The owner's description of the unit, three photographs and `fnlink.py status` after single
key presses (its `generator` line). The image is the one above.

- **Changing mode.** Holding MENU brings a screen with one picture per mode, the current
  one in the middle with its name under it ("Multímetro" in the photograph) and four
  dots above, the first filled. The left and right arrows move between modes and the blue
  key in the middle of the arrows enters the one shown.
- **The multimeter.** Going through the functions past the last one returns to Automatic.
  Two `status` lines taken around presses of the right and left arrows both read menu 0,
  Automatic, so they do not show what each arrow did; the owner had said before that the
  right arrow changes what is measured.
- **The generator's keys.** Nothing changes until the blue key has been pressed: `status`
  reads Square Wave, index 1, before and after a press of the right arrow and of the left
  one. Then the up arrow gave Sine Wave, index 0, and the down arrow Square Wave, index 1,
  again. SELECT does nothing there (`status` unchanged). What a short press of MENU does
  in the generator, how the frequency, the duty and the amplitude are reached and changed,
  and how that state is left were not described.
- **The generator in Spanish.** One photograph shows the list from its second entry,
  "Onda cuadrada" under the cursor, then "Diente de sierra", "Media onda", "Onda
  completa", "Escalera" and "Escalera inversa", and the three settings: "Frecuen / cia",
  "Ciclo de / trabajo" and "Amplit.". Another shows the end of the list with the cursor
  on index 16: "Pulso sinc", "Ondas de / Lorentz", "Triangular / 25/75", "Campana de /
  Gauss", "Ráfaga de 10 / ciclos" and "Portadora / modulada (AM)", each whole. So four of
  the five names given with `--menu-names` have been seen on the unit; the last entry,
  "Onda USB", is below what that photograph shows. In it the row of the duty is drawn in
  grey and the other two in white; with Square Wave under the cursor the three are white.

## Editing a value in the generator, and what the manual says, 2026-11-26

The owner's account of changing the frequency on the unit: the blue key selects the
waveform under the cursor; the blue key again goes to the frequency; the left and right
arrows go to the digit, the up and down arrows change it; MENU leaves the frequency and
goes straight back. `status` afterwards: index 16, 2 kHz, duty 50 %, 3.0 V (it had been
1 kHz).

FNIRSI's manual for the 2C53T (the four-language PDF on its site, read as text, where the
keys are pictures without names) has, for the generator: a short press for back, a long
one for the home page, a short one to confirm, a long one for run/stop, the up and down
keys to choose the waveform, and then one key to enter its parameters, which the English
text calls the centre key and the Chinese one the right arrow. For the multimeter: a long
press for the home page, short presses for automatic measurement, for data hold and for
AC/DC and diode/continuity, and two that move the selection left and right. It does not
describe the home page. Which key each of those rows is cannot be told from the text.

`fnlink.py keys` was written after this, so that a sequence of keys can be followed in
one log instead of a `status` after each press.

## Logs of the keys, 2026-11-26

`fnlink.py keys` on the unit, four logs in
[`sessions/2026-11-26_keys/`](../sessions/2026-11-26_keys/README.md). The keys were asked for
in an order and not noted in the logs, so what follows is what the bytes did; where a key
is named, it is the one the order has at that place.

**The generator.** `ms[0xF69]` took three values. From 0, twelve changes in the order
asked for, none where the order has MENU, right or left with `ms[0xF69]` at 0 or 1:

| `ms[0xF69]` | Change | Key the order has there |
|---|---|---|
| 0 | waveform index 16 to 17, then 17 to 16 | down, up |
| 0 to 1 | | blue |
| 1 | `ms[0xF6B]` 0 to 2, then 2 to 0 | down, up |
| 1 to 2 | | blue |
| 2 | `ms[0xF6B]` `00` to `10` | left |
| 2 | frequency 2000 to 2010, then 2010 to 2000 | up, down |
| 2 | `ms[0xF6B]` `10` to `00` | right |
| 2 to 1, 1 to 0 | | MENU, MENU |

In the first log, with `ms[0xF6B]` at 0, the frequency went 2000, 1999, 2000. So with
`ms[0xF69]` at 2 the high nibble of `ms[0xF6B]` is the digit being changed, counted from
the right, and its low nibble, 0 or 2 here, was chosen with `ms[0xF69]` at 1. The entry
under the cursor was one whose duty row a photograph shows in grey; whether that is why
1 was passed over was not tried with another waveform. `ms[0xF6A]` read 11 and 12 with
the cursor on entry 16.

**The multimeter.** Three things in 47 s, fewer than the order has keys: byte 14 of the
meter's state went to 1 and back within 0.3 s; byte 20 (HOLD) went to 1; and then, in one
reading, the menu entry went from 0 to 7, the function to 10, byte 14 to 1 and HOLD to 0.
Nothing changed where the order has the right arrow first. Which key did each is not
established. In the first log, back in the multimeter from the mode screen, HOLD also
went to 1 after a key that was not noted.

**The oscilloscope.** The time base went 13, 12, 13 with the arrows in mode 0, and each
change started a count in `ms[0x28..0x29]` from about 297 down to 0 in about 3 s. CH1's
range went from 2 to 1 once, the trigger level going from -1 to 100 in the same reading.
The arrows' mode went 0, 1, 2, 0. In mode 1 the horizontal position `ms[0x1A]` went to 1
and back, `ms[0x20..0x27]` reading 20000 with it, and CH1's position `ms[0x04]` went up
one and down one, each with a count in `ms[0x2C]` from 99 down to 0 in about a second;
the trigger level went from 100 to 99 with the second. The link was lost three times in
twenty seconds where the order has the keys that open the menus. `ms[0x2E]` went to 0
and back to 1 in 0.2 s where the order has the blue key twice. Then AUTO: `ms[0x2F]`
went 01, 02, 03, 11, 12, 13, FF and back to 0 in 5.65 s, with CH1 alone, and left the
time base at 9, where the sequence watched earlier, with both channels on, went 01, 02,
03, 11, 21, 22, 32, F3, FF and returned it to 13.

The link going away is the same thing as in the mode screen: the instrument stops
answering while those screens are up, and `fnlink.py keys` picks it up again. After the
first logs it gained `--guide`, which says which key to press next and writes it down.

## The keys, one at a time, 2026-11-26

Three more logs, taken with `fnlink.py keys --guide`: the tool asks for a key every 5 s
and writes the request down, so each change follows the name of a key
([`sessions/2026-11-26_keys/`](../sessions/2026-11-26_keys/README.md), the `guided_*`
files). A change came 0.7 to 1.9 s after its request except where said. "Nothing" below
means: no change in the bytes the tool watches (the generator's settings, bytes 5, 6, 14
and 20 of the meter's state, `ms[0xF60..0xF6B]`, the arrows' mode and the 56 bytes of
the oscilloscope's state) and the link not lost. It does not say the key did nothing
else.

**The multimeter**, starting in Automatic (menu entry 0):

| Key | What the log has |
|---|---|
| right | the menu entry one up: 0 to 1 (DC Voltage, variant 1), 1 to 2 (Continuity, variant 2), and from 7 to 0 |
| left | the menu entry one down: 2 to 1, 1 to 0, and from 0 to 7 (Large DC current, variant 1) |
| MOVE | in entry 1, the function from DC Voltage to AC Voltage and the variant from 1 to 2. Logged 3.65 s after the request, 1.3 s before the next one |
| AUTO | from entry 1 to entry 0, Automatic |
| blue | byte 20 (HOLD) from 0 to 1 |
| SELECT, TRIGGER, PRM, SAVE (twice), CH1, CH2, up, down, a short MENU | nothing |

So the list of functions goes round both ways. HOLD was left on at the end of the log.

**The generator**, starting on the list of waveforms with `ms[0xF69]` at 0 and the cursor
on entry 16:

| `ms[0xF69]` | Key | What the log has |
|---|---|---|
| 0 | a short MENU (twice) | nothing |
| 0 | down, up | the waveform's index 16 to 17, 17 to 16 |
| 0 | right, left | nothing |
| 0 | blue | `ms[0xF69]` 0 to 1 |
| 1 | down, up | `ms[0xF6B]` 0 to 2, 2 to 0 |
| 1 | right, left | nothing |
| 1 | blue | `ms[0xF69]` 1 to 2 |
| 2 | left | nothing |
| 2 | up, down | the frequency 2000 to 2001 Hz, 2001 to 2000 |
| 2 | right | nothing |
| 2 | a short MENU | `ms[0xF69]` 2 to 1 |
| 1 | a short MENU | `ms[0xF69]` 1 to 0 |

This is the order the unguided `generator_keys.txt` was asked for, and the two agree
except at the left arrow with `ms[0xF69]` at 2: there `ms[0xF6B]` went from `00` to `10`
and the two keys after it changed the frequency by 10; here no change is logged after
the request for left, and the two keys after it changed the frequency by 1. 1.2 s after
`ms[0xF69]` went to 1, `ms[0xF66]` read 5 instead of 4 for 66 ms; what that byte is has not
been looked at.

**The oscilloscope**, running, CH1 alone with its range index at 2 and its probe at X10,
10 µs per division, the arrows in mode 0. SELECT took the mode 0, 1, 2 and back to 0, and
in each one the four arrows were pressed:

| Mode | right | left | up | down |
|---|---|---|---|---|
| 0 | time base index 9 to 8 | 8 to 9 | range index 2 to 3; `ms[0x1C]` -8 to -44 | 3 to 2; -44 to -9 |
| 1 | `ms[0x1A]` 0 to 1, `ms[0x20..0x27]` 0 to 1000 | back to 0 | `ms[4]` -79 to -78; `ms[0x1C]` -9 to -8 | -78 to -79; -8 to -9 |
| 2 | `ms[0x1A]` 0 to 1, `ms[0x20..0x27]` 0 to 1000 | back to 0 | `ms[0x1C]` -9 to -8 | -8 to -9 |

Three counters run down after those changes, each read on its way to 0:

| Bytes | First read at | Reaches 0 after | Started by |
|---|---|---|---|
| `ms[0x28..0x29]` | 294 to 299 | 2.95 to 3.03 s | a change of time base (mode 0) or of the horizontal position (modes 1 and 2) |
| `ms[0x2A..0x2B]` | 295, 297 | 2.95, 3.00 s | a change of `ms[0x1C]` in mode 2 |
| `ms[0x2C]` | 97, 100 | 0.97, 1.02 s | a change of the channel's position in mode 1 |

With the range, `ms[0x1C]` changed so that it stayed the same voltage above the
channel's zero, if it is counted from the middle of the screen as `formats.md` has it:
with the position at -79, a level of -8 is 71 counts above zero, 2.84 divisions at range
index 2; -44 is 35 counts, 1.40 divisions at index 3, where a division is twice the
volts; and -9 is 70 counts, 2.80 divisions at index 2 again. In mode 1 it moved one
count with the position, which keeps that distance too.

The rest of that log:

| Key | What the log has |
|---|---|
| MOVE (twice) | nothing |
| CH1, CH2, TRIGGER, PRM | the link lost 1.4 to 2.3 s after each request |
| a short MENU after each of those four | the link back 2.5 to 3.1 s after the request |
| blue, blue | `ms[0x2E]` 1 to 0 (stopped), then 0 to 1 |
| AUTO | `ms[0x2F]` 01, 02, 03, 11, 12, 13, FF and back to 0, 5.69 s from the first to the last |

AUTO in more detail, with CH1 alone: at the first step the coupling `ms[0]` went to 0,
the range to 0, the position to 0, the time base to 4 and `ms[0x31]` to 1; the range
then went to 1 and to 2 during step 01; the time base went to 10 on entering step 03;
the coupling came back to 1 on entering step 13; the position went to -96 and, on
entering step FF, to -74; and the last change, with `ms[0x2F]` back to 0, wrote the
trigger level, 1. It started at 10 µs per division (index 9) and ended at 20 µs (index
10); in the unguided log, also with CH1 alone, it started at index 13 and ended at 9.

What these logs do not have: the screen where the mode is chosen (the link is lost while
it is up, so the arrows there cannot be logged), a long press of any key, SAVE in the
oscilloscope (it stores a capture, so it was left out), and any of this with CH2 on.

## Not answered

- What needs a write to the instrument's memory (the 20M limit; the per-channel "FFT" flag
  was since read by switching it on the instrument, above)
  or a read of peripheral registers (TMR10, TMR11, GPIO): the link reads flash and RAM only,
  and the one thing it writes, from patch 4, is the generator's table.
- The idle counter with USB disconnected: without USB there is no link.
- Whether the unit really powers off at 900 counts with USB connected: not waited for.
- The zoom-out sampling rule (above), and all of this with both
  channels on or at a captured time base below 4, where their notes expect the record to be
  interleaved.
