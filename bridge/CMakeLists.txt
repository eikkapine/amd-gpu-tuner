cmake_minimum_required(VERSION 3.16)
project(voltshift_bridge CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(VOLTSHIFT_BRIDGE_VERSION "2.1.0")

# ---------------------------------------------------------------------------
# ADLX SDK location. The SDK provides headers only; the runtime
# (amdadlx64.dll) ships with the AMD Adrenalin driver and is loaded at
# runtime by ADLXHelper — no import library needed.
#
# Default: third_party/ADLX at the repo root (see README build guide).
# Override: cmake -DADLX_SDK_DIR=C:/path/to/ADLX
# ---------------------------------------------------------------------------
if(NOT DEFINED ADLX_SDK_DIR)
    set(ADLX_SDK_DIR "${CMAKE_SOURCE_DIR}/../third_party/ADLX" CACHE PATH "Path to AMD ADLX SDK root")
endif()

if(NOT EXISTS "${ADLX_SDK_DIR}/SDK/ADLXHelper/Windows/Cpp/ADLXHelper.h")
    message(FATAL_ERROR
        "ADLX SDK not found at: ${ADLX_SDK_DIR}\n"
        "Clone it with:\n"
        "  git clone --depth 1 https://github.com/GPUOpen-LibrariesAndSDKs/ADLX third_party/ADLX\n"
        "or pass -DADLX_SDK_DIR=<path to ADLX repo root>")
endif()

message(STATUS "Using ADLX SDK at: ${ADLX_SDK_DIR}")

add_executable(voltshift_bridge
    src/main.cpp
    src/session.cpp
    src/commands_core.cpp
    src/commands_tuning.cpp
    src/commands_gfx.cpp
    src/commands_display.cpp
    src/commands_extras.cpp
    # ADLX helper: loads amdadlx64.dll at runtime and exposes system services.
    "${ADLX_SDK_DIR}/SDK/ADLXHelper/Windows/Cpp/ADLXHelper.cpp"
    "${ADLX_SDK_DIR}/SDK/Platform/Windows/WinAPIs.cpp"
)

target_include_directories(voltshift_bridge PRIVATE
    "${ADLX_SDK_DIR}"
    "${CMAKE_SOURCE_DIR}/vendor"
)

target_compile_definitions(voltshift_bridge PRIVATE
    _WINDOWS
    WIN32_LEAN_AND_MEAN
    NOMINMAX
    VOLTSHIFT_BRIDGE_VERSION="${VOLTSHIFT_BRIDGE_VERSION}"
)

if(MSVC)
    target_compile_options(voltshift_bridge PRIVATE
        /utf-8
        /bigobj
        $<$<CONFIG:Release>:/O2 /W3>
        $<$<CONFIG:Debug>:/Od /W4 /Zi>
    )
endif()

set_target_properties(voltshift_bridge PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
)
