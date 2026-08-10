#include "SdFontFlashCache.h"

#include <HalStorage.h>
#include <Logging.h>
#include <spi_flash_mmap.h>

#include <algorithm>
#include <cstring>
#include <memory>

namespace {

constexpr char kMagic[8] = {'C', 'P', 'F', 'C', 'A', 'C', 'H', 'E'};
constexpr uint32_t kHeaderVersion = 1;
// One flash erase sector -- comfortably larger than sizeof(CacheHeader), and erasing
// it is a single fast op regardless of struct size.
constexpr size_t kHeaderSectorSize = SPI_FLASH_SEC_SIZE;  // 4 KiB
constexpr size_t kEraseChunk = 65536;                     // 64 KiB block-erase granularity
constexpr size_t kIoChunk = 4096;
constexpr uint32_t kFnvOffsetBasis = 2166136261u;
constexpr uint32_t kFnvPrime = 16777619u;
// Diagnostic-only expectation, must match the `spiffs` line in partitions.csv. A
// mismatch doesn't change behavior (the actual esp_partition_t size from the
// device's burned partition table is always authoritative), but is worth
// flagging loudly: the partition table is only rewritten by a full/USB flash,
// never by an app-only OTA update, so a device that was last full-flashed
// before partitions.csv's `spiffs` entry reached its current size will keep
// mapping a smaller partition than this build assumes indefinitely.
constexpr size_t kExpectedPartitionSize = 0x360000;

// On-flash header, written at partition offset 0. `valid` is only ever set as part
// of the LAST write of an activation (see commitHeader) so a crash mid-copy is
// always observed as invalid, never as a torn-but-"valid" payload.
struct CacheHeader {
  char magic[8];
  uint32_t version;
  uint32_t valid;
  char familyName[64];
  uint8_t pointSize;
  uint8_t reserved[3];
  uint32_t contentHash;
  uint32_t payloadSize;
  uint32_t payloadFnv1a;
};
static_assert(sizeof(CacheHeader) <= kHeaderSectorSize, "CacheHeader must fit in one erase sector");

uint32_t fnv1a(const uint8_t* data, size_t len, uint32_t hash = kFnvOffsetBasis) {
  for (size_t i = 0; i < len; i++) {
    hash ^= data[i];
    hash *= kFnvPrime;
  }
  return hash;
}

}  // namespace

SdFontFlashCache& SdFontFlashCache::instance() {
  static SdFontFlashCache cache;
  return cache;
}

bool SdFontFlashCache::ensurePartitionMapped() {
  // Only try once per boot: a missing/unmappable partition won't fix itself
  // mid-session, and retrying on every font switch would just spam the log.
  if (mapAttempted_) return mappedBase_ != nullptr;
  mapAttempted_ = true;

  partition_ = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, nullptr);
  if (!partition_) {
    LOG_ERR("SDFC", "no spiffs-subtype partition found -- SD fonts will use SD paging only");
    return false;
  }
  if (partition_->size <= kHeaderSectorSize) {
    LOG_ERR("SDFC", "partition '%s' too small (%u bytes) for the font cache", partition_->label,
            static_cast<unsigned>(partition_->size));
    partition_ = nullptr;
    return false;
  }

  const void* mapped = nullptr;
  const esp_err_t err =
      esp_partition_mmap(partition_, 0, partition_->size, ESP_PARTITION_MMAP_DATA, &mapped, &mmapHandle_);
  if (err != ESP_OK || !mapped) {
    LOG_ERR("SDFC", "esp_partition_mmap failed for '%s': %s", partition_->label, esp_err_to_name(err));
    partition_ = nullptr;
    return false;
  }

  mappedBase_ = static_cast<const uint8_t*>(mapped);
  mappedSize_ = partition_->size;
  LOG_DBG("SDFC", "mapped partition '%s' (%u bytes) at %p", partition_->label, static_cast<unsigned>(mappedSize_),
          static_cast<const void*>(mappedBase_));
  if (mappedSize_ != kExpectedPartitionSize) {
    LOG_ERR("SDFC",
            "partition '%s' is %u bytes but this build's partitions.csv declares %u -- device's burned "
            "partition table is stale (needs a full/USB reflash, not OTA) or its physical flash (%u bytes "
            "total) is smaller than this build assumes; large .cpfont files will keep failing to promote "
            "until this is resolved",
            partition_->label, static_cast<unsigned>(mappedSize_), static_cast<unsigned>(kExpectedPartitionSize),
            static_cast<unsigned>(ESP.getFlashChipSize()));
  }
  return true;
}

