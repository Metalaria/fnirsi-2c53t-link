/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * stock-link code cave: appended to stock V1.2.0 after its last byte (image end 0x080BE680).
 * 0x080BE700 is the first 0x100-aligned address past the image. The factory IAP programs an
 * image page by page whatever its length, up to the end of flash, so the limit here is only
 * a sanity bound.
 */
MEMORY
{
    CAVE (rx) : ORIGIN = 0x080BE700, LENGTH = 0x1900
}

SECTIONS
{
    .text :
    {
        KEEP(*(.text.stubs))
        KEEP(*(.text.lang))
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    } > CAVE

    /DISCARD/ : { *(.ARM.exidx*) *(.ARM.attributes) *(.comment) *(.debug*) *(.note*) }

    .data : { *(.data*) } > CAVE
    .bss  : { *(.bss*) *(COMMON) } > CAVE
}

ASSERT(SIZEOF(.data) == 0, "stock-link must not have .data")
ASSERT(SIZEOF(.bss) == 0, "stock-link must not have .bss")
