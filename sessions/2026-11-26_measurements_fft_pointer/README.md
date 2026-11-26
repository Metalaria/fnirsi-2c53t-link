# Stock's measurements against a stopped screen, and what the FFT pointer points at, 2026-11-26

Read over USB from the unit (stock V1.2.0 with patch 6), nothing written to the instrument.
The generator's 1 kHz square wave was on CH1 at 100 mV per division, CH2 on at 10 mV per
division. Written up in
[`docs/hardware_results_2026-11-25.md`](../../docs/hardware_results_2026-11-25.md), section
"The measurements against a stopped screen, and the FFT pointer".

| File | What was done |
|---|---|
| `status_english_rows_image.txt` | The oscilloscope's lines of `fnlink.py status`, copied from the terminal, taken while the image built with `texts/es-english-rows.txt` was on the unit |
| `measurement_snapshots.txt` | Two `fnlink.py snapshot`s of CH1's twelve words as shown (`0x20000208`, 48 bytes) and of `ms[0x00..0x37]`: the first with the oscilloscope stopped, the second running again. Both carry the same label |
| `fft_pointer_snapshot.txt` | One snapshot with CH1's "FFT:" row on: `ms[0x00..0x37]`, and 56 bytes from the address held in `ms[0x0C..0x0F]` |

The twelve words of the two snapshots, as signed 32-bit numbers:

| | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| stopped | 1000 | 1000000 | 5040 | 4960 | 504000 | 496000 | 308000 | -4000 | 312000 | 304000 | 208000 | 148000 |
| running | 1000 | 1000000 | 4993 | 5006 | 499333 | 500666 | 304333 | -2333 | 306666 | 304000 | 209166 | 147833 |

A photograph of the screen, stopped, taken a minute before the first snapshot, shows three
measurements for CH1: "Freq:1.00KHz", "Duty+:50.40%" and "Vp-p:312.0mV". The photograph is
not kept here.

No calibration is in these files.
