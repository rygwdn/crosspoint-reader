# Cross-chapter pre-render lookahead

Status: implementation plan, in progress on branch `area-epub-lookahead`
(worktree `~/crosspoint-reader-worktrees/area-epub-lookahead`, based on
fresh `upstream/develop`, currently `SECTION_FILE_VERSION = 35`). Written
2026-08-06.

## Question

Does the reader currently render/index ahead past the current spine item
(chapter), and if not, could it maintain a constant N-pages-ahead cache
regardless of chapter boundaries without blowing the 380KB RAM budget?

## Current behavior (as of `personal-integration`)

Two "ahead" mechanisms exist today, both scoped to `currentSpineIndex`:

1. **Idle glyph prewarm** — `EpubReaderActivity.cpp:355-394`. On idle
   (debounced 400ms, gated on free heap / max alloc block / no active
   build), scan-renders `currentPage + 1` from the *already-loaded* `section`
   to warm the font glyph cache. No pixels drawn, no next-chapter access.
   The comment at `EpubReaderActivity.cpp:367-368` is explicit: *"Cross-chapter
   prewarm is deliberately out of scope (next spine's section isn't loaded)."*

2. **Partial section background build** — `EpubReaderActivity.cpp:396-430`,
   using `Section::startBuild()`/`buildSomeMore()` (added in #2452, see
   below). Keeps a window of `BUILD_WINDOW_AHEAD = 5` pages
   (`EpubReaderActivity.h:148`) built ahead of `currentPage`, or resumes a
   suspended partial once within `PARTIAL_REBUILD_START_MARGIN = 15` pages
   of its watermark (`EpubReaderActivity.h:155`). Entirely within the
   current spine's `Section` object.

Actual spine advancement is reactive only: `currentSpineIndex++` at
`EpubReaderActivity.cpp:1106`, gated on the current section being fully
built, with no anticipatory `Section` object created for the next spine.

## Why: memory cost of a second `Section`

A **finalized** section costs almost nothing extra to keep reading from —
`Section::loadPage()` reads laid-out pages back from the on-disk `.bin`
cache via `HalFile`, it doesn't hold rendered pages in RAM
(`lib/Epub/Epub/Section.h:17-21`, `81-87`).

An **actively building** section is the expensive case. `Section::BuildContext`
(`lib/Epub/Epub/Section.h:37-55`) holds: a live `ChapterHtmlSlimParser`, the
strings it parses by reference (`parsePath`, `contentBase`, `imageBasePath`,
`htmlPath`, `tmpHtmlPath`), and an in-RAM page-offset LUT
(`std::vector<PageLutEntry>`, 12 bytes/entry) that grows for every page laid
out so far. Starting a second spine's build stacks a second `BuildContext`
on top of the first — that's the real cost the "out of scope" comment is
guarding against, not the finalized-page storage.

Heap guard rails already in place for a *single* background build:
`RENDER_MIN_FREE_HEAP = 24KB` (`EpubReaderActivity.h:140`),
`BACKGROUND_BUILD_MIN_MAX_ALLOC = 16KB` (`EpubReaderActivity.h:128`),
checked at `EpubReaderActivity.cpp:372` (idle prewarm) and referenced again
around `:1769`. `BUILD_POPUP_BYTE_THRESHOLD = 96KB` (`EpubReaderActivity.h:171`)
gates when the blocking indexing popup shows for a spine too large to lay
out fast.

## History: this was tried once, dropped once

**PR #979** ("silent pre-indexing for the next chapter", merged
2026-02-18, commit `2ab4e30b`) did cross-chapter pre-indexing. Mechanism
(`silentIndexNextChapterIfNeeded`, added to `EpubReaderActivity.cpp`):

```cpp
void EpubReaderActivity::silentIndexNextChapterIfNeeded(...) {
  if (section->currentPage != section->pageCount - 2) return;  // penultimate page only
  Section nextSection(epub, nextSpineIndex, renderer);          // stack-local
  if (nextSection.loadSectionFile(...)) return;                 // cache hit -> skip
  nextSection.createSectionFile(...);                           // synchronous one-shot build
}                                                                 // destructor frees everything
```

It bounded memory by making `nextSection` a stack-local that existed only
for the duration of one function call: `createSectionFile()` was the
pre-incremental, *synchronous, one-shot* build (parse whole chapter, lay
out every page, write `.bin`), and the moment the call returned, the
`BuildContext` was freed — nothing about the next chapter stayed resident
afterward. Only current spine's `section` member persisted past the call.
Reader input was explicitly blocked during the call (per the PR
description) specifically because there was no incremental/resumable path
back then — the whole next chapter was built in one blocking stall on the
render task, which is also why it froze GPIO polling for that duration on
a large next chapter.

