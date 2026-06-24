#pragma once
#include <HalStorage.h>

#include <memory>
#include <string>

#include "Block.h"

class ImageBlock final : public Block {
 public:
  ImageBlock(const std::string& imagePath, int16_t width, int16_t height);
  ~ImageBlock() override = default;

  const std::string& getImagePath() const { return imagePath; }
  int16_t getWidth() const { return width; }
  int16_t getHeight() const { return height; }

  bool imageExists() const;

  BlockType getType() override { return IMAGE_BLOCK; }
  bool isEmpty() override { return false; }

  void render(GfxRenderer& renderer, const int x, const int y);

  // Decode + write the .pxc cache for this image WITHOUT needing a strip/page render.
  // If a cache file already exists, returns true immediately (no work). The decode is
  // cooperatively cancellable via cancelFn/cancelCtx (threaded into RenderConfig); a
  // cancelled decode leaves a partial .pxc that the normal render path re-decodes.
  // x,y must be the image's on-page render position (same coords ImageBlock::render gets).
  bool warmCache(GfxRenderer& renderer, int x, int y, bool (*cancelFn)(void* ctx) = nullptr,
                 void* cancelCtx = nullptr);

  bool serialize(HalFile& file);
  static std::unique_ptr<ImageBlock> deserialize(HalFile& file);

 private:
  std::string imagePath;
  int16_t width;
  int16_t height;
};
