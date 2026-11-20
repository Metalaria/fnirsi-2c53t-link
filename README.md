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

**Status: verified in emulation only. It has not run on hardware yet.**

> **Do not flash this expecting it to work.** Every claim below comes from static analysis
> and from running the image's code in an emulator. Until this note is removed, treat the
> repository as a record of work in progress. If you flash it anyway, read
> [Safety](#safety) first and keep the official `APP_2C53T_V1.2.0_251015.bin` at hand.

## What the patch changes

| | |
|---|---|
| Stock bytes changed | 10: 6 at `0x0802A994` (SysTick handler) and 4 at `0x0803E49A` (tail of the `fpga` task loop), each a `b.w` into the cave |
| Code added | ~2.4 KB at `0x080BE700`, after the stock image |
| SRAM used | `0x20037000`–`0x20037983`, above stock's stack top and below the factory IAP's RAM magic |
| Stock SRAM written | the USB core's class and descriptor pointers (`0x20002B28`, `0x20002B2C`) and the endpoint bookkeeping the stock class itself writes |
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

## Build

```bash
sudo apt install gcc-arm-none-eabi
python3 build_link_image.py --stock APP_2C53T_V1.2.0_251015.bin --out APP_2C53T_V1.2.0_link.bin
```

`--without-scope-hook` leaves the `fpga` task untouched (the SysTick hook is then the only
change, the same site OpenScope's stock_caplog uses on hardware). That image still offers
the link, memory read and the meter stream, and is the one to flash first.

The builder refuses any image whose sha256 is not V1.2.0 (`a17c5c35…c760`), checks the
stock facts the cave relies on, and re-reads its own output. With Ubuntu 24.04's
`gcc-arm-none-eabi 15:13.2.rel1-2` the results are in `reference/`.

No patched image is distributed: it contains FNIRSI's firmware.

## Test (no hardware)

```bash
python3 -m pip install unicorn
python3 test/test_link_emu.py --stock APP_2C53T_V1.2.0_251015.bin --image APP_2C53T_V1.2.0_link.bin
```

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
python fnlink.py verify APP_2C53T_V1.2.0_link.bin
python fnlink.py read 0x20001060 16
python fnlink.py meter                           # live display digits, function, raw frame
python fnlink.py scope captures/                 # one file per record: 56 state bytes + 1024 samples
```

`meter` decodes the display digits, sign and decimal point (checked against frames with
known readings from OpenScope's tests). The unit prefix is not decoded yet. Scope records
are saved raw; turning them into volts and seconds needs captures from a real unit.

## Protocol v1

One 64-byte report each way, no report id.

| | byte 0 | 1 | 2 | 3 | 4–7 | 8… |
|---|---|---|---|---|---|---|
| request | cmd | seq | len / mask | 0 | address (LE) | — |
| response | cmd \| 0x80 | seq | status | n | address (LE) | payload |
| event | type | seq | kind / channel | n | tick or offset (LE) | payload |

Requests and responses pair up strictly; events may arrive ahead of a response.

- `0x01` INFO: `"2C53LINK"`, protocol, patch, system mode, stream mask, tick count, link
  count, request count, meter frames seen, scope records captured, scope records skipped.
- `0x02` READ: up to 56 bytes from `0x08000000`–`0x080FFFFF` or `0x20000000`–`0x20037FFF`.
- `0x03` STREAM: byte 2 is the subscription mask (1 meter, 2 scope). Every session starts
  unsubscribed.
- Status: 0 ok, 1 unknown command, 2 range refused, 3 bad length.
- `0x90` meter event: kind 1 (12-byte `5A A5` frame) or 2 (10-byte `AA 55` echo); payload =
  frame (12), last function word sent to the meter chip, system mode, 2 spare, 40 bytes of
  stock's meter state from `0x20001020`.
- `0xA0` scope record header: channel, tick, 56 bytes of scope state (`state[0x00..0x37]`).
- `0xA1` scope record data: channel, offset, up to 56 samples. 20 reports per record.

USB ids default to `1209:0001`, the pid.codes test PID, which is for private testing only
(`--vid/--pid` change them).

## Known limits

- V1.2.0 only.
- The link is connected in every measurement mode, cable or not.
- A scope record takes 20 ms on the wire, so at most ~25 records per second in total.
- `SET_REPORT` over the control endpoint is not implemented; hosts must use the interrupt
  OUT endpoint.
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
