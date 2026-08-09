#include "HalPowerManager.h"

#include <BoardConfig.h>
#include <Logging.h>
#include <PowerManager.h>
#include <WiFi.h>

#include <esp_sleep.h>
#include <soc/soc_caps.h>

#include <cassert>

#include "HalGPIO.h"

#if FREEINK_DEVICE_PAPERMONO
#include <M5Pm1.h>
#endif

HalPowerManager powerManager;  // Singleton instance

// GPIO13 controls the X4 battery latch and the X3 SD power rail on the C3
// Xteink boards. Other boards use it for unrelated signals, including the
// X4 Pro display chip select.
static constexpr gpio_num_t XTEINK_C3_GPIO13 = GPIO_NUM_13;

namespace {
// Distinguishes "RTC memory holds a real prior reading" from "undefined garbage after a
// true power-on/brownout" -- RTC_NOINIT_ATTR survives ESP.restart() (a software reset)
// but its content is unspecified after power actually drops, so the sentinel is what
// makes it safe to trust on the very first poll after boot. See its use in
// HalPowerManager::getBatteryPercentage() below.
constexpr uint32_t BATTERY_PERCENT_RTC_MAGIC = 0xba77e4f0;
}  // namespace

// Last known battery percent, carried across ESP.restart() only (see
// BATTERY_PERCENT_RTC_MAGIC above). Not a HalPowerManager member: RTC_NOINIT_ATTR
// requires static/global storage placed in the RTC_NOINIT linker section, which a class
// member's storage duration doesn't guarantee.
RTC_NOINIT_ATTR uint32_t rtcBatteryPercentMagic;
RTC_NOINIT_ATTR uint16_t rtcBatteryPercent;

void HalPowerManager::begin() {
  if (BoardConfig::ACTIVE.batteryAdc >= 0) {
    pinMode(BoardConfig::ACTIVE.batteryAdc, INPUT);
  }
  normalFreq = getCpuFrequencyMhz();
  modeMutex = xSemaphoreCreateMutex();
  assert(modeMutex != nullptr);
}

void HalPowerManager::setPowerSaving(bool enabled) {
  if (normalFreq <= 0) {
    return;  // invalid state
  }

  auto wifiMode = WiFi.getMode();
  if (wifiMode != WIFI_MODE_NULL) {
    // Wifi is active, force disabling power saving
    enabled = false;
  }

  // Note: We don't use mutex here to avoid too much overhead,
  // it's not very important if we read a slightly stale value for currentLockMode
  const LockMode mode = currentLockMode;

  if (mode == None && enabled && !isLowPower) {
    LOG_DBG("PWR", "Going to low-power mode");
    if (!setCpuFrequencyMhz(LOW_POWER_FREQ)) {
      LOG_DBG("PWR", "Failed to set CPU frequency = %d MHz", LOW_POWER_FREQ);
      return;
    }
    isLowPower = true;

  } else if ((!enabled || mode != None) && isLowPower) {
    LOG_DBG("PWR", "Restoring normal CPU frequency");
    if (!setCpuFrequencyMhz(normalFreq)) {
      LOG_DBG("PWR", "Failed to set CPU frequency = %d MHz", normalFreq);
      return;
    }
    isLowPower = false;
  }

  // Otherwise, no change needed
}

