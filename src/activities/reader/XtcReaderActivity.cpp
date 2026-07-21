#include "XtcReaderActivity.h"

#include <DiskLogger.h>
#include <FsHelpers.h>
#include <GfxRenderer.h>
#include <HalStorage.h>
#include <I18n.h>
#include <Memory.h>

#include <algorithm>

#include "CrossPointSettings.h"
#include "ProgressFile.h"
#include "ReaderActivity.h"
#include "ReaderUtils.h"
#include "RecentBooksStore.h"
#include "XtcReaderChapterSelectionActivity.h"
#include "components/UITheme.h"
#include "fontIds.h"

bool XtcReaderActivity::loadBook() {
  auto loadedXtc = makeUniqueNoThrow<Xtc>(bookPath, "/.crosspoint");
  if (!loadedXtc) {
    LOG_ERR("XTR", "Failed to allocate XTC object");
    return false;
  }
  if (!loadedXtc->load()) {
    LOG_ERR("XTR", "Failed to load XTC");
    return false;
  }
  // Flush now rather than waiting for the next FLUSH_INTERVAL batch: entering
  // the reader activity and decoding page 0 is where a hang has been observed
  // with compressed (.xtcbz/.xtcbzh) pages, and losing this checkpoint to
  // unflushed ring-buffer lines would hide exactly where it got to.
  DiskLogger::flushNow();
  xtc = std::move(loadedXtc);
  xtc->setupCacheDir();
  LOG_DBG("XTR", "loadBook: cache dir ready (heap: %u)", (unsigned)ESP.getFreeHeap());

  loadProgress();
  LOG_DBG("XTR", "loadBook: progress loaded, page=%lu (heap: %u)", currentPage, (unsigned)ESP.getFreeHeap());

  // Force both lazy-loads now, synchronously on the main task, before requestUpdate()
  // hands off to the render task. getChapters()/getSubpageGroups() each seek xtc's
  // shared HalFile to a different part of the file (and getSubpageGroups() closes it
  // afterward) on first access -- handleFormatInput() calls both on every tick once
  // xtc is set, and if that first access lands after the render task has already
  // started reading page 0's (possibly multi-chunk, compressed) data, the interleaved
  // seek corrupts the read it's mid-stream on. Loading them here, before the file is
  // touched for any page, means the first tick always hits the already-cached fast
  // path instead of racing the render task for the file cursor.
  xtc->getChapters();
  xtc->getSubpageGroups();
  LOG_DBG("XTR", "loadBook: chapters/subpages prewarmed (heap: %u)", (unsigned)ESP.getFreeHeap());
  // First render (renderPage() -> loadPage()) is the likeliest place a hang shows up
  // for compressed (.xtcbz/.xtcbzh) pages -- flush so this checkpoint isn't lost to the
  // next FLUSH_INTERVAL batch if it does.
  DiskLogger::flushNow();

  return true;
}

void XtcReaderActivity::openChapterSelection() {
  if (xtc && xtc->hasChapters() && !xtc->getChapters().empty()) {
    startActivityForResult(std::make_unique<XtcReaderChapterSelectionActivity>(renderer, mappedInput, xtc, currentPage),
                           [this](const ActivityResult& result) {
                             if (!result.isCancelled) {
                               currentPage = std::get<PageResult>(result.data).page;
                               requestUpdate();
                             }
                           });
  }
}

