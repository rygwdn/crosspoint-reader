#pragma once

#include <cstddef>
#include <string>

// Captures firmware log lines to SD card by flushing the RTC ring buffer every
// FLUSH_INTERVAL log calls. Enabled/disabled at runtime via SETTINGS.diskLogsEnabled.
// Call begin() once at boot (after Storage and SETTINGS are ready) to flush any content
// left over from before this boot, rotate log-file generations, and register the log
// callback. No heap allocation beyond a transient boot-flush string; all other state is
// static.
class DiskLogger {
 public:
  static void begin();
  // Callback registered with setDiskLogCallback(). Increments the line counter
  // and triggers a ring-buffer flush every FLUSH_INTERVAL calls when enabled.
  static void logLine(const char* line);
  // Flush the current ring buffer to disk immediately (e.g. before download).
  static void flushNow();
  // Delete all log file generations from SD card.
  static void clear();

  // Path for a given generation: 0 = the currently active log, 1..getGenerationCount()-1
  // = rotated-out backups, oldest last (debug.log, debug.log.1, debug.log.2, ...).
  // Used by the log viewer to enumerate files; doesn't check the file actually exists.
  static std::string getLogFilePath(int generation);
  static constexpr int getGenerationCount() { return MAX_LOG_GENERATIONS; }

 private:
  static constexpr const char* LOG_BASE_PATH = "/.crosspoint/debug.log";
  static constexpr size_t MAX_LOG_FILE_SIZE = 512UL * 1024UL;
  // debug.log (active) + this many rotated backups.
  static constexpr int MAX_LOG_GENERATIONS = 5;
  // Must match MAX_LOG_LINES in Logging.cpp so we flush exactly one full cycle.
  static constexpr int FLUSH_INTERVAL = 16;

  static int linesSinceFlush;
  // Guards against same-task re-entry: HalStorage calls LOG_ERR internally,
  // which would otherwise recurse back into logLine() → writeRingBufferToFile().
  static volatile bool reentrant;

  static void writeRingBufferToFile();
  // Shifts debug.log -> .1 -> .2 ... dropping whatever falls off the end of
  // MAX_LOG_GENERATIONS. Safe to call when some/all generations don't exist yet.
  static void rotateGenerations();
  // Appends the frozen pre-boot ring-buffer snapshot (see Logging.h) to the current
  // debug.log, before rotateGenerations() moves it aside. Call once at boot, before the
  // live disk-log callback is registered.
  static void flushBootSnapshot();
};
