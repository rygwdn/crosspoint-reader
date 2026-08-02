#include <gtest/gtest.h>

#include "Epub/parsers/PageBreakDecisions.h"

using PageBreakDecisions::shouldBreakForOrphan;
using PageBreakDecisions::shouldEmitFinalPage;

namespace {
constexpr int kLineHeight = 20;
constexpr int kViewportHeight = 400;
}  // namespace

// ============================================================================
// Orphan prevention
// ============================================================================

TEST(OrphanPrevention, DoesNotBreakOnEmptyPage) {
  // A page with no content yet can't orphan anything -- nothing to protect.
  EXPECT_FALSE(shouldBreakForOrphan(/*pageHasContent=*/false, /*currentPageNextY=*/390, /*topInset=*/0, kLineHeight,
                                     kViewportHeight));
}

TEST(OrphanPrevention, BreaksWhenFewerThanTwoLinesRemain) {
  // 400 - 370 - 0 = 30px remaining, less than 2 lines (40px): would strand 1 line.
  EXPECT_TRUE(
      shouldBreakForOrphan(/*pageHasContent=*/true, /*currentPageNextY=*/370, /*topInset=*/0, kLineHeight, kViewportHeight));
}

TEST(OrphanPrevention, DoesNotBreakWhenExactlyTwoLinesRemain) {
  // 400 - 360 - 0 = 40px remaining, exactly 2 lines: enough room, no orphan risk.
  EXPECT_FALSE(
      shouldBreakForOrphan(/*pageHasContent=*/true, /*currentPageNextY=*/360, /*topInset=*/0, kLineHeight, kViewportHeight));
}

TEST(OrphanPrevention, TopInsetCountsAgainstRemainingRoom) {
  // 400 - 340 - 20(topInset) = 40px remaining -- same as above, topInset eats into the budget.
  EXPECT_FALSE(shouldBreakForOrphan(/*pageHasContent=*/true, /*currentPageNextY=*/340, /*topInset=*/20, kLineHeight,
                                     kViewportHeight));
  // One pixel less room than that should now trip it.
  EXPECT_TRUE(shouldBreakForOrphan(/*pageHasContent=*/true, /*currentPageNextY=*/341, /*topInset=*/20, kLineHeight,
                                    kViewportHeight));
}

// ============================================================================
// Heading keep-with-next
//
// There's no dedicated decision helper for this anymore: stranding a heading run is now
// detected reactively in ChapterHtmlSlimParser::makePages() by reusing shouldBreakForOrphan()
// above against the real block that follows the run, once its actual wrapped line count is
// known -- rather than guessing a fixed line count before the heading's text exists (which
// broke for multi-line headings). That rescue logic manipulates live PageElement objects and
// isn't expressible as a pure primitive decision, so it isn't covered here; see the comments
// at the "HEADING RUN RESCUE" block in makePages() for the reasoning.
// ============================================================================

// ============================================================================
// End-of-chapter blank-page guard
// ============================================================================

TEST(FinalPageGuard, EmitsNonEmptyPage) {
  EXPECT_TRUE(shouldEmitFinalPage(/*pageExists=*/true, /*pageHasContent=*/true));
}

// Regression test for the reported bug: a trailing block that resolves to zero laid-out
// lines (e.g. an empty/whitespace-only heading used purely as a TOC anchor) must not be
// surfaced as a blank page to the reader.
TEST(FinalPageGuard, SuppressesEmptyPage) {
  EXPECT_FALSE(shouldEmitFinalPage(/*pageExists=*/true, /*pageHasContent=*/false));
}

TEST(FinalPageGuard, SuppressesWhenPageDoesNotExist) {
  EXPECT_FALSE(shouldEmitFinalPage(/*pageExists=*/false, /*pageHasContent=*/false));
}
