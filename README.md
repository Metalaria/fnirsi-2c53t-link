# stock-link v1 — a USB HID link inside FNIRSI 2C53T stock firmware V1.2.0

The 2C53T's stock firmware only talks to a computer in "USB Sharing", where it is a
mass-storage drive and does not measure. `stock-link` patches the user's own stock V1.2.0
image so that, while the instrument is measuring, it also shows up as a vendor HID device
that can stream what it measures.

| | v1 |
|---|---|
| Identification, read-only access to MCU flash and SRAM | yes |
| Multimeter: every frame of the meter chip, as it arrives (~6 per second) | yes |
| Oscilloscope: each channel's 1024-sample record plus the scope state | yes |
| Status (patch 3): the generator's settings, the scope's own measurements with RUN/STOP, the rest of the meter's state | yes |
| Generator (patch 4): any waveform of 100 samples, played by stock's own generator | yes |
| Languages (`--languages`): the six that stock still carries but no longer offers, by pressing OK on the second row | yes |
| Languages (`--language-menu`): the same eight as entries of Settings > Language, one each | flashed and read back; the menu not reported on yet |
| Texts (`--texts`): the Spanish names of the multimeter's functions, which stock cuts | emulated only |
| Menu (patch 5, `--menu-wave`): waveforms added to the generator's own list, chosen on the instrument | yes |

**Status: runs on one unit.** On 2026-11-23 both images were flashed on a retail 2C53T
through the factory IAP drive. The flash read back identical to the image, the settings and
calibration page was read out, and the multimeter and both scope channels streamed live.
The record is in [`docs/hardware_results_2026-11-23.md`](docs/hardware_results_2026-11-23.md),
including what was not assessed (acquisition timing, noise, Windows, Android). A second
session the next day, with the same image, settled the rest of the multimeter (AC, µF, diode,
HOLD, the AC frequency) and located the generator's settings and the scope's own
measurements in RAM: [`docs/hardware_results_2026-11-24.md`](docs/hardware_results_2026-11-24.md).

**Patch 3 runs on the same unit** since the afternoon of 2026-11-24. It adds the status
stream and nothing else: same two hooks, same stock bytes changed, no new write to stock's
SRAM. Flashed over patch 2 through the factory IAP drive, the flash read back identical to
the image, the status blocks arrived in every mode with the values on the instrument's
screen, and what worked with patch 2 still does. The record is at the end of
[`docs/hardware_results_2026-11-24.md`](docs/hardware_results_2026-11-24.md).

**Patch 4 runs on the same unit** since 2026-11-25. It adds one request, `WAVE`, which stores
samples in the table that stock's signal generator plays, and with it the first write to
stock's own data: those 200 bytes and no others. Flashed over patch 3, the flash read back
identical to the image, the settings and calibration page was identical before and after, and
two waveforms were written, read back and seen on the instrument's own oscilloscope; a change
of frequency on the instrument kept them. The record is in
[`docs/hardware_results_2026-11-25.md`](docs/hardware_results_2026-11-25.md). The builder's
`--languages` is separate and optional; it reached the unit later, with patch 6 (below).

**Patch 5 runs on the same unit** since the evening of 2026-11-25, built with the five menu
entries of the example below. Without options it is patch 4 with a longer INFO; with
`--menu-wave` / `--menu-usb-wave` it adds entries to the generator's own list of waveforms,
which takes the largest change to stock so far: 49 more bytes in 15 places, all in the
generator's menu, its table builder and the three places that pick a waveform's picture.
On the unit the flash reads back as the image, the calibration table is unchanged, the five
entries are in the list with their names, and they play
([`docs/hardware_results_2026-11-25.md`](docs/hardware_results_2026-11-25.md)).

**Patch 6 runs on the same unit** since later that evening, verified the same way. It changes
one thing the first evening with patch 5 showed to be wrong: with a "USB" entry in the menu, `WAVE` used to overwrite whatever the
instrument was playing, so a waveform sent while Square Wave was selected left the instrument
saying "square" and playing something else. From patch 6 such an image stores what it
receives for the USB entry and writes the playing table only while that entry is selected.
On the unit, a sinc sent under Square Wave left the square playing, and a bell sent under
the USB entry played at once.

**The same image with `--languages`** has run on the unit since 2026-11-25 as well: the flash
reads back as the image, the calibration table is unchanged and the instrument is in Spanish.
Choosing a language by pressing OK again and again on a row that says "English" is not what
a menu should ask for, which is what `--language-menu` is for: it passes the emulated tests,
its panel has been painted with the unit's own fonts in the emulator, and it was flashed on
2026-11-26: the flash reads back as the image and the calibration table is unchanged.

