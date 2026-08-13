#include "HalSystem.h"

#include <string>

#include "Arduino.h"
#include "HalStorage.h"
#include "Logging.h"
#include "esp_debug_helpers.h"
#include "esp_private/esp_cpu_internal.h"
#include "esp_private/esp_system_attr.h"
#include "esp_private/panic_internal.h"
#include "esp_task_wdt.h"

#define MAX_PANIC_STACK_DEPTH 32
#define PANIC_CAPTURE_MAGIC 0x50414E49u

RTC_NOINIT_ATTR char panicMessage[256];
RTC_NOINIT_ATTR HalSystem::StackFrame panicStack[MAX_PANIC_STACK_DEPTH];
// RTC_NOINIT is uninitialized on cold boot, so only this exact marker proves a
// panic diagnostic was captured before the reset.
RTC_NOINIT_ATTR volatile uint32_t panicCaptureMarker;

namespace {
// The vendored WebServer library blocks for up to 5000ms on a single socket
// write/close with no watchdog resets of its own (HTTP_MAX_SEND_WAIT /
// HTTP_MAX_CLOSE_WAIT in WebServer.h). The sdkconfig task watchdog timeout is
// also 5000ms (CONFIG_ESP_TASK_WDT_TIMEOUT_S=5), so a single slow send over
// weak WiFi races the watchdog and can reset the device before the library
// gives up on its own. This bit the multipart /upload path before (see the
// "critical 1% crash point" comment in CrossPointWebServer.cpp::handleUpload)
// and was observed again right after a WebDAV PUT completed on weak WiFi
// (RSSI around -90dBm). Give the watchdog enough headroom that the library's
// own 5s timeouts always lose the race and fail cleanly instead of racing a
// hardware reset. A genuine infinite hang still gets caught, just 5s later.
constexpr uint32_t EXTENDED_TASK_WDT_TIMEOUT_MS = 10000;

const char* resetReasonName(esp_reset_reason_t reason) {
  switch (reason) {
    case ESP_RST_UNKNOWN:
      return "UNKNOWN";
    case ESP_RST_POWERON:
      return "POWERON";
    case ESP_RST_EXT:
      return "EXT";
    case ESP_RST_SW:
      return "SW";
    case ESP_RST_PANIC:
      return "PANIC";
    case ESP_RST_INT_WDT:
      return "INT_WDT";
    case ESP_RST_TASK_WDT:
      return "TASK_WDT";
    case ESP_RST_WDT:
      return "WDT";
    case ESP_RST_DEEPSLEEP:
      return "DEEPSLEEP";
    case ESP_RST_BROWNOUT:
      return "BROWNOUT";
    case ESP_RST_SDIO:
      return "SDIO";
    case ESP_RST_USB:
      return "USB";
    case ESP_RST_JTAG:
      return "JTAG";
    case ESP_RST_EFUSE:
      return "EFUSE";
    case ESP_RST_PWR_GLITCH:
      return "PWR_GLITCH";
    case ESP_RST_CPU_LOCKUP:
      return "CPU_LOCKUP";
    default:
      return "?";
  }
}
}  // namespace

extern "C" {

void __real_panic_abort(const char* message);
void __real_panic_print_backtrace(const void* frame, int core);

static DRAM_ATTR const char PANIC_REASON_UNKNOWN[] = "(unknown panic reason)";
void IRAM_ATTR __wrap_panic_abort(const char* message) {
  if (!message) message = PANIC_REASON_UNKNOWN;
  // IRAM-safe bounded copy (strncpy is not IRAM-safe in panic context)
  int i = 0;
  for (; i < (int)sizeof(panicMessage) - 1 && message[i]; i++) {
    panicMessage[i] = message[i];
  }
  panicMessage[i] = '\0';
  panicCaptureMarker = PANIC_CAPTURE_MAGIC;

  __real_panic_abort(message);
}

void IRAM_ATTR __wrap_panic_print_backtrace(const void* frame, int core) {
  if (!frame) {
    __real_panic_print_backtrace(frame, core);
    return;
  }

#if !__riscv
  __real_panic_print_backtrace(frame, core);
  return;
#else
  for (size_t i = 0; i < MAX_PANIC_STACK_DEPTH; i++) {
    panicStack[i].sp = 0;
  }

  // Copied from components/esp_system/port/arch/riscv/panic_arch.c
  uint32_t sp = (uint32_t)((RvExcFrame*)frame)->sp;
  const int per_line = 8;
  int depth = 0;
  for (int x = 0; x < 1024; x += per_line * sizeof(uint32_t)) {
    uint32_t* spp = (uint32_t*)(sp + x);
    // panic_print_hex(sp + x);
    // panic_print_str(": ");
    panicStack[depth].sp = sp + x;
    for (int y = 0; y < per_line; y++) {
      // panic_print_str("0x");
      // panic_print_hex(spp[y]);
      // panic_print_str(y == per_line - 1 ? "\r\n" : " ");
      panicStack[depth].spp[y] = spp[y];
    }

    depth++;
    if (depth >= MAX_PANIC_STACK_DEPTH) {
      break;
    }
  }
  panicCaptureMarker = PANIC_CAPTURE_MAGIC;

  __real_panic_print_backtrace(frame, core);
#endif
}
}

