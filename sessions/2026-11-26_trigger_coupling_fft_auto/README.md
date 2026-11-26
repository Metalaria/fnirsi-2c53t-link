# Trigger mode and channel, CH2's coupling, the FFT rows and the AUTO key, 2026-11-26

The first 56 bytes of stock's state structure (`ms`, `0x200000F8`: what the link sends as the
header of an oscilloscope record) read on the unit while one setting at a time was changed
on the instrument and put back. Stock V1.2.0 with patch 6, read over USB, nothing written to
the instrument. Both channels were on. Written up in
[`docs/hardware_results_2026-11-25.md`](../../docs/hardware_results_2026-11-25.md), section
"Trigger mode and channel, CH2's coupling, the FFT and the AUTO key"; the bytes are in
[`docs/formats.md`](../../docs/formats.md).

| File | What was done |
|---|---|
| `settings_snapshots.txt` | Eleven `fnlink.py snapshot`s of `0x200000F8`, 56 bytes, each after the step its label says: the starting state (trigger Auto, rising edge, CH1; both couplings DC; FFT off), trigger mode Normal, Single, Auto again, trigger channel CH2, CH1 again, CH2's coupling AC, DC again, CH1's FFT on, CH1's off and CH2's on, both off. **The sixth snapshot carries the wrong label**: it was taken after putting the trigger's channel back to CH1 and says "7 CH2 coupling AC", as the next one does; its bytes are those of the starting state |
| `auto_key_watch.txt` | `fnlink.py watch` of the same 56 bytes while the AUTO key was pressed once: a line for every reading that differs from the one before, with the time since the watch began. The key is the line at 5903 ms; the last line is 8.3 s later |

What was connected to the inputs was not written down. No calibration is in these files:
the tables stock loads it into start at `0x20000358`.
