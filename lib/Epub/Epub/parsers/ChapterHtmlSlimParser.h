#pragma once

#include <HalStorage.h>
#include <expat.h>

#include <climits>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

inline uint64_t arxHash64(const char* s) {
  uint64_t hash = 0xcbf29ce484222325ULL;
  while (char c = *s++) {
    hash ^= static_cast<uint8_t>(c);
    hash = (hash << 19) | (hash >> 45);
    hash += 0x9e3779b97f4a7c15ULL;
  }
  return hash;
}

inline uint64_t arxHash64(const std::string& s) { return arxHash64(s.c_str()); }

#include "Epub/FootnoteEntry.h"
#include "Epub/ParsedText.h"
#include "Epub/blocks/ImageBlock.h"
#include "Epub/blocks/TextBlock.h"
#include "Epub/css/CssParser.h"
#include "Epub/css/CssStyle.h"

class Page;
class GfxRenderer;
class Epub;

#define MAX_WORD_SIZE 200

class ChapterHtmlSlimParser {
  std::shared_ptr<Epub> epub;
  const std::string& filepath;
  GfxRenderer& renderer;
  std::function<void(std::unique_ptr<Page>, uint16_t, uint16_t)> completePageFn;
  std::function<void()> popupFn;  // Popup callback
  int depth = 0;
  int skipUntilDepth = INT_MAX;
  int boldUntilDepth = INT_MAX;
  int italicUntilDepth = INT_MAX;
  int underlineUntilDepth = INT_MAX;
  // buffer for building up words from characters, will auto break if longer than this
  // leave one char at end for null pointer
  char partWordBuffer[MAX_WORD_SIZE + 1] = {};
  int partWordBufferIndex = 0;
  bool nextWordContinues = false;  // true when next flushed word attaches to previous (inline element boundary)
  std::unique_ptr<ParsedText> currentTextBlock = nullptr;
  std::unique_ptr<Page> currentPage = nullptr;
  int16_t currentPageNextY = 0;
  int fontId;
  float lineCompression;
  bool extraParagraphSpacing;
  uint8_t paragraphAlignment;
  uint16_t viewportWidth;
  uint16_t viewportHeight;
  bool hyphenationEnabled;
  bool focusReadingEnabled;
  const CssParser* cssParser;
  bool embeddedStyle;
  uint8_t imageRendering;
  std::string contentBase;
  std::string imageBasePath;
  int imageCounter = 0;

  // Style tracking (replaces depth-based approach)
  struct StyleStackEntry {
    int depth = 0;
    bool hasBold = false, bold = false;
    bool hasItalic = false, italic = false;
    bool hasUnderline = false, underline = false;
    bool hasDirection = false;
    CssTextDirection direction = CssTextDirection::Ltr;
    bool hasSup = false, sup = false;
    bool hasSub = false, sub = false;
  };
  std::vector<StyleStackEntry> inlineStyleStack;
  std::vector<BlockStyle> blockStyleStack;  // accumulated block styles from open ancestor elements
  CssStyle currentCssStyle;
  bool effectiveBold = false;
  bool effectiveItalic = false;
  bool effectiveUnderline = false;
  bool effectiveDirectionDefined = false;
  CssTextDirection effectiveDirection = CssTextDirection::Ltr;
  bool effectiveSup = false;
  bool effectiveSub = false;
  int tableDepth = 0;
  int tableRowIndex = 0;
  int tableColIndex = 0;

  // Anchor-to-page mapping: tracks which page each HTML id attribute lands on.
  int completedPageCount = 0;
  std::vector<std::pair<uint64_t, uint16_t>> anchorData;
  std::optional<uint64_t> pendingAnchorId;  // deferred until after previous text block is flushed
  std::vector<uint64_t> tocAnchors;         // the list of anchors that are TOC chapter boundaries
  uint16_t xpathParagraphIndex = 0;
  uint16_t xpathListItemIndex = 0;

  // <pre> whitespace-preservation tracking
  int preDepth = INT_MAX;          // depth at which <pre> was entered (INT_MAX = not in pre)
  bool preLineHasContent = false;  // true after first non-whitespace char in current pre line
  bool pendingListBullet = false;  // bullet deferred until first word of <li> to avoid bullet-only blocks

  // Widow prevention: hold the most recently filled page so makePages() can rescue
  // its last line when a paragraph leaves only one line on the following page (a widow).
  std::unique_ptr<Page> widowPendingPage;
  uint16_t widowPendingParagraphIndex = 0;
  uint16_t widowPendingListItemIndex = 0;

  // Footnote link tracking
  bool insideFootnoteLink = false;
  int footnoteLinkDepth = -1;
  FootnoteEntry currentFootnote = {};
  int currentFootnoteLinkTextLen = 0;
  std::vector<std::pair<int, FootnoteEntry>> pendingFootnotes;  // <wordIndex, entry>
  int wordsExtractedInBlock = 0;

