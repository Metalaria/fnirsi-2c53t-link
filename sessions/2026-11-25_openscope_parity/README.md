# Stock V1.2.0 watched while it runs, 2026-11-25

Captures taken to answer the questions that OpenScope-2C53T's reverse engineering lists as
needing live stock (`docs/re/parity2_static.md`, section 5, at their commit `7480998`). One
FNIRSI 2C53T, case closed, stock V1.2.0 with patch 3 of this repository, read over USB with
`fnlink.py snapshot` and `fnlink.py watch`. Nothing is written to the instrument. What they
show is written up in [`docs/hardware_results_2026-11-25.md`](../../docs/hardware_results_2026-11-25.md).

Signal: the instrument's own generator ("Sawtooth wave", 2 kHz, 3.0 V) into CH1, CH2 off,
trigger AUTO, 200 us per division after AUTO, 50 mV per division, probe setting X1.

| File | What was done |
|---|---|
| `session1_pan_and_timers.txt` | RUN, STOP, then the arrow keys in their "move" mode: right x4, back, left x4, back, up, down x2, up, RUN. After it the timers: the key-scan counter for 5 s, a block of settings, the click counter over three key presses, the idle counter for 30 s and across one key press, the acquisition counters for 10 s. |
| `session2_pan.txt`, `session3_pan.txt` | The same key sequence again, on two other records. |
| `session4_zoom.txt` | The same sequence with the arrow keys in their "scale" mode: time base 200 us -> 100, 50, 20, 10 us, back, -> 500 us, 1, 2, 5 ms, back, then 100 mV and 20 mV per division, back, RUN. |

Each snapshot holds, as `address length hex`:

| Address | Length | In OpenScope's notation (`ms` = `0x200000F8`) |
|---|---|---|
| `200000F8` | 56 | `ms[0x00..0x37]`: ranges, positions, channel mask, trigger, time base `ms[0x2D]`, RUN flag `ms[0x2E]` |
| `20000EA8` | 96 | `ms[0xDB0..0xE0F]`: acquisition counters, drawn column and width, trigger anchor, the tuple frozen at capture, screen edges, pan, pointers to the interpolated copies, `ms[0xE04]`, `ms[0xE08]` |
| `2000044E` | 602 | `ms+0x356`: the two 301-byte display arrays (CH1, CH2) |
| `200006A8` | 2048 | `ms+0x5B0`: the raw records (CH1, CH2), 1024 bytes each |
| `follow` | | `ms[0xDFC]` and `ms[0xE00]` read as pointers: null in every snapshot |

The `watch` sections list every change of one block with the host's clock; one read takes 2 ms.

To check the display arrays against the records (the zoom-in check needs your own copy of the
stock image, for the interpolator's coefficients):

```bash
python tools/paused_view_check.py sessions/2026-11-25_openscope_parity/session*.txt --stock APP_2C53T_V1.2.0_251015.bin
```

No calibration is in these files: `snapshot` and `watch` refuse the settings/calibration page
and the RAM tables it is loaded into.
