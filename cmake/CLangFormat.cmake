find_program(CLANG_FORMAT NAMES "clang-format" "clang-format-3.6")
if(NOT CLANG_FORMAT)
    message(FATAL_ERROR "clang-format executable is not found!")
else()
    message(STATUS "Found clang-format at ${CLANG_FORMAT}")
endif()

add_custom_target(format
        COMMENT "Running clang-format"
        COMMAND ${CLANG_FORMAT} -i -style=file -fallback-style=none
                ${CMAKE_SOURCE_DIR}/src/*.cpp ${CMAKE_SOURCE_DIR}/src/*.h)

add_custom_target(check-format
        COMMENT "Checking clang-format"
        COMMAND ! ${CLANG_FORMAT} -style=file -fallback-style=none
                 --output-replacements-xml
                 ${CMAKE_SOURCE_DIR}/src/*.cpp ${CMAKE_SOURCE_DIR}/src/*.h
                 | grep -q "replacement offset")
