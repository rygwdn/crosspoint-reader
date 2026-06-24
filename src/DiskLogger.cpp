#include "DiskLogger.h"

#include <HalStorage.h>
#include <Logging.h>

#include <string>

#include "CrossPointSettings.h"

int DiskLogger::linesSinceFlush = 0;
volatile bool DiskLogger::reentrant = false;
SemaphoreHandle_t DiskLogger::flushMutex = nullptr;

void DiskLogger::begin() {
  flushMutex = xSemaphoreCreateMutex();
  assert(flushMutex != nullptr);
  setDiskLogCallback(&DiskLogger::logLine);
}

void DiskLogger::logLine(const char* /*line*/) {
  if (!SETTINGS.diskLogsEnabled || reentrant) return;
  ++linesSinceFlush;
  if (linesSinceFlush >= FLUSH_INTERVAL) {
    linesSinceFlush = 0;
    writeRingBufferToFile();
  }
}

void DiskLogger::flushNow() {
  if (reentrant) return;
  linesSinceFlush = 0;
  writeRingBufferToFile();
}

void DiskLogger::clear() {
  Storage.remove(LOG_PATH);
  Storage.remove(LOG_PATH_OLD);
}

void DiskLogger::rotateIfNeeded() {
  HalFile check = Storage.open(LOG_PATH);
  if (!check) return;
  const size_t sz = check.fileSize();
  check.close();
  if (sz <= MAX_LOG_FILE_SIZE) return;
  Storage.remove(LOG_PATH_OLD);
  Storage.rename(LOG_PATH, LOG_PATH_OLD);
}

void DiskLogger::writeRingBufferToFile() {
  // Non-blocking: if another task is already flushing, skip this cycle.
  // The ring buffer lives in RTC memory and will be written on the next flush.
  if (!flushMutex || xSemaphoreTake(flushMutex, 0) != pdTRUE) return;

  // Set reentrant before any SD access so that LOG_ERR calls from HalStorage
  // (which would re-enter logLine → here) are caught and short-circuited above.
  reentrant = true;

  std::string content = getLastLogs();
  if (!content.empty()) {
    rotateIfNeeded();
    // Open with O_WRITE | O_CREAT (no truncate), seek to end for append semantics.
    HalFile file = Storage.open(LOG_PATH, O_WRITE | O_CREAT);
    if (file) {
      file.seek(file.fileSize());
      file.write(content.c_str(), content.size());
      file.flush();
      // file closes automatically via DESTRUCTOR_CLOSES_FILE
    }
  }

  reentrant = false;
  xSemaphoreGive(flushMutex);
}
