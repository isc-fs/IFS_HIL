#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#include "usb_cmd.h"
#include "cell_state.h"
#include "ltc6811_emu.h"

#include "pico/stdlib.h"
#include "pico/bootrom.h"      // reset_usb_boot

#define LINE_MAX 96

#define FW_VERSION_STR "0.1.6"

usb_cmd_stats_t g_cmd_stats;

static char    line_buf[LINE_MAX];
static uint8_t line_len = 0;

static void emit(const char *s) {
    fputs(s, stdout);
    fflush(stdout);
}

static void emit_line(const char *s) {
    emit(s);
    emit("\n");
}

// Tokenise in-place. Returns argc; fills argv[0..argc-1] with pointers
// into `line` (which gets nul-terminated). Max 8 tokens.
static int tokenize(char *line, char *argv[], int max_argv) {
    int argc = 0;
    char *p = line;
    while (*p && argc < max_argv) {
        while (*p && isspace((unsigned char)*p)) p++;
        if (!*p) break;
        argv[argc++] = p;
        while (*p && !isspace((unsigned char)*p)) p++;
        if (*p) *p++ = '\0';
    }
    return argc;
}

// strtol with 0 base (accepts hex 0x, octal, decimal). Bounds-check
// optional -- caller validates.
static long parse_int(const char *s, int *ok) {
    char *end;
    long v = strtol(s, &end, 0);
    if (ok) *ok = (s != end && *end == '\0');
    return v;
}

static void cmd_ping(void) {
    emit_line("PONG " FW_VERSION_STR);
}

static void cmd_status(void) {
    char buf[224];
    snprintf(buf, sizeof(buf),
             "OK n_cmds_rx=%lu n_spi_xact=%lu last_cmd=0x%03X "
             "n_valid_cmds=%lu last_ltc_cmd=0x%03X n_cs_cycles=%lu "
             "rx_bytes=%lu last_rx=%02X %02X %02X %02X %02X %02X %02X %02X",
             (unsigned long)g_cmd_stats.n_cmds_rx,
             (unsigned long)g_ltc_stats.n_spi_xact,
             (unsigned)g_ltc_stats.last_cmd,
             (unsigned long)g_ltc_stats.n_valid_cmds,
             (unsigned)g_ltc_stats.last_ltc_cmd,
             (unsigned long)g_ltc_stats.n_cs_cycles,
             (unsigned long)g_ltc_stats.rx_byte_count,
             g_ltc_stats.last_rx[0], g_ltc_stats.last_rx[1],
             g_ltc_stats.last_rx[2], g_ltc_stats.last_rx[3],
             g_ltc_stats.last_rx[4], g_ltc_stats.last_rx[5],
             g_ltc_stats.last_rx[6], g_ltc_stats.last_rx[7]);
    emit_line(buf);
}

static void cmd_set_cell(int argc, char *argv[]) {
    if (argc != 4) { emit_line("ERR usage: SET_CELL m c mV"); return; }
    int ok = 1, ok2 = 1, ok3 = 1;
    long m  = parse_int(argv[1], &ok);
    long c  = parse_int(argv[2], &ok2);
    long mV = parse_int(argv[3], &ok3);
    if (!(ok && ok2 && ok3)) { emit_line("ERR bad int"); return; }
    if (m < 0 || m >= LTC_CHAIN_LEN || c < 0 || c >= CELLS_PER_LTC) {
        emit_line("ERR m in 0..9 c in 0..11"); return;
    }
    if (mV < 0 || mV > 0xFFFF) { emit_line("ERR mV in 0..65535"); return; }
    cell_state_set_cell((uint8_t)m, (uint8_t)c, (uint16_t)mV);
    ltc6811_emu_refresh_responses();
    emit_line("OK");
}

static void cmd_set_temp(int argc, char *argv[]) {
    if (argc != 4) { emit_line("ERR usage: SET_TEMP m s dC"); return; }
    int ok = 1, ok2 = 1, ok3 = 1;
    long m  = parse_int(argv[1], &ok);
    long s  = parse_int(argv[2], &ok2);
    long dC = parse_int(argv[3], &ok3);
    if (!(ok && ok2 && ok3)) { emit_line("ERR bad int"); return; }
    if (m < 0 || m >= LTC_CHAIN_LEN || s < 0 || s >= AUX_PER_LTC) {
        emit_line("ERR m in 0..9 s in 0..4"); return;
    }
    if (dC < INT16_MIN || dC > INT16_MAX) { emit_line("ERR dC i16"); return; }
    cell_state_set_temp((uint8_t)m, (uint8_t)s, (int16_t)dC);
    ltc6811_emu_refresh_responses();
    emit_line("OK");
}

static void cmd_set_all_cells(int argc, char *argv[]) {
    if (argc != 2) { emit_line("ERR usage: SET_ALL_CELLS mV"); return; }
    int ok = 1;
    long mV = parse_int(argv[1], &ok);
    if (!ok || mV < 0 || mV > 0xFFFF) { emit_line("ERR mV in 0..65535"); return; }
    cell_state_set_all_cells((uint16_t)mV);
    ltc6811_emu_refresh_responses();
    emit_line("OK");
}

