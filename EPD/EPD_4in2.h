/*****************************************************************************
* | File      	:   EPD_4in2.h
* | Author      :   Waveshare team
* | Function    :   4.2inch e-paper
* | Info        :
*----------------
* |	This version:   V3.0
* | Date        :   2019-06-13
* | Info        :
* -----------------------------------------------------------------------------
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documnetation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to  whom the Software is
# furished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS OR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
******************************************************************************/
#ifndef _EPD_4IN2_H_
#define _EPD_4IN2_H_

#include "DEV_Config.h"

// Display resolution
#define EPD_4IN2_WIDTH       400
#define EPD_4IN2_HEIGHT      300

void EPD_4IN2_Init(void);
void EPD_4IN2_Clear(void);
void EPD_4IN2_Display(UBYTE *Image);
void EPD_4IN2_Sleep(void);

void EPD_4IN2_SendCommand(UBYTE Reg);
void EPD_4IN2_SendData(UBYTE Data);
void EPD_4IN2_TurnOnDisplay(void);

/**
 * @brief Stream-display: render the screen via a per-row callback.
 *
 * No full frame buffer is allocated; the driver calls @p callback once
 * per row (0..EPD_4IN2_HEIGHT-1) supplying a 50-byte (400/8) output
 * buffer.  The callback fills the buffer with the pixel data for that row
 * (1 = black, 0 = white, MSB = leftmost pixel) and the driver sends it
 * immediately over SPI.
 *
 * @param callback  Function called for each scanline.
 */
typedef void (*epd_scanline_callback_t)(uint16_t row, uint8_t *line_buffer);
void EPD_4IN2_DisplayStream(epd_scanline_callback_t callback);

#endif