Using the instrument in Spanish showed why FNIRSI may have dropped these languages: the
multimeter's title for the voltages and the currents is too long for its box and is cut
before the word that tells DC from AC. `--texts` replaces such texts; it has not been
flashed.

> **One unit is not a guarantee.** The board revision of that unit is unknown and nothing
> has been tried on another. If you flash this, read [Safety](#safety) first and keep the
> official `APP_2C53T_V1.2.0_251015.bin` at hand: the factory bootloader is untouched, so the
> official image can always be put back.

## What the patch changes

| | |
|---|---|
| Stock bytes changed | 10: 6 at `0x0802A994` (SysTick handler) and 4 at `0x0803E49A` (tail of the `fpga` task loop), each a `b.w` into the cave. With `--languages`, 4 more at `0x0800BECA` (a `bl` into the cave). With `--language-menu` instead, 5: 4 at `0x0801CF7E` (a `b.w`) and one byte of data at `0x0800B1D8`. With `--texts`, the pointer of each text replaced (data; 2 bytes differ in each of the six of `texts/es.txt`). With menu entries, 49 more: see below |
| Code added | ~2.9 KB at `0x080BE700`, after the stock image; ~4.2 KB with five menu entries, ~4.7 KB with the list of languages too |
| SRAM used | `0x20037000`–`0x20037A53`, above stock's stack top and below the factory IAP's RAM magic |
| Stock SRAM written | the USB core's class and descriptor pointers (`0x20002B28`, `0x20002B2C`) and the endpoint bookkeeping the stock class itself writes. From patch 4, on a `WAVE` request only: the generator's table of samples, `0x20000F5A`–`0x20001021` |
| Flash written at run time | none |

Stock keeps the USB peripheral initialised but disconnected, and its Artery usbd core
reaches the class through two pointers. In a measurement mode (system mode 1–3), once stock
has left the bus idle for one second, the cave points the core at its HID class and
connects. In any other mode it disconnects and restores stock's pointers within one tick,
so USB Sharing is unchanged.

Stock disconnects USB on every mode change and before it rewrites its settings page; the
link honours that and only returns after the bus has been idle for a second. The host
therefore sees the device re-enumerate when the mode changes, and must subscribe again.

**Meter.** No stock code is hooked. The class's start-of-frame callback (1 kHz while a host
is attached) watches the receive index of stock's USART2 handler and copies the 12 bytes
when a frame completes.

**Scope.** Stock's `fpga` task reads each channel into a 1024-byte buffer and corrects it in
place. The second hook runs in that task, after each SPI3 command, and copies the buffer
only while a host is subscribed; otherwise it costs two loads. A record that is on its way
to the host is never overwritten: newer acquisitions are skipped until it has gone.

**Generator (patch 4).** Stock plays a table of 100 samples in SRAM through a circular DMA
into the DAC, and rewrites it at power-on and when the waveform, the duty or the amplitude is
changed, not when the frequency is. `WAVE` stores samples there. The host gives a position in
the table (0 to 99), never an address; both the position and the count are checked before
anything is stored, and each sample is scaled the way stock scales its own
(`sample * amplitude / 33 + 20`, with the amplitude set on the instrument, held at 3.0 V), so
the DAC stays inside the span stock itself drives it over. The waveform lasts until stock
rewrites the table; the instrument's screen keeps showing the waveform chosen in its menu.
(This is how an image without a "USB" menu entry behaves; with one, see the next paragraph.)

**Menu entries (`--menu-wave`, `--menu-usb-wave`, optional, patch 5).** Stock's list of
waveforms has thirteen entries and the code around it mostly does not care how many there
are. Five kinds of places do, and the builder changes each:

| Where | What changes | Bytes |
|---|---|---|
| `0x0800A9C2`, the "next waveform" key | the last index it steps from: 11 becomes 11 + N | 1 |
| `0x0800C2AA`, `0x0800D912`, `0x0800E5F8`, `0x0802E000` | the first row shown when the list opens: `min(index, 7)` becomes `min(index, 7 + N)` | 8 |
| `0x08029E14`, the table builder's first instruction | a `b.w` to a stub that fills the table for an added entry and otherwise replays the instruction and goes on in stock | 4 |
| six `ldr r0, [r0, #-0x34]` in the list (`0x080157E6` …) | a `bl` that returns the added entry's name for rows past the thirteenth and stock's pointer for the others | 24 |
| `0x080108EA`, `0x080127BC`, `0x0801F8EE` | a `bl` that returns, for an added entry, the index of the stock waveform whose picture it borrows (stock has thirteen pictures of each kind on its drive) | 12 |

