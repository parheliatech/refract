# For more information about using CMake with Android Studio, read the
# documentation: https://d.android.com/studio/projects/add-native-code.html

# Sets the minimum version of CMake required to build the native library.

cmake_minimum_required(VERSION 3.10.2)

project(viture_sdk_demo VERSION 1.0.1)
set(LIB_NAME viture_sdk)

include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)


add_library (
	${LIB_NAME}
        SHARED 
	IMPORTED)

set_target_properties(
	${LIB_NAME} PROPERTIES 
	IMPORTED_LOCATION 
        ${CMAKE_CURRENT_SOURCE_DIR}/libs/libviture_one_sdk.so)

add_executable(${PROJECT_NAME} ${CMAKE_CURRENT_SOURCE_DIR}/src/main.c)

target_link_libraries(${PROJECT_NAME}
        ${LIB_NAME}
		m)
