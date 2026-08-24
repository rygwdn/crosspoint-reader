#pragma once

#include <Arduino.h>
#include <BatteryMonitor.h>
#include <InputManager.h>
#include <Logging.h>
#include <freertos/semphr.h>

#include <cassert>

#include "HalGPIO.h"

class HalPowerManager;
extern HalPowerManager powerManager;  // Singleton

class HalPowerManager {
  int normalFreq = 0;  // MHz
  bool isLowPower = false;

  mutable int _batteryCachedPercent = 0;         // Last read battery percentage (0-100)
  mutable unsigned long _batteryLastPollMs = 0;  // Timestamp of last battery read in milliseconds

  // A single implausible jump (see BATTERY_JUMP_LOG_THRESHOLD below) is held here
  // rather than trusted immediately; it's adopted only once the very next poll
  // independently lands on the same value, filtering out one-shot glitches (e.g. a
  // BQ27220 CEDV EDV2 hard-correction triggered by a transient e-ink refresh
  // current sag) without masking a real, sustained state change. -1 = no candidate
  // pending.
  mutable int _batteryPendingPercent = -1;

  // Queries whether battery percentage should be estimated from raw cell
  // voltage instead of the gauge SoC register (see getBatteryPercentage()).
  // The HAL layer cannot include CrossPointSettings.h (src/ sits above lib/),
  // so src/ injects this query once at boot via setBatteryVoltageEstimateQuery()
  // instead. nullptr (the default, before that call) keeps the gauge-SoC path
  // selected unconditionally.
  using BatteryVoltageEstimateQuery = bool (*)();
  BatteryVoltageEstimateQuery _batteryVoltageEstimateQuery = nullptr;

  enum LockMode { None, NormalSpeed };
  LockMode currentLockMode = None;
  SemaphoreHandle_t modeMutex = nullptr;  // Protect access to currentLockMode

 public:
#if BOARD_HAS_PSRAM
  static constexpr int LOW_POWER_FREQ = 80;  // MHz
#else
  static constexpr int LOW_POWER_FREQ = 10;  // MHz
#endif
  static constexpr unsigned long IDLE_POWER_SAVING_MS = 3000;  // ms
  static constexpr unsigned long BATTERY_POLL_MS = 1500;       // ms

  // Gauge-backed boards (X3) report SoC straight from a single I2C register read with
  // no smoothing (unlike the ADC path below, which applies an EMA). A single flaky
  // read showing up as a sudden, unexplained drop is a reported symptom (e.g. 80% ->
  // 7%); log any poll-to-poll change at or above this threshold at ERR level (always
  // compiled in, flushed to the on-disk log) so the jump is diagnosable after the fact.
  static constexpr int BATTERY_JUMP_LOG_THRESHOLD = 15;  // percentage points

  void begin();

  // Control CPU frequency for power saving
  void setPowerSaving(bool enabled);

  // Setup wake up GPIO and enter deep sleep
  // Should be called inside main loop() to handle the currentLockMode
  void startDeepSleep(HalGPIO& gpio) const;

  // Get battery percentage (range 0-100)
  uint16_t getBatteryPercentage() const;

  // Injects the src/-layer query used by getBatteryPercentage() to decide
  // between the gauge-SoC and voltage-estimate paths. Call once during boot,
  // before the first battery poll.
  void setBatteryVoltageEstimateQuery(BatteryVoltageEstimateQuery query) { _batteryVoltageEstimateQuery = query; }

  // RAII helper class to manage power saving locks
  // Usage: create an instance of Lock in a scope to disable power saving, for example when running a task that needs
  // full performance. When the Lock instance is destroyed (goes out of scope), power saving will be re-enabled.
  class Lock {
    friend class HalPowerManager;
    bool valid = false;

   public:
    explicit Lock();
    ~Lock();

    // Non-copyable and non-movable
    Lock(const Lock&) = delete;
    Lock& operator=(const Lock&) = delete;
    Lock(Lock&&) = delete;
    Lock& operator=(Lock&&) = delete;
  };
};
