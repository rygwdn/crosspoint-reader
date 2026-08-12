#include "HalClock.h"

#include <Logging.h>
#include <WiFi.h>
#include <esp_sntp.h>
#include <time.h>

HalClock halClock;  // Singleton instance

// Registered as the Logging lib's timestamp provider (see setLogTimestampProvider in
// Logging.h) so log lines gain a real wall-clock timestamp once the RTC has been read
// at least once, without Logging depending on HalClock directly -- same
// dependency-injection pattern DiskLogger uses for setDiskLogCallback.
static bool logTimestampProviderFn(char* buf, size_t bufSize) { return halClock.formatTimestamp(buf, bufSize); }

void HalClock::begin() {
  _available = _sdkRtc.begin();
  LOG_INF("CLK", _available ? "SDK RTC found" : "RTC not found");
  setLogTimestampProvider(&logTimestampProviderFn);
}

// _cachedDateTime/_hasCachedTime/_lastPollMs are read from logPrintf() (via
// formatTimestamp()) across every FreeRTOS task that logs, with no mutex. Deliberately
// unsynchronized: a torn read here can only produce a cosmetically wrong timestamp
// string (these fields are never used for control flow), and the existing UI-only
// callers (getTime()/formatTime()) never raced before this. Not worth the extra
// mutex/trylock plumbing DiskLogger::flushMutex uses for its (control-flow-relevant)
// SD flush race.
bool HalClock::refreshCache() const {
  if (!_available) return false;

  const unsigned long now = millis();
  if (_lastPollMs != 0 && (now - _lastPollMs) < CLOCK_POLL_MS) {
    return true;
  }

  Rtc::DateTime dt;
  if (!_sdkRtc.now(dt)) {
    if (!_hasCachedTime) return false;
    _lastPollMs = now;
    return true;
  }
  _cachedDateTime = dt;
  _lastPollMs = now;
  _hasCachedTime = true;
  return true;
}

bool HalClock::getTime(uint8_t& hour, uint8_t& minute) const {
  if (!refreshCache()) return false;
  hour = _cachedDateTime.hour;
  minute = _cachedDateTime.minute;
  return true;
}

bool HalClock::formatTimestamp(char* buf, size_t bufSize) const {
  if (bufSize < 21) return false;
  if (!refreshCache()) return false;
  snprintf(buf, bufSize, "%04u-%02u-%02uT%02u:%02u:%02uZ", _cachedDateTime.year, _cachedDateTime.month,
           _cachedDateTime.day, _cachedDateTime.hour, _cachedDateTime.minute, _cachedDateTime.second);
  return true;
}

bool HalClock::formatTime(char* buf, size_t bufSize, uint8_t utcOffsetQuarterHoursBiased, bool use12Hour) const {
  if (bufSize < (use12Hour ? 9u : 6u)) return false;
  uint8_t h, m;
  if (!getTime(h, m)) return false;

  // Apply UTC offset: convert biased value to signed quarter-hours.
  // Clamp against corrupted persisted values so display time can't drift outside [-12:00, +14:00].
  if (utcOffsetQuarterHoursBiased > 104) utcOffsetQuarterHoursBiased = 104;
  int offsetQuarterHours = static_cast<int>(utcOffsetQuarterHoursBiased) - 48;
  int totalMinutes = static_cast<int>(h) * 60 + static_cast<int>(m) + offsetQuarterHours * 15;

  // Wrap around 24 hours
  totalMinutes = ((totalMinutes % 1440) + 1440) % 1440;

  const int hour24 = totalMinutes / 60;
  const int min = totalMinutes % 60;
  if (use12Hour) {
    const bool pm = hour24 >= 12;
    int hour12 = hour24 % 12;
    if (hour12 == 0) hour12 = 12;
    snprintf(buf, bufSize, "%d:%02d %s", hour12, min, pm ? "PM" : "AM");
  } else {
    snprintf(buf, bufSize, "%02d:%02d", hour24, min);
  }
  return true;
}

bool HalClock::syncFromNTP() {
  if (!_available) return false;

  if (WiFi.status() != WL_CONNECTED) {
    LOG_ERR("CLK", "WiFi not connected, cannot sync NTP");
    return false;
  }

  LOG_INF("CLK", "Starting NTP sync...");
  configTzTime("UTC0", "pool.ntp.org", "time.nist.gov");

  // Wait for SNTP sync to complete (up to 5 seconds)
  constexpr int maxAttempts = 50;
  for (int i = 0; i < maxAttempts; i++) {
    if (sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED) {
      time_t now = time(nullptr);
      struct tm timeinfo;
      gmtime_r(&now, &timeinfo);

      Rtc::DateTime dt;
      dt.year = static_cast<uint16_t>(timeinfo.tm_year + 1900);
      dt.month = static_cast<uint8_t>(timeinfo.tm_mon + 1);
      dt.day = static_cast<uint8_t>(timeinfo.tm_mday);
      dt.hour = static_cast<uint8_t>(timeinfo.tm_hour);
      dt.minute = static_cast<uint8_t>(timeinfo.tm_min);
      dt.second = static_cast<uint8_t>(timeinfo.tm_sec);
      dt.weekday = static_cast<uint8_t>(timeinfo.tm_wday);
      if (_sdkRtc.set(dt)) {
        _lastPollMs = 0;
        _cachedDateTime = dt;
        _hasCachedTime = true;
        LOG_INF("CLK", "RTC set to %04u-%02u-%02u %02u:%02u:%02u UTC", dt.year, dt.month, dt.day, dt.hour, dt.minute,
                dt.second);
        return true;
      }
      return false;
    }
    delay(100);
  }

  LOG_ERR("CLK", "NTP sync timed out");
  return false;
}
