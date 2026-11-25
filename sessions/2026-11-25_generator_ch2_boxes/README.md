# Generator menu, CH2 with a signal and the units of the meter's boxes, 2026-11-25

Three loose ends closed on the same unit (stock V1.2.0 with patch 3, read over USB, nothing
written to the instrument). What they show is in
[`docs/hardware_results_2026-11-25.md`](../../docs/hardware_results_2026-11-25.md), section
"Generator menu, CH2 and the boxes".

| File | What was done |
|---|---|
| `generator_waveforms.txt` | Each entry of the generator's waveform menu selected in turn, from "Square Wave" on; under the label typed from the screen (the first has a typing slip, "Wavw"), the `generator` line of `fnlink.py status` as it was printed that day. The entry before "Square Wave", index 0, was not captured |
| `status_ch2_and_boxes.txt` | Lines of `fnlink.py status` copied from the terminal, each group under a `==` line that says what the instrument was doing. The generator's 50 kHz square wave on CH2 with CH1 off, at two settings of CH2's volts per division; then the multimeter in Small DC current |

Two things were seen and not saved as files. With CH2 at 100 mV per division the application
that reads this link drew the record and measured 344 mV peak to peak and 50.0 kHz on it,
against the 345 mV and 50 kHz of stock's own measurement above. And a photograph of the
instrument's screen in Small DC current shows the three boxes in "uA", with the values the
last group prints.

The `boxes` line of the last group says "unit code 0: mV?" because the tool then took the
codes from the wrong bytes of the state; see the write-up. No calibration is in these files.
