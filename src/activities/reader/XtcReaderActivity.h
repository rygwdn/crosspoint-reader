#pragma once

#include <Xtc.h>

#include <memory>
#include <string>

#include "ReaderActivity.h"

class XtcReaderActivity final : public ReaderActivity {
  std::shared_ptr<Xtc> xtc;
  uint32_t currentPage = 0;
  // Tracks whether this book is currently removed from Recent Books by the
  // removeReadBooksFromRecents feature (set at End-of-Book, cleared if paged
  // back in) -- same pattern as EpubReaderActivity's own recentsEntryRemoved.
  bool recentsEntryRemoved = false;

  enum class StatusBarOverlayPosition { Bottom, Top };
  struct StatusBarInfo {
    int currentPage;
    int pageCount;
    std::string title;
  };

  void renderPage();
  void openChapterSelection();
  void renderStatusBarOverlay(GfxRenderer& renderer, StatusBarOverlayPosition position) const;
  StatusBarInfo getStatusBarInfo() const;
  void saveProgress() const;
  void loadProgress();

  bool loadBook() override;
  std::string getBookTitle() const override { return xtc ? xtc->getTitle() : ""; }
  std::string getBookAuthor() const override { return xtc ? xtc->getAuthor() : ""; }
  std::string getBookThumbBmpPath() const override { return xtc ? xtc->getThumbBmpPath() : ""; }
  bool handleFormatInput() override;
  void renderBook() override;
  void applyInitialOrientation() override;

  // Zoom navigation: when the book has a subpage table (XTCBZ/XTCBZH only -- each
  // group spans one manga page's full view followed by its panel/bubble-zoom
  // crops, as produced by cbz2xteink), front Left/Right step *linearly*
  // through every crop in the whole book -- forward past a page's last zoom
  // crop lands on the next page's full view, then its own crops, so a single
  // button can drive the entire reading flow without ever needing side/tilt.
  // Side/tilt page-turn instead moves by whole group (skips straight past
  // zoom crops), landing on a group's full view either way -- a quicker path
  // for a reader who doesn't want to see every crop. No-op (falls back to the
  // original per-page behavior) when the book has no subpage table, e.g. a
  // plain XTC or a single-image book. Independent of real chapters
  // (openChapterSelection), which a subpage book may or may not also have.
  bool hasSubpageGroups() const;
  int findCurrentSubpageGroupIndex() const;
  void stepSubpage(int direction);
  void stepSubpageGroup(int direction, int count);

 public:
  explicit XtcReaderActivity(GfxRenderer& renderer, MappedInputManager& mappedInput, std::string bookPath,
                             bool allowFastInitialRefresh)
      : ReaderActivity("XtcReader", renderer, mappedInput, std::move(bookPath), allowFastInitialRefresh) {}
  ~XtcReaderActivity() override = default;

  bool pageTurn(bool isForward) override;
  bool skipPages(int amount) override;
  bool isAtEndOfBook() const override;
  void onReturnFromEndOfBook() override;

  ScreenshotInfo getScreenshotInfo() const override;
};
