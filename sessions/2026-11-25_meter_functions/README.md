# The meter's functions and the continuity beep on stock V1.2.0, 2026-11-25

Two captures made for OpenScope-2C53T's pull request 102, which moves its continuity beep to
the meter chip's own buzzer tone on PC3, as stock does. Same unit and method as
[`../2026-11-25_openscope_parity/`](../2026-11-25_openscope_parity/README.md): stock V1.2.0
with patch 3, case closed, RAM read over USB, nothing written to the instrument. What they
show is in [`docs/hardware_results_2026-11-25.md`](../../docs/hardware_results_2026-11-25.md),
section "The meter's functions and the buzzer".

| File | What was done |
|---|---|
| `meter_functions.txt` | `fnlink.py status` (its meter lines, as they were printed that day) in each function of stock's meter menu, under the name the screen showed. The first two entries are a false start: the second of them is labelled "DC Voltage" and holds the bytes of AC Voltage. The twelve that follow are the run that counts |
| `continuity_counter.txt` | `fnlink.py watch 0x20002D67:1` for 25 s in Continuity. Leads open for 4.5 s, then shorted three times; the third time they were still shorted when the capture ended |

In `meter_functions.txt` the `state` line is 56 bytes from `0x20001020`. Byte 5 is the entry
of the function menu (`ms[0xF2D]` in OpenScope's notation), byte 6 what is being measured
(`ms[0xF2E]`) and byte 14 the variant inside the entry (`ms[0xF36]`).

To redo the reading of the counter, and of stock's handler (this one needs your own copy of
the stock image and the unicorn package):

```bash
python tools/buzzer_tail_check.py sessions/2026-11-25_meter_functions/continuity_counter.txt
python tools/exti3_gate.py --stock APP_2C53T_V1.2.0_251015.bin
```

No calibration is in these files.