bool XtcReaderActivity::handleFormatInput() {
  if (!xtc) {
    return false;
  }

  // Drop this book from Recent Books at End-of-Book; if the reader pages back in,
  // re-add it. Acts only on the transition (guarded by recentsEntryRemoved) -- no
  // per-frame writes. Same pattern as EpubReaderActivity::loop().
  if (SETTINGS.removeReadBooksFromRecents) {
    const bool atEndOfBook = isAtEndOfBook();
    if (atEndOfBook && !recentsEntryRemoved) {
      recentsEntryRemoved = RECENT_BOOKS.removeByPath(xtc->getPath());
    } else if (!atEndOfBook && recentsEntryRemoved) {
      RECENT_BOOKS.addBook(xtc->getPath(), xtc->getTitle(), xtc->getAuthor(), xtc->getThumbBmpPath());
      recentsEntryRemoved = false;
    }
  }

  // Enter chapter selection activity on Confirm release or touch menu gesture
  if (mappedInput.wasReleased(MappedInputManager::Button::Confirm) ||
      ReaderUtils::isTouchMenuGesture(renderer, mappedInput)) {
    openChapterSelection();
    return true;
  }

  // Front Left/Right: step linearly through every crop in the whole book
  // (full page -> panel 1 -> panel 2 -> ... -> next page's full page -> its
  // panel 1 -> ...) so a single button can drive the entire reading flow.
  // Only meaningful when the book has a subpage table (XTCBZ/XTCBZH; see
  // Xtc::hasSubpages()). Intercepted here, before the base loop()'s
  // detectPageTurn, so Left/Right don't also fire as page-turn aliases (see
  // ReaderUtils::detectPageTurn's prevButton/nextButton fallback) while a
  // subpage book is open.
  if (hasSubpageGroups()) {
    if (mappedInput.wasReleased(MappedInputManager::Button::Right)) {
      stepSubpage(+1);
      return true;
    }
    if (mappedInput.wasReleased(MappedInputManager::Button::Left)) {
      stepSubpage(-1);
      return true;
    }
  }

  return false;
}

void XtcReaderActivity::applyInitialOrientation() { renderer.setOrientation(GfxRenderer::Orientation::Portrait); }

bool XtcReaderActivity::hasSubpageGroups() const { return xtc && xtc->hasSubpages() && !xtc->getSubpageGroups().empty(); }

int XtcReaderActivity::findCurrentSubpageGroupIndex() const {
  if (!xtc || !xtc->hasSubpages()) {
    return -1;
  }
  const auto& groups = xtc->getSubpageGroups();
  const auto it = std::find_if(groups.begin(), groups.end(), [this](const xtc::SubpageGroup& group) {
    return currentPage >= group.startPage && currentPage <= group.endPage;
  });
  if (it == groups.end()) {
    return -1;
  }
  return static_cast<int>(it - groups.begin());
}

void XtcReaderActivity::stepSubpage(int direction) {
  // Linear step across the *whole* book, not wrapped within the current
  // subpage group -- lets a single button (front Left/Right) drive the
  // entire reading flow. Each group's first page is always its own full
  // view, so stepping forward past one group's last zoom crop lands
  // directly on the next page's full view, then continues forward into
  // that page's own crops -- "forward past the last panel goes to [the
  // next] full page, then [continues into] the next page." The previous
  // modulo wrap only ever cycled back through the *current* group's own
  // crops and could never reach another page from here at all.
  const int64_t next = static_cast<int64_t>(currentPage) + direction;
  if (next < 0) {
    currentPage = 0;
  } else if (next >= static_cast<int64_t>(xtc->getPageCount())) {
    currentPage = xtc->getPageCount();  // triggers the existing "End of book" handling
  } else {
    currentPage = static_cast<uint32_t>(next);
  }
  requestUpdate();
}

void XtcReaderActivity::stepSubpageGroup(int direction, int count) {
  const auto& groups = xtc->getSubpageGroups();
  const int groupIndex = findCurrentSubpageGroupIndex();
  const int baseIndex = groupIndex < 0 ? 0 : groupIndex;
  const int targetIndex = baseIndex + direction * count;

  if (targetIndex < 0) {
    currentPage = 0;
    requestUpdate();
    return;
  }
  if (targetIndex >= static_cast<int>(groups.size())) {
    currentPage = xtc->getPageCount();  // triggers the existing "End of book" handling
    requestUpdate();
    return;
  }
  currentPage = groups[targetIndex].startPage;
  requestUpdate();
}

void XtcReaderActivity::renderBook() {
  if (!xtc) {
    return;
  }

  renderPage();
  saveProgress();
}

