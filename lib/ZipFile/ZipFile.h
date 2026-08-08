#pragma once
#include <HalStorage.h>
#include <InflateStream.h>

#include <deque>
#include <memory>
#include <string>
#include <string_view>
#include <unordered_map>

// Defined below, after class ZipFile: it holds a ZipFile member by value, which needs
// ZipFile to be a complete type. Forward-declared here only so ZipFile's own
// beginStreamToFile()/continueStreamToFile() declarations can reference it.
struct ZipStreamContext;

class ZipFile {
 public:
  struct FileStatSlim {
    uint16_t method;             // Compression method
    uint32_t compressedSize;     // Compressed size
    uint32_t uncompressedSize;   // Uncompressed size
    uint32_t localHeaderOffset;  // Offset of local file header
  };

  struct ZipDetails {
    uint32_t centralDirOffset;
    uint16_t totalEntries;
    bool isSet;
  };

  // Target for batch uncompressed size lookup (sorted by hash, then len)
  struct SizeTarget {
    uint64_t hash;   // FNV-1a 64-bit hash of normalized path
    uint16_t len;    // Length of path for collision reduction
    uint16_t index;  // Caller's index (e.g. spine index)
  };

  // FNV-1a 64-bit hash computed from char buffer (no std::string allocation)
  static uint64_t fnvHash64(const char* s, size_t len) {
    uint64_t hash = 14695981039346656037ull;
    for (size_t i = 0; i < len; i++) {
      hash ^= static_cast<uint8_t>(s[i]);
      hash *= 1099511628211ull;
    }
    return hash;
  }

 private:
  const std::string& filePath;
  HalFile file;
  ZipDetails zipDetails = {0, 0, false};
  std::unordered_map<std::string, FileStatSlim> fileStatSlimCache;

  // Cursor for sequential central-dir scanning optimization
  uint32_t lastCentralDirPos = 0;
  bool lastCentralDirPosValid = false;

  bool loadFileStatSlim(const char* filename, FileStatSlim* fileStat);
  long getDataOffset(const FileStatSlim& fileStat);
  bool loadZipDetails();
  // Fill callback for beginStreamToFile()/continueStreamToFile()'s InflateStream (see
  // ZipStreamContext below); a static member so it can reach ctx->zip.file (another
  // ZipFile instance's own private member -- access control is per-class, not per-instance).
  static size_t streamFillCallback(void* vctx, const uint8_t** data);

 public:
  explicit ZipFile(const std::string& filePath) : filePath(filePath) {}
  ~ZipFile() = default;
  // Zip file can be opened and closed by hand in order to allow for quick calculation of inflated file size
  // It is NOT recommended to pre-open it for any kind of inflation due to memory constraints
  bool isOpen() const { return !!file; }
  bool open();
  bool close();
  bool loadAllFileStatSlims();
  bool getInflatedFileSize(const char* filename, size_t* size);
  // Batch lookup: scan ZIP central dir once and fill sizes for matching targets.
  // targets must be sorted by (hash, len). sizes[target.index] receives uncompressedSize.
  // Returns number of targets matched.
  int fillUncompressedSizes(std::deque<SizeTarget>& targets, std::deque<uint32_t>& sizes);
  // Due to the memory required to run each of these, it is recommended to not preopen the zip file for multiple
  // These functions will open and close the zip as needed
  uint8_t* readFileToMemory(const char* filename, size_t* size = nullptr, bool trailingNullByte = false);
  // allowEarlyStop: a short write from `out` is treated as the sink asking to
  // stop (returns true) instead of a write failure — used by header probes
  // that only need the first bytes of an entry.
  bool readFileToStream(const char* filename, Print& out, size_t chunkSize, bool allowEarlyStop = false);

  enum class StreamStatus { More, Done, Error };

  // Opens destPath for writing and locates `filename` within the zip at zipPath, but does
  // NOT read or decompress anything yet -- that's continueStreamToFile()'s job, called
  // repeatedly afterward. Returns nullptr on failure (bad entry, can't open destPath, OOM);
  // any partially-opened destPath is removed.
  static std::unique_ptr<ZipStreamContext> beginStreamToFile(const std::string& zipPath, const char* filename,
                                                              const std::string& destPath, size_t chunkSize);
  // Continue a stream started by beginStreamToFile(), stopping after roughly maxDurationMs
  // of wall-clock time (0 = run to completion, for a foreground/blocking caller) or sooner
  // if the entry finishes or errors. Safe to call repeatedly; each call resumes exactly
  // where the previous one left off. On Error, ctx's destination file is left as-is for the
  // caller to remove (mirrors abandonBuild()-style cleanup elsewhere in this codebase).
  static StreamStatus continueStreamToFile(ZipStreamContext& ctx, unsigned long maxDurationMs);