That tradeoff (RAM bounded, but a real UI freeze proportional to next
chapter size) didn't survive **PR #2452** ("lazy incremental EPUB section
indexing", merged 2026-07-04, commit `685d4e88`), which replaced
synchronous whole-chapter builds with the incremental model
(`Section::startBuild()`/`buildSomeMore(maxPages, maxDurationMs)`/
`finalizeBuild()`) precisely to stop blocking the UI on large single
chapters. The next-chapter pre-index feature doesn't appear to have been
carried forward into that redesign — it isn't present in current
`develop`/`personal-integration`.

## What's different now

The incremental, resumable build primitives added by #2452 didn't exist
when #979 was written. `Section::buildSomeMore(maxPages, maxDurationMs)`
already yields on a time budget for exactly this reason (comment at
`Section.h:100-104`: "for the background tick in `EpubReaderActivity::loop()`,
which must return promptly so GPIO keeps getting polled"). In principle
that primitive removes #979's blocking problem — the open question is
whether it can be pointed at a *second* spine's `Section` without the RAM
cost of two live `BuildContext`s stacking, and how to express "N pages
ahead" as a quantity that spans spine boundaries instead of resetting at
each one.

## Goal for further investigation

- Maintain a constant number of pages pre-cached ahead of `currentPage`,
  independent of spine/chapter boundaries (today it resets to 0 lookahead
  at each `currentSpineIndex++`).
- Reduce the memory cost of a section's *initial* build/load specifically,
  since that's the recurring cost paid at every chapter boundary.
- Make builds (current-chapter extension and any future next-chapter
  prefetch) resumable/interruptible enough that the button poller in
  `EpubReaderActivity::loop()` never stalls — building on the pattern
  `buildSomeMore(maxPages, maxDurationMs)` already established.

---

## Investigation findings

Three background agents were run in parallel against `personal-integration`
to ground the above goal in real numbers and existing mechanisms. Raw
citations are theirs; synthesis below is mine.

### 1. Memory cost of a bounded second build

- The only state that *accumulates* for the life of a build is the page LUT:
  `PageLutEntry` is exactly 12 bytes (`Section.h:28-33`), pushed once per
  completed page (`Section.cpp:397-398`), never trimmed until
  `finalizeBuild()`/`suspendBuild()`/`abandonBuild()`. **No `lut.reserve()`
  call exists anywhere** in `Section.cpp` or `ChapterHtmlSlimParser.cpp` —
  it grows via the default 2x-realloc, which the `heap-discipline` skill
  flags as a fragmentation source (old+new blocks briefly coexist).
- HTML is **streamed, not buffered**: `ChapterHtmlSlimParser::beginParse()`
  opens the SD-cached HTML into a `HalFile` (`ChapterHtmlSlimParser.cpp:1630`)
  and `parseStep()` reads only 1024 bytes (or 128 under a time budget) per
  call (`ChapterHtmlSlimParser.cpp:27,35,1649-1672`). So a second concurrent
  build's HTML cost is one more open file handle + a ~1-2KB expat buffer,
  **not** a second full chapter's text in RAM.
- Estimated cost of a second `BuildContext` **capped to a handful of
  pages**: fixed struct+strings (~150-300B) + `ChapterHtmlSlimParser`
  instance, dominated by its 201-byte `partWordBuffer`
  (`ChapterHtmlSlimParser.h:38`) plus its own small vectors (~0.5-1KB) +
  expat's internal buffer (~1-2KB) + one extra `HalFile` + a capped LUT
  (10 pages × 12B = 120B) ≈ **2-5KB total**. Against the existing guard
  floors — `BACKGROUND_BUILD_MIN_FREE_HEAP = 32KB`,
  `BACKGROUND_BUILD_MIN_MAX_ALLOC = 16KB` (`EpubReaderActivity.h:122,128`,
  the latter added after a real OOM at 34.7KB free / ~11KB max block) —
  that's plausible headroom **as long as the second context stays capped**.
- The risk case is the opposite: an *uncapped* second `BuildContext` on a
  giant chapter, where the un-reserved LUT alone could reach 4-36KB by the
  time a several-hundred-page chapter nears completion. That's the scenario
  that could realistically trip the 16-32KB guard floors, especially
  stacked on the current chapter's own in-progress build state.
- These are field-by-field estimates (no on-device measurement — the repo's
  `scripts/script_profile_mem.sh` and `scripts/firmware_size_history.py`
  are both static/link-time (`.dram0.bss`/rodata/text sizes and PlatformIO's
  linker summary), neither observes runtime heap or fragmentation). Real
  validation needs on-device `ESP.getFreeHeap()`/`ESP.getMaxAllocHeap()`
  logging, the same way `buildTickHeapGate()` already does it.
- Precedent for bounding-by-scope: PR #979's stack-local `Section` (see
  history above) bounded its footprint by lifetime, not by a page cap —
  it just built everything then freed everything. A capped-N approach
  (explicit `lut.reserve(N)` for a small fixed N) is the same idea applied
  incrementally instead of all-at-once.