An added entry's samples are constants in the cave, scaled like stock's own every time the
table is built, so amplitude and frequency work as for any other waveform and the entry
survives power-off. `--menu-usb-wave` adds a last entry that plays whatever `WAVE` received
last, whole and in order; that copy is in the link's SRAM and lasts until power-off, and until
one arrives the entry plays stock's sine. From patch 6, in an image with that entry, `WAVE`
writes the table that is playing only while the entry is selected: sent under any other
waveform, the samples are kept for the entry and the output does not change. The names are ASCII and the same in every language.

The instrument stores the selected index in its settings page as it always did. An image
without these entries does not know an index above 12: before flashing one, select a stock
waveform, or the generator stays silent until you do.

**Languages (`--languages`, optional).** Stock V1.2.0 still has its texts in eight languages
and its fonts still have every character they need, but Settings > Language lists two and
stores the row number. With this option the instruction that loads the row is replaced by a
call to nine instructions in the cave: the first row still gives Chinese, and OK on the
second row steps English, Russian, Japanese, Spanish, Portuguese, German, Korean and back
to English. The list itself still shows two rows, and neither is marked while a third
language is in use. `tools/language_tables.py` checks the tables and the fonts of your own
stock image.

**The list of languages (`--language-menu`, optional, instead of `--languages`).** Settings >
Language shows the eight languages as eight entries, four to a page: 中文, English, Русский,
にほんご, Español, Português, Deutsch, 한국어. The cursor moves through them with the keys that
move it through any other setting, the one in use carries stock's mark, and OK chooses the
one under the cursor. Two things are changed in stock for it. The byte that says where the
cursor stops in this setting goes from 1 to 7 (`0x0800B1D8`, in a table with one byte per
setting); stock's OK handler already stores "the row under the cursor plus one" and is left
alone. And the first instruction of the Language case of the routine that paints a
setting's options (`0x0801CF7E`) becomes a branch to ~470 bytes in the cave that paint four
rows the way stock paints the four of Auto Shutdown, with stock's own picture and text
routines, pictures, font and colours, and then leave through that routine's own exit. The
names are each language's own, except that stock's fonts have no glyph for 日, so Japanese is
written in hiragana; the builder checks every character of every name against the five
fonts of your image. While the cursor is still in the list of settings the panel shows the
page of the language in use; opening it puts the cursor on the first entry, as stock does.
The panel runs in stock's display task and takes 56 bytes more of its stack than stock's
own; `fnlink.py tasks` reads how much of each task's stack has ever been used.

**Texts (`--texts FILE`, optional).** The translations stock still carries were not all made
to fit. In Spanish the multimeter's title reads "Voltaje de Corriente" for both DC and AC
volts, because the text routine draws the characters that fit in the box and drops the
rest, and the word that differs comes last; the four current ranges fare the same. Stock
keeps its texts as pointers in tables with a row per language, so a text is replaced by
pointing its slot at a string of ours after the cave's code: data only, no code. A file of
`SLOT WAS FONT WIDTH TEXT` lines says which; `texts/es.txt` has the six of the Spanish
multimeter (Tensión continua, Tensión alterna, Corriente CC (mA), ...). The builder
refuses a slot that does not hold the pointer expected, a character the font has no glyph
for, and a text wider than its box, measured with the advances of stock's own font, which
is the same sum the routine makes: in emulation 215 px is drawn whole in the title's 216
and 232 is cut. The texts in such a file are ours; FNIRSI's are not copied into this
repository.

## Build

```bash
sudo apt install gcc-arm-none-eabi
python3 build_link_image.py --stock APP_2C53T_V1.2.0_251015.bin --out APP_2C53T_V1.2.0_link.bin
```

`--languages` adds the language call described above, `--language-menu` the list of
languages (one or the other), `--texts texts/es.txt` the Spanish texts, and the menu entries
are added like
this (each with the stock waveform, 0 to 12, whose picture it borrows after the `@`):

