/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Optional part of the stock-link image (build_link_image.py --language-menu): Settings >
 * Language lists the eight languages stock V1.2.0 still carries, one entry each, instead of
 * the two it offers.
 *
 * Stock draws the options of the selected setting with one routine (0x0801CF50), which
 * switches on the row of the settings list (state[0xF6A]). The case of the first row,
 * Language, paints two fixed rows, "中文" and "English", in the first and third of the four
 * slots that the other settings use (36 px apart from y = 61). link_language_panel() stands
 * in for that case and paints four slots the way stock paints those of "Auto Shutdown"
 * (0x0801D03C..0x0801D28A, case 3): the same pictures at the same places, the same fill for
 * the rows off the cursor, the same text call with the same font and colours. Eight
 * languages are two pages of four: the page is the one of the cursor, or the one of the
 * current language while the cursor is still in the settings list.
 *
 * Nothing else is needed for the choice itself. Stock's OK handler already stores the row
 * under the cursor plus one as the language (0x0800BECA..0x0800BEDC), and the key that moves
 * the cursor down stops at the last row it reads from a table with one byte per setting
 * (0x0800B1D8), which the builder raises from 1 to 7 for this one.
 *
 * The names are the languages' own. Stock's fonts lack the first character of 日本語, so
 * Japanese is written in hiragana; the builder checks every character of every name against
 * the five fonts of the image, because the text routine ends a string at a character it has
 * no glyph for.
 *
 * It writes nothing but pixels of the band being painted, through the band's own descriptor
 * and with the same test stock makes for every pixel. No state of its own.
 */
#include <stdint.h>

/* ---- stock V1.2.0 facts (checked by the builder against the image) ------------------------ */

#define STATE                ((const volatile uint8_t *)0x200000F8u)
#define ST_LANGUAGE          0xF60u                        /* 1 Chinese ... 8 Korean            */
#define ST_IN_OPTIONS        0xF69u                        /* 0: the cursor is in the list      */
#define ST_OPTION            0xF6Bu                        /* the row under the cursor          */

typedef struct {                                           /* the band being painted            */
    uint16_t x, y, w, h;
    uint16_t *pixels;                                      /* w * h, RGB565                     */
} band_t;
#define BAND                 ((const volatile band_t *)0x20008350u)

typedef void (*draw_picture_fn)(const char *path, uint32_t x, uint32_t y);
typedef void (*draw_text_fn)(uint32_t x, uint32_t y, uint32_t w, uint32_t h,
                             const char *text, const void *font, uint32_t colour, uint32_t align);
#define stock_draw_picture   ((draw_picture_fn)0x08039F6Du)
#define stock_draw_text      ((draw_text_fn)0x0800F155u)

#define PICTURE_CURSOR       ((const char *)0x080BC499u)   /* 72.jpg: the bar under the cursor  */
#define PICTURE_CURSOR_ON    ((const char *)0x080BC54Au)   /* 75.jpg: mark, chosen, on the bar  */
#define PICTURE_CURSOR_OFF   ((const char *)0x080BC5FCu)   /* 78.jpg: mark, not chosen, on it   */
#define PICTURE_ON           ((const char *)0x080BC4F2u)   /* 74.jpg: mark, chosen              */
#define PICTURE_OFF          ((const char *)0x080BC5CFu)   /* 77.jpg: mark, not chosen          */
#define MENU_FONT            ((const void *)0x20001114u)   /* the 18 px font's descriptor       */
#define MENU_COLOURS         ((const uint16_t *)0x0804C180u)   /* [1] plain, [2] under the cursor */

#define SLOTS                4u                            /* rows of the options panel         */
#define SLOT_Y               61u
#define SLOT_PITCH           36u
#define ROW_FILL             0x18C3u                       /* a row off the cursor              */
#define ROW_FILL_X0          122u
#define ROW_FILL_X1          304u                          /* one past the last column          */
#define ROW_FILL_ROWS        32u
#include "language_names.h"                                /* written by build_link_image.py: LANGUAGES,
                                                              language_name[], in the order of the byte */

/* Stock's fill of a row off the cursor (0x0801D0B0..0x0801D1D6): every pixel of the row that
 * falls inside the band. Kept out of line so that its registers are not on the stack while
 * stock's picture and text routines run below the panel. */
static __attribute__((noinline)) void fill_row(uint32_t y0)
{
    uint32_t bx = BAND->x, by = BAND->y, bw = BAND->w, bh = BAND->h;
    uint16_t *pixels = BAND->pixels;
    uint32_t x, y;

    for (y = y0; y < y0 + ROW_FILL_ROWS; ++y) {
        if (y < by || y >= by + bh) {
            continue;
        }
        for (x = ROW_FILL_X0; x < ROW_FILL_X1; ++x) {
            if (x >= bx && x < bx + bw) {
                pixels[(y - by) * bw + (x - bx)] = (uint16_t)ROW_FILL;
            }
        }
    }
}

/* Called from link_stub_langpanel, which stands at the first instruction of the Language case
 * of stock's options routine and leaves through that routine's own exit. The state is read
 * again for every row, as stock does, which also keeps this frame small: stock's picture and
 * text routines run below it. */
void link_language_panel(void)
{
    uint32_t slot;

    for (slot = 0; slot < SLOTS; ++slot) {
        uint32_t language = STATE[ST_LANGUAGE];
        uint32_t inside = STATE[ST_IN_OPTIONS] != 0u;
        uint32_t cursor = STATE[ST_OPTION];
        uint32_t shown = inside ? cursor : language - 1u;  /* the entry whose page is on screen */
        uint32_t entry = slot + ((shown < LANGUAGES && shown >= SLOTS) ? SLOTS : 0u);
        uint32_t y = SLOT_Y + SLOT_PITCH * slot;
        uint32_t chosen = language == entry + 1u;
        uint32_t colour;

        if (inside && cursor == entry) {
            stock_draw_picture(PICTURE_CURSOR, 121u, y);
            if (chosen) {
                stock_draw_picture(PICTURE_CURSOR_ON, 166u, y + 8u);
            } else {
                stock_draw_picture(PICTURE_CURSOR_OFF, 165u, y + 8u);
            }
            colour = MENU_COLOURS[2];
        } else {
            fill_row(y + 1u);
            stock_draw_picture(chosen ? PICTURE_ON : PICTURE_OFF, 166u, y + 10u);
            colour = MENU_COLOURS[1];
        }
        stock_draw_text(182u, y, 92u, 34u, language_name[entry], MENU_FONT, colour, 1u);
    }
}
