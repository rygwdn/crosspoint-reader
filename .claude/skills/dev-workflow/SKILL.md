---
name: dev-workflow
description: Deciding where a change should be made (which numbered area-* worktree, or whether to create a new one), testing it in the wasm simulator before hardware, and merging into personal-integration for an on-device build. Use whenever starting new work, unsure which branch/worktree to commit to, about to test in the simulator, or about to build/flash firmware for the device.
---

# Worktree-per-branch dev workflow

The user's active integration branch is `personal-integration`, built by
resetting to `upstream/develop` and merging a fixed set of local `area-*`
topic branches (see the `crosspoint-personal-integration` memory for the
current merge order and rebuild procedure). This skill covers the day-to-day
mechanics: where a change is made, how it's proven in the simulator, and how
it gets to the device.

## One worktree per area-* branch, numbered by merge order

Every local `area-*` topic branch has exactly one worktree, at
`~/crosspoint-reader-worktrees/NN-area-<name>`, where `NN` is a two-digit
prefix reflecting the order that branch merges into `personal-integration`
(lower number = merges earlier). The main tree (`~/crosspoint-reader`) is
reserved for `personal-integration` itself — it is the merge target and the
place on-device builds happen, not a place to do topic-branch work.

**Starting new work**: figure out which existing `area-*` branch the change's
theme fits (not `personal-integration` directly — that branch gets discarded
and rebuilt, not incrementally maintained). If none fits, create a new
`area-*` branch and worktree:

```bash
cd ~/crosspoint-reader
git branch area-<name> upstream/develop   # or off whichever branch it should build on
git worktree add ../crosspoint-reader-worktrees/NN-area-<name> area-<name>
git -C ../crosspoint-reader-worktrees/NN-area-<name> submodule update --init --recursive
```

Pick `NN` as one past the highest existing number unless you have a specific
reason to expect it to merge earlier (in which case renumber — see below).
Gaps are fine; two-digit padding just needs to sort correctly in `ls`.

**Renumbering** (when merge-order expectations change): rename the worktree
directory to its new `NN` prefix. `git worktree move` will refuse with
`fatal: working trees containing submodules cannot be moved or removed` for
any worktree with `freeink-sdk` checked out (which is all of them) — don't
fight it, do it manually instead:

```bash
mv ../crosspoint-reader-worktrees/OLD-area-<name> ../crosspoint-reader-worktrees/NEW-area-<name>
git worktree repair ../crosspoint-reader-worktrees/NEW-area-<name>
git -C ../crosspoint-reader-worktrees/NEW-area-<name> submodule update --init --recursive
```

A same-parent-directory rename keeps the submodule's internal relative gitdir
links valid; `git worktree repair` fixes the worktree's own admin file
(`.git/worktrees/<name>/gitdir`), and the trailing `submodule update --init`
is just cheap insurance. Verify with `git -C <path> status --short` in both
the worktree and its `freeink-sdk` submodule afterward — should be clean.

## Test in the wasm simulator first

Before device testing, validate in `~/crosspoint-simulator/wasm/` against the
worktree you're changing (full build/drive procedure in the
`crosspoint-wasm-simulator-testing` memory). Point `CROSSPOINT_FIRMWARE_DIR`
at the specific numbered worktree, not the main tree — the main tree tracks
`personal-integration`, which may be mid-rebuild or behind your change.

**Concurrent agents / multiple in-flight sim builds**: `wasm/build/`'s
CMakeCache pins one firmware checkout at a time, and `serve.py`/
`agent_drive.py` both hardcode the directory name `build`. If more than one
worktree needs simulator testing around the same time, give each its own
build dir — `wasm/build-NN-area-<name>/` — configured against that worktree's
path, and only rename to `wasm/build` (swapping any existing `build` aside
first) for the duration of running `serve.py`/`agent_drive.py`, then swap
back. Confirm the swap-back with `git status --short` in
`~/crosspoint-simulator` (untracked build dirs aren't tracked, so drift there
means a leftover swap).

## Merging to personal-integration and building for device

Once a change is validated in the simulator and ready for hardware testing:

1. **Commit it in its `area-*` worktree first.** Nothing untested/uncommitted
   should cross into `personal-integration`.
2. **In the main tree** (`~/crosspoint-reader`, on `personal-integration`),
   merge the branch: `git merge area-<name>`. If this is a full rebuild
   rather than an incremental merge, follow the reset+re-merge-all procedure
   in the `crosspoint-personal-integration` memory instead.
3. **Build for device in the main tree**: `pio run -e default` (per
   `README.md`'s pre-PR checks). A rebase/merge reporting clean is not proof
   it compiles — always build before trusting the result.
4. **Upload/flash**: use the `device-network` skill to push the built `.bin`
   to the device over WiFi, or upload via USB per the normal PlatformIO flow.

Do not build device firmware from an `area-*` worktree directly — the device
build should always come from the integration branch in the main tree, so
what's flashed matches what's actually merged.
