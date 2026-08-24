#pragma once

#include <Arduino.h>

// Wake-session milestones are serial-log events; state is intentionally small
// and reset with the firmware process.
void wakeMetricBootStart(int resetReason);
void wakeMetricGpioClassified(int wakeReason);
void wakeMetricDisplayBeginComplete(bool seamless);
void wakeMetricRoute(const char* route, const char* path, const char* resume);
void wakeMetricReaderConstructed(const char* path, bool fastInitialRefresh);
void wakeMetricReaderTextStart(const char* path, bool cacheBuildPending);
void wakeMetricWaveformStart(int mode);
void wakeMetricWaveformComplete(int mode);
void wakeMetricReaderConstructionFailed();
void wakeMetricHomeRouteRequested();
