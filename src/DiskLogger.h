#pragma once

#include <cstddef>
#include <freertos/semphr.h>

// Captures firmware log lines to SD card by flushing the RTC ring buffer every
// FLUSH_INTERVAL log calls. Enabled/disabled at runtime via SETTINGS.diskLogsEnabled.
// Call begin() once at boot (after Storage and SETTINGS are ready) to register
// the log callback. No heap allocation; all state is static.
class DiskLogger {
 public:
  static void begin();
  // Callback registered with setDiskLogCallback(). Increments the line counter
  // and triggers a ring-buffer flush every FLUSH_INTERVAL calls when enabled.
  static void logLine(const char* line);
  // Flush the current ring buffer to disk immediately (e.g. before download).
  static void flushNow();
  // Delete both log files from SD card.
  static void clear();

 private:
  static constexpr const char* LOG_PATH = "/.crosspoint/debug.log";
  static constexpr const char* LOG_PATH_OLD = "/.crosspoint/debug.log.old";
  static constexpr size_t MAX_LOG_FILE_SIZE = 512UL * 1024UL;
  // Must match MAX_LOG_LINES in Logging.cpp so we flush exactly one full cycle.
  static constexpr int FLUSH_INTERVAL = 16;

  static int linesSinceFlush;
  // Guards against same-task re-entry: HalStorage calls LOG_ERR internally,
  // which would otherwise recurse back into logLine() → writeRingBufferToFile().
  static volatile bool reentrant;
  // Guards against cross-task concurrent flushes (render task vs. main task).
  // Non-blocking trylock: whichever task loses the race skips this flush cycle;
  // the ring buffer content persists in RTC memory and will be written next time.
  static SemaphoreHandle_t flushMutex;

  static void writeRingBufferToFile();
  static void rotateIfNeeded();
};