```bash
python3 build_link_image.py --stock APP_2C53T_V1.2.0_251015.bin --out APP_2C53T_V1.2.0_link.bin \
    --menu-wave "Triangle 25/75=triangle:25@2" --menu-wave "Gauss Pulse=gauss@12" \
    --menu-wave "Burst 10=burst@10" --menu-wave "AM 80%=am@11" --menu-usb-wave "USB Wave@0"
```

A shape is one of `fnlink.py wave`'s (`pulse:25` and `triangle:25` take a duty,
`harmonics:1:1,3:0.33` a list) or a text or CSV file with one period. Up to eight entries.
Without any of these options the image has the link and nothing else. `--without-scope-hook` leaves the `fpga` task untouched (the SysTick hook is then the only
change, the same site OpenScope's stock_caplog uses on hardware). That image still offers
the link, memory read and the meter stream, and is the one to flash first.

The builder refuses any image whose sha256 is not V1.2.0 (`a17c5c35…c760`), checks the
stock facts the cave relies on, and re-reads its own output. With Ubuntu 24.04's
`gcc-arm-none-eabi 15:13.2.rel1-2` the results are in `reference/`: patch 3, the one that
has been on hardware, `*_patch4*.json` for patch 4 with and without `--languages`, and
`*_patch5*.json` for patch 5 without options and with five menu entries (`AM 80%=am@10` there),
and `*_patch6*.json` for patch 6: without options, with the example above, with it and
`--languages`, with it and `--language-menu`, with those two and `--texts texts/es.txt`, and
with `--language-menu` alone.

No patched image is distributed: it contains FNIRSI's firmware.

## Test (no hardware)

```bash
python3 -m pip install unicorn
python3 test/test_link_emu.py --stock APP_2C53T_V1.2.0_251015.bin --image APP_2C53T_V1.2.0_link.bin
python3 test/test_decode.py
```

`test/test_decode.py` needs nothing but Python: it decodes the real captures in `test/data/`
(the eight functions of the multimeter's key cycle on known inputs, and the instrument's own
generator on the scope)
and compares them with what the instrument showed.

`test/usbemu.py` runs the image's real code in Unicorn against register-level models of the
USB device peripheral, USART2 and SPI3, with a minimal USB host:

- the unpatched image is enumerated first as a control and must come up as stock's
  `2e3c:5720` "AT32 Mass Storage";
- meter frames go through stock's real USART2 interrupt handler, byte by byte;
- acquisitions go through stock's real `fpga` task, and the patched task is compared with
  stock's — registers at every command boundary, SRAM, peripheral writes — for all nine
  commands and over 80 random scope states;
- every SRAM write made by cave code is audited.

What emulation cannot show: electrical behaviour, real host drivers, the effect of the
extra copy on acquisition timing, and whether a connected USB transceiver adds noise.

## Host tool

```bash
python -m pip install hidapi
python fnlink.py info
python fnlink.py dump-cal cal_0x08006000.bin     # 4 KB settings/calibration page, read twice
python fnlink.py cal-info before.bin after.bin   # where two such dumps differ (no device needed)
python fnlink.py snapshot "stopped, 10us" 0x200000F8:56 0x20000EA8:96 --out session.txt
                                                 # labelled RAM blocks added to a text file, one call per step
python fnlink.py watch 0x2000013D:1 --seconds 5  # one small block read over and over: every change, with its time
python fnlink.py verify APP_2C53T_V1.2.0_link.bin
python fnlink.py read 0x20001060 16
python fnlink.py tasks                           # stock's tasks: stack size, in use, deepest since power-on
python fnlink.py status                          # generator settings, scope measurements, meter MAX/MEAN/MIN
python fnlink.py status --events                 # the same from the status stream (patch 3)
python fnlink.py wave sinc                       # patch 4: one period of a built-in shape into the generator
python fnlink.py wave harmonics --harmonics 1:1,3:0.33,5:0.2
python fnlink.py wave --file one_period.csv      # any count of numbers: resampled to 100, stretched to full scale
python fnlink.py wave --show                     # the table being played, drawn; writes nothing (any patch)
python fnlink.py meter                           # live display digits, function, raw frame
python fnlink.py scope captures/                 # one file per record: 56 state bytes + 1024 samples
```

`meter` prints the reading with its unit (`9.827 kOhm`, `1.028 uF`), in AC V the frequency
stock shows (`2.349 V 49Hz`) and, while HOLD is on, what the instrument's frozen screen shows
next to the live reading (`0.0003 V HOLD:-0.0005V`). A `~` marks the frame or two around a
change of function or of range, when the display and stock's own unit do not agree yet. `scope` saves
each record raw and prints its settings and measurements
(`500mV/div X1  200us/div  Vpp 3.04V  1kHz`). The byte layouts behind both, and how each
field was established, are in [`docs/formats.md`](docs/formats.md).

`status` reads what stock keeps in RAM and the stream does not carry: the signal generator's
settings, the oscilloscope's own measurements with its RUN/STOP state, and the multimeter's
MAX / MEAN / MIN boxes. It needs no other firmware than the one with the link: it is four
`READ` requests. `status --watch 0.25` keeps reading and prints again when something changes.
From patch 3 the link also sends these blocks by itself to a host that subscribes to them
(`status --events`); `fnlink.py info` says whether the link on the unit does.

`wave` writes one period into the generator's table and reads it back. The shapes are `sine`,
`triangle`, `ramp`, `ramp-down`, `pulse`, `sinc`, `gauss`, `exp-decay`, `exp-rise`,
`half-sine`, `abs-sine`, `stairs`, `burst`, `am` and `harmonics`; `--duty` sets the high part
of `pulse` and the rising part of `triangle`, `--invert` turns any of them upside down. The
frequency and the amplitude stay the ones set on the instrument: set the amplitude first,
because the samples are scaled when they are stored. Changing the frequency afterwards keeps
the waveform; changing the waveform, the duty or the amplitude brings stock's own back.

## Protocol

Version 1. Patch 3 adds to it without changing anything a patch 2 host relies on: one more
subscription bit, one more event type, and four more bytes at the end of INFO. Patch 4 adds
one request and one feature bit, patch 5 one feature bit and four more bytes of INFO.

One 64-byte report each way, no report id.

| | byte 0 | 1 | 2 | 3 | 4–7 | 8… |
|---|---|---|---|---|---|---|
| request | cmd | seq | len / mask | 0 | address (LE) | — |
| response | cmd \| 0x80 | seq | status | n | address (LE) | payload |
| event | type | seq | kind / channel | n | tick or offset (LE) | payload |

Requests and responses pair up strictly; events may arrive ahead of a response.

- `0x01` INFO: `"2C53LINK"`, protocol, patch, system mode, stream mask, tick count, link
  count, request count, meter frames seen, scope records captured, scope records skipped.
  From patch 3, a feature byte follows (payload byte 36): bit 0 = the link has the status
  stream, bit 1 (patch 4) = it has `WAVE`, bit 2 (patch 5) = the image added entries to the
  generator's menu. A shorter INFO means no features. From patch 5, three more fields follow:
  the number of added entries (payload byte 37), the index of the one that plays what `WAVE`
  sent, or 0 (byte 38), and the address of an array of pointers to their names (bytes 40–43),
  which `READ` can follow.
- `0x02` READ: up to 56 bytes from `0x08000000`–`0x080FFFFF` or `0x20000000`–`0x20037FFF`.
- `0x03` STREAM: byte 2 is the subscription mask (1 meter, 2 scope, 4 status from patch 3;
  a link that does not know a bit answers status 3). Every session starts unsubscribed.
- `0x04` WAVE (patch 4): byte 2 is the number of samples (1 to 28), bytes 4–7 the position of
  the first one in the generator's table (0 to 99), and the samples follow from byte 8 as
  16-bit little-endian values from 0 to 4094 (anything above counts as 4094). The response
  carries two bytes: the amplitude the samples were scaled by, in tenths of a volt, and the
  index of the waveform stock will go back to. A whole table is four requests. Status 2 if
  position + count goes past 100, status 3 for a count of 0 or above 28.
- Status: 0 ok, 1 unknown command, 2 range refused, 3 bad length.
- `0x90` meter event: kind 1 (12-byte `5A A5` frame) or 2 (10-byte `AA 55` echo); payload =
  frame (12), last function word sent to the meter chip, system mode, 2 spare, 40 bytes of
  stock's meter state from `0x20001020`.
- `0xA0` scope record header: channel, tick, 56 bytes of scope state (`state[0x00..0x37]`).
- `0xA1` scope record data: channel, offset, up to 56 samples. 20 reports per record.
- `0xB0` status event (patch 3): one block of stock's SRAM every 100 ms, the ones that apply
  to the current mode in turn. Byte 2 says which: 1 the generator's 10 settings bytes (scope
  and generator modes); 2 and 3 the scope's own measurements of CH1 and CH2, 48 bytes, with
  the run/stop byte after them (scope mode); 4 meter state bytes 40 to 55, the ones a meter
  event has no room for (meter mode). Bytes 4–7 are the tick.

