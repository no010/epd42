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
 * Host-fed streaming, one packed plane at a time.  The host composes the
 * image, so the driver never buffers a frame: bytes arriving between
 * StreamBegin() and the next StreamBegin()/StreamFinish() are pushed
 * straight into the panel's RAM.
 *
 * Wire convention is the panel's own: 1 = white, 0 = black, MSB = leftmost.
 * Plane 0 is RAM command 0x10, plane 1 is 0x13 (same order as
 * EPD_4IN2_Display() and the web host).
 *
 * Init() must have run before the first StreamBegin() of a frame.
 */
#define EPD_4IN2_PLANES        2
#define EPD_4IN2_PLANE_BYTES   ((EPD_4IN2_WIDTH / 8) * EPD_4IN2_HEIGHT)

uint16_t EPD_4IN2_StreamPlaneBytes(void);
void EPD_4IN2_StreamBegin(uint8_t plane);
void EPD_4IN2_StreamWrite(const uint8_t *buffer, uint16_t length);
UBYTE EPD_4IN2_TurnOnDisplayTimeout(UDOUBLE timeout_ms);

#endif
