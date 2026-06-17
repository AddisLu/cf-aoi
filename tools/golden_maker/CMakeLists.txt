cmake_minimum_required(VERSION 3.18)
project(golden_maker LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(OpenCV REQUIRED COMPONENTS core highgui imgproc imgcodecs)

add_executable(golden_maker golden_maker.cpp)

target_include_directories(golden_maker PRIVATE ${OpenCV_INCLUDE_DIRS})
target_link_libraries(golden_maker PRIVATE ${OpenCV_LIBS})