const uint8_t* SdFontFlashCache::tryLoad(const char* familyName, uint8_t pointSize, uint32_t contentHash,
                                          size_t expectedSize) {
  if (!ensurePartitionMapped()) return nullptr;

  // Copy through a local, naturally-aligned struct rather than dereferencing the
  // mapped flash pointer as a CacheHeader* directly -- keeps this code correct even
  // if a future header field ever widens the alignment requirement (RISC-V faults on
  // unaligned multi-byte loads through a non-packed type).
  CacheHeader header;
  memcpy(&header, mappedBase_, sizeof(header));

  if (memcmp(header.magic, kMagic, sizeof(kMagic)) != 0 || header.version != kHeaderVersion || header.valid != 1) {
    LOG_DBG("SDFC", "no valid cached font resident (cold cache or previous copy was interrupted)");
    return nullptr;
  }
  header.familyName[sizeof(header.familyName) - 1] = '\0';
  if (strncmp(header.familyName, familyName, sizeof(header.familyName)) != 0 || header.pointSize != pointSize ||
      header.contentHash != contentHash || header.payloadSize != expectedSize) {
    LOG_DBG("SDFC", "cache miss: resident=%s@%u (hash=0x%08x, %u bytes), wanted=%s@%u (hash=0x%08x, %u bytes)",
            header.familyName, header.pointSize, header.contentHash, static_cast<unsigned>(header.payloadSize),
            familyName, pointSize, contentHash, static_cast<unsigned>(expectedSize));
    return nullptr;
  }
  if (kHeaderSectorSize + header.payloadSize > mappedSize_) {
    // Header matched but claims a payload larger than the partition -- can only
    // happen from flash corruption (bit rot) landing on a value that still passes
    // the checks above. Treat as invalid rather than reading out of bounds.
    LOG_ERR("SDFC", "resident header payloadSize (%u) exceeds partition -- treating as invalid",
            static_cast<unsigned>(header.payloadSize));
    return nullptr;
  }

  const uint8_t* payload = mappedBase_ + kHeaderSectorSize;
  const uint32_t actualFnv = fnv1a(payload, header.payloadSize);
  if (actualFnv != header.payloadFnv1a) {
    LOG_ERR("SDFC", "resident payload checksum mismatch (stored=0x%08x actual=0x%08x) -- treating as miss",
            header.payloadFnv1a, actualFnv);
    return nullptr;
  }

  LOG_DBG("SDFC", "cache hit: %s@%u (hash=0x%08x, %u bytes)", familyName, pointSize, contentHash,
          static_cast<unsigned>(expectedSize));
  return payload;
}

size_t SdFontFlashCache::payloadCapacity() {
  if (!ensurePartitionMapped()) return 0;
  return mappedSize_ - kHeaderSectorSize;
}

bool SdFontFlashCache::invalidateHeader() {
  if (esp_partition_erase_range(partition_, 0, kHeaderSectorSize) != ESP_OK) {
    LOG_ERR("SDFC", "failed to erase header sector (invalidate)");
    return false;
  }
  return true;
}

bool SdFontFlashCache::commitHeader(const char* familyName, uint8_t pointSize, uint32_t contentHash,
                                     uint32_t payloadSize, uint32_t payloadFnv1a) {
  CacheHeader header{};
  memcpy(header.magic, kMagic, sizeof(kMagic));
  header.version = kHeaderVersion;
  header.valid = 1;
  strncpy(header.familyName, familyName, sizeof(header.familyName) - 1);
  header.pointSize = pointSize;
  header.contentHash = contentHash;
  header.payloadSize = payloadSize;
  header.payloadFnv1a = payloadFnv1a;

  if (esp_partition_erase_range(partition_, 0, kHeaderSectorSize) != ESP_OK) {
    LOG_ERR("SDFC", "failed to erase header sector (commit)");
    return false;
  }
  if (esp_partition_write(partition_, 0, &header, sizeof(header)) != ESP_OK) {
    LOG_ERR("SDFC", "failed to write header (commit)");
    return false;
  }
  return true;
}

