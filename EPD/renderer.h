#ifndef RENDERER_H__
#define RENDERER_H__

#include <stdint.h>
#include "subscription.h"

/* Supply the data to render before calling the scanline callback. */
void renderer_set_data(const subscription_data_t *data);

/* EPD scanline callback: fills line_buffer[50] for the given row (0-299).
 * Pixels: 1 = black ink, 0 = white paper (matches EPD_4IN2 0x13 convention). */
void subscription_scanline_cb(uint16_t row, uint8_t *line_buffer);

#endif /* RENDERER_H__ */