XtcReaderActivity::StatusBarInfo XtcReaderActivity::getStatusBarInfo() const {
  const auto sb = SETTINGS.statusBarSpec();
  const int bookPageCount = static_cast<int>(xtc->getPageCount());
  const int bookPage = static_cast<int>(currentPage) + 1;
  std::string title = sb.titleMode == CrossPointSettings::STATUS_BAR_TITLE::BOOK_TITLE ? xtc->getTitle() : "";

  // Chapter title (if requested) is independent of the subpage counter below --
  // a book can have real chapters, a subpage table, both, or neither.
  if (sb.titleMode == CrossPointSettings::STATUS_BAR_TITLE::CHAPTER_TITLE && xtc->hasChapters()) {
    const auto& chapters = xtc->getChapters();
    const auto chapterIt = std::find_if(chapters.begin(), chapters.end(), [this](const xtc::ChapterInfo& chapter) {
      return currentPage >= chapter.startPage && currentPage <= chapter.endPage;
    });
    if (chapterIt != chapters.end()) {
      title = chapterIt->name.empty() ? tr(STR_UNNAMED) : chapterIt->name;
    }
  }

  // The X-of-Y counter shows progress through the current subpage group (manga
  // page + its zoom crops) when one exists, otherwise plain book page/count.
  if (!xtc->hasSubpages()) {
    return StatusBarInfo{bookPage, bookPageCount, std::move(title)};
  }

  const auto& groups = xtc->getSubpageGroups();
  const auto groupIt = std::find_if(groups.begin(), groups.end(), [this](const xtc::SubpageGroup& group) {
    return currentPage >= group.startPage && currentPage <= group.endPage;
  });

  if (groupIt == groups.end() || groupIt->endPage < groupIt->startPage) {
    return StatusBarInfo{bookPage, bookPageCount, std::move(title)};
  }

  return StatusBarInfo{static_cast<int>(currentPage - groupIt->startPage) + 1,
                       static_cast<int>(groupIt->endPage - groupIt->startPage) + 1, std::move(title)};
}

void XtcReaderActivity::renderStatusBarOverlay(GfxRenderer& renderer, const StatusBarOverlayPosition position) const {
  const auto sb = SETTINGS.statusBarSpec();
  const bool drawBottom = sb.xtcMode == CrossPointSettings::XTC_STATUS_BAR_MODE::XTC_STATUS_BAR_BOTTOM &&
                          position == StatusBarOverlayPosition::Bottom;
  const bool drawTop = sb.xtcMode == CrossPointSettings::XTC_STATUS_BAR_MODE::XTC_STATUS_BAR_TOP &&
                       position == StatusBarOverlayPosition::Top;
  if (!drawBottom && !drawTop) {
    return;
  }

  const int statusBarHeight = UITheme::getInstance().getStatusBarHeight();
  if (statusBarHeight <= 0) {
    return;
  }

  int orientedMarginTop, orientedMarginRight, orientedMarginBottom, orientedMarginLeft;
  renderer.getOrientedViewableTRBL(&orientedMarginTop, &orientedMarginRight, &orientedMarginBottom,
                                   &orientedMarginLeft);

  int clearY;
  int paddingBottom = 0;
  if (position == StatusBarOverlayPosition::Bottom) {
    clearY = renderer.getScreenHeight() - orientedMarginBottom - statusBarHeight - 4;
    if (clearY < 0) {
      clearY = 0;
    }
  } else {
    clearY = orientedMarginTop;
    paddingBottom = renderer.getScreenHeight() - statusBarHeight - orientedMarginBottom - orientedMarginTop - 4;
  }
  const int clearHeight = position == StatusBarOverlayPosition::Bottom
                              ? renderer.getScreenHeight() - orientedMarginBottom - clearY
                              : statusBarHeight + 4;
  if (clearHeight > 0) {
    renderer.fillRect(0, clearY, renderer.getScreenWidth(), clearHeight, false);
  }

  const int pageCount = static_cast<int>(xtc->getPageCount());
  const int displayPage = static_cast<int>(currentPage) + 1;
  const float progress = pageCount > 0 ? (static_cast<float>(displayPage) * 100.0f) / pageCount : 0.0f;
  const auto pageInfo = getStatusBarInfo();
  GUI.drawStatusBar(renderer, progress, pageInfo.currentPage, pageInfo.pageCount, pageInfo.title, paddingBottom);
}

