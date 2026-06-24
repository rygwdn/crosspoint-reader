# Spec: Background-task image pre-warm (text→image transitions)

Status: **proposed** — not yet implemented.
Depends on: the merged `perf/image-page-prewarm` work (cache-only decode mode,
`ImageBlock::warmCache`, the `_cancelPrewarm` cancel hook, `close_partial`,
`Section::loadPageFromSectionFile(int)`).

## 1. Problem

The merged pre-warm work hides the JPEG/PNG decode cost in two situations:

- **In-section lookahead** — the next image page is decoded during the current
  image page's W3 grayscale-refresh busy-wait (`displayGrayBuffer`).
- **Chapter boundary** — the next chapter's first page is warmed inside
  `silentIndexNextChapterIfNeeded` at the penultimate page.

Both require the *current* page to do grayscale work (image pages) or to be the
penultimate page of a chapter. The remaining gap is the common case:

> Reading **text** pages, then turning onto an **image** page in the **same
> chapter**.

Text pages do no grayscale refresh, so there is no W3 window and no idle callback
fires. The image is decoded synchronously on arrival → the user eats the full
300–2000 ms decode on top of the irreducible e-ink refresh.

## 2. Why this needs a dedicated task (not a cooperative `loop()` call)

The naive fix — warm the next page from `EpubReaderActivity::loop()` during idle
reading — was rejected earlier and is still wrong, for a single-core reason:

- Input is polled **only** in `loop()` via `gpio.update()`.
- A decode invoked from `loop()` runs *on the main task*. Even though the decode
  yields per MCU block (`vTaskDelay(1)`), `gpio.update()` is **not** on the
  decode's call path, so no input is sampled until the decode returns.
- Result: a 300–2000 ms decode driven from `loop()` makes the device deaf to
  button presses for that whole window — the exact stall we are trying to remove,
  just relocated.

A **separate task** fixes this precisely because the decode's per-MCU
`vTaskDelay(1)` then yields the core *to the main loop task*, which runs
`gpio.update()` and can set `_cancelPrewarm`. The decode observes the flag at its
next MCU block and aborts. Input stays responsive *and* the decode runs in the
background.

## 3. Scheduling model (single core, why a priority-0 task works)

The Arduino loop task and the render task both run at **priority 1**. The main
loop ends each iteration with `delay(10)` (or `delay(50)` after
`IDLE_POWER_SAVING_MS` of inactivity) — `delay()` maps to `vTaskDelay`, which
**blocks** the loop task. The render task blocks on its notify when no render is
pending. So during idle reading **both priority-1 tasks are blocked ~99% of
wall-clock time**, leaving the core free.

A **priority-0** background pre-warm task therefore:

- runs only in those genuine idle gaps;
- is **preempted immediately** by the render task (priority 1) when a render is
  requested — rendering always wins;
- is **preempted immediately** by the main loop (priority 1) when it wakes from
  `delay()` to poll input — input always wins;
- needs no explicit "am I allowed to run" gate for CPU; the scheduler handles it.

This is the key property that makes the feature tractable rather than an overhaul
of the loop/render scheduling.

## 4. Design

### 4.1 Task lifecycle

- Create one long-lived task `xTaskCreate(prewarmTaskLoop, "Prewarm", STACK,
  &activity, 0 /*priority*/, &prewarmTaskHandle)`. Stack: start at **4096 bytes**
  and tune with `uxTaskGetStackHighWaterMark` (the decode itself runs on this
  task; JPEGDEC keeps its large buffers on the heap, not the stack, so 4 KB
  should suffice — verify).
- The task blocks on `ulTaskNotifyTake(pdTRUE, portMAX_DELAY)` until woken, warms
  what it can, then blocks again. It never spins.
- Owned by `EpubReaderActivity` (created in `onEnter`, deleted in `onExit`), or by
  the app shell if pre-warm should outlive a single book. Reader-scoped is
  simpler and matches the data it touches.

### 4.2 Wakeup / trigger

Notify the task (`xTaskNotifyGive(prewarmTaskHandle)`) at the end of a successful
`render()` (render task), once the page is on screen. The task then computes the
warm set for the *current* position and gets to work during the idle gap that
follows. A press that arrives first preempts/cancels it (below).

Optionally also notify on entering idle power-saving, to catch the case where the
user lingers.

### 4.3 Work set

Conservative, matching the existing one-page-lookahead philosophy:

1. Next page in the current section (`currentPage + 1`) if it `hasImages()`.
2. (Optional, later) `currentPage + 2` for fast readers — guarded more tightly on
   heap.

Use `Section::loadPageFromSectionFile(currentPage + 1)` (non-mutating) to peek.
For each `TAG_PageImage`, call
`ImageBlock::warmCache(renderer, x, y, &prewarmCancelRequested, activity)`.
`warmCache` already: checks `Storage.exists` (skip if cached), sets
`cacheOnly = true` (no framebuffer writes), and threads the cancel hook.

### 4.4 Cancellation & coalescing

- Reuse `_cancelPrewarm` (`std::atomic<bool>`). `loop()` already sets it on any
  navigation; the background decode already observes it per MCU and aborts,
  keeping its partial `.pxc` via `close_partial`.
- After the navigation-triggered render runs, `render()` resets `_cancelPrewarm =
  false` (existing behaviour) and re-notifies the task for the new position.
- The task should re-read "what page am I on" from the activity at the **start**
  of each warm item, so a position change already reflected in the activity makes
  it warm the right thing (belt-and-braces with the cancel flag).

### 4.5 Heap

