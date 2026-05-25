// TinyUSB config for pico_ltc_emulator.
// CDC-ACM only -- single virtual serial port to the Pi.

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

#define CFG_TUSB_RHPORT0_MODE   (OPT_MODE_DEVICE | OPT_MODE_FULL_SPEED)
#define CFG_TUSB_MEM_SECTION
#define CFG_TUSB_MEM_ALIGN      __attribute__ ((aligned(4)))

#define CFG_TUD_CDC             1
#define CFG_TUD_MSC             0
#define CFG_TUD_HID             0
#define CFG_TUD_MIDI            0
#define CFG_TUD_VENDOR          0

// CDC FIFOs sized for typical bursts: status lines (~64B) and
// SET_CELL/SET_TEMP commands (~20B).
#define CFG_TUD_CDC_RX_BUFSIZE  256
#define CFG_TUD_CDC_TX_BUFSIZE  256

#ifdef __cplusplus
}
#endif
