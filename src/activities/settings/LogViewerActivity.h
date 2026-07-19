#pragma once

#include <cstddef>
#include <string>
#include <vector>

#include "activities/Activity.h"

// Very basic on-device viewer for the disk log files written by DiskLogger. Debug-build
// only (see ENABLE_LOG_VIEWER in platformio.ini / SettingsActivity) — not compiled into
// gh_release/gh_release_rc/slim, so it costs nothing in the shipped firmware.
//
// Rebuilds a page index fresh every time a file is opened rather than persisting one to
// SD (contrast with TxtReaderActivity): log files rotate and get truncated out from
// under a stale cache in a way books don't, and the index build here is a cheap
// byte-scan (no font-metrics measurement), so there's little to gain from caching it.
class LogViewerActivity final : public Activity {
 public:
  explicit LogViewerActivity(GfxRenderer& renderer, MappedInputManager& mappedInput)
      : Activity("LogViewer", renderer, mappedInput) {}

  void onEnter() override;
  void loop() override;
  void render(RenderLock&&) override;

 private:
  // Generation numbers (0 = active debug.log, higher = older backups) that currently
  // exist on SD, in that order.
  std::vector<int> availableGenerations;
  int availableIndex = 0;  // index into availableGenerations of the file being viewed
  std::string currentPath;

  // Page index: byte offset of the start of each page (one page = linesPerPage lines).
  // Indexed by page, not by line, to keep the vector small (hundreds of pages, not
  // thousands of lines) for a 512KB log file.
  std::vector<size_t> pageOffsets;
  int currentPage = 0;
  int totalPages = 0;
  int linesPerPage = 0;

  int viewportWidth = 0;
  int marginTop = 0;
  int marginLeft = 0;
  int marginBottom = 0;

  void scanAvailableGenerations();
  // Opens availableGenerations[availIdx], rebuilds the page index, and jumps to the
  // last page (most recent entries), per the "default to bottom of log" requirement.
  void openGeneration(int availIdx);
  void buildPageIndex();
  void loadPage(int page, std::vector<std::string>& outLines) const;
};
