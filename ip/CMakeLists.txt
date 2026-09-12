cmake_minimum_required(VERSION 3.24)
# GPU 架構：以 nvcc 內附的 __nvcc_device_query 實測本機 GPU compute capability。
# 必須在 project() 之前設定並寫進 cache——CMake 在 enable_language(CUDA) 時就會把
# CMAKE_CUDA_ARCHITECTURES 定義成自己偵測的值（3.28 不認得 Blackwell，RTX 5070 會誤判成 75）。
# RTX 2080 Super=75、RTX 5070=120、DGX Spark(GB10)=121；可用 -DCMAKE_CUDA_ARCHITECTURES=<n> 覆寫。
if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES)
    find_program(_cfaoi_nvcc nvcc HINTS ENV CUDA_PATH PATHS /usr/local/cuda/bin)
    if(_cfaoi_nvcc)
        get_filename_component(_cfaoi_cuda_bin "${_cfaoi_nvcc}" DIRECTORY)
        # VS Code 的 task 走非互動 shell，.bashrc 的 CUDA PATH 不會生效 → project() 找不到
        # nvcc。這裡把找到的 nvcc 直接指定給 CMake，讓 build task 不必依賴 shell PATH。
        if(NOT DEFINED CMAKE_CUDA_COMPILER)
            set(CMAKE_CUDA_COMPILER "${_cfaoi_nvcc}" CACHE FILEPATH "自動尋得的 nvcc")
        endif()
        execute_process(COMMAND "${_cfaoi_cuda_bin}/__nvcc_device_query"
                        OUTPUT_VARIABLE _cfaoi_cc
                        RESULT_VARIABLE _cfaoi_cc_rc
                        OUTPUT_STRIP_TRAILING_WHITESPACE
                        ERROR_QUIET)
        string(REGEX MATCH "[0-9]+" _cfaoi_cc "${_cfaoi_cc}")   # 多 GPU 時取第一張
        if(_cfaoi_cc_rc EQUAL 0 AND _cfaoi_cc)
            set(CMAKE_CUDA_ARCHITECTURES "${_cfaoi_cc}" CACHE STRING "本機實測 GPU 架構")
            message(STATUS "CUDA arch (auto-detect) = ${_cfaoi_cc}")
        endif()
    endif()
endif()

project(cfaoi_ip LANGUAGES CXX CUDA)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(CUDAToolkit REQUIRED)
find_package(OpenCV REQUIRED COMPONENTS core imgproc imgcodecs)
find_package(nlohmann_json REQUIRED)
find_package(fmt REQUIRED)
find_package(Threads REQUIRED)

# ONNX Runtime（offline AI，x86）— 可選
find_path(ORT_INCLUDE onnxruntime_cxx_api.h PATHS /opt/onnxruntime/include QUIET)
find_library(ORT_LIB onnxruntime PATHS /opt/onnxruntime/lib QUIET)
if(ORT_LIB)
    add_compile_definitions(CFAOI_HAS_ORT)
    message(STATUS "ONNX Runtime found: ${ORT_LIB}")
else()
    message(STATUS "ONNX Runtime not found → AI offline backend disabled")
endif()

# libibverbs + librdmacm（rdma-validate/image-capture/online）— 本階段通常無
find_library(IBVERBS_LIB  ibverbs  QUIET)
find_library(RDMACM_LIB   rdmacm   QUIET)
if(IBVERBS_LIB AND RDMACM_LIB)
    add_compile_definitions(CFAOI_HAS_RDMA)
    message(STATUS "RDMA enabled (ibverbs=${IBVERBS_LIB} rdmacm=${RDMACM_LIB}) → Step 2-5 modes available")
elseif(IBVERBS_LIB)
    add_compile_definitions(CFAOI_HAS_RDMA)
    message(STATUS "RDMA enabled (ibverbs only, rdmacm not found) → Step 2-5 modes available")
else()
    message(STATUS "No RDMA → Step 1 (offline) only")
endif()

