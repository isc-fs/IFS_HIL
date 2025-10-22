# Placeholder CMake configuration for STM32 firmware builds.
cmake_minimum_required(VERSION 3.16)

project(hil_firmware C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

add_executable(hil_firmware
    src/main.c
)

target_include_directories(hil_firmware
    PRIVATE
        ${CMAKE_CURRENT_LIST_DIR}/include
)
