/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Optional part of the stock-link image (build_link_image.py --menu-wave / --menu-usb-wave):
 * the stubs behind the entries added to the generator's list of waveforms. The C side is in
 * link.c ("the generator's menu").
 *
 * 1. link_stub_wave stands at the entry of stock's table builder, 0x08029E14:
 *        08029e14: e92d 4ff0   push.w {r4-r11, lr}     <- replaced by `b.w link_stub_wave`
 *        08029e18: b08e        sub    sp, #0x38        <- stock resumes here
 *    The builder is a leaf that only fills the table (no call, no branch out of itself) and is
 *    reached from five places, three with bl and two with a tail b.w. The stub keeps r0-r3,
 *    r12 and lr, asks link_wave_build(), and either returns to the builder's caller (an added
 *    entry: the table is built) or replays the push and goes on at 0x08029E18 with every
 *    register as it was. The branch back is written by the builder over the placeholder, so
 *    that no register is spent on it.
 *
 * 2. link_stub_name replaces `ldr r0, [r0, #-0x34]` in the six rows of the list (0x0801579C):
 *    r0 in, r0 out, nothing else changed. lr was saved by that function's prologue and its
 *    next use is a bl.
 *
 * 3. link_stub_icon_r0 / _r1 replace `ldrb.w r0, [rX, #0xe59]` (0x080108EA, 0x0801F8EE) and
 *    `ldrb.w r1, [r4, #0xe59]` (0x080127BC): the index comes back in the same register and
 *    the others are kept. All three sites are in functions that saved lr.
 *
 * The flags are not kept: none of the replaced instructions sets them and at each site they
 * are set again before they are read.
 */
    .syntax unified
    .cpu cortex-m4
    .thumb

    .section .text.menu, "ax", %progbits

    .global link_stub_wave
    .thumb_func
link_stub_wave:
    push.w  {r0, r1, r2, r3, r12, lr}
    bl      link_wave_build
    cmp     r0, #0
    pop.w   {r0, r1, r2, r3, r12, lr}
    bne     1f
    push.w  {r4, r5, r6, r7, r8, r9, r10, r11, lr}  /* the displaced instruction */
    .global link_stub_wave_resume
link_stub_wave_resume:
    nop.w                                   /* the builder writes `b.w 0x08029E18` here */
1:  bx      lr

    .global link_stub_name
    .thumb_func
link_stub_name:
    push.w  {r0, r1, r2, r3, r12, lr}
    bl      link_wave_name
    str     r0, [sp]                        /* comes back in r0 */
    pop.w   {r0, r1, r2, r3, r12, lr}
    bx      lr

    .global link_stub_icon_r0
    .thumb_func
link_stub_icon_r0:
    push.w  {r0, r1, r2, r3, r12, lr}
    bl      link_wave_icon
    str     r0, [sp]
    pop.w   {r0, r1, r2, r3, r12, lr}
    bx      lr

    .global link_stub_icon_r1
    .thumb_func
link_stub_icon_r1:
    push.w  {r0, r1, r2, r3, r12, lr}
    bl      link_wave_icon
    str     r0, [sp, #4]                    /* comes back in r1 */
    pop.w   {r0, r1, r2, r3, r12, lr}
    bx      lr
