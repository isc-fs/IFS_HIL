// USB CDC command interface. Line-oriented, NL-terminated. See
// docs/pico_ltc_emulator.md for the protocol.

#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t n_cmds_rx;
} usb_cmd_stats_t;

extern usb_cmd_stats_t g_cmd_stats;

void usb_cmd_init(void);

// Pull pending bytes off stdio_usb, dispatch full lines. Call from
// the main loop; non-blocking.
void usb_cmd_service(void);

#ifdef __cplusplus
}
#endif