void HalPowerManager::startDeepSleep(HalGPIO& gpio) const {
#ifdef ENABLE_SERIAL_LOG
  // Tear down HWCDC so the host sees a clean disconnect and the peripheral
  // doesn't hold power domains that interfere with USB-powered GPIO wake.
  // logSerial is the raw HWCDC reference; Serial is the MySerialImpl proxy
  // (which doesn't expose end()).
  logSerial.end();
#endif

#if !SOC_PM_SUPPORT_EXT1_WAKEUP
  if (gpio.isXteinkDevice()) {
    // GPIO13 gates the battery MOSFET on both Xteink C3 boards; driving it low
    // is the battery power-off (the SDK wake source still handles USB power).
    // Release any surviving pad hold first: hold_en survives deep sleep via
    // the SDK's deepSleep() (esp_sleep_config_gpio_isolate +
    // gpio_deep_sleep_hold_en), and a held pad silently ignores the drive.
    gpio_hold_dis(XTEINK_C3_GPIO13);
    gpio_set_direction(XTEINK_C3_GPIO13, GPIO_MODE_OUTPUT);
    gpio_set_level(XTEINK_C3_GPIO13, 0);
    gpio_hold_en(XTEINK_C3_GPIO13);
  }
#endif

  // Cut the gated peripheral rails (touch/SD/EPD on boards like the Sticky) and
  // hold the enables off through deep sleep — otherwise the GT911 and SD card
  // stay powered all through "off" and drain the battery. No-op on boards with
  // no switched rails (X4/X3). Trade-off: no touch-to-wake; wake is the power
  // button. Must run after display.deepSleep() so the panel controller gets its
  // deep-sleep command while its rail is still up (enterDeepSleep() in main.cpp
  // guarantees that ordering).
  freeink::PowerManager::powerDownRailsForSleep();

#if FREEINK_DEVICE_PAPERMONO
  // Its power button is behind the M5PM1 PMIC rather than an ESP GPIO, so
  // normal GPIO deep sleep would have no wake source. Ask the PMIC to shut the
  // device down; a button click then restarts it through a cold boot.
  if (freeink::m5pm1::requestShutdown()) {
    delay(1000);  // allow the PMIC firmware time to drop power
  }
#endif

  // Waits for the power button to be physically released (so holding it doesn't
  // immediately wake the device again), then arms the wake source and sleeps.
  freeink::PowerManager::deepSleepUntilPowerButton();
}

