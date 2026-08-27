/*
 * Streaming scanline renderer for the Subscription Monitor display.
 *
 * Layout (400 x 300 pixels, 8x16 px font):
 *
 *  Row   0- 15 : Title  "SUB MONITOR"  (centred)
 *  Row  16     : Horizontal rule
 *  Row  20- 35 : Item[0] plan name
 *  Row  36- 51 : Item[0] "Used: XXXX / YYYY unit"
 *  Row  52- 55 : Item[0] progress bar (4 px)
 *  Row  64- 79 : Item[1] plan name          (if present)
 *  Row  80- 95 : Item[1] used / total line
 *  Row  96- 99 : Item[1] progress bar
 *  Row 108-123 : Item[2] plan name          (if present)
 *  Row 124-139 : Item[2] used / total line
 *  Row 140-143 : Item[2] progress bar
 *  Row 280-295 : "Updated: MM-DD HH:MM"
 *
 * Pixel convention (matches EPD_4IN2 0x13 command):
 *   bit = 1  → black ink
 *   bit = 0  → white paper
 *
 * No frame buffer is allocated; everything is computed on-the-fly per row.
 */
#include <string.h>
#include "renderer.h"
#include "font8x16.h"
#include "subscription.h"

/* Screen dimensions */
#define SCREEN_WIDTH   400u
#define SCREEN_HEIGHT  300u
#define LINE_BYTES     (SCREEN_WIDTH / 8u)   /* 50 */

/* Vertical layout constants */
#define ROW_TITLE_START   0u
#define ROW_TITLE_END     15u
#define ROW_HLINE         16u

/* Per-item offsets from item base row */
#define ITEM_NAME_OFF     0u   /* rows +0..+15  : plan name  */
#define ITEM_USAGE_OFF    16u  /* rows +16..+31 : usage line */
#define ITEM_BAR_OFF      32u  /* rows +32..+35 : progress   */
#define ITEM_STRIDE       44u  /* total rows per item slot   */

#define ITEM0_BASE        20u
#define ITEM1_BASE        (ITEM0_BASE + ITEM_STRIDE)
#define ITEM2_BASE        (ITEM1_BASE + ITEM_STRIDE)

#define ROW_UPDATED_START 280u
#define ROW_UPDATED_END   295u

static subscription_data_t g_sub_data;

void renderer_set_data(const subscription_data_t *data)
{
    g_sub_data = *data;
}

/* ------------------------------------------------------------------ */
/*  Pixel helpers                                                       */
/* ------------------------------------------------------------------ */

static void draw_pixel(uint8_t *line, uint16_t x)
{
    if (x < SCREEN_WIDTH)
    {
        line[x >> 3u] |= (uint8_t)(0x80u >> (x & 7u));
    }
}

/* Draw one horizontal pixel row of a single glyph at column x. */
static void draw_char_slice(uint8_t *line, uint16_t x, char c, uint8_t row_in_glyph)
{
    if ((uint8_t)c < FONT8X16_FIRST || (uint8_t)c > FONT8X16_LAST)
    {
        c = ' ';
    }
    uint16_t glyph_idx = ((uint8_t)c - FONT8X16_FIRST) * FONT8X16_HEIGHT + row_in_glyph;
    uint8_t glyph_byte = font8x16[glyph_idx];
    for (uint8_t i = 0; i < 8u; i++)
    {
        if (glyph_byte & (0x80u >> i))
        {
            draw_pixel(line, x + i);
        }
    }
}

/* Render a NUL-terminated string starting at (x_start, base_row) for the
 * given absolute screen row. */
static void draw_text(uint8_t *line, uint16_t abs_row,
                      const char *text, uint16_t x_start, uint16_t base_row)
{
    if (abs_row < base_row || abs_row >= base_row + FONT8X16_HEIGHT)
    {
        return;
    }
    uint8_t slice = (uint8_t)(abs_row - base_row);
    uint16_t x = x_start;
    for (uint16_t i = 0; text[i] != '\0' && x < SCREEN_WIDTH; i++)
    {
        draw_char_slice(line, x, text[i], slice);
        x += FONT8X16_WIDTH;
    }
}

/* Draw centred text on a 400-px wide row. */
static void draw_text_centred(uint8_t *line, uint16_t abs_row,
                               const char *text, uint16_t base_row)
{
    uint16_t len = 0;
    while (text[len]) len++;
    uint16_t total_w = (uint16_t)(len * FONT8X16_WIDTH);
    uint16_t x_start = (total_w < SCREEN_WIDTH) ? (SCREEN_WIDTH - total_w) / 2u : 0u;
    draw_text(line, abs_row, text, x_start, base_row);
}

/* Draw a full-width horizontal rule at the given row. */
static void draw_hline(uint8_t *line, uint16_t abs_row, uint16_t target_row)
{
    if (abs_row != target_row)
    {
        return;
    }
    for (uint16_t i = 0; i < LINE_BYTES; i++)
    {
        line[i] = 0xFF;
    }
}

/* Draw a progress bar (filled rectangle) spanning columns 0..(fill_px-1)
 * for rows [bar_row .. bar_row+3]. */
