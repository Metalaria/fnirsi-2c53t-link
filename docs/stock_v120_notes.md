# Stock V1.2.0 notes

The facts about FNIRSI's stock firmware that `link.c` and the builder rely on, and how
each one was established. Addresses are absolute (the application is linked at
`0x08007000`).

Evidence labels:

- **code**: read from the disassembly of the image.
- **emu**: exercised by running the image's own code in the Unicorn harness (`test/`).
- **unit**: seen on one retail unit, from the host side, without writing to it.
- **ram**: read out of the unit's RAM over the link (`fnlink.py read`) and compared with a
  photograph of its screen at that moment, or with a setting changed between two readings.

Since 2026-11-23 the patch has run on one unit; what that showed is in
[`hardware_results_2026-11-23.md`](hardware_results_2026-11-23.md) and
[`hardware_results_2026-11-24.md`](hardware_results_2026-11-24.md), and the decoded meter and
scope formats are in [`formats.md`](formats.md). The labels below are as they were when
each fact was established.

## Image

| | |
|---|---|
| File | `APP_2C53T_V1.2.0_251015.bin` |
| sha256 | `a17c5c35c97bb898f15672a1747bc1041d8ed507c16999ddba0d1e4e2ec0c760` |
| Size | 751,232 B (`0xB7680`), ends at `0x080BE680` |
| Initial SP | `0x20036F90` |
| `.data` | compressed; unpacked by the scatter-load code at `0x0800718C` (done at `0x080072F0`) |
| MCU | Artery AT32F403A, FPGA Gowin GW1N-UV2 |

The initial RAM contents used by the tests are obtained by running that scatter-load code
in the emulator, not by decompressing by hand. (emu)

## System mode

One byte at `0x20001060` (state base `0x200000F8` + `0xF68`). (code)

