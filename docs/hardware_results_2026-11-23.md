# First run on hardware, 2026-11-23

stock-link v1 on one retail FNIRSI 2C53T. This is the record of what was done and seen; it
replaces "verified in emulation only".

## Setup

| | |
|---|---|
| Instrument | retail 2C53T, stock V1.2.0, case never opened (board revision unknown) |
| Flashing host | MacBookPro14,1, macOS 13.7.8 |
| Flashing tool | `scripts/iap_flash.py` of OpenScope-2C53T at commit `7480998`, through the factory IAP drive |
| Host tool | `fnlink.py` with Python 3.9 and hidapi 0.15.0 |
| Images | built on Ubuntu 24.04 with `gcc-arm-none-eabi 15:13.2.rel1-2`; hashes as in `reference/` |

## What was done, in order

1. **The unmodified stock image, as a rehearsal of the flashing path.** Flashed and booted
   normally.
2. **The image without the scope hook.** Booted and behaved as stock. A few seconds later
   the host saw a HID device `1209:0001`, manufacturer `FNIRSI`, product `2C53T stock-link`,
   with stock's own serial string.
   - `fnlink.py info`: protocol 1, patch 2, system mode 1.
   - `fnlink.py dump-cal`: 4096 bytes from `0x08006000`, read twice, identical. 345 bytes
     are not `0xFF`; the flag page at `0x08006800` starts `38 4B 54 41`.
   - `fnlink.py verify`: 753,776 bytes read back in 27 s, identical to the image.
   - `fnlink.py meter`: frames every ~165 ms.
3. **The full image.** `verify` identical again (28 s). `fnlink.py scope` delivered records.
4. **Captures for decoding** (now in `test/data/`): every multimeter function of the key
   cycle on a 10 kΩ resistor and a 9 V battery, and the instrument's own generator on the
   scope with a photograph of the screen.
5. **A second host program**, the companion web app this link was written for, read the
   stream live.

## Results

| | |
|---|---|
| Flashing through the factory IAP drive from macOS | 3 of 3 |
| Flash content after an update | identical to the image file, both images |
| Multimeter stream | ~6 frames per second; 600 consecutive frames across every function change, link never dropped |
| Scope stream, one channel | ~33 records per second at 50 µs and 200 µs per division; about one acquisition in two is skipped, by design |
| Scope stream, both channels | 10 pairs per second at 2 ms per division |
| USB Sharing | still works: the drive mounts as before |
| Settings | still persist across a power cycle |
| Power on without a cable | normal |
| Readings | 10 kΩ resistor 9.83 kΩ, 9 V battery 9.20 V, generator 3.01 V at 1.00 kHz on the instrument and 3.0 V at 1.00 kHz from the records |

## Observed behaviour worth knowing

- **The link leaves the bus at every mode change** (multimeter, scope, generator) and is
  back one to two seconds later. That is stock disconnecting USB when it enters a mode; the
  patch only reconnects. A host has to reopen the device.
- **macOS (hidapi) sends output reports through the interrupt OUT endpoint**, which is the
  only path the link implements. `SET_REPORT` over the control endpoint is still not
  implemented and was not needed.
- **In Auto the function word is 0** until a key selects a function: stock has not sent the
  meter chip any order yet.
- On leaving USB Sharing, macOS reports the drive as not ejected properly, as it would with
  stock: the instrument removes the drive without warning.

## Not assessed

- Whether the scope's display is any less fluid while records are being copied and sent.
- Whether a connected, active USB link adds noise to measurements.
- Accuracy before and after: there is no copy of the settings/calibration page from before
  the first update, because stock offers no way to read it. The readings above are plausible
  and the page is intact and non-empty, but that is not a before/after comparison.
- Windows with hidapi, and Android.
- Board revisions other than this unit's, whatever it is.
