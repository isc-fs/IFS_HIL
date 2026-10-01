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

#define FW_VERSION_STR "0.7.0"   // byte-2 cmd parse, chip 0 no longer stale (IFS_HIL#116)

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
    char buf[256];
    snprintf(buf, sizeof(buf),
             "OK n_cmds_rx=%lu n_spi_xact=%lu last_cmd=0x%03X "
             "n_valid_cmds=%lu last_ltc_cmd=0x%03X n_cs_cycles=%lu "
             "rx_bytes=%lu tx_stall_cmd=%lu stop_mask=0x%02X adg731_ch=%u "
             "last_rx=%02X %02X %02X %02X %02X %02X %02X %02X",
             (unsigned long)g_cmd_stats.n_cmds_rx,
             (unsigned long)g_ltc_stats.n_spi_xact,
             (unsigned)g_ltc_stats.last_cmd,
             (unsigned long)g_ltc_stats.n_valid_cmds,
             (unsigned)g_ltc_stats.last_ltc_cmd,
             (unsigned long)g_ltc_stats.n_cs_cycles,
             (unsigned long)g_ltc_stats.rx_byte_count,
             (unsigned long)g_ltc_stats.n_tx_stall_cmd,
             (unsigned)ltc6811_emu_get_stop_mask(),
             (unsigned)ltc6811_emu_get_adg731_ch(),
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
        emit_line("ERR m in 0..9 s in 0..31 (ADG731 mux channel)"); return;
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
    ltc6811_emu_set_stop_mask(0u);          // RESET_STATE also drops any stop-mask
    ltc6811_emu_refresh_responses();
    emit_line("OK");
}

// STOP_REPLY <module_mask>  -- 5-bit per-module mask, bit N silences
// LTC chain positions 2N and 2N+1 (= AMS module N). Bytes for the
// silenced positions are replaced with 0xFF on the wire, which fails
// PEC15 at the master and trips the BMS-stale predicate within
// `BmsStaleMs`. Used by Block E E-065 + B-026 fault-injection tests.
static void cmd_stop_reply(int argc, char *argv[]) {
    if (argc != 2) { emit_line("ERR usage: STOP_REPLY <mask 0x00..0x1F>"); return; }
    int ok = 1;
    long mask = parse_int(argv[1], &ok);
    if (!ok || mask < 0 || mask > 0x1F) {
        emit_line("ERR mask 0x00..0x1F (5 bits, one per AMS module)");
        return;
    }
    ltc6811_emu_set_stop_mask((uint8_t)mask);
    emit_line("OK");
}

// RESUME_ALL  -- shorthand for STOP_REPLY 0. Doesn't reset cell/temp
// state (use RESET_STATE for that) -- just resumes nominal SPI reply.
static void cmd_resume_all(void) {
    ltc6811_emu_set_stop_mask(0u);
    emit_line("OK");
}

static void cmd_stats_cmds(void) {
    char line[160];
    uint8_t n = ltc6811_emu_cmd_stats_count();
    snprintf(line, sizeof(line), "OK n=%u lost=%lu", n,
             (unsigned long)ltc6811_emu_cmd_stats_lost());
    emit_line(line);
    for (uint8_t i = 0; i < n; i++) {
        uint16_t cmd = 0, last = 0, mn = 0, mx = 0;
        uint32_t count = 0;
        ltc6811_emu_cmd_stats_get(i, &cmd, &count, &last, &mn, &mx);
        snprintf(line, sizeof(line),
                 "CMD 0x%03X count=%lu len_last=%u len_min=%u len_max=%u",
                 cmd, (unsigned long)count, last, mn, mx);
        emit_line(line);
    }
}

static void cmd_stats_cmds_reset(void) {
    ltc6811_emu_cmd_stats_reset();
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

// Print bytes the SPI slave actually RECEIVED on MOSI during the last
// completed transaction. First 4 bytes are master's cmd frame
// [cmd_hi, cmd_lo, pec_hi, pec_lo]; rest is write data (for writes) or
// 0xFF scratch (for reads). Use to distinguish "the master sent the
// cmd we think it did" from "our parse logic mis-extracted the cmd".
static void cmd_dump_rx(void) {
    uint16_t len = 0;
    const uint8_t *buf = ltc6811_emu_last_rx_snapshot(&len);
    char line[400];
    int n = snprintf(line, sizeof(line), "OK len=%u bytes=", (unsigned)len);
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
    else if (eq(argv[0], "STOP_REPLY"))     cmd_stop_reply(argc, argv);
    else if (eq(argv[0], "RESUME_ALL"))     cmd_resume_all();
    else if (eq(argv[0], "STATS_CMDS"))     cmd_stats_cmds();
    else if (eq(argv[0], "STATS_CMDS_RESET")) cmd_stats_cmds_reset();
    else if (eq(argv[0], "DUMP"))           cmd_dump_response(argc, argv);
    else if (eq(argv[0], "DUMP_TX"))        cmd_dump_tx();
    else if (eq(argv[0], "DUMP_RX"))        cmd_dump_rx();
    else if (eq(argv[0], "BSL"))            cmd_bsl();
    else                                    emit_line("ERR unknown cmd");
}

void usb_cmd_init(void) {
    g_cmd_stats.n_cmds_rx = 0;
    line_len = 0;
}

void usb_cmd_service(void) {
    // ONE getchar per call (IFS_HIL#44). The earlier while-loop kept
    // pulling bytes until the CDC RX buffer drained, with each call
    // to getchar_timeout_us(0) re-entering tud_task() inside the
    // pico-sdk. That could string together hundreds of µs of USB
    // work inside a single usb_cmd_service entry. Now main.c gates
    // entries to ~once per AMS poll cycle, AND each entry pulls at
    // most one byte -- bounds the worst-case tud_task latency per
    // entry to one TinyUSB poll round (~10-100 µs typical).
    //
    // CDC throughput drop is fine: host commands (SET_CELL, STATUS,
    // BSL, etc.) are 10-30 bytes each, so completing a command takes
    // a few main-loop iterations instead of one. AMS poll cycle is
    // 50 ms; even at the slowest service rate (1 byte / 5 ms idle
    // window) a 30-byte command lands in 150 ms, comfortably below
    // the test/operator-tool 1 s timeout.
    int c = getchar_timeout_us(0);
    if (c == PICO_ERROR_TIMEOUT) return;
    if (c == '\r') return;
    if (c == '\n') {
        line_buf[line_len] = '\0';
        dispatch(line_buf);
        line_len = 0;
        return;
    }
    if (line_len < LINE_MAX - 1) {
        line_buf[line_len++] = (char)c;
    } else {
        // Overflow -- drop and reset to avoid corruption.
        line_len = 0;
        emit_line("ERR line overflow");
    }
}
