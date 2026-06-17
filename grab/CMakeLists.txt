# =============================================================================
# grab/CMakeLists.txt  —  cfaoi_grab 建置腳本
# -----------------------------------------------------------------------------
# 依賴：
#   pylon SDK      (/opt/pylon 或 PYLON_ROOT 環境變數)
#   libibverbs     (apt install libibverbs-dev)
#   librdmacm      (apt install librdmacm-dev)
#   nlohmann/json  (apt install nlohmann-json3-dev)
# =============================================================================
cmake_minimum_required(VERSION 3.18)
project(cfaoi_grab CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# shared/ FrameHeader.h 路徑（grab/ 的上一層）
include_directories(${CMAKE_SOURCE_DIR}/..)

# =============================================================================
# 1. RDMA（libibverbs + librdmacm）
# =============================================================================
find_library(IBVERBS_LIB ibverbs)
find_library(RDMACM_LIB  rdmacm)
if(NOT IBVERBS_LIB OR NOT RDMACM_LIB)
  message(FATAL_ERROR "找不到 libibverbs / librdmacm。請安裝：\n"
          "  sudo apt install libibverbs-dev librdmacm-dev")
endif()
set(RDMA_LIBS ${RDMACM_LIB} ${IBVERBS_LIB} pthread)
message(STATUS "[OK] RDMA libs: ${IBVERBS_LIB} ${RDMACM_LIB}")

# =============================================================================
# 2. nlohmann/json（ControlServer 解析 JSON 命令用）
# =============================================================================
find_package(nlohmann_json QUIET)
if(NOT nlohmann_json_FOUND)
  # 降級：手動找 include（apt install nlohmann-json3-dev 裝在 /usr/include）
  find_path(JSON_INCLUDE_DIR nlohmann/json.hpp
            HINTS /usr/include /usr/local/include)
  if(NOT JSON_INCLUDE_DIR)
    message(FATAL_ERROR "找不到 nlohmann/json.hpp。請安裝：\n"
            "  sudo apt install nlohmann-json3-dev")
  endif()
  include_directories(${JSON_INCLUDE_DIR})
  message(STATUS "[OK] nlohmann/json include: ${JSON_INCLUDE_DIR}")
endif()

# =============================================================================
# 3. pylon SDK（Basler raL8192-12gm GigE 相機）
# =============================================================================
set(HAVE_PYLON FALSE)

# 方法 a：find_package（pylon 6.2+ 有 CMake config，裝在 /opt/pylon）
if(EXISTS "/opt/pylon")
  list(APPEND CMAKE_PREFIX_PATH "/opt/pylon")
endif()
find_package(pylon QUIET)
if(pylon_FOUND)
  set(HAVE_PYLON TRUE)
  message(STATUS "[OK] pylon（find_package）")
endif()

# 方法 b：PYLON_ROOT 環境變數
if(NOT HAVE_PYLON AND DEFINED ENV{PYLON_ROOT} AND EXISTS "$ENV{PYLON_ROOT}/include")
  set(HAVE_PYLON TRUE)
  include_directories($ENV{PYLON_ROOT}/include)
  link_directories($ENV{PYLON_ROOT}/lib)
  message(STATUS "[OK] pylon（PYLON_ROOT=$ENV{PYLON_ROOT}）")
endif()

# 方法 c：pylon-config 指令
if(NOT HAVE_PYLON)
  find_program(PYLON_CONFIG pylon-config HINTS /opt/pylon/bin)
  if(PYLON_CONFIG)
    set(HAVE_PYLON TRUE)
    execute_process(COMMAND ${PYLON_CONFIG} --cflags OUTPUT_VARIABLE PYLON_CFLAGS OUTPUT_STRIP_TRAILING_WHITESPACE)
    execute_process(COMMAND ${PYLON_CONFIG} --libs   OUTPUT_VARIABLE PYLON_LFLAGS OUTPUT_STRIP_TRAILING_WHITESPACE)
    separate_arguments(PYLON_CFLAGS_LIST NATIVE_COMMAND "${PYLON_CFLAGS}")
    add_compile_options(${PYLON_CFLAGS_LIST})
    set(PYLON_LINK_FLAGS ${PYLON_LFLAGS})
    message(STATUS "[OK] pylon（pylon-config）")
  endif()
endif()

if(NOT HAVE_PYLON)
  message(FATAL_ERROR "找不到 pylon SDK。請從 baslerweb.com 安裝後設 PYLON_ROOT 或放在 /opt/pylon。")
endif()

# =============================================================================
# 目標：cfaoi_grab
# =============================================================================
set(GRAB_SOURCES
    src/main.cpp
    src/cam_pylon.cpp
    src/rdma_sender.cpp
    src/control_server.cpp
)

add_executable(cfaoi_grab ${GRAB_SOURCES})

# RDMA + pthread
target_link_libraries(cfaoi_grab PRIVATE ${RDMA_LIBS})

# nlohmann/json（若 find_package 成功）
if(nlohmann_json_FOUND)
  target_link_libraries(cfaoi_grab PRIVATE nlohmann_json::nlohmann_json)
endif()

# pylon
if(pylon_FOUND)
  target_link_libraries(cfaoi_grab PRIVATE pylon::pylon)
elseif(DEFINED PYLON_LINK_FLAGS)
  # pylon-config 回傳的是字串，要用 target_link_options
  separate_arguments(PYLON_LINK_LIST NATIVE_COMMAND "${PYLON_LINK_FLAGS}")
  target_link_options(cfaoi_grab PRIVATE ${PYLON_LINK_LIST})
else()
  # PYLON_ROOT 路徑，手動連結常見函式庫
  target_link_libraries(cfaoi_grab PRIVATE
      pylonbase GCBase_gcc_v3_1_Basler_pylon GenApi_gcc_v3_1_Basler_pylon)
endif()

message(STATUS "")
message(STATUS "==== cfaoi_grab 建置目標 ====")
message(STATUS "  src/main.cpp + cam_pylon + rdma_sender + control_server")
message(STATUS "  ctrl port : 8100 (預設)")
message(STATUS "  rdma dest : --rdma-dest IP:18515")
message(STATUS "")

# =============================================================================
# 目標：rdma_nslot_test（Step 3 N-slot 驗證用合成幀送器，不需 pylon/eBUS）
# =============================================================================
add_executable(rdma_nslot_test
    src/rdma_nslot_test.cpp
    src/rdma_sender.cpp
)
target_link_libraries(rdma_nslot_test PRIVATE ${RDMA_LIBS})
if(nlohmann_json_FOUND)
  target_link_libraries(rdma_nslot_test PRIVATE nlohmann_json::nlohmann_json)
endif()
message(STATUS "==== rdma_nslot_test（Step 3 合成幀送器，無相機）====")
message(STATUS "  用法: rdma_nslot_test <server_ip> <port> <num_frames> [width] [height] [delay_ms]")

# =============================================================================
# 目標：probe_cam_nodes（Gap #2 Stage 0 — GenICam 節點探測工具，僅需 pylon）
# =============================================================================
add_executable(probe_cam_nodes src/probe_cam_nodes.cpp)

if(pylon_FOUND)
  target_link_libraries(probe_cam_nodes PRIVATE pylon::pylon)
elseif(DEFINED PYLON_LINK_FLAGS)
  separate_arguments(PYLON_LINK_LIST_P NATIVE_COMMAND "${PYLON_LINK_FLAGS}")
  target_link_options(probe_cam_nodes PRIVATE ${PYLON_LINK_LIST_P})
else()
  target_link_libraries(probe_cam_nodes PRIVATE
      pylonbase GCBase_gcc_v3_1_Basler_pylon GenApi_gcc_v3_1_Basler_pylon)
endif()

message(STATUS "==== probe_cam_nodes（Gap #2 Stage 0，連相機查 GenICam 節點）====")
message(STATUS "  用法: probe_cam_nodes [serial_number]")

# =============================================================================
# 目標：cam_mean_gray_test（Gap #2 Stage 2+3 — 曝光/增益 → mean gray 單調性驗證）
# =============================================================================
add_executable(cam_mean_gray_test src/cam_mean_gray_test.cpp src/cam_pylon.cpp)

if(pylon_FOUND)
  target_link_libraries(cam_mean_gray_test PRIVATE pylon::pylon)
elseif(DEFINED PYLON_LINK_FLAGS)
  separate_arguments(PYLON_LINK_LIST_P2 NATIVE_COMMAND "${PYLON_LINK_FLAGS}")
  target_link_options(cam_mean_gray_test PRIVATE ${PYLON_LINK_LIST_P2})
else()
  target_link_libraries(cam_mean_gray_test PRIVATE
      pylonbase GCBase_gcc_v3_1_Basler_pylon GenApi_gcc_v3_1_Basler_pylon)
endif()

message(STATUS "==== cam_mean_gray_test（Gap #2 Stage 2+3，曝光/增益 mean gray 驗證）====")
message(STATUS "  用法: cam_mean_gray_test [serial_number]")
