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

## Not answered

- What needs a write to the instrument's memory (the 20M limit, the per-channel "FFT" flag)
  or a read of peripheral registers (TMR10, TMR11, GPIO): the link reads flash and RAM only,
  and only reads.
- The idle counter with USB disconnected: without USB there is no link.
- Whether the unit really powers off at 900 counts with USB connected: not waited for.
- The zoom-out sampling rule (above), mode 1 of the arrow keys, and all of this with both
  channels on or at a captured time base below 4, where their notes expect the record to be
  interleaved.