What is inside a meter frame and a scope record: [`docs/formats.md`](docs/formats.md).

USB ids default to `1209:0001`, the pid.codes test PID, which is for private testing only
(`--vid/--pid` change them).

## Known limits

- V1.2.0 only.
- The link is connected in every measurement mode, cable or not.
- The link leaves the bus at every mode change and is back one to two seconds later: stock
  disconnects USB whenever it enters a mode. A host has to reopen the device.
- A scope record is 20 reports. Measured: about 33 records per second with one channel on,
  10 pairs per second with both at 2 ms per division; acquisitions that do not fit are
  skipped whole.
- `SET_REPORT` over the control endpoint is not implemented; hosts must use the interrupt
  OUT endpoint.
- The meter's MIN box, the generator's settings and the scope's own measurements are not in
  the meter and scope streams. A host reads them with `READ` (`fnlink.py status`) or, from
  patch 3, subscribes to the status stream. Part of their layout is still assumed, see
  `docs/stock_v120_notes.md`.
- `snapshot` and `watch` exist to look at stock while it runs without a debug probe: a set of
  RAM blocks at each step of a procedure, or one counter followed in time (a `READ` takes about
  2 ms, which is the resolution). What they save is meant to be passed on, so both refuse the
  settings/calibration page and the RAM tables stock loads the calibration into
  (`--allow-calibration` overrides). The link still only reads, and only flash and RAM: peripheral
  registers are refused by the patch itself.
