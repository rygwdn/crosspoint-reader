#pragma once

#include <string>

class OtaUpdater {
  bool updateAvailable = false;
  std::string latestVersion;
  std::string otaUrl;
  std::string expectedSha256;  // lowercase hex, empty if manifest didn't supply one
  size_t otaSize = 0;
  size_t processedSize = 0;
  size_t totalSize = 0;

 public:
  using ProgressCallback = void (*)(void* ctx);

  enum OtaUpdaterError {
    OK = 0,
    NO_UPDATE,
    HTTP_ERROR,
    JSON_PARSE_ERROR,
    UPDATE_OLDER_ERROR,
    INTERNAL_UPDATE_ERROR,
    OOM_ERROR,
    WRONG_DEVICE_ERROR,
    CHECKSUM_ERROR,
  };

  size_t getOtaSize() const { return otaSize; }

  size_t getProcessedSize() const { return processedSize; }

  size_t getTotalSize() const { return totalSize; }

  OtaUpdater() = default;
  bool isUpdateNewer() const;
  const std::string& getLatestVersion() const;
  OtaUpdaterError checkForUpdate();
  OtaUpdaterError installUpdate(ProgressCallback onProgress = nullptr, void* ctx = nullptr);

  // True when OTA_MANIFEST_URL was overridden away from the upstream GitHub
  // Releases API (see scripts/local_ota_server.py), i.e. this build checks a
  // self-hosted manifest server instead.
  static bool usesCustomManifestServer();
  // host[:port] parsed out of the configured manifest URL, for UI display
  // when usesCustomManifestServer() is true.
  static std::string manifestServerHost();
};
