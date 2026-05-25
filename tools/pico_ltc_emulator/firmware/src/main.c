// pico_ltc_emulator entry point.
//
// Boot sequence:
//   1. stdio_usb_init       (TinyUSB CDC alive)
//   2. cell_state_reset     (defaults: 3750 mV / 25 °C everywhere)
//   3. ltc6811_emu_init     (SPI slave on SPI0 / GP16..19)
//   4. usb_cmd_init
//   5. main loop: service CDC commands + SPI slave events
//
// The Pico runs at default 125 MHz; both services are polled and the
// loop never sleeps -- USB IRQ and the SPI hw FIFOs absorb timing
// slack. If we later need lower CDC latency we'll move SPI service
// into a hardware IRQ.

#include "pico/stdlib.h"

#include "cell_state.h"
#include "ltc6811_emu.h"
#include "usb_cmd.h"

int main(void) {
    stdio_init_all();
    // No `while (!tud_cdc_connected())` -- the firmware must be
    // operational with or without a host CDC client attached, since
    // the AMS firmware's SPI polls don't care if the Pi has the
    // serial port open.

    cell_state_reset();
    ltc6811_emu_init();
    usb_cmd_init();

    while (true) {
        usb_cmd_service();
        ltc6811_emu_service();
    }

    return 0;
}