- [`sessions/`](sessions/2026-11-25_openscope_parity/README.md) holds what was saved that way
  for OpenScope-2C53T's open questions about stock, and `tools/paused_view_check.py` compares
  what stock draws while stopped with the record it captured.
  [`sessions/2026-11-25_meter_functions/`](sessions/2026-11-25_meter_functions/README.md) has
  the meter's state in each of its twelve functions and the buzzer's counter during a
  continuity test; `tools/exti3_gate.py` and `tools/buzzer_tail_check.py` go with it.
  [`sessions/2026-11-25_generator_ch2_boxes/`](sessions/2026-11-25_generator_ch2_boxes/README.md)
  has the generator's menu entry by entry, CH2 with a signal and the units of the meter's boxes.
  [`sessions/2026-11-25_coupling_probe_time_bases/`](sessions/2026-11-25_coupling_probe_time_bases/README.md)
  has CH1's coupling, the X10 probe setting of both channels and 2 us per division.
- Going back to stock: flash `APP_2C53T_V1.2.0_251015.bin` through the factory IAP drive.

## Safety

- **Flash only through the factory IAP drive** (hold MENU, press and hold Power until
  `Firmware upgrade` is on the screen, release Power, then connect USB). The factory
  bootloader and the calibration page are outside what that path writes, so the official
  image can always be put back the same way.
- **On macOS do not copy with Finder.** The bootloader programs whatever reaches the data
  area in arrival order. Use `scripts/iap_flash.py` from OpenScope-2C53T.
- **Never remove the MCU's flash access protection** (ROM DFU `unprotect`, an SWD
  `unlock`, Artery ISP, answering yes to a debugger's "unsecure?" prompt). Doing so
  mass-erases the flash, including the factory calibration, which cannot be recreated.
- **V1.2.0 on a 2C53T only.** The builder refuses any other image. Do not flash the result
  on a 2C23T or a 2C53P.
- **Back up the settings/calibration page before changing any setting** after the first
  flash: `python fnlink.py dump-cal cal_0x08006000.bin`. Flash the `--without-scope-hook`
  image first for this.

There is no warranty of any kind. You are modifying the firmware of a measuring instrument
at your own risk.

## More

- [`docs/stock_v120_notes.md`](docs/stock_v120_notes.md): the facts about stock V1.2.0 and
  the factory bootloader that this patch relies on, with how each one was established.
- [OpenScope-2C53T](https://github.com/DavidClawson/OpenScope-2C53T): the open firmware
  project whose reverse-engineering notes made this possible.

## Licence

Copyright (C) 2026 Gonzalo Mejías. GPL-3.0-or-later, see [`LICENSE`](LICENSE).

This project is not affiliated with FNIRSI. The repository contains no FNIRSI firmware:
the builder needs your own copy of the official image, and patched images must not be
redistributed.
