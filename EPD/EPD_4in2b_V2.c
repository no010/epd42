/*****************************************************************************
* | File      	:   EPD_4in2b_V2.c
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
#include "EPD_4in2b_V2.h"

/******************************************************************************
function :	Software reset
parameter:
******************************************************************************/
static void EPD_4IN2B_V2_Reset(void)
{
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(200);
    DEV_Digital_Write(EPD_RST_PIN, 0);
    DEV_Delay_ms(2);
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(200);
}

/******************************************************************************
function :	send command
parameter:
     Reg : Command register
******************************************************************************/
void EPD_4IN2B_V2_SendCommand(UBYTE Reg)
{
    DEV_Digital_Write(EPD_DC_PIN, 0);
    DEV_Digital_Write(EPD_CS_PIN, 0);
    DEV_SPI_WriteByte(Reg);
    DEV_Digital_Write(EPD_CS_PIN, 1);
}

/******************************************************************************
function :	send data
parameter:
    Data : Write data
******************************************************************************/
void EPD_4IN2B_V2_SendData(UBYTE Data)
{
    DEV_Digital_Write(EPD_DC_PIN, 1);
    DEV_Digital_Write(EPD_CS_PIN, 0);
    DEV_SPI_WriteByte(Data);
    DEV_Digital_Write(EPD_CS_PIN, 1);
}

/******************************************************************************
function :	Wait until the busy_pin goes LOW
parameter:
******************************************************************************/
void EPD_4IN2B_V2_ReadBusy(void)
{
    do{
        EPD_4IN2B_V2_SendCommand(0x71);
		DEV_Delay_ms(50);
    }while(!(DEV_Digital_Read(EPD_BUSY_PIN)));
    DEV_Delay_ms(50);
}

/******************************************************************************
function :	Turn On Display
parameter:
******************************************************************************/
void EPD_4IN2B_V2_TurnOnDisplay(void)
{
    EPD_4IN2B_V2_SendCommand(0x12); // DISPLAY_REFRESH
    DEV_Delay_ms(100);
    EPD_4IN2B_V2_ReadBusy();
}

/******************************************************************************
function :	Initialize the e-Paper register
parameter:
******************************************************************************/
void EPD_4IN2B_V2_Init(void)
{
    EPD_4IN2B_V2_Reset();
    
    EPD_4IN2B_V2_SendCommand(0x04); 
    EPD_4IN2B_V2_ReadBusy();

    EPD_4IN2B_V2_SendCommand(0x00);
    EPD_4IN2B_V2_SendData(0x0f);
}

/******************************************************************************
function :	Clear screen
parameter:
******************************************************************************/
void EPD_4IN2B_V2_Clear(void)
{
    UWORD Width, Height;
    Width = (EPD_4IN2B_V2_WIDTH % 8 == 0)? (EPD_4IN2B_V2_WIDTH / 8 ): (EPD_4IN2B_V2_WIDTH / 8 + 1);
    Height = EPD_4IN2B_V2_HEIGHT;

    EPD_4IN2B_V2_SendCommand(0x10);
    for (UWORD j = 0; j < Height; j++) {
        for (UWORD i = 0; i < Width; i++) {
            EPD_4IN2B_V2_SendData(0xFF);
        }
    }

    EPD_4IN2B_V2_SendCommand(0x13);
    for (UWORD j = 0; j < Height; j++) {
        for (UWORD i = 0; i < Width; i++) {
            EPD_4IN2B_V2_SendData(0xFF);
        }
    }

    EPD_4IN2B_V2_TurnOnDisplay();
}

/******************************************************************************
function :	Sends the image buffer in RAM to e-Paper and displays
parameter:
******************************************************************************/
void EPD_4IN2B_V2_Display(const UBYTE *blackimage, const UBYTE *ryimage)
{
    UWORD Width, Height;
    Width = (EPD_4IN2B_V2_WIDTH % 8 == 0)? (EPD_4IN2B_V2_WIDTH / 8 ): (EPD_4IN2B_V2_WIDTH / 8 + 1);
    Height = EPD_4IN2B_V2_HEIGHT;

    EPD_4IN2B_V2_SendCommand(0x10);
    for (UWORD j = 0; j < Height; j++) {
        for (UWORD i = 0; i < Width; i++) {
            EPD_4IN2B_V2_SendData(blackimage[i + j * Width]);
        }
    }

    EPD_4IN2B_V2_SendCommand(0x13);
    for (UWORD j = 0; j < Height; j++) {
        for (UWORD i = 0; i < Width; i++) {
            EPD_4IN2B_V2_SendData(ryimage[i + j * Width]);
        }
    }

    EPD_4IN2B_V2_TurnOnDisplay();
}

/******************************************************************************
function :	Enter sleep mode
parameter:
******************************************************************************/
void EPD_4IN2B_V2_Sleep(void)
{
    EPD_4IN2B_V2_SendCommand(0X50);
    EPD_4IN2B_V2_SendData(0xf7);		//border floating	

    EPD_4IN2B_V2_SendCommand(0X02);  	//power off
    EPD_4IN2B_V2_ReadBusy(); //waiting for the electronic paper IC to release the idle signal
    EPD_4IN2B_V2_SendCommand(0X07);  	//deep sleep
    EPD_4IN2B_V2_SendData(0xA5);
}

/******************************************************************************
function :	Host-fed streaming, plane 0 = 0x10 (black), plane 1 = 0x13 (red)
parameter:
Info:		Bytes are forwarded verbatim: the host packs the panel's own
		polarity (1 = white).
******************************************************************************/
uint16_t EPD_4IN2B_V2_StreamPlaneBytes(void)
{
    return EPD_4IN2B_V2_PLANE_BYTES;
}

void EPD_4IN2B_V2_StreamBegin(uint8_t plane)
{
    EPD_4IN2B_V2_SendCommand(plane == 0 ? 0x10 : 0x13);
}

void EPD_4IN2B_V2_StreamWrite(const uint8_t *buffer, uint16_t length)
{
    for (uint16_t i = 0; i < length; i++)
    {
        EPD_4IN2B_V2_SendData(buffer[i]);
    }
}

UBYTE EPD_4IN2B_V2_TurnOnDisplayTimeout(UDOUBLE timeout_ms)
{
    UDOUBLE waited;

    EPD_4IN2B_V2_SendCommand(0x12); // DISPLAY_REFRESH
    DEV_Delay_ms(100);

    /* Same protocol as EPD_4IN2B_V2_ReadBusy(): the controller only reports
     * its status in response to 0x71, and HIGH means idle. */
    waited = 100;
    do {
        EPD_4IN2B_V2_SendCommand(0x71);
        DEV_Delay_ms(50);
        waited += 50;
        if (waited > timeout_ms)
        {
            return 0;
        }
    } while (!(DEV_Digital_Read(EPD_BUSY_PIN)));

    DEV_Delay_ms(50);
    return 1;
}
