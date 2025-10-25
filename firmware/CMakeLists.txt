cmake_minimum_required(VERSION 3.22)
project(firmware C ASM)

# --------------------------------------------------------------------
# Toolchain is provided by docker/toolchain-arm-none-eabi.cmake
# Do NOT put -mcpu/-mfpu/-mfloat-abi in the toolchain; keep it here.
# --------------------------------------------------------------------
enable_language(C ASM)

# ---------------- MCU / ABI (H7 = Cortex-M7 with single-precision FPU) --
set(CPU cortex-m7)
set(FPU fpv5-sp-d16)
set(ABI hard)

# ---------------- Paths ---------------------------------------------------
set(PROJ_DIR        ${CMAKE_CURRENT_SOURCE_DIR})
set(DRIVERS_DIR     ${PROJ_DIR}/Drivers)
set(HAL_DIR         ${DRIVERS_DIR}/STM32H7xx_HAL_Driver)
set(CMSIS_DEV_DIR   ${DRIVERS_DIR}/CMSIS/Device/ST/STM32H7xx)
set(CMSIS_CORE_DIR  ${DRIVERS_DIR}/CMSIS/Include)

# ---------------- Linker script selection --------------------------------
# Option 1 (default): auto-pick CubeIDE scripts by variant
# Option 2: override with -DLINKER_SCRIPT=/abs/path/to/your.ld
set(LD_VARIANT "FLASH" CACHE STRING "Linker script variant: FLASH or RAM")
set_property(CACHE LD_VARIANT PROPERTY STRINGS FLASH RAM)

set(_LD_CUBE_FLASH "${PROJ_DIR}/STM32H733ZGTX_FLASH.ld")
set(_LD_CUBE_RAM   "${PROJ_DIR}/STM32H733ZGTX_RAM.ld")
set(LINKER_SCRIPT  "" CACHE FILEPATH "Override linker script path (leave empty to auto-pick by LD_VARIANT)")

if(NOT LINKER_SCRIPT)
  if(LD_VARIANT STREQUAL "FLASH")
    set(LINKER_SCRIPT "${_LD_CUBE_FLASH}")
  elseif(LD_VARIANT STREQUAL "RAM")
    set(LINKER_SCRIPT "${_LD_CUBE_RAM}")
  else()
    message(FATAL_ERROR "Unknown LD_VARIANT='${LD_VARIANT}'. Use FLASH or RAM.")
  endif()
endif()

if(NOT EXISTS "${LINKER_SCRIPT}")
  message(FATAL_ERROR "Linker script not found at: ${LINKER_SCRIPT}")
endif()

# ---------------- Sources -------------------------------------------------
set(SRC_APP
  ${PROJ_DIR}/src/main.c
  ${PROJ_DIR}/src/system_stm32h7xx.c
  ${PROJ_DIR}/startup/startup_stm32h733xx.s
)

# Either list required HAL .c files or take them all (simple & safe)
file(GLOB SRC_HAL ${HAL_DIR}/Src/*.c)

add_executable(${PROJECT_NAME}
  ${SRC_APP}
  ${SRC_HAL}
)

# ---------------- Includes ------------------------------------------------
target_include_directories(${PROJECT_NAME} PRIVATE
  ${PROJ_DIR}/src
  ${PROJ_DIR}/Inc
  ${HAL_DIR}/Inc
  ${HAL_DIR}/Inc/Legacy
  ${CMSIS_DEV_DIR}/Include
  ${CMSIS_CORE_DIR}
)

# ---------------- Preprocessor defs --------------------------------------
target_compile_definitions(${PROJECT_NAME} PRIVATE
  USE_HAL_DRIVER
  STM32H733xx
  HSE_VALUE=24000000UL   # adjust to your board if using HSE
  HSI_VALUE=64000000UL
  LSE_VALUE=32768UL
  LSI_VALUE=32000UL
)

# ---------------- Compiler & Linker flags (as LISTS, not quoted!) --------
set(COMMON_FLAGS
  -mthumb
  -ffunction-sections
  -fdata-sections
  -fno-builtin
  -mcpu=${CPU}
  -mfpu=${FPU}
  -mfloat-abi=${ABI}
)

target_compile_options(${PROJECT_NAME} PRIVATE
  ${COMMON_FLAGS}
  -Wall -Wextra -Os
)

# (Optional) make sure startup .s is assembled with cpp if needed
set_source_files_properties(${PROJ_DIR}/startup/startup_stm32h733xx.s PROPERTIES
  LANGUAGE ASM
)

target_link_options(${PROJECT_NAME} PRIVATE
  ${COMMON_FLAGS}
  -Wl,--gc-sections
  -T${LINKER_SCRIPT}                       # exactly one -T
  -Wl,-Map,${CMAKE_BINARY_DIR}/${PROJECT_NAME}.map
)

# ---------------- Outputs -------------------------------------------------
add_custom_command(TARGET ${PROJECT_NAME} POST_BUILD
  COMMAND ${CMAKE_OBJCOPY} -O ihex   $<TARGET_FILE:${PROJECT_NAME}> ${CMAKE_BINARY_DIR}/${PROJECT_NAME}.hex
  COMMAND ${CMAKE_OBJCOPY} -O binary $<TARGET_FILE:${PROJECT_NAME}> ${CMAKE_BINARY_DIR}/${PROJECT_NAME}.bin
  COMMENT "Generating HEX and BIN"
)