#pragma once

#include <cstddef>
#include <cstdint>

// Forward declaration keeps miniz out of consumer translation units; the
// decompressor state is heap-allocated in the .cpp where the type is complete.
struct tinfl_decompressor_tag;

// Forward declaration keeps HalStorage out of consumer translation units that don't need
// save/restore (e.g. PNG decompression); only InflateStream.cpp needs the full type.
class HalFile;

// Streaming deflate decompressor wrapping miniz's tinfl.
//
// Replaces the uzlib-backed InflateReader on the throughput paths (EPUB zip
// entries, PNG IDAT). tinfl decodes via lookup tables where uzlib walks the
// Huffman tree bit-by-bit -- several times faster on this CPU -- at the cost
// of a larger decompressor state (~11KB, transient for the scope of the
// stream; taken from the lent framebuffer bytes via buildscratch::claim()
// when a FrameBufferLoan is active, heap otherwise). FontDecompressor
// intentionally stays on InflateReader:
// its one-shot flash-resident group decompressions are tiny, and the render
// path should not carry the extra state allocation.
//
// Two modes:
//   init(false) -- one-shot: the destination buffer holds the ENTIRE output,
//                  so back-references resolve inside it and no 32KB window is
//                  allocated. read()/readAtMost() must be driven with
//                  contiguous, forward-only slices of that one buffer
//                  (a single read(dest, totalSize) is the common case).
//   init(true)  -- streaming: allocates a 32KB window; output can go to any
//                  buffer in any-sized chunks across calls.
//
// Input is either a single contiguous buffer (setSource) or pulled on demand
// through a fill callback (setFill): return the number of bytes available and
// point *data at them (valid until the next fill call); return 0 at end of
// input. Call setZlibWrapped() before the first read when the stream has a
// zlib header (e.g. PNG IDAT).
class InflateStream {
 public:
  enum class Status {
    Ok,     // Output buffer full; more decompressed data remains.
    Done,   // Stream ended cleanly. produced may be < maxLen.
    Error,  // Corrupt/truncated stream, or decompression failed.
  };

  using FillFn = size_t (*)(void* ctx, const uint8_t** data);

  InflateStream() = default;
  ~InflateStream();
  InflateStream(const InflateStream&) = delete;
  InflateStream& operator=(const InflateStream&) = delete;

  // Allocate decompressor state (and the 32KB window when streaming) and reset
  // stream state. Reuses existing allocations on repeated calls. Returns false
  // on OOM.
  bool init(bool streaming);

  // Free the decompressor state and window.
  void deinit();

  // Provide the entire compressed input as one contiguous buffer.
  void setSource(const uint8_t* src, size_t len);

  // Provide compressed input on demand. ctx is passed back to fn verbatim.
  void setFill(FillFn fn, void* ctx);

  // Declare the input zlib-wrapped (2-byte header + trailing adler32).
  void setZlibWrapped() { zlibWrapped = true; }

  // Decompress exactly len bytes into dest. Returns false if the stream ends
  // or errors before producing len bytes.
  bool read(uint8_t* dest, size_t len);

  // Decompress up to maxLen bytes into dest; *produced gets the byte count.
  Status readAtMost(uint8_t* dest, size_t maxLen, size_t* produced);

  // Read-only view of the caller-provided fill-buffer bytes not yet consumed (the region
  // [inPtr, inPtr+inAvail)). Lets a caller that wants to saveState() retrieve exactly what to
  // pass as pendingInput/pendingInputLen without tracking its own copy of that bookkeeping.
  void getPendingInput(const uint8_t** ptr, size_t* len) const {
    *ptr = inPtr;
    *len = inAvail;
  }

  // Save/restore support for a resumable stream -- e.g. a background HTML unzip that must
  // give back its ~43KB decompressor state (borrowed from the framebuffer via
  // buildscratch::claim()) the instant a render needs that buffer. Only supported in
  // streaming mode (init(true)); the one-shot mode's output-buffer aliasing can't survive a
  // suspend since the destination buffer itself doesn't persist across the interruption.
  // Both write/read fixed-size raw blocks at f's current position -- not a portable format,
  // just this stream's own bookkeeping round-tripped through a scratch file.

  // Persists this stream's state to f. This object does not own the caller's fill-buffer, so
  // any bytes already pulled from the last fill() call but not yet consumed (the region
  // [inPtr, inPtr+inAvail)) must be passed in explicitly. Returns false on a write failure.
  bool saveState(HalFile& f, const uint8_t* pendingInput, size_t pendingInputLen) const;

  // Restores state written by saveState() into a stream already init(true)'d (init() resets
  // fill/inputExhausted/etc, so this must run before any read and before a fresh
  // setFill()/setSource() call, which it would otherwise overwrite). pendingInputBuf (at
  // least pendingInputBufCap bytes) receives the restored pending-input bytes;
  // *pendingInputLen receives the restored count. The caller must setFill() again afterward
  // (init() cleared it) so the stream can fetch more once the restored bytes drain. Returns
  // false -- leaving the stream unusable -- on a read failure or if the saved pending-input
  // length exceeds pendingInputBufCap (corrupt/mismatched spill file).
  bool restoreState(HalFile& f, uint8_t* pendingInputBuf, size_t pendingInputBufCap, size_t* pendingInputLen);

 private:
  tinfl_decompressor_tag* state = nullptr;  // ~11KB: heap, or inside the claimed build scratch
  uint8_t* window = nullptr;                // 32KB ring, streaming mode only
  uint8_t* arenaBase = nullptr;             // non-null when state/window live in lent framebuffer bytes
  size_t windowPos = 0;                     // ring write cursor
  // Decompressed-but-undelivered region of the window (tinfl can overshoot the
  // caller's requested length; the overshoot waits here for the next read).
  size_t pendingStart = 0;
  size_t pendingLen = 0;

  const uint8_t* inPtr = nullptr;
  size_t inAvail = 0;
  FillFn fill = nullptr;
  void* fillCtx = nullptr;
  bool inputExhausted = false;
  bool zlibWrapped = false;
  bool finished = false;

  // One-shot mode: tinfl needs the output buffer start for back-references.
  uint8_t* oneShotStart = nullptr;
};