add_executable(cfaoi_ip
    src/main.cpp
    src/gpu/gpu_pipeline.cpp
    src/gpu/cuda_kernels.cu          # ← 直接複製來的，不改
    src/ai/ai_kernels.cu             # ← 直接複製來的（tensor_core_classifier.cu），不改
    src/config/zone_config_adapter.cpp
    src/diag/flight_recorder.cpp
    src/image_source/file_source.cpp
    src/image_source/tcp_source.cpp
    src/result_saver.cpp
    src/control_server.cpp
    src/align_engine.cpp
    src/edge_check.cpp
    $<$<BOOL:${IBVERBS_LIB}>:src/image_source/rdma_source.cpp>
)

target_include_directories(cfaoi_ip PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/src/gpu
    ${CMAKE_CURRENT_SOURCE_DIR}/src/ai
    ${CMAKE_CURRENT_SOURCE_DIR}/src/config
    ${CMAKE_CURRENT_SOURCE_DIR}/src/image_source
    ${CMAKE_CURRENT_SOURCE_DIR}/../shared
    ${OpenCV_INCLUDE_DIRS}
    $<$<BOOL:${ORT_INCLUDE}>:${ORT_INCLUDE}>
)

target_link_libraries(cfaoi_ip PRIVATE
    CUDA::cudart
    CUDA::cublas
    ${OpenCV_LIBS}
    nlohmann_json::nlohmann_json
    fmt::fmt
    Threads::Threads
    $<$<BOOL:${ORT_LIB}>:${ORT_LIB}>
    $<$<BOOL:${IBVERBS_LIB}>:${IBVERBS_LIB}>
    $<$<BOOL:${RDMACM_LIB}>:${RDMACM_LIB}>
)

set_property(TARGET cfaoi_ip PROPERTY CUDA_SEPARABLE_COMPILATION ON)

# Debug build 才加 -G（device debug symbols，cuda-gdb 斷點/單步用）；
# 會關掉大部分 device 端優化，效能數字（不變式 15）不可拿 Debug build 量測。
target_compile_options(cfaoi_ip PRIVATE
    $<$<AND:$<CONFIG:Debug>,$<COMPILE_LANGUAGE:CUDA>>:-G;-g>
)

# ── crc_verify（純 CPU；驗 ARM64 硬體 CRC32 與表格版位元級等價 = wire 相容守門）──
add_executable(crc_verify src/crc_verify.cpp)
target_include_directories(crc_verify PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_compile_options(crc_verify PRIVATE -std=c++17 -O2)

# ── align_verify（純 OpenCV，不需 CUDA；Gap #1 精度驗證）──────────────────
add_executable(align_verify
    src/align_verify.cpp
    src/align_engine.cpp
)
target_include_directories(align_verify PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/src/config
    ${OpenCV_INCLUDE_DIRS}
)
target_link_libraries(align_verify PRIVATE ${OpenCV_LIBS})
target_compile_options(align_verify PRIVATE -std=c++17)

# ── coord_verify（純 CPU，不需 CUDA/OpenCV；Gap #5 pixel→μm 換算驗證）─────────
add_executable(coord_verify
    src/coord_verify.cpp
    src/config/zone_config_adapter.cpp
)
target_include_directories(coord_verify PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/src/config
)
target_link_libraries(coord_verify PRIVATE)
target_compile_options(coord_verify PRIVATE -std=c++17)

# ── edge_verify（純 OpenCV，不需 CUDA；玻璃前緣/尾緣健檢 Stage 1 合成驗證）────────
add_executable(edge_verify
    src/edge_verify.cpp
    src/edge_check.cpp
)
target_include_directories(edge_verify PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${OpenCV_INCLUDE_DIRS}
)
target_link_libraries(edge_verify PRIVATE ${OpenCV_LIBS})
target_compile_options(edge_verify PRIVATE -std=c++17)

# ── rules_verify（純 OpenCV，不需 CUDA；#16 Rule 改判 + #32 邊界略過 單元驗證）────
add_executable(rules_verify
    src/rules_verify.cpp
)
target_include_directories(rules_verify PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/src/config
    ${OpenCV_INCLUDE_DIRS}
)
target_link_libraries(rules_verify PRIVATE ${OpenCV_LIBS})
target_compile_options(rules_verify PRIVATE -std=c++17)
