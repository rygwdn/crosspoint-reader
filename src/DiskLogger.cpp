#include "DiskLogger.h"

#include <HalStorage.h>
#include <Logging.h>
#include <freertos/semphr.h>

#include <string>

#include "CrossPointSettings.h"

int DiskLogger::linesSinceFlush = 0;
volatile bool DiskLogger::reentrant = false;
// Guards against cross-task concurrent flushes (render task vs. main task).
// Non-blocking trylock: whichever task loses the race skips this flush cycle;
// the ring buffer content persists in RTC memory and will be written next time.
static SemaphoreHandle_t flushMutex = nullptr;

std::string DiskLogger::getLogFilePath(int generation) {
  if (generation <= 0) {
    return LOG_BASE_PATH;
  }
  return std::string(LOG_BASE_PATH) + "." + std::to_string(generation);
}

void DiskLogger::begin() {
  flushMutex = xSemaphoreCreateMutex();
  assert(flushMutex != nullptr);

  // Preserve anything left over from before this boot into the previous session's log
  // file before rotating it out. Must happen before setDiskLogCallback() below so a
  // LOG_ERR from a failed SD op here can't re-enter logLine()/writeRingBufferToFile().
  if (SETTINGS.diskLogsEnabled) {
    flushBootSnapshot();
  }
  rotateGenerations();

  setDiskLogCallback(&DiskLogger::logLine);
}

void DiskLogger::flushBootSnapshot() {
  std::string content = getBootLogSnapshot();
  if (content.empty()) return;

  HalFile file = Storage.open(LOG_BASE_PATH, O_WRITE | O_CREAT);
  if (!file) {
    LOG_ERR("LOG", "Failed to open %s for boot-snapshot flush", LOG_BASE_PATH);
    return;
  }
  file.seek(file.fileSize());
  file.write(content.c_str(), content.size());
  file.flush();
  // file closes automatically via DESTRUCTOR_CLOSES_FILE
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
  for (int gen = 0; gen < MAX_LOG_GENERATIONS; gen++) {
    Storage.remove(getLogFilePath(gen).c_str());
  }
}

void DiskLogger::rotateGenerations() {
  // Drop the oldest generation, then shift each remaining one up by one slot, oldest
  // shift first so we never clobber a file we haven't moved yet. Storage.remove/rename
  // are no-ops (return false, no log spam) when the source doesn't exist, so this is
  // safe to call unconditionally even before any log file has ever been written.
  Storage.remove(getLogFilePath(MAX_LOG_GENERATIONS - 1).c_str());
  for (int gen = MAX_LOG_GENERATIONS - 2; gen >= 0; --gen) {
    Storage.rename(getLogFilePath(gen).c_str(), getLogFilePath(gen + 1).c_str());
  }
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
    HalFile check = Storage.open(LOG_BASE_PATH);
    if (check) {
      const size_t sz = check.fileSize();
      check.close();  // must close before reopening the same path below
      if (sz > MAX_LOG_FILE_SIZE) {
        rotateGenerations();
      }
    }
    // Open with O_WRITE | O_CREAT (no truncate), seek to end for append semantics.
    HalFile file = Storage.open(LOG_BASE_PATH, O_WRITE | O_CREAT);
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
