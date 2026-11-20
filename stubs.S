/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Entry stubs for the stock-link code cave.
 *
 * Hook 1 is the FreeRTOS SysTick handler of stock V1.2.0, 0x0802A994:
 *     0802a994: b580        push  {r7, lr}
 *     0802a996: f04f 0010   mov.w r0, #16
 *     0802a99a: f380 8811   msr   BASEPRI, r0     <- execution resumes here
 * The builder replaces those first six bytes with `b.w link_stub_tick ; nop`.
 *
 * The push is replayed first, so EXC_RETURN is on the stack before the call clobbers lr; the
 * handler ends with pop {r7, pc}. r0-r3 and r12 are free: this is exception entry, the
 * hardware has already stacked them. The same site is used by OpenScope's stock_caplog,
 * which is how it is known to work on real units.
 */
    .syntax unified
    .cpu cortex-m4
    .thumb

    .section .text.stubs, "ax", %progbits

    .global link_stub_tick
    .thumb_func
link_stub_tick:
    push    {r7, lr}
    bl      link_tick
    mov.w   r0, #16
    ldr     r12, =0x0802A99B        /* msr BASEPRI, r0 (Thumb) */
    bx      r12

/* Hook 2 is the tail of the `fpga` task's command loop, 0x0803E49A:
 *     0803e498: 6878        ldr   r0, [r7, #4]        (drain SPI3 DT; not every path runs it)
 *     0803e49a: f640 4010   movw  r0, #0x0c10         <- replaced by `b.w link_stub_fpga`
 *     0803e49e: f2c4 0001   movt  r0, #0x4001         <- execution resumes here
 *     0803e4a2: 2140        movs  r1, #0x40
 *     0803e4a4: ...         reload r4/r5, release CS, xQueueReceive(next command)
 * Every command handler ends by reaching 0x0803E49A, either falling through from 0x0803E498
 * or branching straight to it, and nothing branches to 0x0803E49C..0x0803E4A2.
 *
 * At that point r0-r3 and r12 are dead (each is reloaded before its next use) and so is lr:
 * the task never returns and the next thing it does is a bl. The command byte that was just
 * served is still in the task's frame at [sp, #0x36]. Only integer registers are touched, so
 * the VFP constants the task keeps in s16-s28 are safe.
 */
    .global link_stub_fpga
    .thumb_func
link_stub_fpga:
    ldrb.w  r0, [sp, #0x36]
    bl      link_fpga_tail
    movw    r0, #0x0c10
    ldr     r12, =0x0803E49F        /* movt r0, #0x4001 (Thumb) */
    bx      r12

    .pool