uint16_t HalPowerManager::getBatteryPercentage() const {
  static const BatteryMonitor battery;
  if (BoardConfig::ACTIVE.batteryGauge.gaugeAddr != 0) {
    const unsigned long now = millis();
    if (_batteryLastPollMs != 0 && (now - _batteryLastPollMs) < BATTERY_POLL_MS) {
      return _batteryCachedPercent;
    }

    const bool firstPoll = (_batteryLastPollMs == 0);
    _batteryLastPollMs = now;

    if (_batteryVoltageEstimateQuery && _batteryVoltageEstimateQuery()) {
      // Bypasses the SoC register (and the jump-debounce below) entirely: some
      // CEDV profiles hard-correct SoC in a single abrupt step at an EDV2
      // threshold crossing (see the jump-debounce comment further down), which
      // no amount of debouncing can fully hide without also delaying every real
      // change. Voltage declines smoothly under load, so it doesn't have that
      // failure mode -- at the cost of the flatter mid-discharge resolution
      // LIION_NOTCH_MV already documents. Opt-in via SETTINGS.batteryUseVoltageEstimate.
      const BatteryMonitor::Status status = battery.readStatus();
      if (!status.millivoltsKnown) {
        LOG_ERR("PWR", "Battery voltage read failed, keeping cached %d%%", _batteryCachedPercent);
        return _batteryCachedPercent;
      }
      // 101 (>100) on the first poll opts out of percentageFromMillivolts's
      // hysteresis, matching the SoC path's own no-baseline-yet handling above.
      const uint16_t previousPercent = firstPoll ? 101 : static_cast<uint16_t>(_batteryCachedPercent);
      _batteryCachedPercent = BatteryMonitor::percentageFromMillivolts(status.millivolts, previousPercent);
      return _batteryCachedPercent;
    }

    uint16_t percent = 0;
    if (!battery.readPercentageChecked(percent)) {
      LOG_ERR("PWR", "Battery gauge SoC read failed, keeping cached %d%%", _batteryCachedPercent);
      return _batteryCachedPercent;
    }

    // _batteryCachedPercent/_batteryLastPollMs are runtime members that reset to 0 on
    // every reboot, so a plain "!firstPoll" gate skips jump detection entirely on the
    // first poll after EVERY reboot -- including main.cpp's silentRestart()/
    // silentRestartToReader(), an intentional ESP.restart() that WiFi/web-server
    // activities trigger in onExit() to defrag heap. That's exactly the boundary where
    // the reported 80%->7% misread was seen in the field: heap had fragmented down to
    // under 1KB free during a slow WebDAV session, the activity exited (triggering
    // silentRestart()), and the first post-reboot poll (7%) was trusted outright with no
    // comparison against the pre-reboot 80% at all -- the ERR log below never fired for
    // the one case it was added to catch. rtcBatteryPercent survives ESP.restart() (see
    // BATTERY_PERCENT_RTC_MAGIC above), so it stands in as the baseline specifically for
    // that first post-reboot poll; a true power-on/brownout leaves the magic unset, so
    // this correctly stays disabled after actual power loss (where the gauge itself
    // needs to re-settle anyway).
    int baseline = _batteryCachedPercent;
    bool haveBaseline = !firstPoll;
    if (firstPoll && rtcBatteryPercentMagic == BATTERY_PERCENT_RTC_MAGIC) {
      baseline = rtcBatteryPercent;
      haveBaseline = true;
    }

    const int delta = static_cast<int>(percent) - baseline;
    const int absDelta = delta < 0 ? -delta : delta;
    if (haveBaseline && absDelta >= BATTERY_JUMP_LOG_THRESHOLD) {
      // Fetch mV/charging/EDV2 only on an anomalous jump, not every poll, to avoid
      // extra I2C traffic on the common path. edv2Below+smoothingActive test the
      // leading hypothesis for X3's reported 80%->7% jumps: the BQ27220's CEDV
      // algorithm hard-corrects RM/SoC to a configured "Battery Low %" the instant
      // compensated cell voltage crosses the EDV2 threshold (TI TRM SLUUBD4A section
      // 1.1.4), rather than declining gradually -- unless CEDV Smoothing is enabled
      // (section 1.1.13). If edv2Below is true here, that confirms this mechanism.
      const BatteryMonitor::Status status = battery.readStatus();
      bool edv2Below = false;
      bool smoothingActive = false;
      const bool edv2Known = battery.readGaugeEdv2Status(edv2Below, smoothingActive);
      LOG_ERR("PWR", "Battery gauge SoC jumped %d%% -> %d%% (mV=%u, charging=%s, edv2=%s, smoothing=%s)%s", baseline,
              percent, status.millivoltsKnown ? status.millivolts : 0,
              !status.chargingKnown ? "unknown" : (status.charging ? "yes" : "no"),
              !edv2Known ? "unknown" : (edv2Below ? "below" : "above"),
              !edv2Known ? "unknown" : (smoothingActive ? "active" : "inactive"), firstPoll ? " [across reboot]" : "");

      // A jump this large is more often a one-shot glitch (see comment above) than a
      // real instantaneous capacity change, so a single sample isn't trusted: hold it
      // back and keep reporting the last trusted value until the very next poll
      // independently lands on the same percentage, confirming it's real. This is
      // what actually keeps the glitch off the on-screen battery indicator --
      // BaseTheme.cpp reads getBatteryPercentage() directly, so without this the
      // logged "jumped" value was still adopted and displayed immediately.
      if (_batteryPendingPercent == static_cast<int>(percent)) {
        LOG_DBG("PWR", "Battery gauge SoC jump confirmed by follow-up poll, adopting %d%%", percent);
        _batteryPendingPercent = -1;
      } else {
        LOG_DBG("PWR", "Battery gauge SoC jump not yet confirmed, holding at cached %d%%", _batteryCachedPercent);
        _batteryPendingPercent = static_cast<int>(percent);
        return _batteryCachedPercent;
      }
    } else {
      _batteryPendingPercent = -1;
      LOG_DBG("PWR", "Battery gauge SoC poll: %d%%", percent);
    }

    _batteryCachedPercent = percent;
    rtcBatteryPercent = percent;
    rtcBatteryPercentMagic = BATTERY_PERCENT_RTC_MAGIC;
    return _batteryCachedPercent;
  }

  // smooth the battery %.
  if (_batteryCachedPercent == 0) {
    _batteryCachedPercent = 10 * battery.readPercentage();
  } else {
    _batteryCachedPercent = (_batteryCachedPercent * 9 + battery.readPercentage() * 10) / 10;
  }
  return _batteryCachedPercent / 10;
}

HalPowerManager::Lock::Lock() {
  xSemaphoreTake(powerManager.modeMutex, portMAX_DELAY);
  // Current limitation: only one lock at a time
  if (powerManager.currentLockMode != None) {
    LOG_ERR("PWR", "Lock already held, ignore");
    valid = false;
  } else {
    powerManager.currentLockMode = NormalSpeed;
    valid = true;
  }
  xSemaphoreGive(powerManager.modeMutex);
  if (valid) {
    // Immediately restore normal CPU frequency if currently in low-power mode
    powerManager.setPowerSaving(false);
  }
}

HalPowerManager::Lock::~Lock() {
  xSemaphoreTake(powerManager.modeMutex, portMAX_DELAY);
  if (valid) {
    powerManager.currentLockMode = None;
  }
  xSemaphoreGive(powerManager.modeMutex);
}