namespace HalSystem {

void begin() {
  // Freeze whatever was in the ring buffer before this function's own logging (and the
  // gpio/power/clock bring-up that follows in setup()) appends anything new. This must
  // run before the very first LOG_* call below: on a panic reboot the buffer already
  // holds pre-crash context sized right up against the 16-line cap, and boot noise
  // appended ahead of a snapshot can crowd that context out before checkPanic() ever
  // gets to dump it (see crash_report.txt in git history for a near-miss where 7 boot
  // lines landed in a 16-line buffer that already held 9 pre-crash lines).
  const bool logStateCorrupt = sanitizeLogHead();
  if (!logStateCorrupt) {
    snapshotBootLogs();
  }

  if (!isRebootFromPanic()) {
    // This is mostly for the first boot: initialize panic info and logs to empty state.
    clearPanic();
  } else if (logStateCorrupt) {
    // Panic occurred before the ring buffer was ever initialized (e.g. a crash in a
    // static constructor before begin() ran). logMessages is untrusted garbage, and
    // there was nothing valid to snapshot above, so wipe it rather than let
    // getLastLogs() dump corrupt data into the crash report.
    clearLastLogs();
  }
  // else: panic reboot with a valid buffer. Preserve panicMessage/panicStack and leave
  // the live ring buffer alone — checkPanic() reads the frozen snapshot captured above
  // for crash_report.txt, not the live buffer, so it doesn't matter that the boot-time
  // logging below keeps appending to it.

  LOG_INF("SYS", "Reset reason: %s", resetReasonName(esp_reset_reason()));

  // The Arduino core auto-initializes the TWDT from sdkconfig defaults
  // (5000ms) before setup() runs, so reconfigure rather than init. See the
  // comment on EXTENDED_TASK_WDT_TIMEOUT_MS above for why.
  const esp_task_wdt_config_t wdtConfig = {
      .timeout_ms = EXTENDED_TASK_WDT_TIMEOUT_MS,
      .idle_core_mask = 0,
      .trigger_panic = true,
  };
  const esp_err_t wdtErr = esp_task_wdt_reconfigure(&wdtConfig);
  if (wdtErr != ESP_OK) {
    LOG_ERR("SYS", "Failed to extend task watchdog timeout: %d", (int)wdtErr);
  }
}

void checkPanic() {
  if (isRebootFromPanic()) {
    auto panicInfo = getPanicInfo(true);
    auto file = Storage.open("/crash_report.txt", O_WRITE | O_CREAT | O_TRUNC);
    if (file) {
      const size_t written = file.write(panicInfo.c_str(), panicInfo.size());
      file.close();
      if (written == panicInfo.size()) {
        // Keep the crash data for CrashActivity, but mark it consumed so a
        // later watchdog reset cannot be mistaken for this panic.
        panicCaptureMarker = 0;
        LOG_INF("SYS", "Dumped panic info to SD card");
      } else {
        LOG_ERR("SYS", "Failed to write complete crash report (%zu of %zu bytes)", written, panicInfo.size());
      }
    } else {
      LOG_ERR("SYS", "Failed to open crash_report.txt for writing");
    }
  }
}

void clearPanic() {
  panicCaptureMarker = 0;
  panicMessage[0] = '\0';
  for (size_t i = 0; i < MAX_PANIC_STACK_DEPTH; i++) {
    panicStack[i].sp = 0;
  }
  clearLastLogs();
}

std::string getPanicInfo(bool full) {
  if (!full) {
    return panicMessage;
  } else {
    std::string info;

    info += "CrossPoint version: " CROSSPOINT_VERSION;
    info += "\n\nReset reason: " + std::string(resetReasonName(esp_reset_reason()));
    info += "\n\nPanic reason: " + std::string(panicMessage);
    // Prefer the boot snapshot: by the time this runs, the live ring buffer already has
    // boot-time bring-up logging appended after it (see the ordering comment in
    // HalSystem::begin()). Fall back to the live buffer if no snapshot was taken.
    std::string logs = getBootLogSnapshot();
    if (logs.empty()) {
      logs = getLastLogs();
    }
    info += "\n\nLast logs:\n" + logs;
    info += "\n\nStack memory:\n";

    auto toHex = [](uint32_t value) {
      char buffer[9];
      snprintf(buffer, sizeof(buffer), "%08X", value);
      return std::string(buffer);
    };
    for (size_t i = 0; i < MAX_PANIC_STACK_DEPTH; i++) {
      if (panicStack[i].sp == 0) {
        break;
      }
      info += "0x" + toHex(panicStack[i].sp) + ": ";
      for (size_t j = 0; j < 8; j++) {
        info += "0x" + toHex(panicStack[i].spp[j]) + " ";
      }
      info += "\n";
    }

    return info;
  }
}

bool isRebootFromPanic() {
  const auto resetReason = esp_reset_reason();
  // Brownout is reported unconditionally, same as a true panic/lockup: SD write
  // current spikes stacking on weak-WiFi TX retries can dip the rail enough to
  // trip the brownout detector, which previously reset with no crash_report.txt
  // at all. panicMessage/panicStack are only ever populated by the
  // __wrap_panic_abort path, which a brownout never goes through -- but the
  // reset-reason + last-logs snapshot are still useful even with no panic message.
  if (resetReason == ESP_RST_PANIC || resetReason == ESP_RST_CPU_LOCKUP || resetReason == ESP_RST_BROWNOUT) {
    return true;
  }

  const bool watchdogReset =
      resetReason == ESP_RST_INT_WDT || resetReason == ESP_RST_TASK_WDT || resetReason == ESP_RST_WDT;
  return watchdogReset && panicCaptureMarker == PANIC_CAPTURE_MAGIC;
}

}  // namespace HalSystem
