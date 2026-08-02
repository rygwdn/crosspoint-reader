#pragma once

// Pure page-break decision helpers for ChapterHtmlSlimParser.
//
// These take only primitive layout state (no GfxRenderer/HAL dependency) so the decision
// math can be unit-tested on the host without the full parsing/rendering pipeline.
namespace PageBreakDecisions {

// ORPHAN PREVENTION: would starting this block leave fewer than 2 lines of room on the
// current page? If so, the whole block should move to a fresh page rather than starting
// with 1 line stranded at the bottom.
inline bool shouldBreakForOrphan(const bool pageHasContent, const int currentPageNextY, const int topInset,
                                  const int lineHeight, const int viewportHeight) {
  if (!pageHasContent) return false;
  const int remaining = viewportHeight - currentPageNextY - topInset;
  return remaining < lineHeight * 2;
}

// HEADING KEEP-WITH-NEXT: headings don't get a dedicated decision helper. Stranding is
// detected reactively instead of guessed proactively -- see the heading-run rescue in
// ChapterHtmlSlimParser::makePages(), which reuses shouldBreakForOrphan() above (evaluated
// against whatever block actually follows the heading run, once its real wrapped line count
// is known) rather than guessing a fixed line count before the heading's text even exists.

// END-OF-CHAPTER FLUSH: should this page actually be emitted? A page with no elements
// (e.g. the trailing block was an empty/whitespace-only heading used purely as a TOC
// anchor) should be dropped instead of surfacing as a blank page to the reader.
inline bool shouldEmitFinalPage(const bool pageExists, const bool pageHasContent) {
  return pageExists && pageHasContent;
}

}  // namespace PageBreakDecisions
