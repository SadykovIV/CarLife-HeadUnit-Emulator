cmake_minimum_required(VERSION 3.18)
project(hsd_hicar_auth C)
set(CMAKE_C_STANDARD 11)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(SOURCE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/sources")
set(AUTH "${SOURCE_ROOT}/deviceauth/frameworks/deviceauth_lite/source")
set(HUKS "${SOURCE_ROOT}/huks/frameworks/huks_lite/source/hw_keystore_sdk")
set(SECUREC "${SOURCE_ROOT}/securec")
file(GLOB_RECURSE AUTH_C CONFIGURE_DEPENDS "${AUTH}/*.c")
file(GLOB_RECURSE HUKS_C CONFIGURE_DEPENDS "${HUKS}/*.c")
file(GLOB SECUREC_C CONFIGURE_DEPENDS "${SECUREC}/src/*.c")
if(NOT AUTH_C OR NOT HUKS_C OR NOT SECUREC_C)
  message(FATAL_ERROR "Missing pinned native sources")
endif()

if(WIN32)
  set(SECUREC_C "${SECUREC}/src/memset_s.c" "${SECUREC}/src/snprintf_s.c"
      )
endif()
find_path(CJSON_INCLUDE cJSON.h PATH_SUFFIXES cjson REQUIRED)
find_library(CJSON_LIB cjson REQUIRED)
find_library(MBEDCRYPTO_LIB mbedcrypto REQUIRED)
find_path(MBEDTLS_INCLUDE mbedtls/version.h REQUIRED)
add_library(hsd_hicar_auth SHARED compat.c ${AUTH_C} ${HUKS_C} ${SECUREC_C})
target_include_directories(hsd_hicar_auth PRIVATE
  "${SOURCE_ROOT}/huks/interfaces/innerkits/huks_lite" "${HUKS}/.."
  "${CRYPTO_INCLUDE}" compat "${SECUREC}/include" "${SOURCE_ROOT}/deviceauth/interfaces/innerkits/deviceauth_lite"
  "${AUTH}/base" "${AUTH}/auth_info" "${AUTH}/huks_adapter" "${AUTH}/json"
  "${AUTH}/key_agreement" "${AUTH}/log" "${AUTH}/schedule" "${AUTH}/struct"
  "${SOURCE_ROOT}/huks/interfaces/innerkits/huks_lite" "${HUKS}" "${HUKS}/common"
  "${HUKS}/soft_service" "${CJSON_INCLUDE}" "${MBEDTLS_INCLUDE}")
target_compile_definitions(hsd_hicar_auth PRIVATE _CUT_LOG_ _POSIX_C_SOURCE=200809L)
target_link_libraries(hsd_hicar_auth PRIVATE "${CRYPTO_LIB}" "${CJSON_LIB}" "${MBEDCRYPTO_LIB}")
if(WIN32)
  target_sources(hsd_hicar_auth PRIVATE exports.def)
  # SecureC supplies the bounds-checking APIs used by HUKS on every platform.
  target_compile_definitions(hsd_hicar_auth PRIVATE SECUREC_SNPRINTF_TRUNCATED=0 HKS_DLL_EXPORT DLL_EXPORT SECUREC_COMPATIBLE_LINUX_FORMAT)
  target_link_libraries(hsd_hicar_auth PRIVATE bcrypt crypt32 ws2_32 advapi32)
  set_target_properties(hsd_hicar_auth PROPERTIES PREFIX "" WINDOWS_EXPORT_ALL_SYMBOLS ON)
endif()