void XtcReaderActivity::renderPage() {
  const uint16_t pageWidth = xtc->getPageWidth();
  const uint16_t pageHeight = xtc->getPageHeight();
  const uint8_t bitDepth = xtc->getBitDepth();

  size_t pageBufferSize;
  if (bitDepth == 2) {
    pageBufferSize = ((static_cast<size_t>(pageWidth) * pageHeight + 7) / 8) * 2;
  } else {
    pageBufferSize = ((pageWidth + 7) / 8) * pageHeight;
  }

  uint8_t* pageBuffer = static_cast<uint8_t*>(malloc(pageBufferSize));
  if (!pageBuffer) {
    LOG_ERR("XTR", "Failed to allocate page buffer (%lu bytes)", pageBufferSize);
    renderer.clearScreen();
    renderer.drawCenteredText(UI_12_FONT_ID, 300, tr(STR_MEMORY_ERROR), true, EpdFontFamily::BOLD);
    renderer.displayBuffer();
    return;
  }

  // Load page data. This is where a compressed (.xtcbz/.xtcbzh) page runs through
  // XtcParser::decompressPage()/InflateStream -- log + flush around it so a
  // hang here is pinpointed instead of just showing the last loadBook() checkpoint.
  LOG_DBG("XTR", "Loading page %lu (bufferSize=%lu, bitDepth=%u, heap: %u)", currentPage, pageBufferSize, bitDepth,
          (unsigned)ESP.getFreeHeap());
  DiskLogger::flushNow();
  size_t bytesRead = xtc->loadPage(currentPage, pageBuffer, pageBufferSize);
  LOG_DBG("XTR", "Loaded page %lu: %lu bytes (heap: %u)", currentPage, bytesRead, (unsigned)ESP.getFreeHeap());
  if (bytesRead == 0) {
    LOG_ERR("XTR", "Failed to load page %lu: bufferSize=%lu bitDepth=%u error=%s", currentPage, pageBufferSize,
            bitDepth, xtc::errorToString(xtc->getLastError()));
    free(pageBuffer);
    renderer.clearScreen();
    renderer.drawCenteredText(UI_12_FONT_ID, 300, tr(STR_PAGE_LOAD_ERROR), true, EpdFontFamily::BOLD);
    renderer.displayBuffer();
    return;
  }

  renderer.clearScreen();

  const uint16_t maxSrcY = pageHeight;

  if (bitDepth == 2) {
    const size_t planeSize = (static_cast<size_t>(pageWidth) * pageHeight + 7) / 8;
    const uint8_t* plane1 = pageBuffer;
    const uint8_t* plane2 = pageBuffer + planeSize;
    const size_t colBytes = (pageHeight + 7) / 8;

    auto getPixelValue = [&](uint16_t x, uint16_t y) -> uint8_t {
      const size_t colIndex = pageWidth - 1 - x;
      const size_t byteInCol = y / 8;
      const size_t bitInByte = 7 - (y % 8);
      const size_t byteOffset = colIndex * colBytes + byteInCol;
      const uint8_t bit1 = (plane1[byteOffset] >> bitInByte) & 1;
      const uint8_t bit2 = (plane2[byteOffset] >> bitInByte) & 1;
      return (bit1 << 1) | bit2;
    };

    for (uint16_t y = 0; y < pageHeight; y++) {
      for (uint16_t x = 0; x < pageWidth; x++) {
        if (getPixelValue(x, y) >= 1) {
          renderer.drawPixel(x, y, true);
        }
      }
    }

    if (pagesUntilFullRefresh <= 1) {
      // Periodic ghost cleanup: scrub via the normal path, then run the
      // settle flavor of the grayscale base pass (DTM planes are equal after
      // the display sync, so only the gentle reinforcement cells fire).
      // Combined-base panels (Paper Mono) instead defer the base so the gray
      // planes below join it in one waveform.
      if (renderer.combinesGrayscaleBase()) {
        renderer.displayGrayscaleBase(HalDisplay::HALF_REFRESH);
      } else {
        renderer.displayBuffer(HalDisplay::HALF_REFRESH);
        renderer.preconditionGrayscale();
      }
      pagesUntilFullRefresh = SETTINGS.getRefreshFrequency();
    } else {
      renderer.displayGrayscaleBase(HalDisplay::FAST_REFRESH);
      pagesUntilFullRefresh--;
    }

    renderer.clearScreen(0x00);
    for (uint16_t y = 0; y < pageHeight; y++) {
      for (uint16_t x = 0; x < pageWidth; x++) {
        if (getPixelValue(x, y) == 1) {
          renderer.drawPixel(x, y, false);
        }
      }
    }
    renderer.copyGrayscaleLsbBuffers();

    renderer.clearScreen(0x00);
    for (uint16_t y = 0; y < pageHeight; y++) {
      for (uint16_t x = 0; x < pageWidth; x++) {
        const uint8_t pv = getPixelValue(x, y);
        if (pv == 1 || pv == 2) {
          renderer.drawPixel(x, y, false);
        }
      }
    }
    renderer.copyGrayscaleMsbBuffers();

    renderer.displayGrayBuffer();

    renderer.clearScreen();
    for (uint16_t y = 0; y < pageHeight; y++) {
      for (uint16_t x = 0; x < pageWidth; x++) {
        if (getPixelValue(x, y) >= 1) {
          renderer.drawPixel(x, y, true);
        }
      }
    }

    renderer.cleanupGrayscaleWithFrameBuffer();

    free(pageBuffer);

    // The grayscale pass above leaves gray charge in the page region that a
    // plain fast diff on the *next* page turn can't clear, so content there
    // ghosts (same root cause EpubReaderActivity hit and fixed for its own
    // grayscale AA pass, see its renderPage()'s pagesUntilFullRefresh=1,
    // issue #2190). This branch never set that, so every XTH page ghosted
    // into the next fast page-turn. Force the next page onto the HALF
    // ghost-cleanup path, which drives every pixel to its target regardless
    // of residue.
    pagesUntilFullRefresh = 1;

    LOG_DBG("XTR", "Rendered page %lu/%lu (2-bit grayscale)", currentPage + 1, xtc->getPageCount());
    return;
  } else {
    const size_t srcRowBytes = (pageWidth + 7) / 8;

    for (uint16_t srcY = 0; srcY < maxSrcY; srcY++) {
      const size_t srcRowStart = srcY * srcRowBytes;

      for (uint16_t srcX = 0; srcX < pageWidth; srcX++) {
        const size_t srcByte = srcRowStart + srcX / 8;
        const size_t srcBit = 7 - (srcX % 8);
        const bool isBlack = !((pageBuffer[srcByte] >> srcBit) & 1);

        if (isBlack) {
          renderer.drawPixel(srcX, srcY, true);
        }
      }
    }
  }

  free(pageBuffer);

  if (SETTINGS.statusBarSpec().xtcMode == CrossPointSettings::XTC_STATUS_BAR_MODE::XTC_STATUS_BAR_TOP) {
    renderStatusBarOverlay(renderer, StatusBarOverlayPosition::Top);
  } else {
    renderStatusBarOverlay(renderer, StatusBarOverlayPosition::Bottom);
  }

  ReaderUtils::displayWithRefreshCycle(renderer, pagesUntilFullRefresh);

  LOG_DBG("XTR", "Rendered page %lu/%lu (%u-bit)", currentPage + 1, xtc->getPageCount(), bitDepth);
}

