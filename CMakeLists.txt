# CMakeLists.txt (更新版)
cmake_minimum_required(VERSION 3.10)
project(LEO_TE_Engine)
set(CMAKE_CXX_STANDARD 17)

find_path(HIREDIS_INCLUDE_DIR hiredis/hiredis.h)
find_library(HIREDIS_LIBRARY hiredis)
# 寻找 yaml-cpp
find_package(yaml-cpp REQUIRED)

include_directories(${HIREDIS_INCLUDE_DIR})

add_executable(te_engine 
    cpp_te_engine/main.cpp
    cpp_te_engine/redis_client.cpp
    cpp_te_engine/topo_graph.cpp
    cpp_te_engine/routing_algo.cpp
)
target_link_libraries(te_engine ${HIREDIS_LIBRARY} yaml-cpp)