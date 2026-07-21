/**
 * XtcParser.h
 *
 * XTC file parsing and page data extraction
 * XTC ebook support for CrossPoint Reader
 */

#pragma once

#include <HalStorage.h>

#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "XtcTypes.h"

namespace xtc {

/**
 * XTC File Parser
 *
 * Reads XTC files from SD card and extracts page data.
 * Designed for ESP32-C3's limited RAM (~380KB) using streaming.
 *
 * The source file is kept closed between reads to free heap for rendering.
 * It is reopened on-demand for page table lookups and bitmap data reads.
 */
class XtcParser {
 public:
  XtcParser();
  ~XtcParser();

  // File open/close
  XtcError open(const char* filepath);
  void close();
  bool isOpen() const { return m_isOpen; }

  // Header information access
  const XtcHeader& getHeader() const { return m_header; }
  uint16_t getPageCount() const { return m_header.pageCount; }
  uint16_t getWidth() const { return m_defaultWidth; }
  uint16_t getHeight() const { return m_defaultHeight; }
  uint8_t getBitDepth() const { return m_bitDepth; }  // 1 = XTC/XTG, 2 = XTCH/XTH

  // Page information
  bool getPageInfo(uint32_t pageIndex, PageInfo& info);

  /**
   * Load page bitmap (raw 1-bit data, skipping XTG header)
   *
   * @param pageIndex Page index (0-based)
   * @param buffer Output buffer (caller allocated)
   * @param bufferSize Buffer size
   * @return Number of bytes read on success, 0 on failure
   */
  size_t loadPage(uint32_t pageIndex, uint8_t* buffer, size_t bufferSize);

  /**
   * Streaming page load
   * Memory-efficient method that reads page data in chunks.
   *
   * @param pageIndex Page index
   * @param callback Callback function to receive data chunks
   * @param chunkSize Chunk size (default: 1024 bytes)
   * @return Error code
   */
  XtcError loadPageStreaming(uint32_t pageIndex,
                             std::function<void(const uint8_t* data, size_t size, size_t offset)> callback,
                             size_t chunkSize = 1024);

  // Get title/author from metadata
  std::string getTitle() const { return m_title; }
  std::string getAuthor() const { return m_author; }

  bool hasChapters() const { return m_hasChapters; }
  const std::vector<ChapterInfo>& getChapters();

  // XTCBZ/XTCBZH only (see XtcTypes.h) -- always empty/false for plain XTC/XTCH files.
  bool hasSubpages() const { return m_subpageTableOffset != 0; }
  const std::vector<SubpageGroup>& getSubpageGroups();

  // Validation
  static bool isValidXtcFile(const char* filepath);

  // Error information
  XtcError getLastError() const { return m_lastError; }

 private:
  HalFile m_file;
  std::string m_filepath;
  bool m_isOpen;
  XtcHeader m_header;
  std::vector<ChapterInfo> m_chapters;
  std::vector<SubpageGroup> m_subpageGroups;
  std::string m_title;
  std::string m_author;
  uint16_t m_defaultWidth;
  uint16_t m_defaultHeight;
  uint8_t m_bitDepth;  // 1 = XTC/XTG (1-bit), 2 = XTCH/XTH (2-bit)
  // true for XTCBZ/XTCBZH: header is XtcbzHeader (64 bytes), so metadata starts 8 bytes
  // later than in a classic XTC/XTCH file. Independent of hasSubpages() -- an
  // XTCBZ/XTCBZH file can have m_subpageTableOffset == 0 (no subpage table) and still
  // need the extended header's title/author offsets.
  bool m_isExtended;
  bool m_hasChapters;
  bool m_chaptersLoaded;
  // 0 for plain XTC/XTCH (upstream format, never has subpages). Read from the
  // XtcbzHeader tail for XTCBZ/XTCBZH; see readHeader().
  uint64_t m_subpageTableOffset;
  bool m_subpageGroupsLoaded;
  XtcError m_lastError;

  // Internal helper functions
  XtcError readHeader();
  XtcError readFirstPageInfo();
  XtcError readTitle();
  XtcError readAuthor();
  XtcError readChapters();
  XtcError readSubpageGroups();
  // Tightest known offset that starts the section *after* the one beginning at
  // `sectionStart`, out of {chapter table, subpage table, page table, first page's
  // data} -- so a variable-length table's entry count isn't over-counted into a
  // neighboring section regardless of which order they're laid out in. `extraCandidate`
  // lets a caller pass an offset it knows locally but that isn't an XtcParser member
  // (e.g. chapterOffset, read fresh from disk each call rather than cached). Falls
  // back to fileSize if nothing qualifies as a tighter bound.
  uint64_t nextSectionOffset(uint64_t sectionStart, uint64_t fileSize, uint64_t extraCandidate = 0) const;
  bool readPageTableEntry(uint32_t pageIndex, PageInfo& info);
  size_t decompressPage(const XtgPageHeader& pageHeader, uint8_t* buffer, size_t bufferSize);

  // File handle management — reopen on demand, close after use
  bool ensureFileOpen();
  void closeFile();
};

}  // namespace xtc
