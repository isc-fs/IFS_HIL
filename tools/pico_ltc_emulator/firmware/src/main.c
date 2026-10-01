// pico_ltc_emulator entry point.
//
// Boot sequence:
//   1. stdio_usb_init       (TinyUSB CDC alive)
//   2. cell_state_reset     (defaults: 3750 mV / 25 °C everywhere)
//   3. ltc6811_emu_init     (SPI slave on SPI0 / GP16..19)
//   4. usb_cmd_init
//   5. main loop: service SPI slave events; service USB CDC only while
//      CS is HIGH (bus idle). USB stdio's tud_task can spend hundreds
//      of µs (or more during a long fputs/fflush) inside a single
//      poll, and that's plenty to underrun the PL022 TX FIFO during
//      an active LTC xact (at ~781 kHz SCK an xact takes ~700 µs,
//      one byte = 1.28 µs, FIFO depth = 8 bytes = 10 µs of headroom).
//      An underrun causes the slave to drive 0xFF for the missed
//      bytes -- master gets a corrupted chunk and PEC fails for that
//      chip in that group. Gating USB on CS-high gives SPI dedicated
//      CPU during an xact while still keeping CDC responsive between
//      poll cycles.

#include "pico/stdlib.h"
#include "hardware/gpio.h"

#include "cell_state.h"
#include "ltc6811_emu.h"
#include "usb_cmd.h"

// Pin must match ltc6811_emu.c::PIN_SPI_CSN. Hard-coded here to avoid
// dragging the whole ltc6811_emu_init contract into main.
#define LTC_CSN_PIN  17

int main(void) {
    stdio_init_all();
    // No `while (!tud_cdc_connected())` -- the firmware must be
    // operational with or without a host CDC client attached, since
    // the AMS firmware's SPI polls don't care if the Pi has the
    // serial port open.

    cell_state_reset();
    ltc6811_emu_init();
    usb_cmd_init();

    // USB service rate-limit (IFS_HIL#44 conservative fix).
    //
    // The earlier `if (gpio_get(CSN)) usb_cmd_service();` gate failed
    // because tud_task() inside getchar_timeout_us can run for
    // hundreds of µs once entered — long enough that CS goes LOW
    // mid-call and the SPI TX FIFO (8 bytes ≈ 68 µs of headroom at
    // 937 kHz SCK) underruns, masking MISO. Diag confirmed: with USB
    // fully disabled, per-IC PEC drops from ~38/IC over 3 s to 0 for
    // all chips except chip 1's residual ~31 (separate issue).
    //
    // This rate-limit keeps CDC alive (BSL trigger + status queries
    // still respond, just slower) while bounding the rate of USB
    // service entries to ~once per SPI poll cycle. Combined with the
    // CS-HIGH gate it puts USB into a narrow window between AMS
    // polls (typically 50 ms - 5 ms = ~45 ms idle per cycle).
    //
    // USB_SERVICE_EVERY_N picked so AMS polls (5 ms xact, 50 ms
    // period) almost always finish before the next USB service slot
    // opens. usb_cmd_service was also tightened to one byte per call
    // (see usb_cmd.c) so each entry's tud_task() can't drag in a
    // multi-byte burst.
    #define USB_SERVICE_EVERY_N 256u
    uint32_t spi_ticks = 0;
    while (true) {
        // Always service SPI; never let it starve.
        ltc6811_emu_service();
        ++spi_ticks;
        if ((spi_ticks & (USB_SERVICE_EVERY_N - 1u)) == 0u
            && gpio_get(LTC_CSN_PIN)) {
            usb_cmd_service();
        }
    }

    return 0;
}