  template <typename F>
  bool enumerateFilePaths(F&& callback) {
    if (!fileStatSlimCache.empty()) {
      for (const auto& entry : fileStatSlimCache) {
        callback(std::string_view{entry.first});
      }
      return true;
    }

    return enumerateFileEntries([&callback](std::string_view path, uint32_t, uint32_t) { callback(path); });
  }

  // Callback receives (path, crc32, compressedSize) for each central-directory
  // entry. Always scans the central directory: the slim-stat cache does not
  // hold CRCs.
  template <typename F>
  bool enumerateFileEntries(F&& callback) {
    const bool wasOpen = isOpen();
    if (!wasOpen && !open()) {
      return false;
    }

    if (!loadZipDetails()) {
      if (!wasOpen) {
        close();
      }
      return false;
    }

    file.seek(zipDetails.centralDirOffset);

    uint32_t sig;
    char itemName[256];

    while (file.available()) {
      file.read(&sig, 4);
      if (sig != 0x02014b50) {
        break;
      }

      file.seekCur(12);
      uint32_t crc32, compressedSize;
      file.read(&crc32, 4);
      file.read(&compressedSize, 4);
      file.seekCur(4);
      uint16_t nameLen, m, k;
      file.read(&nameLen, 2);
      file.read(&m, 2);
      file.read(&k, 2);
      file.seekCur(12);

      if (nameLen < sizeof(itemName)) {
        file.read(itemName, nameLen);
        itemName[nameLen] = '\0';
        callback(std::string_view{itemName, nameLen}, crc32, compressedSize);
      } else {
        file.seekCur(nameLen);
      }

      file.seekCur(m + k);
    }

    if (!wasOpen) {
      close();
    }
    return true;
  }
};

// Resumable state for one in-progress ZipFile::beginStreamToFile()/continueStreamToFile()
// extraction, spanning as many continueStreamToFile() calls as needed. Owns the destination
// file, the source zip's own file handle (via `zip`), and InflateStream's bookkeeping.
//
// For the DEFLATED case, InflateStream's own ~43KB decompressor state (borrowed from the
// framebuffer via buildscratch when a FrameBufferLoan is active, heap otherwise -- see
// lib/Memory/BuildScratch.h) is NOT held across continueStreamToFile() calls: each call
// claims it fresh, restores whatever was spilled to stateFile by the previous call, does
// bounded work, and spills it back before returning (unless the entry finished). This lets a
// caller wrap each call in its own short-lived FrameBufferLoan (see
// Section::buildSomeMore()) instead of holding the loan -- and the framebuffer it denies to
// everything else -- for the entry's entire multi-tick extraction.
//
// readBuf/outputBuf (chunkSize each) and stateFile's ~43KB are the fixed cost this struct
// carries directly: pass a small chunkSize for a background/interruptible caller,
// readFileToStream()'s existing 8KB for a one-shot foreground one.
//
// Declared after ZipFile (not nested inside it): it holds a ZipFile member by value, which
// needs ZipFile to already be a complete type.
struct ZipStreamContext {
  explicit ZipStreamContext(const std::string& zipPath) : zip(zipPath) {}
  ~ZipStreamContext();
  ZipStreamContext(const ZipStreamContext&) = delete;
  ZipStreamContext& operator=(const ZipStreamContext&) = delete;

  ZipFile zip;
  HalFile destFile;
  HalFile stateFile;      // DEFLATED only: single-slot spill of InflateStream state between calls
  std::string statePath;  // stateFile's path, removed by the destructor once no longer needed
  InflateStream inflate;
  std::unique_ptr<uint8_t[]> readBuf;
  std::unique_ptr<uint8_t[]> outputBuf;
  size_t chunkSize = 0;
  uint32_t fileRemaining = 0;  // compressed bytes not yet read from the zip entry
  uint32_t inflatedSize = 0;   // expected total decompressed size
  uint32_t totalProduced = 0;  // decompressed bytes written to destFile so far
  bool storedMethod = false;   // true: ZIP_METHOD_STORED (raw copy, no inflate)
  bool hasSpilledState = false;  // true once a prior call has written stateFile
};