static void draw_progress_bar(uint8_t *line, uint16_t abs_row,
                               uint16_t bar_row, uint32_t used, uint32_t total)
{
    if (abs_row < bar_row || abs_row >= bar_row + 4u)
    {
        return;
    }
    if (total == 0u)
    {
        return;
    }
    /* Avoid overflow: compute fill_px = (SCREEN_WIDTH * used) / total */
    uint32_t fill_px;
    if (used >= total)
    {
        fill_px = SCREEN_WIDTH;
    }
    else
    {
        fill_px = (uint32_t)(((uint32_t)SCREEN_WIDTH * used) / total);
    }
    /* Outline: full-width 1-px border on first and last row */
    if (abs_row == bar_row || abs_row == bar_row + 3u)
    {
        for (uint16_t i = 0; i < LINE_BYTES; i++)
        {
            line[i] = 0xFF;
        }
        return;
    }
    /* Inner fill rows */
    uint16_t full_bytes = (uint16_t)(fill_px / 8u);
    uint8_t  rem_bits   = (uint8_t)(fill_px % 8u);
    for (uint16_t i = 0; i < full_bytes && i < LINE_BYTES; i++)
    {
        line[i] = 0xFF;
    }
    if (full_bytes < LINE_BYTES && rem_bits > 0u)
    {
        line[full_bytes] |= (uint8_t)(0xFFu << (8u - rem_bits));
    }
}

/* ------------------------------------------------------------------ */
/*  Integer-only number formatting                                      */
/* ------------------------------------------------------------------ */

/* Write unsigned decimal of value into buf; returns pointer to first char. */
static char *fmt_u32(char *buf_end, uint32_t value)
{
    *--buf_end = '\0';
    if (value == 0u)
    {
        *--buf_end = '0';
        return buf_end;
    }
    while (value > 0u)
    {
        *--buf_end = (char)('0' + (value % 10u));
        value /= 10u;
    }
    return buf_end;
}

/* Format "Used: XXXX / YYYY unit" or "Balance: XX.YY$" into out[].
 * out must be at least 40 bytes. */
static void fmt_usage_line(char *out, const subscription_item_t *item)
{
    char tmp[12];
    char *p;

    /* "Used: " */
    const char prefix[] = "Used: ";
    uint8_t pi = 0;
    for (; prefix[pi]; pi++) out[pi] = prefix[pi];

    /* quota_used */
    p = fmt_u32(tmp + sizeof(tmp), item->quota_used);
    for (; *p; p++) out[pi++] = *p;

    out[pi++] = ' ';
    out[pi++] = '/';
    out[pi++] = ' ';

    /* quota_total */
    p = fmt_u32(tmp + sizeof(tmp), item->quota_total);
    for (; *p; p++) out[pi++] = *p;

    out[pi++] = ' ';

    /* unit (max 3 chars) */
    for (uint8_t u = 0; u < 3 && item->unit[u]; u++) out[pi++] = item->unit[u];

    /* balance (if non-zero): "  $XX.YY" */
    if (item->balance > 0u)
    {
        out[pi++] = ' ';
        out[pi++] = ' ';
        out[pi++] = '$';
        p = fmt_u32(tmp + sizeof(tmp), item->balance / 100u);
        for (; *p; p++) out[pi++] = *p;
        out[pi++] = '.';
        uint32_t cents = item->balance % 100u;
        out[pi++] = (char)('0' + cents / 10u);
        out[pi++] = (char)('0' + cents % 10u);
    }

    out[pi] = '\0';
}

/* ------------------------------------------------------------------ */
/*  Main scanline callback                                              */
/* ------------------------------------------------------------------ */

void subscription_scanline_cb(uint16_t row, uint8_t *line_buffer)
{
    memset(line_buffer, 0x00, LINE_BYTES);

    /* --- Title --- */
    draw_text_centred(line_buffer, row, "SUB MONITOR", ROW_TITLE_START);

    /* --- Horizontal rule under title --- */
    draw_hline(line_buffer, row, ROW_HLINE);

    /* --- Items --- */
    const uint16_t bases[3] = { ITEM0_BASE, ITEM1_BASE, ITEM2_BASE };
    uint8_t count = g_sub_data.item_count;
    if (count > SUBSCRIPTION_MAX_ITEMS)
    {
        count = SUBSCRIPTION_MAX_ITEMS;
    }

    for (uint8_t idx = 0; idx < count; idx++)
    {
        const subscription_item_t *item = &g_sub_data.items[idx];
        uint16_t base = bases[idx];

        /* Plan name */
        draw_text(line_buffer, row, item->plan_name, 8u, (uint16_t)(base + ITEM_NAME_OFF));

        /* Usage line */
        {
            char buf[40];
            fmt_usage_line(buf, item);
            draw_text(line_buffer, row, buf, 8u, (uint16_t)(base + ITEM_USAGE_OFF));
        }

        /* Progress bar */
        draw_progress_bar(line_buffer, row,
                          (uint16_t)(base + ITEM_BAR_OFF),
                          item->quota_used, item->quota_total);
    }

    /* --- Last-updated timestamp --- */
    if (g_sub_data.last_update[0] != '\0')
    {
        char upd[24];
        const char up_prefix[] = "Updated: ";
        uint8_t pi = 0;
        for (; up_prefix[pi]; pi++) upd[pi] = up_prefix[pi];
        for (uint8_t i = 0; i < 15u && g_sub_data.last_update[i]; i++) upd[pi++] = g_sub_data.last_update[i];
        upd[pi] = '\0';
        draw_text(line_buffer, row, upd, 8u, ROW_UPDATED_START);
    }
}
