# Stock's measurements against the screen, and the block the FFT pointer points at, 2026-11-26

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
| `status_stopped.txt` | Later, with the image built with `texts/es-english-rows.txt` on the unit again: the oscilloscope's lines of `fnlink.py status`, stopped, CH1 alone |
| `stopped_snapshots_fft_off.txt` | Three snapshots of `ms[0x00..0x37]` meant for the FFT block at three time bases, stopped. **Their labels do not describe them**: in all three the FFT flag `ms[0x0A]` is 0, the pointer is 0 (so nothing was followed) and the time base `ms[0x2D]` is 13, 200 µs per division. What does differ is `ms[0x1A..0x1B]` (0, -63, -13 as a signed 16-bit number) and `ms[0x20..0x27]` (0, -1260000, -260000 as a signed 64-bit number); what was done on the instrument between them was not noted |
| `fft_block_snapshots.txt` | Three snapshots, running, CH1 alone, of `ms[0x00..0x37]` and 448 bytes at `0x20033940`: CH1's FFT on with the 1 kHz square, on with the probe taken off the generator, and off. Each block of 448 bytes is eight reads of 56 a few milliseconds apart while the instrument keeps acquiring, so it is not one instant |

The twelve words of the two snapshots, as signed 32-bit numbers:

| | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| stopped | 1000 | 1000000 | 5040 | 4960 | 504000 | 496000 | 308000 | -4000 | 312000 | 304000 | 208000 | 148000 |
| running | 1000 | 1000000 | 4993 | 5006 | 499333 | 500666 | 304333 | -2333 | 306666 | 304000 | 209166 | 147833 |

A photograph of the screen, stopped, taken a minute before the first snapshot, shows three
measurements for CH1: "Freq:1.00KHz", "Duty+:50.40%" and "Vp-p:312.0mV".

What three later photographs show for CH1, with eight measurements chosen in the
oscilloscope's "Parameter Measurement" menu (the labels are those of
`texts/es-english-rows.txt`; in stock's words: Freq, Duty+, Wid+, Wid-, Amp, Vrms, Vp-p, Avg):

| Photograph | Frec | Ciclo+ | Anc+ | Anc- | Amp | Vef | Vp-p | Med |
|---|---|---|---|---|---|---|---|---|
| 13:15:05, stopped, CH1 and CH2 | 1.00KHz | 50.40% | 504.0uS | 496.0uS | 304.0mV | 212.0mV | 308.0mV | 148.0mV |
| 13:15:22, stopped, CH1 alone | 1.00KHz | 49.60% | 496.0uS | 504.0uS | 316.0mV | 208.0mV | 316.0mV | 144.0mV |
| 13:16:37, running, CH1's FFT on | 1.00KHz | 49.97% | 499.8uS | 500.2uS | 316.0mV | 209.2mV | 316.5mV | 146.4mV |
| 13:59:37, stopped, CH1 alone | 1.00KHz | 50.40% | 504.0uS | 496.0uS | 316.0mV | 212.0mV | 320.0mV | 148.0mV |
| 14:01:01, running, CH1 alone | 1.00KHz | 50.03% | 500.3uS | 499.7uS | 315.4mV | 208.3mV | 317.2mV | 146.3mV |
| 14:01:46, stopped, CH1 alone | 1.00KHz | 50.40% | 504.0uS | 496.0uS | 316.0mV | 208.0mV | 316.0mV | 148.0mV |

`status_stopped.txt` was taken between the verification of the flash and 13:16:26. Its CH1
words are those of neither stopped photograph (against the first, Vp-p, Amp and Vef differ;
against the second, the duty, the widths and Med), so for CH1 no photograph and reading
here are of the same acquisition. For CH2 the first photograph shows "Frec:923.3KHz",
"Ciclo+:56.47%", "Anc-:472.0nS" and "Vp-p:6.00mV", and the reading, taken with CH2 off,
has 5647, 472 and 6000 in CH2's words 2, 5 and 8, and 846076 and 1181 in words 0 and 1.
The photograph of 14:01:46 shows exactly the eight values of `status_stopped.txt`, taken
some three quarters of an hour earlier: another acquisition with the same result.
The photographs are not kept here.

No calibration is in these files.
