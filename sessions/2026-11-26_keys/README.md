# What stock's state does while the keys are pressed, 2026-11-26

Logs of `fnlink.py keys` on the unit (stock V1.2.0 with patch 6, read over USB, nothing
written to the instrument), taken to find out what each key does in each mode. Written up
in [`docs/hardware_results_2026-11-25.md`](../../docs/hardware_results_2026-11-25.md),
sections "Logs of the keys", "The keys, one at a time" and "The keys, what the first
guided logs left open".

## The guided logs: each key is in the log

Three logs taken with `--guide`: the tool printed `press <key>` every 5 s, the owner
pressed that key once, and the line is in the log before whatever changed. So a change
belongs to the key asked for last, as long as each key was pressed within its 5 s; where
a change came late that is said in the write-up.

| File | Mode at the start | Keys asked for |
|---|---|---|
| `guided_multimeter.txt` | multimeter, Automatic | right, right, left, left, left, right, right, SELECT, MOVE, TRIGGER, PRM, AUTO, SAVE, SAVE, CH1, CH2, blue, up, down, MENU |
| `guided_generator.txt` | generator, the list on entry 16 | MENU, MENU, down, up, right, left, blue, down, up, right, left, blue, left, up, down, right, MENU, MENU |
| `guided_oscilloscope.txt` | oscilloscope, running, CH1 alone, arrows in mode 0 | right, left, up, down, SELECT, the same five again, and again, MOVE, MOVE, CH1, MENU, CH2, MENU, TRIGGER, MENU, PRM, MENU, blue, blue, AUTO, and two waits |

Three more, taken the next day (2026-11-27) for what those left open:

| File | Mode at the start | Keys asked for |
|---|---|---|
| `guided_generator_digit.txt` | generator, the list on Square Wave, 1 kHz | blue, blue, left, up, down, left, up, down, right, right, up, down, MENU, MENU |
| `guided_multimeter_move.txt` | multimeter, the voltage entry, DC | MOVE, wait, MOVE, wait, right, MOVE, wait, MOVE, wait, AUTO, wait |
| `guided_oscilloscope_ch2.txt` | oscilloscope, running, CH1 and CH2 on, arrows in mode 0, 200 µs per division | up, down, MOVE, up, down, MOVE, SELECT, the same seven again, and again, right, left, AUTO, and two waits |

`guided_multimeter.txt` holds two runs: the first was stopped after 16 s and the second
is complete. The keys are named in Spanish, as they were typed: `derecha` right,
`izquierda` left, `arriba` up, `abajo` down, `azul` the blue key in the middle of the
arrows, `menu corto` a short press of MENU, `espera` wait (no key). `test/test_decode.py`
holds what the write-up says against these six files.

## The first logs: no key in the log

Four earlier logs. **None of them has a note of which key was pressed**: the keys were
asked for in an order, given below, and pressed without being written down. So each log
says what changed and when, and the key behind a change is only as sure as the order was
kept.

| File | Mode | The order asked for |
|---|---|---|
| `generator_first.txt` | generator, then the mode screen and the multimeter | none: blue on the frequency, the arrows, MENU twice, a long MENU, the multimeter |
| `generator_keys.txt` | generator, the list on entry 16 | MENU, MENU, down, up, right, left, blue, down, up, right, left, blue, left, up, down, right, MENU, MENU |
| `multimeter_keys.txt` | multimeter, in Automatic | right, SELECT, MOVE, TRIGGER, PRM, AUTO, SAVE, CH1, CH2, blue, up, down, left, MENU |
| `oscilloscope_keys.txt` | oscilloscope, running, CH1 alone | right, left, up, down, SELECT, right, left, up, down, SELECT until the bar shows V and H, MOVE, MOVE, CH1, MENU, CH2, MENU, TRIGGER, MENU, PRM, MENU, blue, blue, AUTO |

In `oscilloscope_keys.txt` `ms[0xF6F]` (the last byte of the `ui` block) changes about once
a second with no key pressed; `fnlink.py keys` has left it out since. `link lost` and `link back` are the
tool noticing that the instrument stopped answering and answering again.

No calibration is in these files.