const uint8_t* SdFontFlashCache::store(const char* familyName, uint8_t pointSize, uint32_t contentHash,
                                        HalFile& source, size_t size) {
  if (!ensurePartitionMapped()) return nullptr;
  if (kHeaderSectorSize + size > mappedSize_) {
    LOG_ERR("SDFC", "%s@%u is %u bytes -- partition only has %u bytes of payload space", familyName, pointSize,
            static_cast<unsigned>(size), static_cast<unsigned>(mappedSize_ - kHeaderSectorSize));
    return nullptr;
  }

  // Invalidate before touching the payload: if we crash partway through the copy
  // below, the next boot's tryLoad() sees valid=0 and falls back to SD paging
  // instead of trusting a torn payload against a stale-but-matching header.
  if (!invalidateHeader()) return nullptr;

  auto buffer = std::unique_ptr<uint8_t[]>(new (std::nothrow) uint8_t[kIoChunk]);
  if (!buffer) {
    LOG_ERR("SDFC", "OOM allocating %u-byte copy buffer", static_cast<unsigned>(kIoChunk));
    return nullptr;
  }

  LOG_DBG("SDFC", "copying %s@%u (%u bytes) from SD to flash cache", familyName, pointSize,
          static_cast<unsigned>(size));

  size_t streamPos = 0;
  size_t erasedUpto = 0;
  uint32_t runningFnv = kFnvOffsetBasis;
  while (streamPos < size) {
    const size_t destOffset = kHeaderSectorSize + streamPos;
    if (destOffset >= erasedUpto) {
      const size_t eraseBase = std::max<size_t>(erasedUpto, kHeaderSectorSize);
      const size_t eraseLen = std::min<size_t>(kEraseChunk, mappedSize_ - eraseBase);
      if (esp_partition_erase_range(partition_, eraseBase, eraseLen) != ESP_OK) {
        LOG_ERR("SDFC", "payload erase @%u (len=%u) failed", static_cast<unsigned>(eraseBase),
                static_cast<unsigned>(eraseLen));
        return nullptr;
      }
      erasedUpto = eraseBase + eraseLen;
    }

    const size_t want = std::min<size_t>(kIoChunk, size - streamPos);
    const int got = source.read(buffer.get(), want);
    if (got <= 0 || static_cast<size_t>(got) != want) {
      LOG_ERR("SDFC", "SD read @%u failed (got=%d want=%u)", static_cast<unsigned>(streamPos), got,
              static_cast<unsigned>(want));
      return nullptr;
    }
    if (esp_partition_write(partition_, destOffset, buffer.get(), want) != ESP_OK) {
      LOG_ERR("SDFC", "flash write @%u failed", static_cast<unsigned>(destOffset));
      return nullptr;
    }
    runningFnv = fnv1a(buffer.get(), want, runningFnv);
    streamPos += want;
  }

  // Verify by re-reading the bytes actually landed in flash (via the mmap'd
  // pointer), not just trusting esp_partition_write()'s return code -- catches
  // write corruption in addition to the SD-read errors already checked above.
  const uint8_t* payload = mappedBase_ + kHeaderSectorSize;
  const uint32_t verifyFnv = fnv1a(payload, size);
  if (verifyFnv != runningFnv) {
    LOG_ERR("SDFC", "post-write verify mismatch (source=0x%08x flash=0x%08x) -- aborting, cache left invalid",
            runningFnv, verifyFnv);
    return nullptr;
  }

  if (!commitHeader(familyName, pointSize, contentHash, static_cast<uint32_t>(size), runningFnv)) return nullptr;

  LOG_INF("SDFC", "stored %s@%u (%u bytes, hash=0x%08x) to flash cache", familyName, pointSize,
          static_cast<unsigned>(size), contentHash);
  return payload;
}
