#pragma once
#include <HalStorage.h>

#include <memory>
#include <string>

class GfxRenderer;

struct ImageDimensions {
  int16_t width;
  int16_t height;
};

struct RenderConfig {
  int x, y;
  int maxWidth, maxHeight;
  bool useGrayscale = true;
  bool useDithering = true;
  bool performanceMode = false;
  bool useExactDimensions = false;  // If true, use maxWidth/maxHeight as exact output size (no recalculation)
  std::string cachePath;            // If non-empty, decoder will write pixel cache to this path

  // When true the decode writes only the .pxc cache, never the framebuffer (so a
  // pre-warm can run without corrupting the display). Requires cachePath set.
  bool cacheOnly = false;

  // Optional cooperative-cancel hook for mid-decode abort. When cancelFn is null
  // (the default) the decode runs uninterrupted with zero added overhead. When
  // supplied, the decode callbacks periodically yield to the main task and call
  // cancelFn(cancelCtx); returning true aborts the decode, which keeps the
  // partial .pxc on disk so the reader re-decodes it later (a short read on the
  // truncated file triggers the full-decode fallback in renderFromCache).
  bool (*cancelFn)(void* ctx) = nullptr;  // return true to abort the decode
  void* cancelCtx = nullptr;
};

class ImageToFramebufferDecoder {
 public:
  virtual ~ImageToFramebufferDecoder() = default;

  virtual bool decodeToFramebuffer(const std::string& imagePath, GfxRenderer& renderer, const RenderConfig& config) = 0;

  virtual bool getDimensions(const std::string& imagePath, ImageDimensions& dims) const = 0;

  virtual const char* getFormatName() const = 0;

 protected:
  // Size validation helpers
  static constexpr int MAX_SOURCE_PIXELS = 3145728;  // 2048 * 1536

  bool validateImageDimensions(int width, int height, const std::string& format);
  void warnUnsupportedFeature(const std::string& feature, const std::string& imagePath);
};