| Value | Meaning |
|---|---|
| 1 | Multimeter (enables USART2). Seen on the unit: meter frames flow in mode 1. |
| 2 | Oscilloscope. Seen on the unit: scope records flow in mode 2. |
| 3 | Signal generator (per OpenScope-2C53T's notes; not checked here). |
| 4 | Settings (per OpenScope-2C53T's notes). |
| 7 | USB Sharing: full-screen image `3:/System file/87.jpg`, mass storage, no measuring. |
| 8 | Set at power-on when the settings page does not start with `0x55` (code, `0x0802CD9A`): a first start. Its up and down keys step the language byte. Not seen on the unit. |

USB Sharing is modal. On the unit, the drive exists only while that image is shown, and
with USB Sharing off the host sees no device. (unit)

## USB

Stock uses Artery's usbd core. (code, emu)

- `usbd_core_type` at `0x20002B24`: USB registers `+0`, class handler `+4`, descriptor
  handler `+8`, IN endpoints from `+0x0C`, OUT endpoints from `+0x10C` (0x20 bytes each),
  setup packet `+0x20C`, EP0 state `+0x220`, connection state `+0x224`.
- Stock's mass-storage class table at `0x20000030`, descriptor table at `0x20000058`.
- USB interrupt: vector 36 at `0x08007090`, handler `0x0802E8E5`, core address hard-coded.
- `usbd_ept_open(udev, addr, type)` `0x08040B24` (max packet fixed at 64),
  `usbd_ept_close` `0x08040874`, `usbd_ept_send(udev, addr, buf, len)` `0x08040EB4`.
- The peripheral is initialised at boot (`0x0802CB86` onwards) and then left
  disconnected. Stock connects only at `0x080106B6`, when the mode is 7.
- Stock disconnects on entering modes 1, 2, 3 and 4 (`0x0800E336`, `0x0800E49C`,
  `0x0800E5BA`, `0x0800BF0E`) and at the start of the settings-save function `0x080293BC`.

The unpatched image enumerates in the harness as `2e3c:5720` "AT32 Mass Storage", which is
what Windows reports for the real unit (`VID_2E3C&PID_5720`, `REV_0200`). (emu, unit)

That is why the link is a second class behind the same core: the cave swaps the two
pointers at `0x20002B28` and `0x20002B2C` while a measurement mode is active and gives
them back otherwise.

## Settings and calibration page

- `0x08006000`-`0x080067FF`, below the application. (code)
- The settings-save function `0x080293BC` erases and reprograms it from RAM tables at
  `0x20000358`-`0x2000044A`. (code)
- The factory bootloader's update path does not touch it (see below). (code)

The patch never writes flash. `fnlink.py dump-cal` reads this page out over the link, with
the bootloader's flag page after it (4 KB in all).

What is where in those 4 KB. The structure is OpenScope-2C53T's reading of stock's code
(`reverse_engineering/analysis_v120/factory_cal_truth_2026-08-14.md`): 304 bytes, a signature
and the user settings first, the calibration table from `0x38`. What this unit adds (unit),
from two dumps a day apart with stock saving settings in between:

| Offset | Content | Between the two dumps |
|---|---|---|
| `0x000`-`0x037` | signature and user settings | 18 bytes changed, within `0x003`-`0x016` and `0x025`-`0x02E` (scope and generator settings had been changed) |
| `0x038`-`0x12F` | calibration table | identical |
| `0x130`-`0x1FF` | not part of the structure | all 168 bytes of `0x140`-`0x1E7` went from `0xFF` to zeros and a few RGB565 pixel values |
| `0x200`-`0x7FF` | never programmed | `0xFF` |
| `0x800`-`0xFFF` | bootloader flag page | identical |

Stock programs 512 bytes on every save although its structure is 304: the rest is whatever
its staging buffer holds. OpenScope's analysis puts that buffer at `0x2002AC00`, in a region
stock also uses for graphics, and this unit agrees (ram: the 168 bytes were found, all of
them, at `0x2002AD40`, which is the buffer plus `0x140`). The structure ends before them, so
they carry nothing, but they make the sha256 of the page useless as a
fingerprint of a unit's calibration. `fnlink.py dump-cal` therefore also prints the sha256 of
the table alone, and `fnlink.py cal-info A B` says in which of these regions two dumps differ.

## Multimeter

Meter chip on USART2, 9600 8N1. (code, emu)

- Interrupt handler `0x0802E7B4`, receive index `0x20004E10`, receive buffer `0x20004E11`.
- Data frame: 12 bytes starting `5A A5`. Echo of a command: 10 bytes starting `AA 55`.
- Transmit buffer `0x20000005` (function word at `0x20000008`), meter mode `0x20001025`,
  value `0x20001028`, semaphore `0x20002D7C`, task entry `0x0803DAC0`.

Meter state, from `0x20001020` (unit; details in `formats.md`): menu entry `+5`, function
`+6`, variant inside the entry `+14`, screen status `+13`, unit code `+16`, AC frequency `+18`,
HOLD flag `+20` (`0x20001034`), MAX / MEAN / MIN floats `+32`, `+36`, `+40`, their decimals
`+44..46` and unit codes `+50..52` (0 mV, 1 V, 5 nF, 9 °C, 10 µA).

Buzzer. PC3 (EXTI line 3) carries the meter chip's own buzzer tone. Stock's handler at
`0x08009C10` turns the buzzer on at each edge (duty 10 to TMR11 C1DT, `0x40015434`) only in
the multimeter and with menu entry 0, or entry 2 with variant 2: Automatic and Continuity on
the unit. Every edge zeroes the tick counter `0x20002D67`, which the 10 ms tick advances while
the buzzer is on and which turns it off at 11, so the beep ends 100 to 110 ms after the last
edge; a key click uses the same counter. (code, emu, unit: see
[`hardware_results_2026-11-25.md`](hardware_results_2026-11-25.md))

Display digits: bytes 2 to 6 carry four 7-segment digits, each split across two bytes.
Digit `i` (0 to 3) is the high nibble of byte `2+i` together with the low nibble of byte
`3+i`. Within that 8-bit mask:

| Bit | `0x80` | `0x40` | `0x20` | `0x10` | `0x08` | `0x04` | `0x02` | `0x01` |
|---|---|---|---|---|---|---|---|---|
| Segment | a | f | e | decimal point | b | g | c | d |

The decimal point bit of digits 2 to 4 means "point in front of this digit". Byte 2 bit 4
is the minus sign and byte 2 bit 3 a leading "1" (they are the low-nibble side of byte 2,
which belongs to no digit). With no explicit point and byte 8 bit 7 set, the reading is in
the lowest range (`x.xxxx`). `decode_meter_frame` in `fnlink.py` is the reference.

Frames with known readings (from OpenScope's tests), used as decoder vectors:

| Frame | Reading |
|---|---|
| `5AA5C6F7EBEB0F000200014E` | 5.008 |
| `5AA546DECF4F0E0002000182` | 4.994 |
| `5AA54ECE8FEA0F008200017F` | 1.4978 |
| `5AA5A5ADED9F0F0002000031` | 228.3 |
| `5AA5E42E6325070000000000` | "Auto" |

Unit and prefix: decoded on hardware, see [`formats.md`](formats.md).

## Oscilloscope

- `fpga` FreeRTOS task, entry `0x0803E454`, one function with the SPI3 command byte at
  `[sp+0x36]`. (code, emu)
- Commands 4 and 5 read 1026 bytes (2 discarded) into CH1 `0x200006A8` and CH2
  `0x20000AA8`, 1024 samples each, and apply the calibration in place. (code, emu)
- Every command handler returns to `0x0803E498` or `0x0803E49A`. Nothing branches into
  `0x0803E49C`-`0x0803E4A2`, which is what makes the 4-byte hook at `0x0803E49A` safe.
  (code; the patched task is compared with stock's in the tests)
- The 301-point display traces are at state `+0x356` and `+0x483`. The file
  `Screenshot simple file/1.bin` (607 B) is 301 CH1 points, about 300 CH2 points and 6
  trailing bytes. (unit, code)

Scaling of the 1024-sample records to volts and seconds: established on hardware, see
[`formats.md`](formats.md).

Stock's own measurements (ram). Three blocks, each with twelve 32-bit words for CH1 followed
by twelve for CH2, in the units the screen's measurement boxes are computed from:

| Block | Address | Content |
|---|---|---|
| last acquisition | `0x20000148` | as measured on the newest record |
| averaged | `0x200001A8` | a running average of the block above |
| shown | `0x20000208` | what the screen shows: the averaged block while running, the last acquisition once stopped |

| Word | Content | Unit |
|---|---|---|
| 0 | frequency | Hz |
| 1 | period | ns |
| 2, 3 | positive and negative duty cycle | 0.01 % |
| 4, 5 | positive and negative pulse width | ns |
| 6, 7, 8 | Vmax, Vmin, Vpp | µV, signed (Vmin goes below zero) |
| 9 | a second amplitude, equal to Vpp or a few mV under it; stock labels it "Amp:" | µV |
| 10, 11 | what stock labels "Vrms:" and "Avg:". With a square from about 0 to 304 mV at about 50 %, 208000 to 209500 and 147833 to 148000 in four readings, and 208.0 to 212.0 mV and 144.0 to 148.0 mV under those labels on three screens, none of them of an acquisition that was also read (2026-11-26) | µV |

Compared with photographs of two stopped screens: 40232 with "40.23KHz", 6451 with "64.51%",
114000 with "114.0mV"; and, on another day's wave, 50176 with "50.18KHz", 5050 with
"50.50%", 220000 with "220.0mV". While stopped the shown block does not change. The running
flag is scope state `+46` (`0x20000126`). CH2's twelve words follow the same layout: with the
generator's 50 kHz square wave on CH2 alone they read 50 kHz, 20 µs, 49.39 % and 329 mV peak
to peak at 50 mV per division, and 345 mV at 100 mV per division, where the record itself
gives 344 mV (2026-11-25). With a channel off its words hold a frequency and its period (0 Hz,
tens of Hz, or about 1.7 MHz) and a few millivolts. Outside the oscilloscope mode the shown
block is all zeros.

The code that prints them (it loads the table of labels at `0x0801BB8A`; code) takes word
*i* of the shown block, CH1's from `ms+0x110` and CH2's from `ms+0x140`, and prints it after
label *i* of a table at `0x0804BF28` (twelve pointers for English, then twelve for Chinese,
which is the row used when the language byte `ms[0xF60]` is 1, `0x0801B9B0`: `Freq:`, `Peri:`,
`Duty+:`, `Duty-:`, `Wid+:`, `Wid-:`, `Vmax:`, `Vmin:`, `Vp-p:`, `Amp:`, `Vrms:`, `Avg:`)
with `"%s%.2f%s"` or `"%s%.1f%s"` (`0x0801CA64`, `0x0801CA70`). The unit comes from a
table of four per kind at `0x0801CA24`, the kind of each word from the bytes at
`0x0801CA18` (`00 01 02 02 01 01 03 03 03 03 03 03`): Hz, KHz, MHz for the frequency; nS,
uS, mS, S for the period and the widths; % for the duties; mV, V for the six voltages.
Label, value and unit are drawn as one text in a box of 100 x 15 in the 15 px font, one box
every 100 px.

**The settings in the scope state** (unit, 2026-11-26, each switched on the instrument and
back while the 56 bytes were read; the table is in [`formats.md`](formats.md)): the
couplings are `ms[0]` and `ms[1]` (1 DC, 0 AC), the menu's "FFT:" rows `ms[0x0A]` and
`ms[0x0B]`, the trigger's channel `ms[0x16]` (0 CH1, 1 CH2), its mode `ms[0x17]` (0 Normal,
1 Auto, 2 Single) and its edge `ms[0x18]` (0 rising, 1 falling). While a channel's FFT is
on, `ms[0x0C..0x0F]` (CH1) or `ms[0x10..0x13]` (CH2) hold `0x20033940` and are zero
otherwise. In Single the running flag `ms[0x2E]` read 0.

**The FFT block** (unit, 2026-11-26). That address, `0x20033940` in every reading, holds 128
bytes and then 32-bit floats while the FFT is on: the floats are a spectrum by index, and
byte *k* is float *k* scaled so that the largest is 50, which is the height of the bars
stock draws in the upper left of the grid. With the generator's 1 kHz square at 200 µs per
division the fundamental is at index 2 and the odd harmonics follow 4 to 4.2 indexes
apart; the bytes are nearly a mirror image about index 64. With the FFT off the block is
zero (three 16-bit values aside). How many floats there are and what frequency an index
stands for are not established; no instruction or literal of the image holds the address.

**The AUTO key** (unit: `ms[0x00..0x37]` watched every 10 ms through one press, both
channels on and in DC; code for the two addresses). At the key stock saves the couplings of
the channels that are on in `ms[0x31]`, CH1's in the low nibble (`0x0800CC46`) and CH2's in
the high one (`0x0800CD8C`), sets `ms[0x2F]` to 1 (`0x0800CC38`) and clears the couplings,
the ranges and CH1's position; the time base went from 13 to 4. From there `ms[0x2F]` is
the step and `ms[0x32]` a count of 10 ms ticks that starts again at most steps:

| After the key | `ms[0x2F]` | What else changed |
|---|---|---|
| 0 | `01` | couplings 1 to 0, ranges 2 to 0, CH1's position -76 to 0, time base 13 to 4, `ms[0x31]` = `0x11`, `ms[0x32]` = 0 |
| 0.41 s, 0.85 s | `01` | CH1's range 0 to 1, then to 2 |
| 1.28 s | `02` | |
| 1.85 s | `03` | time base 4 to 13 |
| 1.96 s | `11` | |
| 2.40 s | `21` | CH1's range 2 to 3 |
| 2.84 s | `22`, then `32` | CH2's coupling back to 1 |
| 3.27 s | `F3` | CH1's coupling back to 1, CH2's position 0 to -45 |
| 3.71 s | `FF` | CH1's position 0 to 9 |
| 5.76 s | `00` | trigger level -1 to 46 |

After that `ms[0x32]` counted on to `0xFF` and stayed, which is the value it had before the
key. The couplings are restored from `ms[0x31]` (`0x08008E3E`: the nibble of the channel,
then 1 in its coupling byte). `ms[0x31]` kept its value to the end of the watch, so it tells
how the couplings were at the last AUTO; it read 0 after the unit had been reflashed, which
restarts it. What was connected to the inputs during the watch was not
noted, so the ranges and positions it ended at say nothing by themselves.

What the arrow keys do is the low nibble of state `+0x354` (`0x2000044C`): 0 changes the time
base and the range, 2 moves the trace and the trigger level, 3 is for the cursors; SELECT
steps it (code: handler `0x0800D2F8`, entry 4 of the key table at `0x08046548`; unit: 2 -> 0
with one press). While stopped, the display array at `+0x356` is a slice of the record at the
captured time base, the record through a 16-tap interpolator when zoomed in, and is left alone
when zoomed out; the rules, checked on 56 snapshots, are in
[`hardware_results_2026-11-25.md`](hardware_results_2026-11-25.md). (unit, code)

## Signal generator

Settings from `0x20000F50` (ram: five dumps, one setting changed between each, compared with
photographs of the generator screen):

| Offset | Content |
|---|---|
| 0 | 0 or 1; it changes while the generator screen is operated. The routine that programs the timer (`0x08029D40`) stops it when this byte or the frequency is 0, which makes it the output's on/off (code) |
| 1 | waveform as its position in the menu, 0 to 12: 1 Square Wave, 2 Sawtooth wave (a triangle at 50 % duty), 3 Half-wave, 4 Full Wave, 5 Step Wave, 6 Reverse Step Wave, 7 Index Up, 8 Index Decrease, 9 Direct Current, 10 Multi-audio, 11 Sink Pulse, 12 Lorentz waves. All twelve were read one by one on 2026-11-25, the label from the screen and the index from this byte; the shapes of 7 to 12 were not looked at. Index 0 is one step before Square Wave in the menu (unit) and is the sine: stock builds it from the 100-point table at `0x0804D848`, a sine to within half a count (code: the builder at `0x08029E14` switches on this byte, 0 to 12), and stock's table of waveform names, thirteen per language from `0x0804C61C`, starts with "Sine Wave". The table spells entry 9 "Direct current". The samples are played by a circular DMA of 100 transfers from the table in RAM to the DAC, clocked by TMR7 (code: `0x0802CAC2`, `0x08029D40`). The timer's period register gets 1, or `0x13` when `200 * frequency` is under 4000, and its divider is computed from a clock frequency so that the updates come at `100 * frequency`: one pass of the table per period |
| 4–7 | frequency in Hz, 32 bits (1000, 15000, 35000, 50000 seen) |
| 8 | duty cycle in % (50 seen) |
| 9 | amplitude in tenths of a volt (30 and 20 seen for 3.0 V and 2.0 V) |
| 10–209 | one period of the wave as 100 16-bit DAC values |

The sample table follows the waveform and the amplitude: 20 to 3742 for a 3.0 V square,
1880 to 3742 for a 3.0 V full wave, 1260 to 2501 at 2.0 V.

Who rewrites the table (code): the builder `0x08029E14` is called from five places and no
other: at start-up (`0x0802E284`), and from the two key handlers after a change of waveform
(`0x0800A27C`, `0x0800AA38`) and of duty or amplitude (`0x0800A5DA`, `0x0800AFF6`). A change
of frequency only reprograms the timer (`0x0800A468`, `0x0800AF52`) and jumps past the
builder. The amplitude is held at 30 (3.0 V) by the handler (`0x0800A592`), and the sine is
`table * amplitude / 33 + 20` (`0x0802A1CE`). Patch 4's `WAVE` request stores into this table
with the same scaling; what it stores lasts until one of those five calls.

### The list of waveforms (code, and stock's own routines run in emulation)

What in stock depends on there being thirteen waveforms, found by going through every access
to the waveform byte `ms[0xE59]` (18 of them):

- The "next waveform" key steps only from an index of at most 11 (`cmp r0, #0xb` at
  `0x0800A9C2`); the "previous" one stops at 0. Both keep the first row shown, `ms[0xF6A]`,
  so that the cursor is among the six rows of the list, for any length of list.
- Four places set that first row when the list is opened or at start-up, as `min(index, 7)`:
  `0x0800C2A6`, `0x0800D90E`, `0x0800E5F8`, `0x0802DFFC`.
- The list itself (`0x0801579C`, called from `0x08010862`) draws six rows, each loading its
  name with `ldr r0, [r0, #-0x34]` from `names + 52 * language + 4 * row`: a row past the
  thirteenth would read the next language's names.
- The builder `0x08029E14` returns without touching the table for an index above 12.
- Three places turn the index into a picture on the drive, `3:/System file/N.jpg`:
  `0x080108EA` (N = 90 + index with the output on, 103 + index off) and `0x080127BC`,
  `0x0801F8EE` (N = 10 + index on, 23 + index off). An index above 12 would name a file that
  is another picture.
- Nothing else: the two places that ask whether the waveform has a duty test "index - 1 < 2",
  the loader and the saver move the byte as it is.

Patch 5's `--menu-wave` changes exactly these and is tested against them.

## User interface

### Keys

The key scan does not act on a key: it sends a one-byte code to a FreeRTOS queue, and a task of
its own takes it from there. (code)

- Queue handle at `0x20002D70`. The scan sends at `0x080404F4` (`xQueueGenericSend`,
  `0x08041CF0`, no waiting).
- Key task at `0x08040008`: it waits on the queue for ever, then ignores the key while any of
  `ms[0x2F]`, `ms[0x30]`, `ms[0x33]`, `ms[0xF2C]` is not 0 or `ms[0xE10]` is 1 or `0xFF` (2 and
  3 are cleared and the key handled). Otherwise it calls entry `code - 1` of the table of
  handlers at `0x08046548`.
- After every key taken from the queue, handled or not: the buzzer's duty is set to the volume
  `ms[0xF62]` when that is not 0 (the click, see the buzzer above) and the idle counter
  `ms[0xF6C]` is zeroed.

### Languages

V1.2.0 offers Chinese and English, but the tables of eight languages that V1.1.0 added are
still in the image and still indexed by the language byte. (code; on the unit, Spanish has
been in use since 2026-11-25 with `--languages`)

- `ms[0xF60]`: 1 Chinese, 2 English, 3 Russian, 4 Japanese, 5 Spanish, 6 Portuguese, 7 German,
  8 Korean, by the order of the rows of every table.
- **What limits Settings > Language to two.** The list is drawn with two fixed rows, `中文`
  and `English` (strings at `0x080BD09B` and `0x080BC6AE`), each with a mark that is on when
  the byte is 1 or 2 (pictures `75.jpg` / `78.jpg`, and `74.jpg` / `77.jpg` off the cursor;
  compares at `0x0801CFAA`, `0x0801DC8A`, `0x0801DDCE`). The OK handler `0x0800BD60` stores
  the row under the cursor plus one (`ldrb.w r0, [r5, #0xf6b]` at `0x0800BECA`, `adds`, store
  at `0x0800BEDC`).
- The two handlers that step the byte directly, `0x0800AB12` (up, only while it is at most 1:
  `cmp r0, #1` at `0x0800AB16`) and `0x0800A340` (down to 1), belong to system mode 8, not to
  the settings menu (emulation of the key handlers over the menu's state bytes). Mode 8 is
  set at power-on when the first byte of the settings page is `0xAA`, or is neither that nor
  `0x55` (`0x0802CD9A`): the screen of a first start. An earlier version of this note took
  that compare for what limits the menu; it is not.
- **Nothing checks the byte's range.** The settings loader stores the word of the page that
  holds it as it comes (`0x0802CE48`), the saver packs it back (`0x0802962A`), and a factory
  reset writes 1 (`0x08029506`).
- 74 places read the byte, all with `ldrb [base, #0xf60]`. 55 use it as an index, into 31
  tables, and every one of those tables has all eight rows of valid strings (checked row by
  row). 10 turn it into "is it 1?" and take the English row otherwise, so they show English
  in the six other languages: the twelve measurement labels (`Freq:`, `Peri:` …), the trigger
  menu and the channel menu among them. The other 9 compare it with 1 or 2: the two mode-8
  handlers, the three marks above, `0x0801F4A6` (it draws `152.jpg` in English and takes
  another path otherwise) and three that ask "is it 1?" and were not read one by one
  (`0x08013642`, `0x0801D876`, `0x0801F332`).
- In all eight languages, 70 strings each: the four mode names and the screen titles, the
  settings menu (7 labels) with its start-up choices, version and model, the meter's twelve
  function names, the generator's three labels and thirteen waveform names, the
  oscilloscope's menu (8 labels), and the messages: battery low, auto shutdown, saving, saved,
  save failed, the four "waiting" ones, exceeded limit, deleting, delete / confirm / cancel /
  return, and the factory-reset warning.
- **The fonts are in the image and still cover all of it.** Text is drawn by an LVGL-style
  routine (`0x0800F154`) with fonts whose descriptors are in RAM (0x30 bytes each) and whose
  glyphs are in the image. Five of them, of 16, 18, 20, 25 and 28 px, are at `ms+0xFEC`
  (`0x200010E4`) on, with their glyphs from `0x08077F9C` to `0x080BA1B8`. Each has one list of
  code points, searched for the character itself (`0x0802F868`): 567 of them, 569 in the
  25 px font. Every character of the 560 strings above is in all five fonts, except two that
  only the 25 px font has and only the "over limit" message in Chinese and Japanese uses. A
  character without a glyph ends the string there (`0x0800F19C`). There are three more
  descriptors, two before those five and one after (see "Texts that do not fit").
- Not looked at: whether the longer strings fit where V1.2.0 draws them, and what mode 8
  shows.

`build_link_image.py --languages` uses this: it replaces the load at `0x0800BECA` by a call
that returns the row for the first row and, on the second, the current language while that
is 2 to 7, so that stock's own `adds` steps to the next one. `tools/language_tables.py`
repeats the table and font checks on a stock image.

### Texts that do not fit (code, the routine run in emulation, and the unit)

- **The text routine**, `0x0800F154(x, y, w, h, string, font, colour, align)`. The string is
  UTF-8 and ends at NUL, LF, CR or the first character the font has no glyph for. A first
  pass (`0x0800F22A`) makes lines: characters are added while the sum of their advances is
  at most `w`; at the one that does not fit, the line ends after its last space (or at
  that character, if it is a space or the line has none). If the lines, each as high as
  the font, fit in `h`, they are drawn centred in the box, each centred in `w` (align 1).
  If they do not, a second pass (`0x0800F2E4`) breaks wherever the width runs out, and the
  lines are drawn from the top of the box down, past its bottom: nothing clips a text to
  its box, only the band being painted does. (code, and the routine run in emulation: its
  fonts are in the image's initialised data, so it paints without a dump of the unit)
- **Fonts.** Eight descriptors of 0x30 bytes in RAM, from `0x20001084`, with the line
  height at +8 and, at +0x18 and +0x1C, the addresses of the glyph descriptors and of the
  character map:

  | Descriptor | Height | Glyph descriptors | Character map | Characters |
  |---|---|---|---|---|
  | `0x20001084` | 12 px | `0x0806F9D8` | `0x0806FCD8` | 96: U+0020 to U+007F |
  | `0x200010B4` | 15 px | `0x08076938` | `0x08077B10` | 571 |
  | `0x200010E4` | 16 px | `0x080807F0` | `0x080819A8` | 567 |
  | `0x20001114` | 18 px | `0x0808B214` | `0x0808C3CC` | 567 |
  | `0x20001144` | 20 px | `0x08098FDC` | `0x0809A194` | 567 |
  | `0x20001174` | 25 px | `0x080A0468` | `0x080A1630` | 569 |
  | `0x200011A4` | 28 px | `0x080B9000` | `0x080BA1B8` | 567 |
  | `0x200011D4` | 63 px | `0x080BB2F0` | `0x080BB3A8` | 23: the digits, space, `-`, `.` and ten letters |

  The height is also that of a line. A character map has a pointer at +8 to a sorted list
  of 16-bit code points and their number at +16; the glyph descriptors, 8 bytes each, are
  in the same order: bitmap offset in the low 20 bits of the first word and the advance, in
  whole pixels, in the top 12; then box width, box height and the two offsets. The 15 px
  font is the one of the oscilloscope's measurements (`0x0801BBEC`); one place each loads
  the 12 px (`0x08016190`) and the 63 px (`0x0801633C`), not looked at. Of the accented
  Latin letters the six from 15 to 28 px have `Ä Í Ü ß à á â ã ä ç ê í ñ ó õ ö ü`: no `é`
  and no `ú`, which Spanish needs, and no inverted question or exclamation mark.
- **The multimeter's title.** Table of pointers at `0x0804C43C`: twelve functions
  (`ms[0xF2E]`: 0 auto, 1 DC V, 2 AC V, 3 continuity, 4 diode, 5 resistance, 6 capacitance,
  7 temperature, 8 and 9 small DC and AC current, 10 and 11 large) for each of eight
  languages, read at `0x08010AE8` and `0x08015C28` and drawn at (52, 0) in a box of 216 x 29
  in the 28 px font: one line, and the band is the box, so a second line is never seen.
  Chinese, English, Japanese and Korean fit. The Spanish names of functions 1, 2 and 8 to
  11 are 234 to 306 px wide, and the first 207 px of the two voltages are the same words,
  so both read alike on the screen; the currents are cut one to five letters into the word
  that tells DC from AC. (seen on the unit, 2026-11-26) By the same sums, the same six
  functions do not fit in Russian (248 to 330 px) or Portuguese (270 to 308), and four of
  them in German (221 to 244).
- **The generator's settings.** Table at `0x0804C5BC`: three labels (frequency, duty,
  amplitude) for each language, drawn by `0x08015988` at x = 122 in a box of 54 x 22 in the
  18 px font, one row every 32 px from y = 142. Only the Chinese labels all fit on one
  line; none of the English, Spanish, Portuguese or German ones do, and English shows
  "Frequen" over "cy". The second line lands in the gap before the next row,
  except for the third row, whose second line is at the bottom edge of the screen: the
  Spanish for amplitude is 55 px, and the unit shows it without its last letter. (seen on
  the unit, 2026-11-26)
- **The generator's waveforms.** Table at `0x0804C61C`: thirteen names for each language,
  six of them drawn at a time by `0x0801579C` in boxes of 100 x 36 in the 18 px font, so
  two lines fit and stock's longer names break at their space. The generator's title is
  one text per language at `0x0804C398` (box of 216 x 20, 20 px font); all eight fit.
- **The oscilloscope's menu.** Table at `0x0804C804`: eight entries for each language, six
  drawn at a time by `0x08012DE0` at x = 24, one every 36 px from y = 24, in a box of
  76 x 36 in the 18 px font, left-aligned; the high nibble of `ms[0xF6A]` is the first one
  shown. Two lines of 76 px, then. In Spanish five entries fit, two of them to the pixel,
  and three begin with a word of 88 px: the routine breaks it after its eleventh letter,
  and of "Configuración de disparo" the last two letters go to a third line, which in
  emulation lands on the next entry when there is one below. English has the same trouble ("Cursor Mea /
  surement", "Parameter / Measureme"). (seen on the unit in both languages, 2026-11-26,
  and the routine run in emulation paints the same)
- **Texts with two rows only.** Some tables have a Chinese row and an English one, and
  every language but Chinese is shown the English: the labels of a channel's settings
  (`0x08014198`, five a row: channel, probe, coupling, FFT and a 20M limit that the panel
  never draws; box of 100 x 32 at x = 128, 18 px font, left-aligned), the labels of the
  trigger's settings (`0x0804C7CC`, three a row: mode, edge, channel; the same box) and its
  three modes (`0x0804C7E4`; box of 66 x 20, and again in the bar at the bottom of the
  screen, `0x0801E784`, which measures the text), the multimeter's three boxes
  (`0x0804C3D8`: MAX, MEAN, MIN; boxes of 100 x 20, 20 px font, `0x0801CAB0`) and the
  oscilloscope's twelve measurement labels (`0x0804BF28`, English row first; see
  "Oscilloscope"). The grid of buttons where the measurements are chosen ("Freq",
  "Wid+", ..., "A-on", "A-off") is not text of the image: no such strings are in it.
  Both panels are painted by the oscilloscope menu's options routine,
  `0x08013338`, which switches on `ms[0xF6A]`: 3 and 4 are CH1's and CH2's settings, 5 the
  trigger's (`0x08013674`). So an instrument set to Spanish says "Coupling:", "Edge:"
  and "MEAN".

`build_link_image.py --texts` replaces a text by pointing its slot at a string in the cave,
after laying the string out as the first pass does and checking it against the box;
`texts/es.txt` does it for the six titles, the amplitude label, six waveform names and the
three entries of the oscilloscope's menu that begin with that word.
`texts/es-english-rows.txt` does it for eighteen texts of the two-row tables, which changes
them for every language but Chinese; the two panels, the bar and the boxes were painted in
emulation by stock's own routines with that file, on a RAM dump of the unit (the
measurements were not). The image is on the unit since 2026-11-26: the flash reads back as
built and the calibration table is unchanged. Photographs show six of its ten measurement
labels with their values, whole (Frec:, Ciclo+:, Anc+:, Anc-:, Vef:, Med:, beside stock's
Amp: and Vp-p:), and a channel's panel as it was painted in emulation; the trigger's panel,
the bar and the multimeter's boxes have not been reported.

### The options of a setting (code, and stock's own routines run in emulation)

The settings screen (system mode 4) has the list of seven settings on the left and the
options of the selected one on the right. `ms[0xF6A]` is the setting, `ms[0xF69]` is 1 while
the cursor is among the options and `ms[0xF6B]` is the option under it.

- **Keys.** Handler 12 (`0x0800A900`, next) moves the option cursor at `0x0800ABC2`: it stops
  at the byte of a table indexed by the setting, `0x0800B1D8`: `01 01 03 03 00 00 01`. Handler
  11 (`0x0800A148`, previous) stops at 0. OK (`0x0800BD60`) with the cursor in the list opens
  the options with the cursor on the first; with the cursor on an option of Language it
  stores that option plus one as the language, whatever the option is. Keys 1 and 10 leave the
  options. So raising the first byte of that table is all it takes for the cursor to reach,
  and OK to choose, an eighth language.
- **Painting.** One routine, `0x0801CF50` (called at `0x080111DA` after the band is cleared,
  and listed in the table of routines at `0x0804C130`), switches on the setting: seven cases,
  Language at `0x0801CF7E`, Auto Shutdown at `0x0801D292`, one exit at `0x0801D28C`. A panel
  has four slots, 36 px apart from y = 61. Language uses the first and third for its two
  fixed rows; Start-up mode and Auto Shutdown use all four (loop at `0x0801D03C`).
- **A row.** Under the cursor: picture `72.jpg` at (121, y), then the mark, `75.jpg` at
  (166, y + 8) when it is the one in use or `78.jpg` at (165, y + 8), and the text in colour
  index 2 (`0x055F`) of the table at `0x0804C180`. Off the cursor: a fill with `0x18C3` of
  x 122..303, y + 1..y + 32, the mark `74.jpg` or `77.jpg` at (166, y + 10), and the text in
  colour index 1 (white). The text is `0x0800F154(x, y, w, h, string, font, colour, align)`
  with (182, y, 92, 34), the 18 px font's descriptor `0x20001114` and align 1 (centred);
  strings are UTF-8. Pictures are `0x08039F6C(path, x, y)`.
- **Bands.** The screen is painted a band at a time. The band is described at `0x20008350`:
  x, y, width and height as 16-bit values, then a pointer to width x height RGB565 pixels.
  The fill tests every pixel against it; the picture and text routines clip for themselves.
- **Stack.** All of this runs in the `display` task (1536 bytes of stack). With the text
  routine really running, a panel reaches 264 bytes below the routine's entry. On the unit,
  after sessions in the three instruments, 710 bytes of that stack had never been written
  (2026-11-24, patch 3); with the language panel of `--language-menu` in use, 719
  (2026-11-26). The other tasks are tighter: on the 24th `dvom_RX` had 21 bytes of 512 left
  and `key` 76 of 512. `fnlink.py tasks` reads these figures from the heap, where each task's
  stack sits just before its control block and what was never reached is still zero.

`build_link_image.py --language-menu` uses this instead of the call above: the table's first
byte becomes 7 and the Language case branches to `link_language_panel()` (`langmenu.c`),
which paints the four slots of the cursor's page as Auto Shutdown's are painted and leaves
through the routine's exit. The image has no name for six of the languages; the builder
supplies them and checks them against the fonts (`日` of 日本語 is in none of the six from 15
to 28 px).
In emulation the result was compared with stock's own Auto Shutdown panel in the same state:
the same calls with the same arguments, the same pixels outside the text, in every
combination of cursor and language, and with the band whole, partial and elsewhere.

## FreeRTOS

SysTick handler `0x0802A994`, `xTaskIncrementTick` `0x0804278C`, `xTickCount`
`0x200062D4`, `uxSchedulerSuspended` `0x200061E4`, `xQueueReceive` `0x080421D8`,
`xQueueGiveFromISR` `0x0804209C`, `vTaskDelay` `0x08041390`. (code)

## Memory used by the patch

| | |
|---|---|
| Code cave | from `0x080BE700`, first 0x100-aligned address after the image |
| RAM | `0x20037000`-`0x20037987`, above stock's stack top (`0x20036F90`) and below the factory bootloader's RAM magic at `0x20037FE0` |

## Factory bootloader

Read from the dump archived by OpenScope-2C53T; written up in full for that project
(issue 98 and pull request 99 there). The points that matter here: (code, unit)

- MENU is sampled once at start-up. Power has to be held until `Firmware upgrade` is
  drawn, and USB only starts after Power is released.
- Data written to the drive's data area is programmed in arrival order, starting at
  `0x08007000`. Any other file written during the copy ends up in the image.
- An update erases the flag page `0x08006800`-`0x08006FFF` and the application area.
  The calibration page is a different flash page and is not touched.
- The application is started without any validity check.

What Windows shows on the unit:

| | Upgrade mode | USB Sharing |
|---|---|---|
| Disk | `AT32 Msc iap`, 8,386,560 B, 2048-byte sectors | `AT32 Disk0`, 14,680,064 B, 4096-byte sectors |
| Volume | FAT, label `IAP`, file `Ready.TXT` | FAT, no label |

## First run on hardware

Done on 2026-11-23, in this order: the unmodified stock image as a rehearsal, the
`--without-scope-hook` image (`fnlink.py info`, `dump-cal` before touching any setting,
`verify`), then the full image. See
[`hardware_results_2026-11-23.md`](hardware_results_2026-11-23.md).

Still to find out: the cost of the 1 KB copy in the acquisition loop, whether a connected
USB link adds noise, Windows with hidapi, Android, and anything on a second unit.
