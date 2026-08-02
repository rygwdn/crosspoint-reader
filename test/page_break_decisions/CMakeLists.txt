add_executable(PageBreakDecisionsTest
  PageBreakDecisionsTest.cpp
)

target_include_directories(PageBreakDecisionsTest PRIVATE
  ${REPO_ROOT}/lib/Epub
)

target_link_libraries(PageBreakDecisionsTest PRIVATE
  crosspoint_test_common
  GTest::gtest_main
)

gtest_discover_tests(PageBreakDecisionsTest)
