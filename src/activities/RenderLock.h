#pragma once

class Activity;  // forward declaration

// RAII helper to lock rendering mutex for the duration of a scope.
class RenderLock {
  bool isLocked = false;

 public:
  explicit RenderLock();
  explicit RenderLock(Activity&);  // unused for now, but keep for compatibility
  RenderLock(const RenderLock&) = delete;
  RenderLock& operator=(const RenderLock&) = delete;
  ~RenderLock();
  void unlock();
  static bool peek();

  // Contention callback: fired synchronously, on the WAITING task's own call stack, the
  // moment a RenderLock construction finds the mutex already held -- before it falls back
  // to the normal blocking wait. Lets a long background operation that holds the lock in
  // short, resumable bursts (e.g. a background HTML unzip) learn "someone's waiting" the
  // instant it happens, instead of the waiter silently blocking for however much of the
  // holder's current burst is left.
  //
  // The callback runs on a DIFFERENT task than the holder (whichever task is trying to
  // construct a new RenderLock right now) -- it must be fast and must not touch the
  // holder's local state directly (that's a different task's stack/data, mid-use). The
  // correct shape is "flip a flag the holder checks at its own next safe checkpoint", the
  // same producer/consumer split used elsewhere in this codebase (e.g. HalGPIO's
  // onBusyWaitBegin/onBusyWaitCaptureSlice capturing button edges during a blocking e-ink
  // refresh for the main task to consume afterward).
  //
  // Only the current holder should have one registered (set right after acquiring, cleared
  // right before releasing) -- ContentionCallbackScope below does this via RAII so a call
  // site can't forget the clear() and leave a stale callback armed for an unrelated later
  // holder. At most one callback is live at a time: RenderLock wraps a single global mutex,
  // so at most one task ever holds it.
  static void setContentionCallback(void (*fn)(void* ctx), void* ctx);
  static void clearContentionCallback();

  // RAII wrapper for the above: register on construction, clear on destruction (or early
  // via end()). Construct this right after acquiring a RenderLock you intend to hold across
  // multiple short bursts of work.
  class ContentionCallbackScope {
   public:
    ContentionCallbackScope(void (*fn)(void* ctx), void* ctx) { RenderLock::setContentionCallback(fn, ctx); }
    ~ContentionCallbackScope() { end(); }
    void end() {
      if (!ended_) {
        RenderLock::clearContentionCallback();
        ended_ = true;
      }
    }
    ContentionCallbackScope(const ContentionCallbackScope&) = delete;
    ContentionCallbackScope& operator=(const ContentionCallbackScope&) = delete;

   private:
    bool ended_ = false;
  };
};
