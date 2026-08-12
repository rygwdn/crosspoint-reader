#pragma once

#include <Arduino.h>
#include <HardwareSerial.h>
#if defined(ARDUINO_USB_CDC_ON_BOOT) && ARDUINO_USB_CDC_ON_BOOT
#include <HWCDC.h>
#endif

#include <string>

/*
Define ENABLE_SERIAL_LOG to enable logging
Can be set in platformio.ini build_flags or as a compile definition

Define LOG_LEVEL to control log verbosity:
0 = ERR only
1 = ERR + INF
2 = ERR + INF + DBG
If not defined, defaults to 0

If you have a legitimate need for raw Serial access (e.g., binary data,
special formatting), use the underlying logSerial object directly:
    logSerial.printf("Special case: %d\n", value);
    logSerial.write(binaryData, length);

The logSerial reference (defined below) points to the real Serial object and
won't trigger deprecation warnings.
*/

#ifndef LOG_LEVEL
#define LOG_LEVEL 0
#endif

#if defined(ARDUINO_USB_CDC_ON_BOOT) && ARDUINO_USB_CDC_ON_BOOT
static HWCDC& logSerial = Serial;
#define LOG_SERIAL_HAS_TX_TIMEOUT 1
#else
static HardwareSerial& logSerial = Serial;
#define LOG_SERIAL_HAS_TX_TIMEOUT 0
#endif

void logPrintf(const char* level, const char* origin, const char* format, ...);

#ifdef ENABLE_SERIAL_LOG
#if LOG_LEVEL >= 0
#define LOG_ERR(origin, format, ...) logPrintf("ERR", origin, format "\n", ##__VA_ARGS__)
#else
#define LOG_ERR(origin, format, ...)
#endif

#if LOG_LEVEL >= 1
#define LOG_INF(origin, format, ...) logPrintf("INF", origin, format "\n", ##__VA_ARGS__)
#else
#define LOG_INF(origin, format, ...)
#endif

#if LOG_LEVEL >= 2
#define LOG_DBG(origin, format, ...) logPrintf("DBG", origin, format "\n", ##__VA_ARGS__)
#else
#define LOG_DBG(origin, format, ...)
#endif
#else
#define LOG_DBG(origin, format, ...)
#define LOG_ERR(origin, format, ...)
#define LOG_INF(origin, format, ...)
#endif

// Must match MAX_LOG_LINES/MAX_ENTRY_LEN in Logging.cpp -- shared here (rather than
// duplicated as a magic number) because callers need it to size a buffer correctly,
// not just as documentation.
inline constexpr size_t LOG_RING_BUFFER_LINES = 16;
inline constexpr size_t LOG_RING_BUFFER_LINE_LEN = 256;
// Upper bound on getLastLogs()'s output, including the terminating NUL.
inline constexpr size_t LOG_DUMP_BUFFER_SIZE = LOG_RING_BUFFER_LINES * LOG_RING_BUFFER_LINE_LEN + 1;

// Copies the ring buffer's contents (oldest to newest) into out, truncating safely if
// it doesn't fit rather than growing out -- this runs from DiskLogger's periodic flush,
// which must not heap-allocate: a prior std::string-based version allocated up to ~4KB
// per flush and called abort() (via operator new's -fno-exceptions failure path) when a
// long-running WebDAV request had fragmented the heap down to a couple KB of contiguous
// space. out is always NUL-terminated (even if outSize is 0, as long as outSize > 0).
void getLastLogs(char* out, size_t outSize);
void clearLastLogs();
// Validates the RTC log state (magic word + logHead range). Returns true if
// corruption was detected (magic mismatch or logHead out of range), meaning
// logMessages is untrusted garbage. Callers should call clearLastLogs() when
// this returns true so getLastLogs() does not dump corrupt data into crash reports.
bool sanitizeLogHead();

// Freezes a copy of the current ring buffer contents. Must be called as the very first
// thing in HalSystem::begin(), before any LOG_* call runs — otherwise boot bring-up
// noise (gpio/power/clock init) gets appended to the live ring buffer ahead of the
// snapshot and can crowd out genuine pre-boot/crash context in the small (16-line)
// buffer. Cheap (<=4KB one-shot heap copy, well before fonts/framebuffer allocate).
void snapshotBootLogs();

// Returns the frozen snapshot captured by snapshotBootLogs(), or an empty string if
// none was taken (e.g. cold boot with no prior content). Consumers (crash report, disk
// log flush) should prefer this over getLastLogs() early in boot, since the live buffer
// may already contain interleaved boot-noise lines by the time they run.
std::string getBootLogSnapshot();

// Releases the snapshot buffer once all boot-time consumers are done with it.
void clearBootLogSnapshot();

// Register a callback invoked after every log line is added to the ring buffer.
// Pass nullptr to unregister. Not thread-safe; call once at boot before tasks start.
void setDiskLogCallback(void (*cb)(const char*));

// Register a callback that formats the current wall-clock time into buf (>=24 bytes
// provided) and returns true, or returns false if no wall-clock time is available yet
// (e.g. before the RTC has been read for the first time this boot). When registered
// and returning true, logPrintf() includes the formatted timestamp in each line's
// prefix; otherwise lines keep the millis()-only prefix. Pass nullptr to unregister.
// Not thread-safe; call once at boot before tasks start.
void setLogTimestampProvider(bool (*fn)(char* buf, size_t bufSize));

class MySerialImpl : public Print {
 public:
  void begin(unsigned long baud) { logSerial.begin(baud); }

  // Support boolean conversion for compatibility with code like:
  //   if (Serial) or while (!Serial)
  operator bool() const { return logSerial; }

  __attribute__((deprecated("Use LOG_* macro instead"))) size_t printf(const char* format, ...);
  size_t write(uint8_t b) override;
  size_t write(const uint8_t* buffer, size_t size) override;
  void flush() override;
  static MySerialImpl instance;
};

#ifdef Serial
#undef Serial
#endif
#define Serial MySerialImpl::instance