### 2. Scheduling and responsiveness

- **Threading**: ESP32-C3 is single-core. GPIO polling (`gpio.update()`,
  `main.cpp:500`) and `EpubReaderActivity::loop()` (`main.cpp:602`) run on
  the same task, sequentially — no OS-level interleaving. A slow tick
  delays the *next* `gpio.update()` by exactly its own duration.
- **Today's tick budget is approximate, not enforced.** The background tick
  calls `buildSomeMore(BACKGROUND_BUILD_PAGES_PER_TICK=2,
  BACKGROUND_BUILD_MAX_MS=15)` (`EpubReaderActivity.h:113,163`), gated by
  `buildTickHeapGate()` (`EpubReaderActivity.cpp:308-318`) and
  `!RenderLock::peek()`. Inside `Section::buildSomeMore`
  (`Section.cpp:423-455`), the time check happens **only between whole
  `parseStep()` calls**, not inside one. `maxDurationMs > 0` shrinks the
  parser's read chunk from 1024B to 128B (`ChapterHtmlSlimParser.cpp:27-35`)
  specifically so a dense run of synchronous `<img>` extractions can't all
  land inside one uninterrupted step — but a single `parseStep` covering
  one image extraction can still overrun the 15ms budget. The code already
  logs this itself (`EpubReaderActivity.cpp:443-448`, "tick ran %lums
  (budget %lums)").
- **Real stalls already exist**, just not from the background tick: the
  synchronous first-load/build path in `render()` runs on the render task
  holding `RenderLock` (a real blocking `xSemaphoreTake(...,
  portMAX_DELAY)`, `ActivityManager.cpp:326-329`) for the *entire* build —
  percent-jump navigation calls the fully-blocking `createSectionFile`
  (`EpubReaderActivity.cpp:1251-1270`), and normal opens loop
  `buildSomeMore(BUILD_PAGES_PER_CHUNK=8)` with **no time budget at all**
  until the target page is reached (`:1339-1360`, `:1425-1452`).
  `BUILD_POPUP_BYTE_THRESHOLD` only gates whether a popup is *shown*, not
  how long the block runs. Any input path that blocks on `RenderLock`
  (spine-advance/back at `:1104,1116`, chapter-skip long-press `~:686`,
  screenshot combo, forced refresh) genuinely freezes during a multi-second
  giant-spine build today — this is a pre-existing gap, not something a
  prefetch feature would introduce.
- **The actual blocker for crossing a spine boundary mid-tick**:
  `Section::startBuild()` (`Section.cpp:246-421`) does a synchronous HTML
  materialization *before* the parser can run at all — if the next spine's
  HTML isn't SD-cached yet, it does a blocking unzip-to-SD stream in 8KB
  chunks with retry/`delay(50)` (`Section.cpp:290-320`), explicitly
  documented as "multi-second on a giant spine." Even cached, `startBuild`
  still does directory creation, tmp-file cleanup, opening two file
  handles, header write, and TOC-anchor vector construction — all
  synchronous, no internal time-check. **This is the piece that would need
  to become interruptible/time-boxed before a next-chapter prefetch could
  safely run inside the existing 15ms tick budget.**
- **`suspendBuild()` is the right primitive to build on.** It already
  implements "pause now, resume transparently later": persists in-progress
  pages as a partial file with a byte-watermark trailer
  (`Section.cpp:643-680`), keyed purely by `spineIndex`
  (`Section.h:18`) — not "is this the reader's current chapter." Any
  `Section` object, including a prefetch one, gets this for free just by
  existing and being destructed normally (`~Section()` calls it
  unconditionally, `Section.cpp:73`). A prefetch build interrupted by tick
  budget, heap gate, or an actual page turn can call `suspendBuild()`
  instead of `abandonBuild()`, and the next real chapter open finds a
  partial instead of starting cold — no new persistence code needed.

### 3. Cross-boundary design options

Grounded in `Epub.h:67-79` (`getSpineItem`, `getSpineItemsCount`,
`getCumulativeSpineItemSize`, `calculateProgress`,
`getTocIndexForSpineIndex`) and the `EpubReaderActivity` member/call-site
map (`currentSpineIndex` mutated only at three sites: page-forward
`:1106`, page-back `:1113-1119`, chapter-skip `~:696`).

**Option A — chained single-slot prefetch, discard-on-completion
(recommended).**
New members: `std::unique_ptr<Section> prefetchSection`,
`int prefetchSpineIndex = -1`, `int prefetchAheadPages = 0`. Ahead-count =
(current section's remaining pages, using `estimatedTotalPages()` while
still building) + `prefetchAheadPages` + (live prefetch section's page
count so far). Tick logic: only ever tick `prefetchSection` when `section`
itself isn't short of its own window — **the two builds are serialized, so
at most one `BuildContext` is ever alive at a time**, same peak as today,
just relocated across a boundary instead of confined within one chapter.
When a prefetch hop finalizes, fold its page count into
`prefetchAheadPages` and discard the `Section` object (frees
`BuildContext`); if still short of target and the spine has more tiny
items ahead, open the next one and repeat. At the three
`currentSpineIndex++`/`--` sites: if `prefetchSpineIndex ==
currentSpineIndex + 1` and a live `prefetchSection` exists, promote it via
`section = std::move(prefetchSection)` (cheap, no reopen); otherwise fall
back to today's `section.reset()` + lazy reopen, which hits the partial
cache the prefetch already wrote — fast, no popup. Progress/TOC/bookmark
logic (`calculateProgress`, `getTocIndexForSpineIndex`,
`bookmarkMatchesProgress`) all read `currentSpineIndex` directly and never
touch the new members, so they're structurally inert until promotion flips
the index. Suspend/resume needs zero new code — `~Section()` already
`suspendBuild()`s unconditionally, keyed by spine index alone.
*Tradeoff*: peak new RAM ≈ one extra `Section` shell most of the time, one
extra `BuildContext` only during an active hop (never two at once). Extra
CPU only on the rare "prefetch overshot by 2+ spines" promotion path (one
cache-hit reopen). Reuses ~90% of existing machinery.

**Option B — multi-slot live prefetch queue (rejected).** Keep every
hopped-through `Section` alive in a small `deque` instead of discarding
after finalize, avoiding the reopen cost on multi-hop promotion. Rejected:
peak RAM scales with however many tiny spines fit in the window (worst
case several concurrent `Section` shells), and the reopen it avoids is
already a cheap SD cache hit — not worth the extra container-management
code and memory ceiling.

**Option C — no chaining, next-spine only (minimal, insufficient).** Only
ever prefetch `currentSpineIndex + 1`, capped at its own page count, never
hopping further even if still short of the lookahead target. Much less
code, but silently fails to deliver constant-N lookahead across a run of
tiny front-matter spine items (the exact case PR #2452's motivation calls
out) — doesn't meet the stated goal.

**Recommendation: Option A.** It's the only one that satisfies constant-N
lookahead across arbitrarily many tiny spine items while keeping peak
memory pinned at "one extra `Section` shell, one `BuildContext` at a time,"
and it reuses `buildSomeMore`, the existing heap gates, `suspendBuild`, and
`loadSectionFile` almost entirely as-is.

## Prerequisites before this is buildable

Both independent investigations converged on the same blocker: today's
15ms background-tick budget only bounds *parsing* time, not the
synchronous HTML-materialization step inside `Section::startBuild()`
(unzip-to-SD on a cache miss, file/handle setup, TOC-anchor construction).
A prefetch build has to call `startBuild()` on a brand-new `Section` mid-tick
to open the next spine — so **`startBuild()`'s inflate path needs to become
interruptible/time-boxed** (mirroring the existing `useSmallChunks`
approach in `ChapterHtmlSlimParser`) before Option A can safely run inside
the existing per-tick budget without risking the same kind of stall
`BUILD_POPUP_BYTE_THRESHOLD` already exists to warn about.

Second prerequisite, smaller: add an explicit `lut.reserve(N)` cap (or a
hard page-count ceiling) on any prefetch `Section`'s build, so its LUT
growth is bounded by construction rather than relying on discipline alone
— this is what keeps a prefetch's worst case at ~2-5KB instead of
accidentally reproducing the current chapter's unbounded-growth pattern
(itself worth fixing independently, since `Section.cpp` has no
`lut.reserve()` call today even for the single current-chapter build).

## Scope check

Falls inside `SCOPE.md`'s in-scope "Reading UX: ... page navigation" and
directly serves the project's current-focus priority of memory-footprint
work, *if* implemented as Option A: bounded, serialized, reuses existing
mechanisms rather than adding a new subsystem. Would fail the
`scope-discipline` gate if implemented as Option B (unbounded queue) or
without the LUT cap — cost would no longer be quantifiable/bounded against
the reading benefit.

---

## Implementation plan

Three phases, strictly ordered: Phase 1 removes the unbounded-LUT risk
(standalone win, benefits every build today, not just prefetch). Phase 2
makes `startBuild()`'s HTML materialization interruptible (prerequisite
for Phase 3's background tick). Phase 3 is Option A itself.

### Phase 1 — Bound the in-progress page LUT

**Problem being fixed**: `BuildContext::lut` (`Section.h:39`) is a
`std::vector<PageLutEntry>` (12 bytes/entry) that grows for every page laid
out in a build and is never trimmed until finalize/suspend/abandon. No
`reserve()` call exists for it anywhere. On a giant single-spine chapter
this is the dominant, unbounded-with-position RAM cost of a build.

**Key existing fact that makes this safe**: page *content* already avoids
this problem — `onPageComplete()` (`Section.cpp:75-95`) serializes each
page straight to the tmp `.bin` on SD the instant it's built; nothing about
page content stays in RAM. The LUT is the only per-page state still living
entirely in RAM for the build's whole duration. Apply the same trick to it.

**Design**:
1. Add a small spill file per build: `binTmpPath() + ".lut"`, opened
   `O_RDWR`/created alongside the main tmp `.bin` in `startBuild()`
   (`Section.cpp` ~line 339-412, where the other build file handles are
   set up). Every `PageLutEntry` produced gets appended there immediately
   as fixed-size records (same field layout/order as the struct — written
   field-by-field via `serialization::writePod`, not a raw struct write,
   per the RISC-V alignment rule in `CLAUDE.md`). Because it's one fixed-size
   record per page in page order, entry `N` always lives at a computable
   byte offset (`N * recordSize`) — no index needed, direct seek.
2. Cap `BuildContext::lut` (the in-RAM copy) to a small fixed window —
   new constant e.g. `constexpr int LUT_RAM_WINDOW_PAGES = 64` (768 bytes
   worst case, comfortably above `BUILD_WINDOW_AHEAD=5` and
   `PARTIAL_REBUILD_START_MARGIN=15` so today's sequential-access patterns
   always hit RAM) in the same anonymous namespace as `SECTION_FILE_VERSION`
   (`Section.cpp:13-60`). `reserve(LUT_RAM_WINDOW_PAGES)` once at
   `BuildContext` construction (heap-discipline rule: reserve before any
   push loop). When a push would exceed the cap, `erase(lut.begin())` —
   O(window size), i.e. O(64), not O(total pages); this is not the same
   unbounded cost being removed.
3. Centralize page recording: replace the page-complete lambda's direct
   `ctxPtr->lut.push_back(...)` (`Section.cpp:395-399`) with a new
   `Section::recordBuiltPage(fileOffset, paragraphIndex, listItemIndex,
   visibleTextOffset)` that does the spill-file append, the windowed
   RAM push/evict, and updates a new scalar `BuildContext::lastVisibleTextOffset`
   (replaces needing `lut.back()` once the vector is windowed).
4. Add `Section::getLutEntry(int page) -> std::optional<PageLutEntry>`:
   if `page` falls within the current RAM window (`page >= builtPageCount_ -
   lut.size()`), return directly by computed index; otherwise seek the
   spill file to `page * recordSize` and read the record back — mirroring
   the existing seek-read-restore pattern `loadPageDuringBuild` already
   uses for page content (`Section.cpp:715-720`, "read the already-written
   page, then restore the write cursor").
5. **Replace every raw `build_->lut[page]` / `build_->lut.size()` /
   `build_->lut.empty()` / `build_->lut.back()` access with the
   `builtPageCount_`-based accessor.** This is the part that must not be
   missed — today's code assumes `lut[page]` == absolute page `page`,
   which breaks the moment the vector is windowed. Confirmed call sites:
   - `loadPageDuringBuild` (`Section.cpp:707-724`): guard becomes
     `page < builtPageCount_`; entry via `getLutEntry(page)`.
   - `loadPage`'s build-branch check (`Section.cpp:769`): `page <
     static_cast<int>(build_->lut.size())` → `page < builtPageCount_`.
   - `getVisibleTextOffsetForPage` (`Section.cpp:976-979`): same pattern.
   - `getPageForVisibleTextOffset`'s `findInEntries(build_->lut)` and its
     `build_->lut.back()` guard (`Section.cpp:1015-1034`): replace the
     generic-container lambda with a loop over `getLutEntry(i)` for `i` in
     `[0, builtPageCount_)`, and replace `.back().visibleTextOffset` with
     the new `lastVisibleTextOffset` scalar.
   - `findAnchorDuringBuild` (`Section.cpp:463-469`) does **not** touch
     `lut` (reads `parser->getAnchors()` directly) — no change needed,
     confirmed by reading it.
6. `commitBuildFile()` (`Section.cpp:528-580`) currently loops `for (const
   auto& entry : build_->lut)` four times to write four columnar arrays
   (file offsets, paragraph indices, list-item indices, visible-text
   offsets — this on-disk layout is unrelated to and unaffected by the
   in-RAM windowing). Change each loop to `for (int page = 0; page <
   builtPageCount_; page++) { const auto entry = getLutEntry(page); ...
   }`, reading through the same accessor (mix of spill-file reads for
   older pages + RAM for the recent tail). **Output must stay byte-for-byte
   identical to today** — this is what avoids needing a `SECTION_FILE_VERSION`
   bump, sidestepping the version-collision issue already seen once between
   area branches (see `crosspoint-personal-integration` memory).
7. Cleanup: `abandonBuild()` (`Section.cpp:682-705`) and both branches of
   `commitBuildFile()` (success and `failCommit()`) must close and
   `Storage.remove()` the spill file — same lifetime as the main tmp `.bin`.

**Explicitly not changed**: the finalized/partial on-disk file format
(header, four columnar LUT arrays, anchor map, watermark trailer) — Phase 1
is purely about what's held in RAM *during* an active build. No
`SECTION_FILE_VERSION` bump, no `docs/file-formats.md` update needed.

**Done when**: `pio run -e default` builds clean; a giant single-spine
chapter's peak RAM during build no longer scales with pages built so far
(spot-check by logging `ESP.getFreeHeap()`/`ESP.getMaxAllocHeap()` before
and periodically during a build of a large test chapter, comparing against
today's `personal-integration` build); existing behavior unchanged (page
turns, TOC/anchor jumps, percent-jump, bookmark restore, partial
suspend/resume all still correct) — no on-disk format change means no
`.crosspoint/` cache-clear is needed to test.

### Phase 2 — Make `startBuild()`'s HTML materialization interruptible

**Problem being fixed**: per the scheduling/responsiveness investigation,
`Section::startBuild()` (`Section.cpp:246-421`) does a fully synchronous,
uninterruptible sequence before the resumable parser can even begin: on an
HTML-cache miss, a blocking unzip-to-SD stream in 8KB chunks
(`Section.cpp:290-320`, documented as "multi-second on a giant spine"),
plus directory setup, tmp-file handling, two file opens, header write, and
TOC-anchor vector construction (`Section.cpp:339-412`) — none of it
time-boxed. A next-chapter prefetch calling `startBuild()` on a fresh
`Section` mid-tick would hit this same unbounded stall the existing
`BUILD_POPUP_BYTE_THRESHOLD` popup logic already exists to warn users
about, except with no popup to explain the freeze (background tick, not a
foreground open).

**Design**: split `startBuild()` into an explicit resumable phase, mirroring
how `ChapterHtmlSlimParser` already does `beginParse()`/`parseStep()`/
`finishParse()`:
1. New `Section` build sub-state (e.g. an enum on `BuildContext` or a
   separate small state machine) distinguishing "materializing HTML" from
   "parsing." Entered via a `startBuildAsync()` (or extend `startBuild()`
   with a chunked mode) that does just the cheap setup (dir/tmp-file/header)
   synchronously, then does the unzip-to-SD stream in bounded chunks driven
   by repeated calls, same shape as `buildSomeMore(maxPages,
   maxDurationMs)` — i.e. a `continueMaterializingHtml(maxDurationMs)` that
   does one bounded burst of the existing 8KB-chunk loop
   (`Section.cpp:290-320`) and returns, to be called from
   `EpubReaderActivity::loop()`'s tick alongside `buildSomeMore()`.
2. `EpubReaderActivity::loop()`'s tick logic checks which phase the active
   build (current-chapter or, after Phase 3, prefetch) is in and calls the
   matching bounded step function, so a single `RenderLock`-free tick
   never spans more than its budget regardless of which phase it's in.
3. Existing callers of `startBuild()` that need it to run to completion
   synchronously (first foreground open, percent-jump navigation) keep a
   `startBuild()` wrapper that loops the new chunked calls with no time
   limit — same blocking behavior as today for those paths, since fixing
   *their* responsiveness is out of scope for this plan (the doc already
   notes this as a pre-existing gap, not something this feature introduces
   or needs to solve).

**Done when**: `pio run -e default` builds clean; opening a chapter whose
HTML isn't yet SD-cached, via the background-tick path (once Phase 3 uses
it), no longer produces a multi-second single tick — verify by timing/
logging individual tick durations the same way `EpubReaderActivity.cpp:443-448`
already logs tick overrun today.

### Phase 3 — Option A: chained single-slot prefetch

Implements the design already recommended above. Concretely:

1. New members on `EpubReaderActivity` (`EpubReaderActivity.h`, near
   `section`/`currentSpineIndex`): `std::unique_ptr<Section>
   prefetchSection`, `int prefetchSpineIndex = -1`, `int
   prefetchAheadPages = 0`.
2. New helper computing total ahead-count: current section's
   `(pageCount or estimatedTotalPages()) - (currentPage + 1)`, plus
   `prefetchAheadPages`, plus the live `prefetchSection`'s `pageCount` if
   one is building. Replaces the single-term check at
   `EpubReaderActivity.cpp:429`.
3. Tick logic in `loop()`: tick `section` first if it's short of its own
   window (today's existing behavior, unchanged). Only tick
   `prefetchSection` when `section` isn't mid-tick-budget itself (never two
   active `BuildContext`s ticking in the same pass — peak stays "one extra
   `Section` shell, one `BuildContext` at a time," per the design doc
   above). If combined ahead-count is short of target and
   `currentSpineIndex + 1 < epub->getSpineItemsCount()`: lazily construct
   `prefetchSection` for `currentSpineIndex + 1` (or the next unopened
   index in the chain) if none is live, `startBuildAsync()`/materialize
   step from Phase 2, then `buildSomeMore()`. On `isBuildComplete()`, fold
   its `pageCount` into `prefetchAheadPages`, discard the `Section` object
   (frees its `BuildContext`), advance `prefetchSpineIndex`, and continue
   the chain if still short (the tiny-front-matter-spine case).
4. Promotion at the three existing `currentSpineIndex` mutation sites
   (`EpubReaderActivity.cpp:1106` forward, `:1113-1119` back, `~:696`
   chapter-skip): if `prefetchSpineIndex == currentSpineIndex + 1` and
   `prefetchSection` is live, `section = std::move(prefetchSection);
   currentSpineIndex++;` (cheap handoff, no reopen); otherwise fall back to
   today's `section.reset()` + lazy reopen — which is a cache hit against
   whatever the prefetch already wrote/suspended, not a cold rebuild.
5. No changes needed to progress/TOC/bookmark logic
   (`calculateProgress`, `getTocIndexForSpineIndex`,
   `bookmarkMatchesProgress`) — confirmed they only read `currentSpineIndex`,
   which the new members never touch until promotion.
6. No new suspend/resume code — `~Section()` already calls `suspendBuild()`
   unconditionally, keyed by `spineIndex`, so a torn-down `prefetchSection`
   (activity exit mid-prefetch) persists as an ordinary partial file that
   the next natural visit to that spine picks up via the existing
   `loadSectionFile()`/`isPartial()` path.

**Done when**: `pio run -e default` builds clean; reading through a chapter
boundary shows the promoted section's first page rendering instantly (no
build popup) when the prefetch had time to finish; reading through a run of
several tiny (1-page) spine items still maintains roughly constant
lookahead rather than resetting to 0 at each one; peak heap during normal
reading (checked via the same `ESP.getFreeHeap()`/`ESP.getMaxAllocHeap()`
spot-check as Phase 1) does not regress versus `personal-integration`
baseline; exiting/reopening mid-prefetch resumes correctly, verified by
killing the reader activity while a prefetch is mid-build and confirming
the next open of that chapter shows the partial's pages instantly.

### Sequencing note

Phase 3 explicitly depends on Phase 1 (a prefetch build's LUT is bounded
"for free," satisfying the memory-quantification finding's prerequisite
without extra work in Phase 3) and Phase 2 (a prefetch build's HTML-open
step can't stall the shared render/input task otherwise). Phase 1 has
standalone value even if Phases 2-3 are deferred or reworked later — it
fixes a real unbounded-growth gap in every build today, not just future
prefetch builds.

## Pre-existing bug found while stress-testing Phase 1 (2026-08-06, not caused by this branch)

Built a synthetic multi-chapter EPUB with a ~124KB single chapter (see
`docs/notes/make_test_epub.py`) and drove it via the wasm
simulator, deliberately paced (`wait_stable` after every single page-forward
press, so no rendering is ever mid-flight -- rules out ordinary input-queue
races). Reproducibly, **on both this branch's build and an unmodified
`area-epub-layout-polish` build**, page-turning into that chapter fails at
the exact same page (~30) and the exact same byte offset (`fileOffset=58297`
in both builds) with `[ERR] [PGE] Deserialization failed: Unknown tag 0` →
`[ERR] [ERS] Failed to load page from SD - clearing section cache`, and the
chapter rebuilds from scratch. Confirmed via a temporary debug-logging pass
(reverted, not part of this branch's diff) that `getLutEntry`'s RAM-window
lookup was returning the correct, expected `PageLutEntry` for the failing
page — the corruption is downstream of the LUT (either in `file`'s actual
byte content at that offset, or in a race on the shared `file` handle
between the render task and the background-tick task, since both write/seek
it without a full mutual-exclusion lock around the whole read-or-write
sequence — see `RenderLock::peek()`'s non-blocking check-then-act pattern
noted in the scheduling/responsiveness investigation earlier in this doc).

**This is not a regression from Phase 1** — same page, same byte offset,
same symptom on the unmodified base branch. It's a real, pre-existing
latent bug, apparently rare enough that normal rapid/human-paced reading
doesn't trigger it (an earlier rapid-fire 190-page stress test on the
unmodified build did NOT reproduce it), but reliably triggered by fully-
settled single-page-turn pacing deep into a large (~30+ page) chapter. Given
it reproduces byte-for-byte identically regardless of my LUT changes, root-
causing and fixing it is out of scope for this branch — flagging here so
whoever investigates next doesn't have to rediscover the repro. Test fixture
generator (`docs/notes/make_test_epub.py`) and the exact repro steps (open the
book, then press-and-fully-settle "right" ~30-40 times into the largest
chapter) are reusable for whoever picks this up.

## Eviction path validated (2026-08-06), after fixing an unrelated simulator bug

The bug above (interactive page-by-page navigation) blocks reaching page 65+
through normal reading before eviction ever triggers, so it couldn't be used
to validate the actual point of Phase 1. Percent-jump navigation ("Go to %"
in the reader menu) sidesteps it — it forces one large synchronous build via
`buildSomeMore()` with no per-tick time limit, in `render()`, rather than many
discrete incremental ticks — different code path, doesn't hit the bug above.

Using that, jumping to 50% of the test book (landing deep in the 124KB
chapter) hit a **second, different** failure: `[ERR] [SCT] Failed to write
LUT due to invalid page positions` during `commitBuildFile`, i.e. inside the
actual eviction/spill-file-read path this branch adds. Root-caused via
temporary instrumentation (not part of this branch's diff) to
`crosspoint-simulator/src/HalStorage.cpp`'s `openFileForWrite`, which opened
files `O_WRONLY | O_CREAT | O_TRUNC` — write-only. `seek()`/`size()`/
`position()` (all `lseek`-only) kept reporting correct values, masking it,
but `read()` after seeking back into already-written data returned `-1`
every time. The real ESP32 implementation
(`freeink-sdk/libs/hardware/SDCardManager/src/SDCardManager.cpp:307`) opens
`O_RDWR`, matching this codebase's established pattern of using
`openFileForWrite` for files that are later read back during the same build
(e.g. `Section::file` itself, via `loadPageDuringBuild`'s seek-read-restore —
which happened to keep working here, apparently because MEMFS is lenient
about *some* read patterns on an O_WRONLY fd but not this one). Fixed the
simulator (`O_WRONLY` → `O_RDWR`, one line, matches the doc comment there
about avoiding "write-only seek restrictions") — **not a bug in this
branch's code**, and not present on real hardware.

With that fixed, the same 50% jump completed cleanly: chapter 2 built to
**238 pages** (comfortably past `LUT_RAM_WINDOW_PAGES = 64`, meaning ~174
pages were evicted from the RAM window and read back from the spill file
during `commitBuildFile`'s four columnar passes), zero errors, heap flat
throughout (`used` unchanged across the whole build), landed exactly at
"Chapter 2: The Long Middle, 121/238, 50%" with correct rendered content.
Exiting and reopening re-read the finalized 238-page file purely from disk
("Deserialization succeeded: 238 pages", "Cache found, skipping build...",
correct restored position) — confirms `commitBuildFile`'s rewritten
columnar-array output is byte-correct even when most of its source data came
from spill-file reads, not the RAM window. This is the validation the LUT
work was actually for.

**Net status**: Phase 1's own logic is validated end-to-end, including the
one path (eviction) the earlier, smaller demo-book test couldn't reach. The
still-open, unrelated pre-existing bug (previous section) remains
unresolved and un-investigated beyond confirming it isn't this branch's
fault.
