# Stock V1.2.0 notes

The facts about FNIRSI's stock firmware that `link.c` and the builder rely on, and how
each one was established. Addresses are absolute (the application is linked at
`0x08007000`).

Evidence labels:

- **code**: read from the disassembly of the image.
- **emu**: exercised by running the image's own code in the Unicorn harness (`test/`).
- **unit**: seen on one retail unit, from the host side, without writing to it.

Since 2026-11-23 the patch has run on one unit; what that showed is in
[`hardware_results_2026-11-23.md`](hardware_results_2026-11-23.md), and the decoded meter and
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

The patch never writes flash. `fnlink.py dump-cal` reads this page out over the link.

## Multimeter

Meter chip on USART2, 9600 8N1. (code, emu)

- Interrupt handler `0x0802E7B4`, receive index `0x20004E10`, receive buffer `0x20004E11`.
- Data frame: 12 bytes starting `5A A5`. Echo of a command: 10 bytes starting `AA 55`.
- Transmit buffer `0x20000005` (function word at `0x20000008`), meter mode `0x20001025`,
  value `0x20001028`, semaphore `0x20002D7C`, task entry `0x0803DAC0`.

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

## FreeRTOS

SysTick handler `0x0802A994`, `xTaskIncrementTick` `0x0804278C`, `xTickCount`
`0x200062D4`, `uxSchedulerSuspended` `0x200061E4`, `xQueueReceive` `0x080421D8`,
`xQueueGiveFromISR` `0x0804209C`, `vTaskDelay` `0x08041390`. (code)

## Memory used by the patch

| | |
|---|---|
| Code cave | from `0x080BE700`, first 0x100-aligned address after the image |
| RAM | `0x20037000`-`0x20037983`, above stock's stack top (`0x20036F90`) and below the factory bootloader's RAM magic at `0x20037FE0` |

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
