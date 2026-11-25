/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Optional part of the stock-link image (build_link_image.py --languages): makes the six
 * languages that stock V1.2.0 still carries, but no longer offers, selectable again.
 *
 * Stock's Settings > Language list has two rows and its OK handler stores row + 1:
 *     0800beca: f895 0f6b   ldrb.w r0, [r5, #0xf6b]     <- replaced by `bl link_stub_language`
 *     0800bece: f642 566c   movw   r6, #0x2d6c
 *     0800bed2: 3001        adds   r0, #1
 *     0800bed4: f642 5453   movw   r4, #0x2d53
 *     0800bed8: f2c2 0600   movt   r6, #0x2000
 *     0800bedc: f885 0f60   strb.w r0, [r5, #0xf60]     (the language: 1 Chinese ... 8 Korean)
 *
 * The stub returns in r0 what stock would have loaded there, with one difference: on the
 * second row ("English") it returns the current language while that is 2..7, so stock's own
 * `adds r0, #1` moves on to the next one: English, Russian, Japanese, Spanish, Portuguese,
 * German, Korean, and English again. The first row gives Chinese as before.
 *
 * At the call r5 holds stock's state base (set at 0x0800BD66 and kept), lr was saved by the
 * function's prologue (push {r4-r8, lr} at 0x0800BD60) and is dead until the next bl, and the
 * flags are dead (the adds sets them again). Only r0 and lr are written.
 */
    .syntax unified
    .cpu cortex-m4
    .thumb

    .section .text.lang, "ax", %progbits

    .global link_stub_language
    .thumb_func
link_stub_language:
    ldrb.w  r0, [r5, #0xf6b]        /* the row under the cursor, as stock */
    cbz     r0, 2f                  /* first row: Chinese */
    ldrb.w  r0, [r5, #0xf60]
    cmp     r0, #2
    blo     1f                      /* coming from Chinese: English */
    cmp     r0, #8
    blo     2f                      /* 2..7: stock's adds makes it the next language */
1:  movs    r0, #1                  /* after Korean, or anything unexpected: English */
2:  bx      lr
