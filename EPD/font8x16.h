#ifndef FONT8X16_H__
#define FONT8X16_H__

#include <stdint.h>

/* 8x16 bitmap font, ASCII 32-126 (95 characters).
 * Each character occupies 16 bytes; byte N is the pixel row N (top=0).
 * Bit 7 (MSB) is the leftmost pixel; 1 = ink, 0 = paper.
 * Stored in ROM (.rodata) — does not consume RAM. */
#define FONT8X16_WIDTH   8u
#define FONT8X16_HEIGHT  16u
#define FONT8X16_FIRST   32u  /* first ASCII code */
#define FONT8X16_LAST    126u /* last  ASCII code */

extern const uint8_t font8x16[95 * 16];

#endif /* FONT8X16_H__ */