bool XtcReaderActivity::pageTurn(bool isForward) {
  if (!xtc) return false;
  if (hasSubpageGroups()) {
    // With a subpage table, page-turn moves by whole manga page instead of
    // by individual XTC page, so normal reading skips straight past zoom
    // crops -- front Left/Right (see handleFormatInput()) are the way to see
    // them. But if a forward turn lands mid-group (on a zoom crop, not the
    // full page), it snaps back to that page's full view first instead of
    // advancing -- so escaping a deep zoom never costs a page of content,
    // and forward progress is always at most one press away from "back to
    // what I was reading."
    if (isForward) {
      const int groupIndex = findCurrentSubpageGroupIndex();
      if (groupIndex >= 0 && currentPage != xtc->getSubpageGroups()[groupIndex].startPage) {
        currentPage = xtc->getSubpageGroups()[groupIndex].startPage;
        return true;
      }
    }
    stepSubpageGroup(isForward ? +1 : -1, 1);
    return true;
  }
  if (isForward) {
    if (currentPage < xtc->getPageCount()) {
      currentPage++;
      return true;
    }
  } else {
    if (currentPage > 0) {
      currentPage--;
      return true;
    }
  }
  return false;
}

bool XtcReaderActivity::skipPages(int amount) {
  if (!xtc) return false;
  if (hasSubpageGroups()) {
    // Long-press skip: always jump by whole subpage groups, no mid-group
    // snap-back -- that's only for a plain forward tap; a deliberate big
    // jump shouldn't get redirected back to a page already seen.
    const int count = amount < 0 ? -amount : amount;
    stepSubpageGroup(amount > 0 ? +1 : -1, count);
    return true;
  }
  int newPage = static_cast<int>(currentPage) + amount;
  if (newPage < 0) newPage = 0;
  if (newPage > static_cast<int>(xtc->getPageCount())) newPage = static_cast<int>(xtc->getPageCount());
  if (newPage != static_cast<int>(currentPage)) {
    currentPage = static_cast<uint32_t>(newPage);
    return true;
  }
  return false;
}

