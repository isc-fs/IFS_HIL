cmake_minimum_required(VERSION 3.13)

# Pico SDK path resolution: env var PICO_SDK_PATH, otherwise the
# system-default location set by the Pi SDK installer.
include(pico_sdk_import.cmake)

project(pico_ltc_emulator C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

add_executable(pico_ltc_emulator
    src/main.c
    src/usb_cmd.c
    src/pec15.c
    src/cell_state.c
    src/ltc6811_emu.c
)

target_include_directories(pico_ltc_emulator PRIVATE
    src
)

target_link_libraries(pico_ltc_emulator
    pico_stdlib
    pico_unique_id
    pico_bootrom        # reset_usb_boot()
    hardware_spi
    hardware_dma
    hardware_irq
    # TinyUSB is pulled in transitively by pico_stdio_usb (via
    # pico_enable_stdio_usb below) with the SDK's default CDC
    # descriptors. We don't need to link tinyusb_device directly.
)

# stdio_usb is what gives us the CDC printf channel; stdio_uart off
# because the Pico has no spare UART pads in our wiring.
pico_enable_stdio_usb(pico_ltc_emulator 1)
pico_enable_stdio_uart(pico_ltc_emulator 0)

pico_add_extra_outputs(pico_ltc_emulator)
