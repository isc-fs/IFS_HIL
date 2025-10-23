# CMake toolchain for STM32
set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_SYSTEM_PROCESSOR arm)

set(ARM_NONE_EABI arm-none-eabi)
set(CMAKE_C_COMPILER   ${ARM_NONE_EABI}-gcc)
set(CMAKE_CXX_COMPILER ${ARM_NONE_EABI}-g++)
set(CMAKE_ASM_COMPILER ${ARM_NONE_EABI}-gcc)

set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)
set(CMAKE_C_FLAGS_INIT   "-mcpu=cortex-m4 -mthumb -ffunction-sections -fdata-sections -fno-builtin")
set(CMAKE_CXX_FLAGS_INIT "-mcpu=cortex-m4 -mthumb -ffunction-sections -fdata-sections -fno-builtin -fno-exceptions -fno-rtti")
set(CMAKE_EXE_LINKER_FLAGS_INIT "-Wl,--gc-sections")
set(CMAKE_BUILD_TYPE_INIT Release)