static void cmd_set_all_temps(int argc, char *argv[]) {
    if (argc != 2) { emit_line("ERR usage: SET_ALL_TEMPS dC"); return; }
    int ok = 1;
    long dC = parse_int(argv[1], &ok);
    if (!ok || dC < INT16_MIN || dC > INT16_MAX) { emit_line("ERR dC i16"); return; }
    cell_state_set_all_temps((int16_t)dC);
    ltc6811_emu_refresh_responses();
    emit_line("OK");
}

static void cmd_reset_state(void) {
    cell_state_reset();
    ltc6811_emu_reset_stats();
    ltc6811_emu_refresh_responses();
    emit_line("OK");
}

static void cmd_dump_response(int argc, char *argv[]) {
    if (argc != 2) { emit_line("ERR usage: DUMP <cmd_hex>  (e.g. DUMP 0x004)"); return; }
    int ok = 1;
    long c = parse_int(argv[1], &ok);
    if (!ok) { emit_line("ERR bad int"); return; }
    const uint8_t *buf = ltc6811_emu_response_for((uint16_t)c);
    if (!buf) { emit_line("ERR unknown read cmd"); return; }
    char line[256];
    int n = snprintf(line, sizeof(line), "OK cmd=0x%03X len=%d bytes=", (unsigned)c, LTC_RESPONSE_LEN);
    for (int i = 0; i < LTC_RESPONSE_LEN; i++) {
        n += snprintf(line + n, sizeof(line) - n, "%02X%s",
                      buf[i], (i == LTC_RESPONSE_LEN - 1) ? "" : " ");
        if (n >= (int)sizeof(line) - 4) break;
    }
    emit_line(line);
}

// Print bytes the SPI slave actually pushed to its TX FIFO during the
// last completed transaction. Compare against DUMP <cmd> to see if the
// streamer's wire-side output matches the intended response buffer.
// Output: "OK cmd=0xNNN len=N bytes=BB BB BB ...".
static void cmd_dump_tx(void) {
    uint16_t cmd = 0xFFFFu, len = 0;
    const uint8_t *buf = ltc6811_emu_last_tx_snapshot(&cmd, &len);
    char line[400];
    int n = snprintf(line, sizeof(line), "OK cmd=0x%03X len=%u bytes=",
                     (unsigned)cmd, (unsigned)len);
    for (int i = 0; i < len; i++) {
        n += snprintf(line + n, sizeof(line) - n, "%02X%s",
                      buf[i], (i == len - 1) ? "" : " ");
        if (n >= (int)sizeof(line) - 4) break;
    }
    emit_line(line);
}

static void cmd_bsl(void) {
    emit_line("OK -- bye");
    // Flush stdio_usb then jump to bootloader. The first arg is
    // "GPIO-pin mask for activity LED" (0 = none), second arg is
    // "disable interface mask" (0 = enable both PICOBOOT + MSC).
    sleep_ms(50);   // let CDC drain
    reset_usb_boot(0, 0);
    // Unreached.
}

// Case-insensitive prefix match. cmd is the canonical form.
static int eq(const char *a, const char *cmd) {
    while (*a && *cmd) {
        char ca = (*a >= 'a' && *a <= 'z') ? (char)(*a - 32) : *a;
        if (ca != *cmd) return 0;
        a++; cmd++;
    }
    return *a == *cmd;
}

static void dispatch(char *line) {
    char *argv[8];
    int argc = tokenize(line, argv, 8);
    if (argc == 0) return;
    g_cmd_stats.n_cmds_rx++;

    if      (eq(argv[0], "PING"))           cmd_ping();
    else if (eq(argv[0], "STATUS"))         cmd_status();
    else if (eq(argv[0], "SET_CELL"))       cmd_set_cell(argc, argv);
    else if (eq(argv[0], "SET_TEMP"))       cmd_set_temp(argc, argv);
    else if (eq(argv[0], "SET_ALL_CELLS"))  cmd_set_all_cells(argc, argv);
    else if (eq(argv[0], "SET_ALL_TEMPS"))  cmd_set_all_temps(argc, argv);
    else if (eq(argv[0], "RESET_STATE"))    cmd_reset_state();
    else if (eq(argv[0], "DUMP"))           cmd_dump_response(argc, argv);
    else if (eq(argv[0], "DUMP_TX"))        cmd_dump_tx();
    else if (eq(argv[0], "BSL"))            cmd_bsl();
    else                                    emit_line("ERR unknown cmd");
}

void usb_cmd_init(void) {
    g_cmd_stats.n_cmds_rx = 0;
    line_len = 0;
}

void usb_cmd_service(void) {
    int c;
    while ((c = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) {
        if (c == '\r') continue;
        if (c == '\n') {
            line_buf[line_len] = '\0';
            dispatch(line_buf);
            line_len = 0;
            continue;
        }
        if (line_len < LINE_MAX - 1) {
            line_buf[line_len++] = (char)c;
        } else {
            // Overflow -- drop and reset to avoid corruption.
            line_len = 0;
            emit_line("ERR line overflow");
        }
    }
}