bool XtcReaderActivity::isAtEndOfBook() const { return xtc && currentPage >= xtc->getPageCount(); }

void XtcReaderActivity::onReturnFromEndOfBook() {
  if (xtc && xtc->getPageCount() > 0) {
    currentPage = xtc->getPageCount() - 1;
  } else {
    currentPage = 0;
  }
}

void XtcReaderActivity::saveProgress() const {
  if (!xtc) return;
  uint8_t data[4];
  data[0] = currentPage & 0xFF;
  data[1] = (currentPage >> 8) & 0xFF;
  data[2] = (currentPage >> 16) & 0xFF;
  data[3] = (currentPage >> 24) & 0xFF;
  if (!ProgressFile::writeAtomic(xtc->getCachePath(), data, sizeof(data))) {
    LOG_ERR("XTC", "Failed to save progress: page %lu", currentPage);
  }
}

void XtcReaderActivity::loadProgress() {
  if (!xtc) return;
  HalFile f;
  if (Storage.openFileForRead("XTC", xtc->getCachePath() + "/progress.bin", f)) {
    uint8_t data[4];
    if (f.read(data, 4) == 4) {
      currentPage = data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24);
      if (currentPage >= xtc->getPageCount() && xtc->getPageCount() > 0) {
        currentPage = xtc->getPageCount() - 1;
      }
      LOG_DBG("XTC", "Loaded progress: page %lu/%lu", currentPage + 1, xtc->getPageCount());
    }
  }
}

ScreenshotInfo XtcReaderActivity::getScreenshotInfo() const {
  ScreenshotInfo info;
  info.readerType = ScreenshotInfo::ReaderType::Xtc;
  if (xtc) {
    const std::string t = xtc->getTitle();
    snprintf(info.title, sizeof(info.title), "%s", t.c_str());
    const uint32_t pageCount = xtc->getPageCount();
    info.totalPages = pageCount;
    uint32_t clampedPage = (pageCount > 0 && currentPage >= pageCount) ? pageCount - 1 : currentPage;
    info.progressPercent = pageCount > 0 ? xtc->calculateProgress(clampedPage) : 0;
    info.currentPage = static_cast<int>(clampedPage) + 1;
  } else {
    info.currentPage = currentPage + 1;
  }
  return info;
}
