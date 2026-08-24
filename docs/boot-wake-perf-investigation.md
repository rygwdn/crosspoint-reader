# Boot/wake perceived-latency investigation

Goal: reduce the wall-clock time from power-button-hold to visible reader
text toward a 1-2s target. This doc records the stage-by-stage latency
budget, the evidence behind it, the ranked list of ideas, and exactly what
was prototyped and why.

Branch: `area-boot-wake-perf-trimmed` in
`crosspoint-reader-worktrees/12-area-boot-wake-perf`. The task referred to
this branch as `area-boot-wake-perf`; the worktree is actually checked out
on the sibling `-trimmed` branch (a naming convention also seen on other
worktrees in this repo, e.g. `01-area-reliability-diagnostics`). Flagging
this discrepancy explicitly since it wasn't caught until mid-session.

## Evidence base

Two independent sources were used:

1. **Static code reading** of this worktree's `src/` (file:line citations
   below).
2. **Real-device debug logs**: 13 logical boot/wake sessions reconstructed
   from 15 physical log files by a sibling analysis
   (`~/crosspoint-reader/logset-analysis.md`, all X3 devices). That report
   parses exact `GFX Time = ...` and `ERS ... display=...` fields — no
   rounding, no synthetic benchmarking.

The wasm simulator does **not** model e-ink waveform timing (its
`HalDisplay::refreshDisplay()` just rasterizes to a texture instantly —
`crosspoint-simulator/src/HalDisplay.cpp:265-267`), and it turns out it also
cannot execute a full sleep→wake cycle end-to-end at all (see "Simulator
wake-cycle limitation" below). So the *magnitude* of the winning idea's
benefit is grounded in the real-device log data and static code tracing,
not simulator timing. Where the simulator *can* prove something (no
crash/corruption, correct file writes, correct control flow), it was used
and is reported below with a transcript.

## Named-stage latency budget

From `logset-analysis.md`'s aggregate of 1,406 GFX timing records + 1,063
ERS page-render records across 13 real sessions:

| stage | cost | evidence |
|---|---|---|
| First reader page after a power-button wake (`display` waveform) | **median 3228ms, range 2283-3236ms** (9 sessions) | `logset-analysis.md:77`, ranked #1 at `logset-analysis.md:87` |
| Ordinary steady-state page turn (`display` waveform) | median 445ms, range 430-491ms (919 pages) | `logset-analysis.md:78,88` |
| Grayscale display waveform (`gray_display`) | near-invariant median 227ms, present on every grayscale page | `logset-analysis.md:79,89` — not optimizable, fixed panel cost |
| Home-only wake first paint (no EPUB) | 269-304ms | `logset-analysis.md:81` |
| Recovery-combo settle loop (fixed wait before any boot work starts) | up to 500ms worst case, ~10-20ms common case after the fix below | `src/main.cpp` (was a fixed `while(millis()-settleStart<500)` loop) |

**The dominant fixed cost is the ~2.8s gap between a normal page turn
(445ms) and the first reader page after a power-button wake (median
3228ms).** This gap is present even on warm-cache sessions with no pending
section build, so it is not a parsing/cache artifact — it is a refresh-mode
choice.

## Root cause of the ~2.8s gap

`ReaderUtils::displayWithRefreshCycle()` selects the *requested* mode from
`pagesUntilFullRefresh` (`src/activities/reader/ReaderUtils.h:140-152`):
`HALF_REFRESH` when `<= 1`, otherwise `FAST_REFRESH`.

The requested mode is not the whole X3 waveform decision. On every boot or
deep-sleep wake, `setupDisplayAndFonts(resume != BootResume::Splash)` calls
`HalDisplay::begin(true)` for `SplashlessWake`
(`src/main.cpp:404`, `src/main.cpp:232-235`). `HalDisplay::begin()` calls
`einkDisplay.begin()` and then, for `seamless=true`, unconditionally calls
`einkDisplay.skipInitialResync()` and returns
(`lib/hal/HalDisplay.cpp:13-27`). The compatibility header confirms
`EInkDisplay` is an alias of `freeink::FreeInkDisplay`
(`freeink-sdk/libs/display/FreeInkDisplay/include/EInkDisplay.h:3-14`), and
the facade forwards `skipInitialResync()` to the selected driver
(`freeink-sdk/libs/display/FreeInkDisplay/src/FreeInkDisplay.cpp:889-890`).
This is unconditional on whether a sleep frame was restored; it depends only
on `seamless`.

For the X3 driver, `begin()` resets `_isScreenOn=false` and arms one initial
full-sync (`Uc8253X3Driver.cpp:140-157`); `skipInitialResync()` then sets
`_initialFullSyncsRemaining=0` and `_redRamSynced=true`
(`Uc8253X3Driver.cpp:491-494`). `cleanupGrayscaleBuffers()`, called by
`renderer.cleanupGrayscaleWithFrameBuffer()` on the restored-frame path,
also rebases both controller planes and sets `_redRamSynced=true`
(`Uc8253X3Driver.cpp:451-465`; `FreeInkDisplay.cpp:864-880`).

The remaining decisive state is `_isScreenOn`: X3's `displayStart()`
unconditionally changes any first requested mode to `Half` while that flag is
false (`Uc8253X3Driver.cpp:164-178`), and only sets it true in that call's
power-on sequence (`Uc8253X3Driver.cpp:209-214`). Therefore the first
SplashlessWake paint is never a genuine `FAST_REFRESH`.

The old and new DARK-mode paths are nevertheless different, and the code
proves exactly how:

- **Old code (before `4d2a44e4`):** DARK sleep did not create
  `SLEEP_FRAME_FILE`, so the `SplashlessWake` block skipped restore/cleanup
  and left `allowFastInitialReaderRefresh=false` (parent `src/main.cpp`,
  `SplashlessWake` block). The reader's first `displayBuffer(HALF_REFRESH)`
  reached X3 with `_isScreenOn=false`, `_redRamSynced=true` from the
  unconditional seamless `skipInitialResync()`, and
  `_initialFullSyncsRemaining=0`; however `HalDisplay::displayBuffer()`
  calls `requestResync(1)` for X3 HALF (`lib/hal/HalDisplay.cpp:60-65`),
  setting `_forceFullSyncNext=true` (`Uc8253X3Driver.cpp:486-489`). Thus
  `displayStart()` promotes the request to `Half` and `doFullSync` is true
  (`Uc8253X3Driver.cpp:173-190`): the old first paint was a **full-sync
  preparation using the FULL bank**, not merely a half scrub.
- **New code (`4d2a44e4`), DARK mode:** unconditional save creates the frame;
  `loadSleepFrameBuffer()` succeeds and X3 cleanup rebases the controller
  planes (`src/main.cpp:417-429`). `allowFastInitialReaderRefresh=true`
  makes the reader request FAST (`ReaderActivity.cpp:19-25`), but
  `_isScreenOn=false` still promotes it to `Half`. No HALF wrapper call is
  made, so no `requestResync(1)` is armed; with `_redRamSynced=true` and the
  seamless counter already cleared, `doFullSync=false` and `doHalfSync=true`
  (`Uc8253X3Driver.cpp:173-190`). The new first paint is therefore a
  **genuine HALF scrub waveform**, not FAST and not FULL.

The field logs' 3228ms first-page median (`logset-analysis.md:77`) and
445ms ordinary-page median (`:78,88`) establish the observed gap. Static
source proves the old/new transition is FULL-bank preparation → HALF scrub,
but it does **not** provide a measured DARK-mode old-vs-new wall-clock delta:
the SDK does not expose per-call waveform telemetry, and the wasm simulator
does not model waveform timing. The prior `~2.8s` savings headline is
therefore requalified: it is a real, code-supported *mode-strength change*
and removal of the old loading-icon refresh, but its exact savings remain an
open real-hardware measurement. It must not be described as "the first paint
becomes FAST_REFRESH" or as a proven 2.8s reduction for DARK mode.

## Ranked ideas

| rank | idea | potential savings | status |
|---|---|---|---|
| 1 | Extend the sleep-frame save/restore fast-path to every sleep-screen mode, not just Quick Resume | expected to replace the old X3 DARK-path FULL-bank first paint with a HALF scrub, plus remove the redundant icon refresh; exact wall-clock savings unmeasured | **prototyped** |
| 2 | Replace the fixed 500ms recovery-combo settle loop with a debounce-driven early exit | up to 500ms, ~10-20ms common case | **prototyped** (already present from an earlier phase of this session) |
| 3 | Remove the redundant loading-icon paint+refresh on the fast-wake path | one additional e-ink refresh (~200ms-3s) previously spent painting a icon over a frame that was about to be repainted anyway | **prototyped** (bundled into idea #1's code, see diff) |
| — | Fix the "Indexing..." popup's forced full refresh after an incremental section build | out of scope — a legitimate, separate mechanism (clean paint after real cache work); not touched | not attempted |
| — | Extend the X3-only differential-refresh fast path to X4 | unknown — no evidence on X4's controller-RAM-resync safety, all 13 log sessions are X3 | not attempted, stays out of scope |
| — | Two-stage wake refresh: issue a FAST_REFRESH first for immediate legibility, then a HALF/FULL cleanup pass shortly after | none — see "Idea considered and rejected" below | **considered and rejected** |

Idea #1 remains the highest-ranked candidate, but its expected savings are
now explicitly requalified: static code proves a FULL-bank → HALF-scrub
transition on the X3 DARK path and removal of an extra icon refresh, while
the exact wall-clock delta requires real-hardware measurement.

**Idea #1 has bounded scope**: it only helps wake sessions where the
resumed section does *not* need a fresh incremental build. Sessions with a
pending `SCT startBuild` (the "Indexing..." popup) independently force
`pagesUntilFullRefresh=1` via `EpubReaderActivity.cpp:1013-1014,1049-1050,1125-1126`
regardless of this fix — that's correct, separate behavior, not touched.
Of the 9 first-reader-page sessions in the log data, S02
(`logset-analysis.md:461-465`, complete 36-page section cache, no build)
and S12 (`logset-analysis.md:9685-9689`, complete 34-page section cache, no
build) are the cleanest confirmation this fix would have applied to them —
both paid the full ~2.3s `display` cost with a complete, no-build-needed
cache, which is exactly the case this fix targets. Sessions like S01
(`:142-148`), S03 (`:1453-1457`), S04 (`:2676-2680`), S09 (`:9063-9067`)
have partial section caches requiring `SCT startBuild`, so this fix would
not have shortened their first page.

## What was prototyped

All in `src/main.cpp`'s `enterDeepSleep()` and `setup()`'s `SplashlessWake`
case:

1. **Unconditional sleep-frame save.** Removed the
   `isQuickResumeSleep`-gated branch that only called
   `saveSleepFrameBuffer()` for Quick Resume sleeps (and deleted any stale
   file otherwise). `saveSleepFrameBuffer()` is now called unconditionally
   on every `enterDeepSleep()`. This is safe for every sleep-screen mode:
   `SleepActivity.cpp:484-543`'s `onEnter()` dispatch renders a complete,
   valid full-screen frame into `renderer.getFrameBuffer()` for every mode
   (BLANK, CUSTOM, COVER, COVER_CUSTOM, TRANSPARENT_CUSTOM, QUICK_RESUME,
   and the DARK default via `renderDefaultSleepScreen()`) before
   `enterDeepSleep()` reaches the save point, so there is no
   partial/garbage-buffer risk. `SLEEP_FRAME_FILE` has no other
   consumers/semantic coupling anywhere in `src/`/`lib/` beyond
   `main.cpp`'s own save/load/restore code, so widening its lifecycle is
   safe.

2. **Removed the redundant loading-icon paint on the fast-wake path.** The
   `SplashlessWake` case used to draw a loading icon over the restored
   frame and issue a refresh (`FAST_REFRESH` requested on X3,
   `HALF_REFRESH` otherwise) just to show that icon. Since the restored frame
   is already physically on the panel (deep sleep never touches it) and the
   real activity's own first paint replaces it shortly after, that
   intermediate icon+refresh was pure waste — removed entirely. X3 still
   rebases the controller's differential-refresh baseline via
   `renderer.cleanupGrayscaleWithFrameBuffer()` (a RAM write over SPI, not a
   physical refresh). The subsequent reader request is `FAST_REFRESH`, but
   X3's first-display wake guard promotes it to `HALF`; the baseline rebase
   is still required for later differential refreshes.

3. **Recovery-combo settle loop** (idea #2, from an earlier phase of this
   session, unchanged): replaced the fixed `while (millis() - settleStart <
   500) { gpio.update(); delay(10); }` with a debounce-driven early exit —
   `gpio.isDebouncePending()` (`InputManager.h:47`, exposed via
   `HalGPIO::isDebouncePending()`) — while forcing a minimum of two samples
   (needed because the `shortPwrBtn==SLEEP` fast path skips
   `verifyPowerButtonWakeup()`'s own prior polling). Same 500ms worst-case
   ceiling; common case now exits in ~10-20ms.

X4 was deliberately left out of the differential-refresh fast path (same
as before this session) — no evidence its controller-RAM-resync is equally
cheap/safe, and none of the 13 real-device log sessions are X4.

### Known, disclosed tradeoff of idea #1 — NOT hardware-verified

- One additional ~48KB (X4) / ~52KB (X3) SD write on *every* sleep entry,
  not just Quick Resume sleeps (confirmed exact sizes in the simulator, see
  below). This happens on the sleep-entry path, which is not the
  latency-sensitive side of this investigation (the user-facing metric is
  wake-to-reader-text).
- Real-hardware-only, unverifiable-in-this-session risk: the new DARK-mode
  wake path replaces the old FULL-bank preparation with a `HALF` scrub for
  the large sleep-screen → reader content swap. Static tracing proves that
  `cleanupGrayscaleWithFrameBuffer()` restores both X3 controller planes and
  marks the differential baseline synchronized before that scrub
  (`Uc8253X3Driver.cpp:451-465`), but it cannot prove the resulting optical
  cleanliness or contrast on glass. The HALF waveform may leave residual
  ghosting on this unusually large swap, and the restored controller baseline
  must remain correct for subsequent differential page turns. **Must be
  verified on real hardware before merging past this branch.**

## Idea considered and rejected: two-stage wake refresh (FAST first, HALF/FULL cleanup after)

A later phase of this session investigated a new idea before writing any
code: on wake, issue a `FAST_REFRESH` first for immediate legibility, then
follow with a `HALF`/`FULL` refresh shortly after to clean up ghosting,
instead of paying the full waveform cost up front. Investigated per the
user's explicit request, before touching `src/main.cpp`. Conclusion:
**not viable — rejected, no code change.**

### Refresh-mode semantics (X3 and X4), verified against SDK source

- `RefreshMode` is `FULL_REFRESH`, `HALF_REFRESH`, `FAST_REFRESH`
  (`freeink-sdk/libs/display/FreeInkDisplay/include/FreeInkDisplay.h:31`).
- `displayBuffer()`/`displayWithRefreshCycle()` issue one blocking call:
  `Uc8253X3Driver::display()` is `displayStart(...); displayFinish(...);`
  back to back (`Uc8253X3Driver.cpp:159-161`), and `displayFinish()` blocks
  on `bus.waitRefreshComplete()` (`Uc8253X3Driver.cpp:242`) — the CPU
  cannot issue another display command until the panel's current waveform
  physically finishes. Non-fast modes additionally pay a fixed `delay(200)`
  settle after the waveform (`Uc8253X3Driver.cpp:249`).
- **The decisive fact: neither X3 nor X4 will actually run a genuinely
  fast waveform on the first display call after wake, no matter what mode
  is requested.**
  - X3 (`Uc8253X3Driver::displayStart`, `Uc8253X3Driver.cpp:166-168`):
    `if (!_isScreenOn && !turnOff) { mode = RefreshMode::Half; }` —
    "wake transition gets a stronger waveform." `_isScreenOn` defaults
    `false` (`Uc8253X3Driver.h:95`) and is only set `true` inside a
    `display()` call's own power-on sequence, so it is unconditionally
    `false` on the driver's first `display()` call since any boot/reset,
    wake included. A caller-requested `FAST_REFRESH` is silently upgraded
    to `Half` on that call.
  - X4 (SSD1677, `Ssd1677Driver::display`, `Ssd1677Driver.cpp:425-429`):
    the equivalent override — `else if (!_isScreenOn && _cfg.fullSeqOverride
    == 0) { mode = RefreshMode::Half; }`, comment: "X4-class cold start:
    panel asleep -> a (warmed) HALF full-clear." Same mechanism, same
    result. (X4 boards can also run `Uc8279X4Driver` or `Uc8179Driver`
    depending on the runtime hardware probe,
    `freeink-sdk/libs/display/FreeInkDisplay/src/FreeInkDisplay.cpp:140-178`;
    the SSD1677 variant is the one the rest of this doc's X4 evidence
    already refers to, e.g. the RED-RAM comments in
    `FreeInkDisplay.h:195-196`. The other two drivers were not individually
    checked for the same override — out of scope since no idea in this doc
    targets X4 with real log evidence either way.)
  - `Uc8253X3Driver.cpp:143-151`'s comment documents concrete numbers for
    this class of forced-strong-waveform call: "a FAST refresh that costs
    435 ms once warm cost 2989 ms [when forced into a full sync] — the X4
    running the identical boot paid 526 ms for the same paint [when it
    wasn't]." 435-526ms warm-`FAST_REFRESH` matches this doc's own
    steady-state median (445ms, `logset-analysis.md:78,88`); ~2989ms
    forced-strong-waveform is in the same range as the field-observed
    first-reader-page median (3228ms, `logset-analysis.md:77`) — the two
    independent evidence sources corroborate each other.

### Requirement 2 — is `FAST_REFRESH` output legible as text?

Yes. `ReaderUtils::displayWithRefreshCycle()`
(`src/activities/reader/ReaderUtils.h:140-152`) already uses
`FAST_REFRESH` for every ordinary page turn once `pagesUntilFullRefresh >
1`, and the real-device logs show this is the normal, everyday reading
experience: 919 of 1,063 logged pages, median 445ms
(`logset-analysis.md:78,88`). It is the shipped default for reading, not
an experimental/edge-case mode, so its legibility for text is already
proven by production use. No ghosting/contrast/legibility caveat for
`FAST_REFRESH` was found in either LUT header
(`freeink-sdk/libs/display/FreeInkDisplay/src/lut/Uc8253X3Luts.h`,
`.../Ssd1677Luts.h` — grepped for ghost/contrast/degrad/legib, no matches).

### Requirement 3 — chaining feasibility: additive time, not overlapping

Answered unambiguously: **a second refresh cannot start until the first's
waveform physically completes** (`displayFinish()`'s blocking
`bus.waitRefreshComplete()`, see above) — chaining is strictly additive in
wall-clock time, not free/overlapping. Combined with the wake-transition
override above, this closes off the idea from two independent directions:

1. **You cannot get a genuinely fast first post-wake refresh at all** —
   the very call the idea wants to be "fast" is forced to `Half` strength
   by the panel driver regardless of the requested mode, on both X3 and
   X4. Requesting `FAST_REFRESH` for that first call is a no-op from a
   timing perspective.
2. **Even setting that aside, a second (cleanup) refresh call adds to
   total wall-clock time rather than shortening it** — it must wait for
   the first waveform to finish, then pay its own waveform duration plus
   a 200ms non-fast settle delay. Total time would be
   *first-waveform-duration + second-waveform-duration + 200ms*, more
   than doing one refresh alone, plus a visible flash-then-correct
   transition (a UX regression).
3. The first post-wake call is not a FAST pass followed by cleanup: on
   X3 it is a HALF scrub in the new DARK path, while the old DARK path
   schedules a FULL-bank preparation because `HalDisplay::displayBuffer()`
   arms `requestResync(1)`. The source does not prove that HALF is
   non-ghosting, so no such claim is made here. The idea is rejected because
   its proposed FAST first stage is impossible at this call site, and its
   second refresh would still add time rather than replace either existing
   waveform.

### Requirement 4 — not previously in the ranked list

Confirmed: this idea was not present in the "Ranked ideas" table before
this phase (grepped the doc for
`FAST_REFRESH|HALF_REFRESH|FULL_REFRESH|two-stage|flicker` — no existing
entry). Added as a new row with status "considered and rejected" rather
than merged into any existing rank.

### Requirements 5-8 — not prototyped

Given requirement 3's unambiguous answer, prototyping was not attempted:
the idea cannot deliver its claimed benefit on either X3 or X4, and would
make total wake latency worse while adding a visible flash. Per the task's
own framing ("don't ship a bad idea just because it was requested"), this
is reported as a rejection with citations, not shipped. No changes were
made to `src/main.cpp`; commit `4d2a44e4` was not amended for this idea (a
separate, unrelated doc-only correction was folded into the "What was
prototyped" section above, describing behavior that was already true of
the already-shipped code, not a new change).

## Pre-existing bug found and fixed (unrelated to perf, needed to unblock testing)

`src/activities/home/HomeActivity.h:9` forward-declares `struct RecentBook;`
and holds `std::vector<RecentBook> recentBooks;` (a member) while relying on
an implicit inline constructor/destructor. Under Emscripten's stricter
libc++ this fails to compile — the implicit destructor's point of
instantiation and the constructor's exception-cleanup path for
already-constructed members are both pinned to the header's closing brace,
where `RecentBook` is still incomplete. Works fine under ESP32/GCC, so it
was never caught before. Fixed with the standard compilation-firewall
pattern: declared (not defined) the constructor/destructor in
`HomeActivity.h`, defined them out-of-line in `HomeActivity.cpp` after
`RecentBooksStore.h` is visible. Also added a missing
`#include "RecentBooksStore.h"` to `ActivityManager.cpp`, which
instantiates/destroys `HomeActivity` via `std::unique_ptr<Activity>`
without otherwise ever seeing the complete `RecentBook` type. This was
necessary to get *any* wasm build working on this branch; it's orthogonal
to the perf work.

## Simulator verification

### Cold boot (both X3 and X4)

`agent_drive.py --device x4|x3 --do wait_stable --do screenshot --do heap`
against a from-scratch wasm rebuild containing all changes above. Both
targets boot to the Home screen with no visual corruption and normal heap
stats:

- X4: `{"used": 3391328, "free": 4617424, "peak": 3391328, "tasks": 2}`
- X3: `{"used": 3664736, "free": 5029888, "peak": 3664736, "tasks": 2}`

### Reader open + sleep entry (default DARK mode — the mode this fix newly
covers)

Chained `agent_drive.py` actions: Home → Browse Files → `books/` →
`demo.epub` → reader renders page 1/4 → wait past the 2s
`allowSleepAt` guard → long power-button hold → sleep screen renders
correctly (`CrossPoint / SLEEPING`, the default `renderDefaultSleepScreen()`
path — confirms `SETTINGS.sleepScreen` really is `DARK` by default, matching
the Settings screenshot below).

### Sleep-frame file write — direct FS inspection (the actual functional
claim for idea #1)

Since the wasm sim can't measure waveform timing, idea #1's functional
correctness was verified the way the change is actually observable: does
`saveSleepFrameBuffer()` now write `SLEEP_FRAME_FILE` for the **default
DARK** sleep-screen mode, which it never did before this fix? Using a
direct Playwright script against the same build (not just the
`agent_drive.py` CLI, since this needed an `FS.stat()` call `agent_drive.py`
doesn't expose):

```
before sleep, sleep_frame.bin: {'exists': False, 'err': 'ErrnoError: No such file or directory'}
after sleep entry, sleep_frame.bin: {'exists': True, 'size': 48000}   # X4
after sleep entry, sleep_frame.bin: {'exists': True, 'size': 52272}   # X3
```

48000 = 480×800/8 (X4 frame buffer size), 52272 = 792×528/8 (X3 frame
buffer size) — full, non-truncated frame buffers, confirming
`saveSleepFrameBuffer()` runs to completion without error on the exact
sleep-screen mode (`DARK`, the default) that was previously never covered.
No `[SIM]`/`[ERR]` lines appear around the save in the captured console
log — only the expected `Entering activity: Sleep` → `Entering deep sleep`
sequence.

### Simulator wake-cycle limitation (discovered this session, pre-existing,
unrelated to this fix)

Real hardware wakes from deep sleep via a full reboot. The wasm/native
simulator models this with `SimulatorLifecycle::rebootAsPowerWake()`
(`crosspoint-simulator/src/SimulatorLifecycle.cpp:32-43`), which calls
`execvp()` to re-exec the process with a wake-reason env var set. Captured
directly from the browser console during a sleep→wake attempt:

```
[log] [3935] [DBG] [MAIN] Entering deep sleep
[error] execvp: Exec format error
```

`execvp()` has no real OS process to exec inside the Emscripten/browser
sandbox, so it fails (`ENOEXEC`) and the wasm runtime `_exit(1)`s with no
page-reload fallback — the canvas simply freezes. **This means the wasm
simulator cannot execute a full sleep→wake cycle at all, for any
sleep-screen mode, with or without this session's changes** — it's a
structural gap in the wasm harness (native SDL builds have a real process
to re-exec; the wasm target doesn't), not a regression introduced here.
Confirmed this is unrelated to the code changes in this branch: the
`execvp` call site is generic simulator-lifecycle code untouched by any
edit in this diff, and the failure occurs identically regardless of
sleep-screen mode.
Given this, the wake-restore side of idea #1 (`loadSleepFrameBuffer()` →
`renderer.cleanupGrayscaleWithFrameBuffer()` →
`allowFastInitialReaderRefresh = true` → a `FAST_REFRESH` *request* for the
reader's first page) is verified by **static code-path tracing only** (the
"Root cause" section above traces every consumer and the X3 driver's
mode-promotion state), not simulator execution — consistent with the
explicit allowance that simulator timing/full-cycle proof isn't available
for this fix and shouldn't be fabricated.

Caveat: the X3 driver promotes that first request to `HALF` while
`_isScreenOn` is false. The simulator cannot instrument the physical
waveform or establish the old-vs-new wall-clock delta; real-hardware logs
with mode-level instrumentation remain an open verification gap.

### Settings screen (confirms default sleep-screen mode)

Screenshot of Settings → Display confirms `Sleep Screen: Dark` is the
shipped default, matching the assumption this fix is built on.

## Commit

Local commit only, on `area-boot-wake-perf-trimmed`, no push/merge/device
build, per task constraints.

## Wake metric instrumentation

`lib/Logging/WakeMetrics.h/.cpp` emits `WAKE` events with both the firmware
monotonic `t` value and `dt` from the beginning of `setup()`. The earliest
observable boundary is `boot_setup_start`, emitted before board bring-up;
firmware cannot observe the physical beginning of a button press before the
chip wakes/resets. `gpio_wake_classified` follows `gpio.begin()` and the
first `gpio.pollUsbState()`, then records the wake classification
(`HalGPIO::getWakeupReason()`) after USB/SOF state is available. Subsequent
events are `display_begin_complete`, `activity_route`, `reader_constructed`,
`reader_text_frame_start`, `reader_waveform_start`, and `reader_text_visible`.
`activity_route` is the authoritative route/path/resume record; `resume` is
`splash`, `silent`, or `splashless_wake`. The successful `reader_constructed`
event carries only success and `fast_initial` context (`path=-`) to avoid
retaining a moved string. `reader_text_frame_start` and
`reader_text_visible` include `first=1` only for the first candidate and
endpoint of the armed boot-time reader route selected by `activity_route`;
later page events remain possible but cannot replace that endpoint.
Those events report `requested_mode`, the renderer's requested or effective
mode—not the controller's actual waveform strength: an X3 driver may promote
a refresh to HALF or FULL based on synchronization state. Mode `3` is the
explicit grayscale-base request used by XTC 2-bit pages; its
`displayGrayscaleBase()` completion is also covered by this endpoint. The
last event is a post-waveform driver-completion proxy emitted after
`display.displayBuffer()`, `displayGrayscaleBase()`, or
`waitRefreshComplete()` returns. On X3 this can include BUSY completion,
power-off, non-fast delays, post-condition refreshes, and DTM1 synchronization;
it is conservative relative to physical waveform completion and not human
optical perception. It excludes activity construction, framebuffer drawing,
and loading frames. `home_route_requested` identifies home-only route requests
(not a rendered-frame boundary).

For one session, `reader_text_visible.dt - gpio_wake_classified.dt` is a
reproducible firmware milestone interval, not a human press-to-visible metric:
It is neither guaranteed equal to, a lower bound, nor an upper bound for human
latency. Physical onset→visible requires external button-onset and optical
measurements; firmware logs provide milestone timing and a post-waveform
completion proxy. The entire button-down → wake/reset → GPIO classification/
setup portion is unobserved by this firmware; `boot_setup_start` also begins
after ROM/bootloader and reset, not at physical button onset. Use
`activity_route`'s route, `resume`, and path fields, plus `first` and cache-build
fields on `reader_text_frame_start`, to classify sessions. Logging's line formatter
truncates around 255 bytes, so long EPUB paths can truncate trailing WAKE
fields; route/path is context only, while timing fields precede it. Prefer
short or normalized paths, or rely on the first fields.
The earliest WAKE line is retained through the RTC log snapshot when logging
is enabled and the normal `HalSystem` snapshot path runs. Later events depend
on normal ring-buffer flushing through `DiskLogger`; if SD initialization
fails, those later events are not immediately persisted by that boot's
`DiskLogger` path. RTC ring content may survive to a later successful boot,
subject to overwrite or power loss. All WAKE events are
conditional on `ENABLE_SERIAL_LOG` and `LOG_LEVEL >= 1`
(`lib/Logging/Logging.h:44-66`), so measurement requires a corresponding
debug build. No explicit flush, delay, or dedicated SD write was added, but
enabled logging still invokes the normal ring-buffer/`DiskLogger` callback
path and can add timing overhead when that path writes or locks; compare
measurements using the same logging build and treat them as instrumented
measurements. The wasm simulator cannot execute the full deep-sleep reboot
(`execvp()` returns `ENOEXEC` in the browser sandbox), so it cannot provide
complete wake timing; use serial/SD logs and validate the BUSY endpoint on
hardware.