- Keep the existing `ESP.getFreeHeap() < 44 * 1024` guard before each decode
  (JPEGDEC ~20 KB + band ~8 KB + headroom).
- Because the task is reader-scoped and only allocates the decoder transiently
  per image, steady-state heap cost is ~0.

### 4.6 Watchdog

- The per-MCU `vTaskDelay(1)` already feeds the watchdog. A priority-0 task that
  blocks on a notify when idle will not trip the task WDT. If the idle/IDLE0 task
  WDT is enabled, confirm the priority-0 task yields often enough (it does, via
  the decode's `vTaskDelay` and the notify-block).

## 5. Primary risk: shared SPI bus (SD ⇄ display)

This is the sharp edge and the reason the feature is non-trivial.

The SD card and the e-ink controller **share one SPI bus**. The background task
reads the source image from SD (`jpegRead`/`pngRead` → `HalFile` → `storageMutex`)
while the render task drives the display over the same bus.

- Same core, so only one task runs at a time; the `storageMutex` serializes SD
  access. But a **render task (priority 1) preempts the background task (priority
  0)** at any instruction boundary — including **mid-SD-SPI-transaction**.
- If the render task then starts a **display** SPI transaction while the
  background task is suspended mid-**SD** transaction, the bus state is corrupt
  unless *both* SD and display access are guarded by a **single** bus lock, or the
  background task is prevented from running during display I/O.

Mitigations (pick one; recommend **B**):

- **A. Single SPI/bus mutex.** Wrap every SD *and* display SPI transaction in one
  shared `busMutex`. Cleanest correctness, but touches the SDK display path and
  every SD op; priority inheritance keeps render-task stalls bounded to one
  background SD chunk (a few ms). Largest blast radius.
- **B. Gate the background task on render state (recommended).** The background
  task only runs when no render is active/pending. Implement a `renderActive`
  flag (or reuse the render notification / lock state): the task checks it before
  each SD chunk and, if a render is pending, releases the `storageMutex`, aborts
  the current decode (`close_partial`), and blocks until re-notified. Combined
  with `_cancelPrewarm` this means: navigation → render requested → task backs off
  immediately. Residual window: a render that starts *between* the task's check
  and its next SD op — bounded by `storageMutex` priority inheritance (one SD
  chunk, single-digit ms). Acceptable and far smaller than option A.
- **C. Chunked, lock-free-of-display SD reads.** Reduce `HalFile` read chunk size
  so the background task holds the SD path only briefly, shrinking the inversion
  window. Complementary to B, not a substitute.

The merged code already removed the *framebuffer* hazard (cache-only decode), so
SPI arbitration is the only remaining shared resource.

## 6. Secondary risks

- **`storageMutex` contention / priority inversion.** Bounded by FreeRTOS
  priority inheritance to one SD operation; keep SD chunks small (§5C).
- **Section object lifetime.** The task reads the current `Section` via the
  activity. If a chapter change frees/replaces the `Section` while the task holds
  a raw pointer, that's a use-after-free. Mitigate: the task should re-fetch the
  `Section*` from the activity under the same flag that gates navigation, and
  treat `_cancelPrewarm` (set on every nav, including chapter change) as a barrier
  — abort and re-sync on wake. Do **not** cache a `Section*` across a yield.
- **Double work.** The W3 in-section path and the background task can target the
  same page. `warmCache`'s `Storage.exists` check makes the second a fast no-op;
  no locking needed beyond that.
- **Power.** Pre-warming spins the CPU up during what would be deep idle. Skip
  pre-warm once `setPowerSaving(true)` has engaged (after
  `IDLE_POWER_SAVING_MS`), or accept the trade. Recommend: warm eagerly right
  after a page turn, then go quiet — don't warm during long-idle.

## 7. Integration points

| Area | Change |
|---|---|
| `EpubReaderActivity.h/.cpp` | `prewarmTaskHandle`, `prewarmTaskLoop`, create/delete in `onEnter`/`onExit`; notify at end of `render()`; `renderActive` flag (option B). |
| `EpubReaderActivity::loop()` | already sets `_cancelPrewarm` on nav — reuse. |
| SD / SPI | option B gating, or option A bus mutex (SDK + `HalStorage`). |
| `ImageBlock::warmCache` | reused as-is. |
| `Section` | `loadPageFromSectionFile(int)` reused as-is. |

No changes to the decoders or `PixelCache` — the merged work already exposed
everything needed.

## 8. Validation

- **Functional:** read text pages up to an in-chapter image page; confirm the
  image appears with no decode stall (timing logs around the BW pass should show a
  cache hit, not a decode).
- **Responsiveness:** hold/repeat page turns across an image page while pre-warm
  is active; confirm no dropped/lagged input (gpio sampled within one `delay(10)`
  window + one MCU block).
- **SPI integrity:** stress rapid navigation during pre-warm; watch for display
  corruption or SD read errors (the option-B gate should prevent both). Run with
  the WDT enabled.
- **Heap:** confirm steady-state free heap is unchanged after many warms (no
  leak; decoder freed each time).
- **Stack:** check `uxTaskGetStackHighWaterMark` and right-size `STACK`.

## 9. Scope estimate

- Option **B** (recommended): ~150–250 lines, mostly task lifecycle + the
  render-state gate + careful `Section*`/cancel handling. No decoder/SDK changes.
- Option **A** (bus mutex): add SDK display-path locking → larger blast radius,
  more review, higher regression risk on the e-ink driver. Only pursue if option
  B's residual inversion window proves problematic in practice.

Recommend implementing **B**, measuring the inversion window under stress, and
escalating to **A** only if needed.
