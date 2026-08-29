/*****************************************************************************
* | File      	:   EPD_4in2b_V2.h
* | Author      :   Waveshare team
* | Function    :   4.2inch e-paper b V2
* | Info        :
*----------------
* |	This version:   V1.0
* | Date        :   2020-11-27
* | Info        :
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
#ifndef __EPD_4IN2B_V2_H_
#define __EPD_4IN2B_V2_H_

#include "DEV_Config.h"

// Display resolution
#define EPD_4IN2B_V2_WIDTH       400
#define EPD_4IN2B_V2_HEIGHT      300

void EPD_4IN2B_V2_Init(void);
void EPD_4IN2B_V2_Clear(void);
void EPD_4IN2B_V2_Display(const UBYTE *blackimage, const UBYTE *ryimage);
void EPD_4IN2B_V2_Sleep(void);

void EPD_4IN2B_V2_SendCommand(UBYTE Reg);
void EPD_4IN2B_V2_SendData(UBYTE Data);
void EPD_4IN2B_V2_TurnOnDisplay(void);

/**
 * Host-fed streaming, one packed plane at a time: plane 0 is RAM command
 * 0x10 (black), plane 1 is 0x13 (red) - the order used by
 * EPD_4IN2B_V2_Display().  Bytes are forwarded verbatim, so the host packs
 * the panel's own polarity (1 = white, MSB = leftmost).  This controller
 * has no cursor command, so a truncated plane can only be recovered by
 * running Init() again before the next frame.
 */
#define EPD_4IN2B_V2_PLANES        2
#define EPD_4IN2B_V2_PLANE_BYTES   ((EPD_4IN2B_V2_WIDTH / 8) * EPD_4IN2B_V2_HEIGHT)

uint16_t EPD_4IN2B_V2_StreamPlaneBytes(void);
void EPD_4IN2B_V2_StreamBegin(uint8_t plane);
void EPD_4IN2B_V2_StreamWrite(const uint8_t *buffer, uint16_t length);
UBYTE EPD_4IN2B_V2_TurnOnDisplayTimeout(UDOUBLE timeout_ms);

#endif
