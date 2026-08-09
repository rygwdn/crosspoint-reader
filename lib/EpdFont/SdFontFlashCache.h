#pragma once

#include <cstddef>
#include <cstdint>

#include <esp_partition.h>

class HalFile;

// Backs SdCardFont's "flash residency" fast path (see SdCardFont::activateFlashCache):
// on activation, the active .cpfont file is copied byte-for-byte into a reserved
// flash partition and memory-mapped, so glyph lookups become plain pointer
// dereferences with zero SD I/O -- the same cost shape as a builtin font.
//
// Deliberately reuses the `spiffs`-subtype partition from partitions.csv rather than
// adding a new partition table entry: that partition has shipped byte-identical in
// every release since the project's "Public release" commit and is never mounted as
// a filesystem anywhere in this codebase (grep confirms no esp_spiffs_mount /
// SPIFFS.begin call exists), so repurposing it needs no partition-table change and
// therefore works over a normal OTA update -- no full device reflash required.
//
// Single slot: holds exactly one cached font (family + point size) at a time,
// matching SdCardFontManager's "one reader-size font loaded at once" model. Every
// method fails soft (returns false/nullptr, logs why) so SdCardFont can always fall
// back to its existing SD-paging path -- this class never causes a hard failure.
class SdFontFlashCache {
 public:
  static SdFontFlashCache& instance();

  SdFontFlashCache(const SdFontFlashCache&) = delete;
  SdFontFlashCache& operator=(const SdFontFlashCache&) = delete;

  // Returns a mapped pointer to the cached payload if the partition already holds
  // (familyName, pointSize, contentHash) with a payload of exactly `expectedSize`
  // bytes whose checksum still verifies. Returns nullptr on any miss, mismatch, or
  // failure -- callers must not assume "not nullptr" vs "nullptr" implies anything
  // beyond hit/no-hit.
  const uint8_t* tryLoad(const char* familyName, uint8_t pointSize, uint32_t contentHash, size_t expectedSize);

  // Copies `size` bytes from `source` (read starting at its current position) into
  // the partition, verifies the round trip by re-reading the just-written flash,
  // then commits the header last -- so a power loss mid-copy leaves the header
  // invalid and the next boot's tryLoad() reports a clean miss rather than serving a
  // torn payload. Returns a mapped pointer to the payload on success, nullptr on any
  // failure (partition missing/too small, erase/write/verify failure) -- caller must
  // fall back to SD paging.
  const uint8_t* store(const char* familyName, uint8_t pointSize, uint32_t contentHash, HalFile& source, size_t size);

 private:
  SdFontFlashCache() = default;

  bool ensurePartitionMapped();
  bool invalidateHeader();
  bool commitHeader(const char* familyName, uint8_t pointSize, uint32_t contentHash, uint32_t payloadSize,
                     uint32_t payloadFnv1a);

  const esp_partition_t* partition_ = nullptr;
  const uint8_t* mappedBase_ = nullptr;
  size_t mappedSize_ = 0;
  esp_partition_mmap_handle_t mmapHandle_ = 0;
  bool mapAttempted_ = false;
};
