# What stock's state does while the keys are pressed, 2026-11-26

Four logs of `fnlink.py keys` on the unit (stock V1.2.0 with patch 6, read over USB, nothing
written to the instrument), taken to find out what each key does outside the oscilloscope.
**None of them has a note of which key was pressed**: the keys were asked for in an order,
given below, and pressed without being written down. So each log says what changed and
when, and the key behind a change is only as sure as the order was kept. Written up in
[`docs/hardware_results_2026-11-25.md`](../../docs/hardware_results_2026-11-25.md), section
"Logs of the keys".

| File | Mode | The order asked for |
|---|---|---|
| `generator_first.txt` | generator, then the mode screen and the multimeter | none: blue on the frequency, the arrows, MENU twice, a long MENU, the multimeter |
| `generator_keys.txt` | generator, the list on entry 16 | MENU, MENU, down, up, right, left, blue, down, up, right, left, blue, left, up, down, right, MENU, MENU |
| `multimeter_keys.txt` | multimeter, in Automatic | right, SELECT, MOVE, TRIGGER, PRM, AUTO, SAVE, CH1, CH2, blue, up, down, left, MENU |
| `oscilloscope_keys.txt` | oscilloscope, running, CH1 alone | right, left, up, down, SELECT, right, left, up, down, SELECT until the bar shows V and H, MOVE, MOVE, CH1, MENU, CH2, MENU, TRIGGER, MENU, PRM, MENU, blue, blue, AUTO |

In these logs `ms[0xF6F]` (the last byte of the `ui` block) changes about once a second with
no key pressed; `fnlink.py keys` has left it out since. `link lost` and `link back` are the
tool noticing that the instrument stopped answering and answering again.

No calibration is in these files.