  // Resumable parse state. The one-shot parseAndBuildPages() drives these
  // internally; the incremental section builder drives them across render ticks
  // so a large single chapter can yield between pages instead of blocking the UI
  // until the whole thing is laid out. parseFile_ and the expat parser stay alive
  // for the lifetime of the parse so it can be paused and resumed at buffer
  // boundaries.
  XML_Parser xmlParser_ = nullptr;
  HalFile parseFile_;
  uint32_t parseStartTime_ = 0;

  void updateEffectiveInlineStyle();
  void startNewTextBlock(const BlockStyle& blockStyle);
  void flushPendingAnchor();
  void flushPartWordBuffer();
  void makePages();
  void commitPendingPage();
  static void applyDirectionToEntry(StyleStackEntry& entry, const CssStyle& css);
  void emitHorizontalRule(const BlockStyle& blockStyle);
  // XML callbacks
  static void XMLCALL startElement(void* userData, const XML_Char* name, const XML_Char** atts);
  static void XMLCALL characterData(void* userData, const XML_Char* s, int len);
  static void XMLCALL defaultHandlerExpand(void* userData, const XML_Char* s, int len);
  static void XMLCALL endElement(void* userData, const XML_Char* name);

 public:
  explicit ChapterHtmlSlimParser(std::shared_ptr<Epub> epub, const std::string& filepath, GfxRenderer& renderer,
                                 const int fontId, const float lineCompression, const bool extraParagraphSpacing,
                                 const uint8_t paragraphAlignment, const uint16_t viewportWidth,
                                 const uint16_t viewportHeight, const bool hyphenationEnabled,
                                 const bool focusReadingEnabled,
                                 const std::function<void(std::unique_ptr<Page>, uint16_t, uint16_t)>& completePageFn,
                                 const bool embeddedStyle, const std::string& contentBase,
                                 const std::string& imageBasePath, const uint8_t imageRendering = 0,
                                 std::vector<uint64_t> tocAnchors = {}, const std::function<void()>& popupFn = nullptr,
                                 const CssParser* cssParser = nullptr)

      : epub(epub),
        filepath(filepath),
        renderer(renderer),
        fontId(fontId),
        lineCompression(lineCompression),
        extraParagraphSpacing(extraParagraphSpacing),
        paragraphAlignment(paragraphAlignment),
        viewportWidth(viewportWidth),
        viewportHeight(viewportHeight),
        hyphenationEnabled(hyphenationEnabled),
        focusReadingEnabled(focusReadingEnabled),
        completePageFn(completePageFn),
        popupFn(popupFn),
        cssParser(cssParser),
        embeddedStyle(embeddedStyle),
        imageRendering(imageRendering),
        contentBase(contentBase),
        imageBasePath(imageBasePath),
        tocAnchors(std::move(tocAnchors)) {}

  ~ChapterHtmlSlimParser();

  // One-shot parse: builds every page before returning (begin + step* + finish).
  bool parseAndBuildPages();

  // Resumable parse, for the incremental section builder. Drive as:
  //   if (!beginParse()) fail;
  //   loop: switch (parseStep(useSmallChunks)) { More: keep going / yield; Done: finishParse(); Error: abortParse(); }
  // Pages are emitted via completePageFn as they complete during parseStep(), so
  // the caller can stop once enough pages are built and resume on a later tick.
  // useSmallChunks: feed Expat a much smaller read chunk (see SMALL_PARSE_BUFFER_SIZE in the
  // .cpp) so a single call can't advance past more than roughly one <img> tag. The normal
  // PARSE_BUFFER_SIZE chunk is prose-sized and can contain a run of several image tags back to
  // back; each one does a synchronous SD extract + dimension read that a page-count/time budget
  // checked only *between* parseStep() calls can't see until the whole chunk is done. Pass true
  // from a time-budgeted caller (the background build tick) so the caller's own budget check
  // -- run after every parseStep() return -- gets a chance to yield roughly every image instead
  // of every ~1KB of markup. The render-critical path (building the page about to be shown) has
  // no time budget and passes false to parse at full chunk size as before.
  enum class ParseStatus { More, Done, Error };
  bool beginParse();
  ParseStatus parseStep(bool useSmallChunks);
  bool finishParse();  // flush the trailing page and tear down; returns true
  void abortParse();   // tear down without flushing (error / abandon)

  void addLineToPage(std::shared_ptr<TextBlock> line);
  const std::vector<std::pair<uint64_t, uint16_t>>& getAnchors() const { return anchorData; }

  // Byte progress of the in-flight parse, used to estimate a still-building section's total page
  // count (a giant single-spine book never fully lays out, so its real count is unknown). Valid
  // between beginParse() and finishParse()/abortParse().
  size_t parseBytesConsumed() { return parseFile_ ? parseFile_.position() : 0; }
  size_t parseTotalBytes() { return parseFile_ ? parseFile_.size() : 0; }
};
