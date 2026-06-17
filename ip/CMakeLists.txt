cmake_minimum_required(VERSION 3.24)
project(cfaoi_ip LANGUAGES CXX CUDA)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# GPU 架構：預設 native；RTX 2080 Super=75，DGX Spark(GB10)=121
if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES)
    set(CMAKE_CUDA_ARCHITECTURES "native")
endif()

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
