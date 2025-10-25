# Generic cross toolchain for ARM (no MCU flags here)
set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_SYSTEM_PROCESSOR arm)

# Prefer the toolchain we installed in the image
find_program(ARM_GCC   NAMES arm-none-eabi-gcc PATHS /opt/toolchains/arm-gcc/bin NO_DEFAULT_PATH)
find_program(ARM_GPP   NAMES arm-none-eabi-g++ PATHS /opt/toolchains/arm-gcc/bin NO_DEFAULT_PATH)
find_program(ARM_OBJCOPY NAMES arm-none-eabi-objcopy PATHS /opt/toolchains/arm-gcc/bin NO_DEFAULT_PATH)
find_program(ARM_OBJDUMP NAMES arm-none-eabi-objdump PATHS /opt/toolchains/arm-gcc/bin NO_DEFAULT_PATH)
find_program(ARM_AR    NAMES arm-none-eabi-ar PATHS /opt/toolchains/arm-gcc/bin NO_DEFAULT_PATH)
find_program(ARM_RANLIB NAMES arm-none-eabi-ranlib PATHS /opt/toolchains/arm-gcc/bin NO_DEFAULT_PATH)

# Fallback to PATH if not found in the expected location
if(NOT ARM_GCC)
  find_program(ARM_GCC arm-none-eabi-gcc)
endif()
if(NOT ARM_GPP)
  find_program(ARM_GPP arm-none-eabi-g++)
endif()
if(NOT ARM_OBJCOPY)
  find_program(ARM_OBJCOPY arm-none-eabi-objcopy)
endif()
if(NOT ARM_OBJDUMP)
  find_program(ARM_OBJDUMP arm-none-eabi-objdump)
endif()
if(NOT ARM_AR)
  find_program(ARM_AR arm-none-eabi-ar)
endif()
if(NOT ARM_RANLIB)
  find_program(ARM_RANLIB arm-none-eabi-ranlib)
endif()

# Fail early with a clear message if compilers are missing
if(NOT ARM_GCC OR NOT ARM_GPP)
  message(FATAL_ERROR "arm-none-eabi toolchain not found. Ensure it is installed in /opt/toolchains/arm-gcc/bin or in PATH.")
endif()

# Compilers (absolute paths)
set(CMAKE_C_COMPILER   ${ARM_GCC})
set(CMAKE_CXX_COMPILER ${ARM_GPP})
set(CMAKE_ASM_COMPILER ${ARM_GCC})

# Binutils (absolute paths)
set(CMAKE_OBJCOPY ${ARM_OBJCOPY})
set(CMAKE_OBJDUMP ${ARM_OBJDUMP})
set(CMAKE_AR      ${ARM_AR})
set(CMAKE_RANLIB  ${ARM_RANLIB})

# Try-compile does not run on target
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

# Cross-compile search behavior
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)

# Default build type
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()