# Coupling, the X10 probe setting and faster time bases, 2026-11-25

More of what the notes still carried as assumed, on the same unit (stock V1.2.0 with patch 3,
read over USB, nothing written to the instrument). Written up in
[`docs/hardware_results_2026-11-25.md`](../../docs/hardware_results_2026-11-25.md), section
"Coupling, the X10 setting, 2 us and the first waveform".

| File | What was done |
|---|---|
| `steps.txt` | Fourteen numbered steps. For each: two records from `fnlink.py scope` (the line gives what the record itself measures, with 25 samples to a division), then the `generator`, `scope` and `CH shown` lines of `fnlink.py status` (stock's own measurements) |
| `generator_first_entry.txt` | The `generator` line of `status` at the two ends of the waveform menu, copied from the terminal |

The generator's Square Wave at 50 kHz goes into CH1 for steps 1 to 12 and into CH2 for 13 and 14.

| Step | The instrument |
|---|---|
| 1 | as set up: CH1 on, CH2 off, after AUTO |
| 2 | CH1 coupling to AC |
| 3 | CH1 coupling back to DC (the first try found no device; the second is the capture) |
| 4 | meant to be the trigger on the falling edge. What changed in the state is CH1's range (to 20 mV) and the trigger level (to 100): the edge was not found there, and this step settles nothing |
| 5 | meant to be the rising edge again: range and level back |
| 6 | CH1 probe setting at X10, the cable untouched |
| 7 | CH1 probe setting back at X1 |
| 8 | 2 us per division |
| 9 | 1 us per division: two periods in the record, too few for `scope` to give a frequency |
| 10, 11, 12 | generator at 100 Hz. Meant for 5, 10 and 20 ms per division, taken at 5, 10 and 20 us by mistake: the record holds a flat piece of the wave, and these three settle nothing |
| 13 | generator back at 50 kHz, cable to CH2, CH2 on and CH1 off, after AUTO |
| 14 | CH2 probe setting at X10 |

No calibration is in these files.
