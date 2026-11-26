# The trigger edge and two slow time bases, 2026-11-26

Two things the session of 2026-11-25 left open, on the same unit (stock V1.2.0 with patch 6,
read over USB, nothing written to the instrument). Written up in
[`docs/hardware_results_2026-11-25.md`](../../docs/hardware_results_2026-11-25.md), section
"The trigger edge and the slow time bases".

| File | What was done |
|---|---|
| `trigger_edge_snapshots.txt` | Three `fnlink.py snapshot`s with the generator's 100 Hz square wave on CH1: trigger on the rising edge, on the falling edge, on the rising edge again. Each holds `ms[0x38..0x25F]` (`20000130`, 552 bytes) and `ms[0x354..0xFFF]` (`2000044C`, 3244 bytes), `ms` being stock's state structure at `0x200000F8`. The gap between the two blocks is where stock keeps the calibration it loaded, which is not read; `ms[0x00..0x37]` was left out by mistake |
| `slow_time_bases.txt` | At 5 and at 20 ms per division: the two lines `fnlink.py scope` printed for the two records it saved (what the record itself measures, with 25 samples to a division), then the `generator`, `scope` and `CH1 shown` lines of `fnlink.py status` (stock's own measurements), copied from the terminal. The trigger was on the rising edge |

No calibration is in these files.
