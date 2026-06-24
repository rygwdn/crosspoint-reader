# Image Page Performance — Proposed Changes

## Prefetch windows: the direct answer

The user asked whether the display's busy periods can be used to pre-fetch the next
strip or the next page's cache from SD. Here is exactly what the pipeline looks like on
an image page with AA on the X4 (tiled path, `renderContents` starting at
`EpubReaderActivity.cpp:986`):

```
BW render (blanked image area)
  └─ displayBuffer(FAST_REFRESH)
       └─ pollBusy()  ← W1 ~1.5 s  SPI bus IDLE, SD available
Re-render with images  [reads .pxc from SD here]
  └─ displayBuffer(FAST_REFRESH)
       └─ pollBusy()  ← W2 ~1.5 s  SPI bus IDLE, SD available
Strip render loop ×12
  for each strip:
    beginStripTarget / clearScreen / page->render  [reads .pxc from SD here]
    writeGrayscalePlaneStrip  ← ~2 ms SPI burst, no gap between strips
displayGrayBuffer()
  └─ refreshDisplay → pollBusy()  ← W3 ~1.5 s  SPI bus IDLE, SD available
cleanupGrayscaleWithFrameBuffer
```

**W1 and W2 are before the SD-intensive strip renders. W3 is after them.**
W1 and W2 are not used by any change below — pre-loading the `.pxc` into RAM during
those windows was considered and dropped (heap cost not justified). W3 is used by
Change 3 to pre-warm the next page's image cache.

Between individual strips there is no idle window — the render pass (including the SD
cache read) IS the work between SPI bursts. The X3 comment at
`EInkDisplay.cpp:1162–1163` ("SPI bus is free for SD-card font reads between bands")
describes font reads that happen naturally inside the render call, not a deliberate
prefetch opportunity.

Total available idle SPI-free time: **~4.5 s per image page turn**. None of it is
currently used for pre-loading.

---

## Change 1 — Idle-work callback in `HalDisplay::displayBuffer()`

**What.** Add a C-style callback slot to `HalDisplay::displayBuffer()` (and down to
`EInkDisplay::pollBusy()`) so callers can do incremental work each millisecond during
the busy-wait, without a FreeRTOS task.

**Why not `std::function`.** The CLAUDE.md forbids it in hot paths: heap alloc, ~4 KB
per unique signature. Use a plain function-pointer + `void*` context pair.

**Interface change** (`lib/hal/HalDisplay.h`):
```cpp
struct IdleWork {
    void (*fn)(void* ctx);
    void* ctx;
};

// Pass {} (zero-initialised) to get current behaviour.
void displayBuffer(RefreshType refreshType, IdleWork idle = {});
```

**Implementation** (`EInkDisplay::pollBusy`, `EInkDisplay.cpp:548`):
```cpp
while (digitalRead(_busy) == HIGH) {
    delay(1);  // → vTaskDelay, FreeRTOS scheduler runs
    if (idle.fn) idle.fn(idle.ctx);
}
```

