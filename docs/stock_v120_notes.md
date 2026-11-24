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
| `0x000`-`0x037` | signature and user settings | changed within `0x003`-`0x016` and `0x025`-`0x02E` (scope and generator settings had been changed) |
| `0x038`-`0x12F` | calibration table | identical |
| `0x130`-`0x1FF` | not part of the structure | `0x140`-`0x1E7` (168 bytes) went from `0xFF` to zeros and a few RGB565 pixel values |
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

Meter state, from `0x20001020` (unit; details in `formats.md`): screen status `+13`, unit code
`+16`, AC frequency `+18`, HOLD flag `+20` (`0x20001034`), MAX / MEAN / MIN floats `+32`, `+36`,
`+40`, their decimals `+44..46` and unit codes `+47..49`.

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
| 9 | a second amplitude, a few mV under Vpp (top to base?) | µV |
| 10, 11 | not identified | µV |

Compared with photographs of two stopped screens: 40232 with "40.23KHz", 6451 with "64.51%",
114000 with "114.0mV"; and, on another day's wave, 50176 with "50.18KHz", 5050 with
"50.50%", 220000 with "220.0mV". While stopped the shown block does not change. The running
flag is scope state `+46` (`0x20000126`). CH2's twelve words were only seen with the channel
off: a frequency (tens of Hz, or about 1.7 MHz) and exactly its period, the rest zero. That
they are CH2's is assumed from the layout. Outside the oscilloscope mode the shown block is
all zeros.

## Signal generator

Settings from `0x20000F50` (ram: five dumps, one setting changed between each, compared with
photographs of the generator screen):

| Offset | Content |
|---|---|
| 0 | 0 or 1; it changes while the generator screen is operated. Not established |
| 1 | waveform as its position in the menu, 0 to 12: 1 Square Wave, 2 Sawtooth wave (a triangle at 50 % duty), 3 Half-wave, 4 Full Wave, 5 Step Wave, 6 Reverse Step Wave. 1 and 4 were checked on their own and 2 by its shape on the scope; 3, 5 and 6 follow from the menu's order. The first entry and the last six were not photographed |
| 4–7 | frequency in Hz, 32 bits (1000, 15000, 35000, 50000 seen) |
| 8 | duty cycle in % (50 seen) |
| 9 | amplitude in tenths of a volt (30 and 20 seen for 3.0 V and 2.0 V) |
| 10–209 | one period of the wave as 100 16-bit DAC values |

The sample table follows the waveform and the amplitude: 20 to 3742 for a 3.0 V square,
1880 to 3742 for a 3.0 V full wave, 1260 to 2501 at 2.0 V.

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
