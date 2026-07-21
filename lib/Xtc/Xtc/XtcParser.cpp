/**
 * XtcParser.cpp
 *
 * XTC file parsing implementation
 * XTC ebook support for CrossPoint Reader
 */

#include "XtcParser.h"

#include <FsHelpers.h>
#include <HalStorage.h>
#include <InflateStream.h>
#include <Logging.h>

#include <algorithm>
#include <cstring>
#include <vector>

namespace xtc {

namespace {
// Feeds InflateStream from a HalFile in small chunks -- same pattern as
// ZipFile's zipFillCallback, reused here for XTCBZ/XTCBZH compressed pages.
struct PageInflateCtx {
  HalFile* file = nullptr;
  size_t remaining = 0;
  uint8_t* readBuf = nullptr;
  size_t readBufSize = 0;
};

size_t pageInflateFillCallback(void* vctx, const uint8_t** data) {
  auto* ctx = static_cast<PageInflateCtx*>(vctx);
  if (ctx->remaining == 0) return 0;

  const size_t toRead = ctx->remaining < ctx->readBufSize ? ctx->remaining : ctx->readBufSize;
  const size_t bytesRead = ctx->file->read(ctx->readBuf, toRead);
  ctx->remaining -= bytesRead;

  *data = ctx->readBuf;
  return bytesRead;
}
}  // namespace

XtcParser::XtcParser()
    : m_isOpen(false),
      m_defaultWidth(DISPLAY_WIDTH),
      m_defaultHeight(DISPLAY_HEIGHT),
      m_bitDepth(1),
      m_isExtended(false),
      m_hasChapters(false),
      m_chaptersLoaded(false),
      m_subpageTableOffset(0),
      m_subpageGroupsLoaded(false),
      m_lastError(XtcError::OK) {
  memset(&m_header, 0, sizeof(m_header));
}

XtcParser::~XtcParser() { close(); }

XtcError XtcParser::open(const char* filepath) {
  // Close if already open
  if (m_isOpen) {
    close();
  }

  m_filepath = filepath;

  // Open file
  if (!Storage.openFileForRead("XTC", filepath, m_file)) {
    m_lastError = XtcError::FILE_NOT_FOUND;
    return m_lastError;
  }

  // Read header
  m_lastError = readHeader();
  if (m_lastError != XtcError::OK) {
    LOG_DBG("XTC", "Failed to read header: %s", errorToString(m_lastError));
    // Explicit close() required: member variable persists beyond function scope
    m_file.close();
    return m_lastError;
  }

  // Read title & author if available
  if (m_header.hasMetadata) {
    m_lastError = readTitle();
    if (m_lastError != XtcError::OK) {
      LOG_DBG("XTC", "Failed to read title: %s", errorToString(m_lastError));
      // Explicit close() required: member variable persists beyond function scope
      m_file.close();
      return m_lastError;
    }
    m_lastError = readAuthor();
    if (m_lastError != XtcError::OK) {
      LOG_DBG("XTC", "Failed to read author: %s", errorToString(m_lastError));
      // Explicit close() required: member variable persists beyond function scope
      m_file.close();
      return m_lastError;
    }
    // Trim excess capacity from metadata strings
    m_title.shrink_to_fit();
    m_author.shrink_to_fit();
  }

  // Read first page info for default dimensions (no bulk page table allocation)
  m_lastError = readFirstPageInfo();
  if (m_lastError != XtcError::OK) {
    LOG_DBG("XTC", "Failed to read first page info: %s", errorToString(m_lastError));
    // Explicit close() required: member variable persists beyond function scope
    m_file.close();
    return m_lastError;
  }

  // Defer chapter parsing until actually needed (lazy load).
  // Chapter strings can use significant heap; keeping them out of memory
  // during rendering leaves more room for the page bitmap buffer.
  // Older XTC files start the page table at 0x30, so they do not have the later
  // chapterOffset field even if the bytes read into that slot are non-zero.
  m_hasChapters = (m_header.hasChapters == 1 && m_header.pageTableOffset >= sizeof(XtcHeader));
  m_chaptersLoaded = false;

  // Close the source file to free its internal SdFat buffers.
  // It will be reopened on-demand for page table lookups and bitmap reads.
  m_file.close();

  m_isOpen = true;
  LOG_DBG("XTC", "Opened file: %s (%u pages, %dx%d)", filepath, m_header.pageCount, m_defaultWidth, m_defaultHeight);
  return XtcError::OK;
}

void XtcParser::close() {
  closeFile();
  m_isOpen = false;
  m_chaptersLoaded = false;
  m_chapters.clear();
  m_subpageGroupsLoaded = false;
  m_subpageGroups.clear();
  m_subpageTableOffset = 0;
  m_isExtended = false;
  m_title.clear();
  m_author.clear();
  m_hasChapters = false;
  memset(&m_header, 0, sizeof(m_header));
}

bool XtcParser::ensureFileOpen() {
  if (m_file.isOpen()) {
    return true;
  }
  return Storage.openFileForRead("XTC", m_filepath.c_str(), m_file);
}

void XtcParser::closeFile() {
  if (m_file.isOpen()) {
    m_file.close();
  }
}

XtcError XtcParser::readHeader() {
  // Read the shared 56-byte header, identical for XTC/XTCH and XTCBZ/XTCBZH (XtcbzHeader
  // is XtcHeader plus a trailing subpageTableOffset -- see XtcTypes.h).
  size_t bytesRead = m_file.read(reinterpret_cast<uint8_t*>(&m_header), sizeof(XtcHeader));
  if (bytesRead != sizeof(XtcHeader)) {
    return XtcError::READ_ERROR;
  }

  // Verify magic number (XTC/XTCH: upstream format; XTCBZ/XTCBZH: CrossPoint's extended container)
  const bool isExtended = m_header.magic == XTCBZ_MAGIC || m_header.magic == XTCBZH_MAGIC;
  if (m_header.magic != XTC_MAGIC && m_header.magic != XTCH_MAGIC && !isExtended) {
    LOG_DBG("XTC", "Invalid magic: 0x%08X (expected XTC/XTCH/XTCBZ/XTCBZH)", m_header.magic);
    return XtcError::INVALID_MAGIC;
  }

  // Determine bit depth from file magic
  m_bitDepth = (m_header.magic == XTCH_MAGIC || m_header.magic == XTCBZH_MAGIC) ? 2 : 1;

  m_isExtended = isExtended;
  m_subpageTableOffset = 0;
  if (isExtended) {
    uint64_t subpageTableOffset = 0;
    if (m_file.read(reinterpret_cast<uint8_t*>(&subpageTableOffset), sizeof(subpageTableOffset)) !=
        sizeof(subpageTableOffset)) {
      return XtcError::READ_ERROR;
    }
    m_subpageTableOffset = subpageTableOffset;
  }

  // Check version
  // Currently, version 1.0 is the only valid version, however some generators are swapping the bytes around, so we
  // accept both 1.0 and 0.1 for compatibility
  const bool validVersion = m_header.versionMajor == 1 && m_header.versionMinor == 0 ||
                            m_header.versionMajor == 0 && m_header.versionMinor == 1;
  if (!validVersion) {
    LOG_DBG("XTC", "Unsupported version: %u.%u", m_header.versionMajor, m_header.versionMinor);
    return XtcError::INVALID_VERSION;
  }

  // Basic validation
  if (m_header.pageCount == 0) {
    return XtcError::CORRUPTED_HEADER;
  }

  const char* magicName = m_header.magic == XTCH_MAGIC     ? "XTCH"
                          : m_header.magic == XTCBZ_MAGIC  ? "XTCBZ"
                          : m_header.magic == XTCBZH_MAGIC ? "XTCBZH"
                                                            : "XTC";
  LOG_DBG("XTC", "Header: magic=0x%08X (%s), ver=%u.%u, pages=%u, bitDepth=%u, subpages=%d", m_header.magic, magicName,
          m_header.versionMajor, m_header.versionMinor, m_header.pageCount, m_bitDepth, isExtended);

  return XtcError::OK;
}

XtcError XtcParser::readTitle() {
  // Title immediately follows the header: 0x38 (56) for XTC/XTCH, 0x40 (64) for
  // XTCBZ/XTCBZH's 8-byte-longer XtcbzHeader (see XtcTypes.h).
  const uint64_t titleOffset = m_isExtended ? sizeof(XtcbzHeader) : sizeof(XtcHeader);
  if (!m_file.seek64(titleOffset)) {
    return XtcError::READ_ERROR;
  }

  char titleBuf[128] = {0};
  m_file.read(titleBuf, sizeof(titleBuf) - 1);
  m_title = titleBuf;

  LOG_DBG("XTC", "Title: %s", m_title.c_str());
  return XtcError::OK;
}

XtcError XtcParser::readAuthor() {
  // Read author as null-terminated UTF-8 string with max length 64, directly following title
  const uint64_t authorOffset = (m_isExtended ? sizeof(XtcbzHeader) : sizeof(XtcHeader)) + 128;
  if (!m_file.seek64(authorOffset)) {
    return XtcError::READ_ERROR;
  }

  char authorBuf[64] = {0};
  m_file.read(authorBuf, sizeof(authorBuf) - 1);
  m_author = authorBuf;

  LOG_DBG("XTC", "Author: %s", m_author.c_str());
  return XtcError::OK;
}

XtcError XtcParser::readFirstPageInfo() {
  if (m_header.pageTableOffset == 0) {
    LOG_DBG("XTC", "Page table offset is 0, cannot read");
    return XtcError::CORRUPTED_HEADER;
  }

  // Verify the file is large enough to contain the full page table
  const uint64_t fileSize = m_file.fileSize64();
  const uint64_t pageTableSize = static_cast<uint64_t>(m_header.pageCount) * sizeof(PageTableEntry);
  if (m_header.pageTableOffset < XTC_LEGACY_HEADER_SIZE || m_header.pageTableOffset > fileSize ||
      pageTableSize > fileSize - m_header.pageTableOffset) {
    LOG_DBG("XTC",
            "Page table exceeds file bounds: file=%llu tableOffset=%llu tableSize=%llu pages=%u entrySize=%u "
            "dataOffset=%llu minTableOffset=%llu",
            static_cast<unsigned long long>(fileSize), static_cast<unsigned long long>(m_header.pageTableOffset),
            static_cast<unsigned long long>(pageTableSize), m_header.pageCount,
            static_cast<unsigned int>(sizeof(PageTableEntry)), static_cast<unsigned long long>(m_header.dataOffset),
            static_cast<unsigned long long>(XTC_LEGACY_HEADER_SIZE));
    return XtcError::CORRUPTED_HEADER;
  }

  // Read only the first entry to get default page dimensions
  // All other entries are read on-demand via readPageTableEntry()
  // This avoids allocating pageCount * 16 bytes (e.g. 65KB for 4000+ pages)
  PageTableEntry entry;
  if (!m_file.seek64(m_header.pageTableOffset)) {
    LOG_DBG("XTC", "Failed to seek to page table at %llu", m_header.pageTableOffset);
    return XtcError::READ_ERROR;
  }
  size_t bytesRead = m_file.read(reinterpret_cast<uint8_t*>(&entry), sizeof(PageTableEntry));
  if (bytesRead != sizeof(PageTableEntry)) {
    LOG_DBG("XTC", "Failed to read first page table entry");
    return XtcError::READ_ERROR;
  }

  m_defaultWidth = entry.width;
  m_defaultHeight = entry.height;

  LOG_DBG("XTC", "Page table validated: %u pages, default %dx%d", m_header.pageCount, m_defaultWidth, m_defaultHeight);
  return XtcError::OK;
}

bool XtcParser::readPageTableEntry(uint32_t pageIndex, PageInfo& info) {
  if (pageIndex >= m_header.pageCount) {
    return false;
  }

  if (!ensureFileOpen()) {
    LOG_DBG("XTC", "Failed to reopen file for page table read");
    return false;
  }

  // Seek to the specific page table entry on the SD card
  const uint64_t entryOffset = m_header.pageTableOffset + static_cast<uint64_t>(pageIndex) * sizeof(PageTableEntry);
  if (!m_file.seek64(entryOffset)) {
    LOG_DBG("XTC", "Failed to seek to page table entry %lu at %llu", pageIndex, entryOffset);
    return false;
  }

  PageTableEntry entry;
  size_t bytesRead = m_file.read(reinterpret_cast<uint8_t*>(&entry), sizeof(PageTableEntry));
  if (bytesRead != sizeof(PageTableEntry)) {
    LOG_DBG("XTC", "Failed to read page table entry %lu", pageIndex);
    return false;
  }

  info.offset = entry.dataOffset;
  info.size = entry.dataSize;
  info.width = entry.width;
  info.height = entry.height;
  info.bitDepth = m_bitDepth;
  return true;
}

uint64_t XtcParser::nextSectionOffset(uint64_t sectionStart, uint64_t fileSize, uint64_t extraCandidate) const {
  uint64_t end = fileSize;
  auto consider = [&](uint64_t candidate) {
    if (candidate > sectionStart && candidate <= fileSize && candidate < end) {
      end = candidate;
    }
  };
  consider(m_header.pageTableOffset);
  consider(m_header.dataOffset);
  consider(m_subpageTableOffset);
  consider(extraCandidate);
  return end;
}

XtcError XtcParser::readChapters() {
  m_chapters.clear();

  if (!ensureFileOpen()) {
    return XtcError::READ_ERROR;
  }

  uint8_t hasChaptersFlag = 0;
  if (!m_file.seek(0x0B)) {
    return XtcError::READ_ERROR;
  }
  if (m_file.read(&hasChaptersFlag, sizeof(hasChaptersFlag)) != sizeof(hasChaptersFlag)) {
    return XtcError::READ_ERROR;
  }

  if (hasChaptersFlag != 1) {
    return XtcError::OK;
  }

  uint64_t chapterOffset = 0;
  if (!m_file.seek(0x30)) {
    return XtcError::READ_ERROR;
  }
  if (m_file.read(reinterpret_cast<uint8_t*>(&chapterOffset), sizeof(chapterOffset)) != sizeof(chapterOffset)) {
    return XtcError::READ_ERROR;
  }

  if (chapterOffset == 0) {
    return XtcError::OK;
  }

  const uint64_t fileSize = m_file.fileSize64();
  if (chapterOffset < sizeof(XtcHeader) || chapterOffset >= fileSize || chapterOffset + 96 > fileSize) {
    return XtcError::OK;
  }

  // Clamp maxOffset to fileSize (and to the subpage table, if any -- see
  // nextSectionOffset) so bogus header values can't inflate chapterCount
  const uint64_t maxOffset = nextSectionOffset(chapterOffset, fileSize);

  if (maxOffset <= chapterOffset) {
    return XtcError::OK;
  }

  constexpr size_t chapterSize = 96;
  const uint64_t available = maxOffset - chapterOffset;
  const size_t chapterCount = static_cast<size_t>(available / chapterSize);
  if (chapterCount == 0) {
    return XtcError::OK;
  }

  if (!m_file.seek64(chapterOffset)) {
    return XtcError::READ_ERROR;
  }

  m_chapters.reserve(chapterCount);
  std::vector<uint8_t> chapterBuf(chapterSize);
  for (size_t i = 0; i < chapterCount; i++) {
    if (m_file.read(chapterBuf.data(), chapterSize) != chapterSize) {
      return XtcError::READ_ERROR;
    }

    char nameBuf[81];
    memcpy(nameBuf, chapterBuf.data(), 80);
    nameBuf[80] = '\0';
    const size_t nameLen = strnlen(nameBuf, 80);
    std::string name(nameBuf, nameLen);

    uint16_t startPage = 0;
    uint16_t endPage = 0;
    memcpy(&startPage, chapterBuf.data() + 0x50, sizeof(startPage));
    memcpy(&endPage, chapterBuf.data() + 0x52, sizeof(endPage));

    if (name.empty() && startPage == 0 && endPage == 0) {
      break;
    }

    if (startPage > 0) {
      startPage--;
    }
    if (endPage > 0) {
      endPage--;
    }

    if (startPage >= m_header.pageCount) {
      continue;
    }

    if (endPage >= m_header.pageCount) {
      endPage = m_header.pageCount - 1;
    }

    if (startPage > endPage) {
      continue;
    }

    ChapterInfo chapter{std::move(name), startPage, endPage};
    m_chapters.push_back(std::move(chapter));
  }

  m_hasChapters = !m_chapters.empty();
  LOG_DBG("XTC", "Chapters: %u", static_cast<unsigned int>(m_chapters.size()));
  return XtcError::OK;
}

const std::vector<ChapterInfo>& XtcParser::getChapters() {
  // Lazy load chapters on first access
  if (!m_chaptersLoaded && m_hasChapters) {
    const XtcError err = readChapters();
    if (err != XtcError::OK) {
      LOG_ERR("XTC", "Failed to lazy-load chapters: %s", errorToString(err));
      m_hasChapters = false;
      m_chapters.clear();
    }
    m_chaptersLoaded = true;
    // Close file after chapter read to free buffers for rendering
    closeFile();
  }
  return m_chapters;
}

XtcError XtcParser::readSubpageGroups() {
  m_subpageGroups.clear();

  if (m_subpageTableOffset == 0) {
    return XtcError::OK;
  }

  if (!ensureFileOpen()) {
    return XtcError::READ_ERROR;
  }

  const uint64_t fileSize = m_file.fileSize64();
  constexpr size_t entrySize = sizeof(uint16_t) * 2;  // {startPage, endPage}, 1-based on disk
  if (m_subpageTableOffset < sizeof(XtcHeader) || m_subpageTableOffset >= fileSize) {
    return XtcError::OK;
  }

  // Read fresh (not cached on XtcParser) purely as a bound for nextSectionOffset below,
  // same as readChapters() does locally -- lets the subpage table clamp against the
  // chapter table's start when the two are adjacent, regardless of layout order.
  uint64_t chapterOffset = 0;
  if (m_file.seek(0x30)) {
    m_file.read(reinterpret_cast<uint8_t*>(&chapterOffset), sizeof(chapterOffset));
  }

  // Clamp maxOffset to fileSize so bogus header values can't inflate the entry count
  const uint64_t maxOffset = nextSectionOffset(m_subpageTableOffset, fileSize, chapterOffset);

  if (maxOffset <= m_subpageTableOffset) {
    return XtcError::OK;
  }

  const uint64_t available = maxOffset - m_subpageTableOffset;
  // A group can span no fewer than one page, so there can never legitimately be more
  // groups than pages -- cap here regardless of what the byte-range clamp above allows,
  // so a bogus/corrupt table offset can't blow up reserve() into an OOM abort.
  size_t groupCount = static_cast<size_t>(available / entrySize);
  if (groupCount > m_header.pageCount) {
    groupCount = m_header.pageCount;
  }
  if (groupCount == 0) {
    return XtcError::OK;
  }

  if (!m_file.seek64(m_subpageTableOffset)) {
    return XtcError::READ_ERROR;
  }

  m_subpageGroups.reserve(groupCount);
  std::vector<uint8_t> entryBuf(entrySize);
  for (size_t i = 0; i < groupCount; i++) {
    if (m_file.read(entryBuf.data(), entrySize) != entrySize) {
      return XtcError::READ_ERROR;
    }

    uint16_t startPage = 0;
    uint16_t endPage = 0;
    memcpy(&startPage, entryBuf.data(), sizeof(startPage));
    memcpy(&endPage, entryBuf.data() + sizeof(startPage), sizeof(endPage));

    if (startPage > 0) {
      startPage--;
    }
    if (endPage > 0) {
      endPage--;
    }

    if (startPage >= m_header.pageCount) {
      continue;
    }
    if (endPage >= m_header.pageCount) {
      endPage = m_header.pageCount - 1;
    }
    if (startPage > endPage) {
      continue;
    }

    m_subpageGroups.push_back(SubpageGroup{startPage, endPage});
  }

  LOG_DBG("XTC", "Subpage groups: %u", static_cast<unsigned int>(m_subpageGroups.size()));
  return XtcError::OK;
}

const std::vector<SubpageGroup>& XtcParser::getSubpageGroups() {
  // Lazy load on first access, mirroring getChapters()
  if (!m_subpageGroupsLoaded && m_subpageTableOffset != 0) {
    const XtcError err = readSubpageGroups();
    if (err != XtcError::OK) {
      LOG_ERR("XTC", "Failed to lazy-load subpage groups: %s", errorToString(err));
      m_subpageGroups.clear();
      m_subpageTableOffset = 0;  // keeps hasSubpages() consistent with the now-empty result
    }
    m_subpageGroupsLoaded = true;
    closeFile();
  }
  return m_subpageGroups;
}

bool XtcParser::getPageInfo(uint32_t pageIndex, PageInfo& info) { return readPageTableEntry(pageIndex, info); }

bool XtcParser::getPageOverlayInfo(uint32_t pageIndex, PageOverlayInfo& info) {
  info = PageOverlayInfo{};

  PageInfo page;
  if (!readPageTableEntry(pageIndex, page)) {
    return false;
  }
  if (!ensureFileOpen()) {
    return false;
  }
  if (!m_file.seek64(page.offset)) {
    return false;
  }

  // Always read the larger (overlay) header size -- for a plain page the
  // trailing 4 bytes actually belong to the start of its bitmap data, which
  // is harmless here since this is a peek-only read (doesn't affect any
  // other call's file position; loadPage() does its own independent seek).
  XtgOverlayPageHeader header;
  size_t bytesRead = m_file.read(reinterpret_cast<uint8_t*>(&header), sizeof(header));
  if (bytesRead < sizeof(XtgPageHeader)) {
    return false;
  }

  const bool isOverlayMagic = (header.base.magic == XTG_OVERLAY_MAGIC || header.base.magic == XTH_OVERLAY_MAGIC);
  info.isOverlay = isOverlayMagic;
  info.width = header.base.width;
  info.height = header.base.height;
  if (isOverlayMagic && bytesRead == sizeof(XtgOverlayPageHeader)) {
    info.patchX = header.patchX;
    info.patchY = header.patchY;
  }
  return true;
}

bool XtcParser::readPageBitmapHeader(uint32_t pageIndex, PageInfo& page, XtgPageHeader& pageHeader,
                                     size_t& bitmapSize) {
  if (!m_isOpen) {
    m_lastError = XtcError::FILE_NOT_FOUND;
    return false;
  }

  if (pageIndex >= m_header.pageCount) {
    m_lastError = XtcError::PAGE_OUT_OF_RANGE;
    return false;
  }

  if (!readPageTableEntry(pageIndex, page)) {
    m_lastError = XtcError::READ_ERROR;
    return false;
  }

  if (!ensureFileOpen()) {
    m_lastError = XtcError::FILE_NOT_FOUND;
    return false;
  }

  // Seek to page data
  if (!m_file.seek64(page.offset)) {
    LOG_DBG("XTC", "Failed to seek to page %u at offset %llu", pageIndex, static_cast<unsigned long long>(page.offset));
    m_lastError = XtcError::READ_ERROR;
    return false;
  }

  // Read page header. Always read the larger (overlay) header size first --
  // for a plain (non-overlay) page the trailing 4 bytes actually belong to
  // the start of its bitmap data, so the code below explicitly re-seeks to
  // the correct post-header offset once it knows which kind this is, rather
  // than trusting this read's end position.
  XtgOverlayPageHeader rawHeader;
  size_t headerRead = m_file.read(reinterpret_cast<uint8_t*>(&rawHeader), sizeof(rawHeader));
  if (headerRead < sizeof(XtgPageHeader)) {
    LOG_DBG("XTC", "Failed to read page header for page %u", pageIndex);
    m_lastError = XtcError::READ_ERROR;
    return false;
  }
  pageHeader = rawHeader.base;

  // Verify page magic: XTG/XTH for a normal page, or XTG_OVERLAY_MAGIC/
  // XTH_OVERLAY_MAGIC (XTCBZ/XTCBZH only -- see XtcTypes.h) for an overlay
  // patch page, matching this file's own bit depth either way.
  const bool isOverlay = (pageHeader.magic == XTG_OVERLAY_MAGIC || pageHeader.magic == XTH_OVERLAY_MAGIC);
  const uint32_t expectedMagic = isOverlay ? (m_bitDepth == 2 ? XTH_OVERLAY_MAGIC : XTG_OVERLAY_MAGIC)
                                            : (m_bitDepth == 2 ? XTH_MAGIC : XTG_MAGIC);
  if (pageHeader.magic != expectedMagic) {
    LOG_DBG("XTC", "Invalid page magic for page %u: 0x%08X (expected 0x%08X)", pageIndex, pageHeader.magic,
            expectedMagic);
    m_lastError = XtcError::INVALID_MAGIC;
    return false;
  }

  // Seek to the correct offset past whichever header size this page
  // actually has, since the single over-sized read above may have
  // consumed 4 bytes of bitmap data for a plain (non-overlay) page.
  const size_t actualHeaderSize = isOverlay ? sizeof(XtgOverlayPageHeader) : sizeof(XtgPageHeader);
  if (!m_file.seek64(page.offset + actualHeaderSize)) {
    LOG_DBG("XTC", "Failed to seek past page header for page %u", pageIndex);
    m_lastError = XtcError::READ_ERROR;
    return false;
  }

  // Calculate bitmap size based on bit depth. pageHeader.width/height are
  // this page's *own* on-disk bitmap dimensions -- the patch's own
  // (smaller) size for an overlay page, not the canvas size.
  // XTG (1-bit): Row-major, ((width+7)/8) * height bytes
  // XTH (2-bit): Two bit planes, column-major, ((width * height + 7) / 8) * 2 bytes
  if (m_bitDepth == 2) {
    // XTH: two bit planes, each containing (width * height) bits rounded up to bytes
    bitmapSize = ((static_cast<size_t>(pageHeader.width) * pageHeader.height + 7) / 8) * 2;
  } else {
    bitmapSize = ((pageHeader.width + 7) / 8) * pageHeader.height;
  }
  return true;
}

size_t XtcParser::loadPage(uint32_t pageIndex, uint8_t* buffer, size_t bufferSize) {
  PageInfo page;
  XtgPageHeader pageHeader;
  size_t bitmapSize;
  if (!readPageBitmapHeader(pageIndex, page, pageHeader, bitmapSize)) {
    return 0;
  }

  // Check buffer size
  if (bufferSize < bitmapSize) {
    LOG_DBG("XTC", "Buffer too small: need %u, have %u", bitmapSize, bufferSize);
    m_lastError = XtcError::MEMORY_ERROR;
    return 0;
  }

  if (pageHeader.compression == 0) {
    // Read raw bitmap data
    size_t bytesRead = m_file.read(buffer, bitmapSize);
    if (bytesRead != bitmapSize) {
      LOG_DBG("XTC", "Page read error: expected %u, got %u", bitmapSize, bytesRead);
      m_lastError = XtcError::READ_ERROR;
      return 0;
    }
    m_lastError = XtcError::OK;
    return bytesRead;
  }

  if (pageHeader.compression != 1) {
    LOG_DBG("XTC", "Unsupported page compression %u for page %u", pageHeader.compression, pageIndex);
    m_lastError = XtcError::DECOMPRESSION_ERROR;
    return 0;
  }

  // XTCBZ/XTCBZH only (see XtcTypes.h): pageHeader.dataSize is the on-disk deflated length,
  // immediately following the header we already read.
  // lib code can't call DiskLogger::flushNow() (src/-only, see the caller chain's
  // flush points instead), but this still lands in the RTC ring buffer immediately,
  // so it survives into a crash report even if the SD write hasn't happened yet.
  LOG_DBG("XTC", "Decompressing page %u: %u -> %u bytes (heap: %u)", pageIndex, pageHeader.dataSize, bitmapSize,
          (unsigned)ESP.getFreeHeap());
  if (decompressPage(pageHeader, buffer, bitmapSize) != bitmapSize) {
    LOG_DBG("XTC", "Failed to decompress page %u (compressed=%u -> expected %u)", pageIndex, pageHeader.dataSize,
            bitmapSize);
    m_lastError = XtcError::DECOMPRESSION_ERROR;
    return 0;
  }

  m_lastError = XtcError::OK;
  return bitmapSize;
}

size_t XtcParser::decompressPage(const XtgPageHeader& pageHeader, uint8_t* buffer, size_t bufferSize) {
  // One-shot mode: `buffer` holds the entire decompressed page, so back-references
  // resolve inside it and no 32KB window is allocated (see InflateStream's header comment).
  constexpr size_t kReadChunkSize = 1024;
  std::vector<uint8_t> readBuf(kReadChunkSize);

  PageInflateCtx ctx;
  ctx.file = &m_file;
  ctx.remaining = pageHeader.dataSize;
  ctx.readBuf = readBuf.data();
  ctx.readBufSize = readBuf.size();

  InflateStream inflate;
  if (!inflate.init(false)) {
    LOG_ERR("XTC", "Failed to init inflate stream for page decompression");
    return 0;
  }
  inflate.setFill(pageInflateFillCallback, &ctx);

  if (!inflate.read(buffer, bufferSize)) {
    return 0;
  }
  return bufferSize;
}

bool XtcParser::streamPageBitmap(uint32_t pageIndex, PageChunkFn fn, void* ctx) {
  PageInfo page;
  XtgPageHeader pageHeader;
  size_t bitmapSize;
  if (!readPageBitmapHeader(pageIndex, page, pageHeader, bitmapSize)) {
    return false;
  }

  // Small stack chunk buffer (well under the 256-byte local-variable budget):
  // the whole point of this path is to never hold a full-bitmap-sized buffer,
  // so this is intentionally tiny rather than sized for throughput.
  constexpr size_t kChunkSize = 128;
  uint8_t chunk[kChunkSize];

  if (pageHeader.compression == 0) {
    size_t offset = 0;
    while (offset < bitmapSize) {
      const size_t want = std::min(kChunkSize, bitmapSize - offset);
      const size_t got = m_file.read(chunk, want);
      if (got != want) {
        LOG_DBG("XTC", "Page stream read error for page %u: expected %u, got %u", pageIndex, want, got);
        m_lastError = XtcError::READ_ERROR;
        return false;
      }
      fn(ctx, chunk, got, offset);
      offset += got;
    }
    m_lastError = XtcError::OK;
    return true;
  }

  if (pageHeader.compression != 1) {
    LOG_DBG("XTC", "Unsupported page compression %u for page %u", pageHeader.compression, pageIndex);
    m_lastError = XtcError::DECOMPRESSION_ERROR;
    return false;
  }

  constexpr size_t kReadChunkSize = 1024;
  std::vector<uint8_t> readBuf(kReadChunkSize);
  PageInflateCtx inflateCtx;
  inflateCtx.file = &m_file;
  inflateCtx.remaining = pageHeader.dataSize;
  inflateCtx.readBuf = readBuf.data();
  inflateCtx.readBufSize = readBuf.size();

  InflateStream inflate;
  if (!inflate.init(true)) {  // streaming: 32KB window instead of a full-bitmap output buffer
    LOG_ERR("XTC", "Failed to init streaming inflate for page %u", pageIndex);
    m_lastError = XtcError::DECOMPRESSION_ERROR;
    return false;
  }
  inflate.setFill(pageInflateFillCallback, &inflateCtx);

  size_t offset = 0;
  while (offset < bitmapSize) {
    size_t produced = 0;
    const InflateStream::Status status = inflate.readAtMost(chunk, std::min(kChunkSize, bitmapSize - offset), &produced);
    if (produced > 0) {
      fn(ctx, chunk, produced, offset);
      offset += produced;
    }
    if (status == InflateStream::Status::Error) {
      LOG_DBG("XTC", "Streaming decompression failed for page %u at offset %zu", pageIndex, offset);
      m_lastError = XtcError::DECOMPRESSION_ERROR;
      return false;
    }
    if (status == InflateStream::Status::Done && offset < bitmapSize) {
      LOG_DBG("XTC", "Streaming decompression truncated for page %u: got %zu of %zu bytes", pageIndex, offset,
              bitmapSize);
      m_lastError = XtcError::DECOMPRESSION_ERROR;
      return false;
    }
  }
  m_lastError = XtcError::OK;
  return true;
}

bool XtcParser::isValidXtcFile(const char* filepath) {
  HalFile file;
  if (!Storage.openFileForRead("XTC", filepath, file)) {
    return false;
  }

  uint32_t magic = 0;
  size_t bytesRead = file.read(reinterpret_cast<uint8_t*>(&magic), sizeof(magic));
  file.close();

  if (bytesRead != sizeof(magic)) {
    return false;
  }

  return (magic == XTC_MAGIC || magic == XTCH_MAGIC || magic == XTCBZ_MAGIC || magic == XTCBZH_MAGIC);
}

}  // namespace xtc