**Risk.** The callback runs in the main task. It must never take a mutex that an ISR
could also want (HalStorage's `storageMutex` is fine — no ISR touches it). Keep each
callback invocation under ~1 ms of wall time so the poll doesn't visibly delay.

**Files.** `open-x4-sdk/libs/display/EInkDisplay/src/EInkDisplay.cpp:548–586`,
`lib/hal/HalDisplay.h`, `lib/hal/HalDisplay.cpp`.

---

## Change 2 — Seek past irrelevant rows in `renderFromCache` (landscape orientations)

**What.** When a strip target is active and the orientation is landscape, seek the `.pxc`
file to the first row that intersects the strip, read only those rows, and stop. This
replaces reading all `cachedHeight` rows on every strip render.

**When it applies.** In `DirectPixelWriter`, landscape orientations have `phyYStepY ≠ 0`
and `phyYStepX = 0`, meaning physical Y is a function of logical row alone. A strip's
physical Y range therefore maps to a contiguous range of image rows.

Portrait and PortraitInverted have `phyYStepX = ±1` — physical Y varies with column,
not row — so all rows must be read; the seek optimisation does not apply.

**Implementation** (in `renderFromCache`, after reading the 4-byte header,
`ImageBlock.cpp:~line 55`):
```cpp
int firstRow = 0, lastRow = cachedHeight;  // default: read all rows

if (renderer.hasActiveStrip()) {
    const int stripY    = renderer.getWriteOriginY();
    const int stripRows = renderer.getWriteRows();

    switch (renderer.getOrientation()) {
    case GfxRenderer::LandscapeCounterClockwise:
        // phyY = logicalRow  →  row range [stripY, stripY+stripRows) maps directly
        firstRow = std::max(0,            stripY - y);
        lastRow  = std::min(cachedHeight, stripY + stripRows - y);
        break;
    case GfxRenderer::LandscapeClockwise:
        // phyY = (displayH-1) - logicalRow
        firstRow = std::max(0,            (renderer.getDisplayHeight()-1) - (stripY+stripRows-1) - y);
        lastRow  = std::min(cachedHeight, (renderer.getDisplayHeight()-1) - stripY - y + 1);
        break;
    default:
        break;  // portrait: read all rows
    }

    if (firstRow >= lastRow) return true;  // strip doesn't intersect image
    cacheFile.seekSet(4 + static_cast<size_t>(firstRow) * bytesPerRow);
}

// Row loop now runs from firstRow to lastRow instead of 0 to cachedHeight
```

`HalFile::seekSet()` is available (`HalStorage.h:83`, `HalStorage.cpp:155`) and takes
the storage mutex, so this is safe.

**Estimated gain.** Full-page landscape image (728 columns × 482 rows, 80-row strips):
- Current: 482 rows / 22 rows-per-read × 12 strips = ~264 SD reads
- With seek: ~80 rows / 22 × 12 strips = ~44 SD reads (~6× reduction)
- Time saved: ~50–150 ms per page turn in landscape mode with large images


---

## Change 3 — Pre-warm next page's `.pxc` during W3 (grayscale display pollBusy)

**What.** Window W3 (`displayGrayBuffer()` pollBusy, ~1.5 s) occurs after all strip
data has been sent to the controller. Use this window to decode the next image page's
JPEG and write its `.pxc` to SD, so the user never sees the 300–2000 ms JPEG decode
delay when turning to the next image page.

**Implementation sketch** (`EpubReaderActivity.cpp`, inside `renderContents()`):
```cpp
struct PrewarmCtx {
    std::string cachePath, imagePath;
    int16_t     x, y, w, h;
    bool        done = false;
};
const PrewarmCtx prewarm = buildPrewarmForNextPage(section, currentPage + 1);

// ... strip render loop ...

renderer.displayGrayBuffer({
    [](void* c) {
        auto* pw = static_cast<PrewarmCtx*>(c);
        if (pw->done || Storage.exists(pw->cachePath.c_str())) {
            pw->done = true;
            return;
        }
        // Decode runs synchronously across multiple 1-ms ticks;
        // vTaskDelay() in the outer poll loop feeds the watchdog.
        pw->done = warmImageCache(pw->imagePath, pw->x, pw->y, pw->w, pw->h);
    },
    const_cast<PrewarmCtx*>(&prewarm)
});
```

`warmImageCache()` calls
`ImageDecoderFactory::getDecoder(imagePath)->decodeToFramebuffer()` with
`config.cachePath` set. The framebuffer is dirtied but the display is mid-refresh;
the next page turn's BW render overwrites it completely.

**Heap guard.** JPEGDEC needs ~20 KB; the `PixelCache` band buffer needs ~4–8 KB.
Only start if `ESP.getFreeHeap() > 44 * 1024`.

**Watchdog.** A 2000 ms JPEG decode fits inside the 30 s watchdog. The
`delay(1)` / `vTaskDelay(1)` in `pollBusy()` runs continuously while the decode
executes; no explicit `esp_task_wdt_reset()` is needed.

**Chapter-boundary case.** Extend `silentIndexNextChapterIfNeeded`
(`EpubReaderActivity.cpp:951`): after `createSectionFile()` succeeds, iterate the new
section's pages and call `warmImageCache()` for any image without a `.pxc`. This runs
synchronously on the penultimate page of a chapter — already an established idle-work
pattern.

---

## Change 4 — In-chapter lookahead for `.pxc` warming (idle loop, no callback needed)

**What.** At the end of `loop()`, after the current page has rendered and before the
next button press, check whether page N+1 has an image without a `.pxc`. If so, warm it
synchronously. The decode time is hidden inside reading time.

**Where.** In `EpubReaderActivity::loop()`, after the render+save block near line 936:

```cpp
tryPrewarmNextPageImage();
```

```cpp
void EpubReaderActivity::tryPrewarmNextPageImage() {
    if (ESP.getFreeHeap() < 44 * 1024) return;
    if (!section) return;
    const int next = section->currentPage + 1;
    if (next >= section->pageCount) return;

    const Page* p = section->getPage(next);
    if (!p || !p->hasImages()) return;

    for (const auto& img : p->getImages()) {
        const std::string cp = getCachePath(img.getImageBlock().getImagePath());
        if (!Storage.exists(cp.c_str())) {
            const auto& ib = img.getImageBlock();
            warmImageCache(ib.getImagePath(), img.x, img.y,
                           ib.getWidth(), ib.getHeight());
            break;  // one image per loop() call; next call handles the rest
        }
    }
}
```

Requires `Section::getPage(int)` to return a `const Page*` and
`Page::getImages()` to expose the `images` vector — both are trivial one-line
additions if not already present.

---

## Implementation order and dependencies

```
Change 4  (loop lookahead)     standalone — ship first, highest return/effort ratio
Change 2  (seek optimisation)  standalone — low risk, benefits landscape layouts
─────────────────────────────────────────────────────────────────────────────────
Change 1  (pollBusy callback)  prerequisite for Change 3
Change 3  (W3 pre-warm)        depends on Change 1; eliminates first-view JPEG cost
```

Changes 4 and 2 can be merged independently. Change 3 requires the `IdleWork`
callback surface introduced by Change 1.

---

## What this does NOT solve

- **First-ever cold open of a book to an image page.** Changes 3 and 4 both require a
  prior page turn to trigger pre-warming. A startup scan is out of scope (too slow on
  open, speculative).
- **The 1–1.7 s e-ink waveform refresh** — irreducible, hardware-bound.
- **Portrait-orientation large images** — Change 2 (seek) does not apply; they still
  read all rows per strip on every AA pass.
- **Non-tiled path (X3 devices)** — Changes 1–3 apply to the same code paths without
  modification.
