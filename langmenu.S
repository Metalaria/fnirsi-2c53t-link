/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Optional part of the stock-link image (build_link_image.py --language-menu): the way into
 * link_language_panel() (langmenu.c) and back.
 *
 * Stock's routine for the options of the selected setting, 0x0801CF50:
 *     0801cf50: e92d 4ff0   push.w {r4-r11, lr}
 *     0801cf54: b08b        sub    sp, #0x2c
 *     0801cf56: ...         r3 = state base; r0 = state[0xF6A] & 15; tbh on r0 (7 cases)
 *     0801cf7e: f893 0f69   ldrb.w r0, [r3, #0xf69]   <- case 0, Language: `b.w link_stub_langpanel`
 *     ...
 *     0801d28c: b00b        add    sp, #0x2c          <- the routine's exit
 *     0801d28e: e8bd 8ff0   pop.w  {r4-r11, pc}
 *
 * At the case's first instruction nothing is live but the frame: r0-r3 and r12 are scratch
 * (r3 is reloaded by every path that needs it), lr is on the stack, and r4-r11 still hold the
 * caller's values, which link_language_panel() keeps as any C function does. The stack is
 * 8-byte aligned there (36 + 44 bytes pushed). So the stub calls the panel and leaves through
 * stock's own exit; the branch to it is written by the builder over the placeholder.
 */
    .syntax unified
    .cpu cortex-m4
    .thumb

    .section .text.langmenu, "ax", %progbits

    .global link_stub_langpanel
    .thumb_func
link_stub_langpanel:
    bl      link_language_panel
    .global link_stub_langpanel_resume
link_stub_langpanel_resume:
    nop.w                                   /* the builder writes `b.w 0x0801D28C` here */
