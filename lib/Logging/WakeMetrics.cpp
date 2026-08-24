#include "WakeMetrics.h"

#include <Logging.h>
#include <cstring>

namespace {
uint32_t bootStartMs = 0;
bool readerTextPending = false;
bool waveformPending = false;
bool readerVisibleEmitted = false;
bool readerTextFirst = false;
bool wakeReaderRouteArmed = false;
int waveformMode = 0;

void event(const char* name) {
  const uint32_t now = millis();
  LOG_INF("WAKE", "%s t=%lu dt=%lu", name, now, now - bootStartMs);
}
}  // namespace

void wakeMetricBootStart(int resetReason) {
  const uint32_t now = millis();
  bootStartMs = now;
  readerVisibleEmitted = false;
  wakeReaderRouteArmed = false;
  LOG_INF("WAKE", "boot_setup_start t=%lu reset=%d", now, resetReason);
}
void wakeMetricGpioClassified(int wakeReason) {
  const uint32_t now = millis();
  LOG_INF("WAKE", "gpio_wake_classified t=%lu dt=%lu reason=%d", now, now - bootStartMs, wakeReason);
}
void wakeMetricDisplayBeginComplete(bool seamless) {
  const uint32_t now = millis();
  LOG_INF("WAKE", "display_begin_complete t=%lu dt=%lu seamless=%d", now, now - bootStartMs, seamless ? 1 : 0);
}
void wakeMetricRoute(const char* route, const char* path, const char* resume) {
  wakeReaderRouteArmed = route && strcmp(route, "reader") == 0;
  const uint32_t now = millis();
  LOG_INF("WAKE", "activity_route t=%lu dt=%lu route=%s resume=%s splashless=%d path=%s", now, now - bootStartMs,
          route ? route : "unknown", resume ? resume : "unknown", resume && strcmp(resume, "splashless_wake") == 0 ? 1 : 0,
          path ? path : "-");
}
void wakeMetricReaderConstructed(const char* path, bool fastInitialRefresh) {
  const uint32_t now = millis();
  LOG_INF("WAKE", "reader_constructed t=%lu dt=%lu fast_initial=%d path=%s", now, now - bootStartMs,
          fastInitialRefresh ? 1 : 0, path ? path : "-");
}
void wakeMetricReaderTextStart(const char* path, bool cacheBuildPending) {
  if (!wakeReaderRouteArmed) return;
  readerTextPending = true;
  waveformPending = false;
  readerTextFirst = !readerVisibleEmitted;
  const uint32_t now = millis();
  LOG_INF("WAKE", "reader_text_frame_start t=%lu dt=%lu first=%d cache_build=%d path=%s", now, now - bootStartMs,
          readerTextFirst ? 1 : 0, cacheBuildPending ? 1 : 0, path ? path : "-");
}
void wakeMetricWaveformStart(int mode) {
  if (!readerTextPending || waveformPending || readerVisibleEmitted) return;
  waveformPending = true;
  waveformMode = mode;
  const uint32_t now = millis();
  LOG_INF("WAKE", "reader_waveform_start t=%lu dt=%lu requested_mode=%d", now, now - bootStartMs, mode);
}
void wakeMetricWaveformComplete(int mode) {
  (void)mode;
  if (!readerTextPending || !waveformPending || readerVisibleEmitted) return;
  const uint32_t now = millis();
  LOG_INF("WAKE", "reader_text_visible t=%lu dt=%lu first=%d requested_mode=%d", now, now - bootStartMs,
          readerTextFirst ? 1 : 0, waveformMode);
  readerVisibleEmitted = true;
  readerTextPending = false;
  waveformPending = false;
}
void wakeMetricReaderConstructionFailed() {
  wakeReaderRouteArmed = false;
  readerTextPending = false;
  waveformPending = false;
}
void wakeMetricHomeRouteRequested() {
  wakeReaderRouteArmed = false;
  readerTextPending = false;
  waveformPending = false;
  event("home_route_requested");
}
