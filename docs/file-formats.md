# File Formats

These formats describe the SD-card cache files under `/.crosspoint/epub_<hash>/`.
All POD fields are written in the ESP32 little-endian representation used by
`Serialization.h`; strings are length-prefixed UTF-8.

See [XTC / XTCBZ (manga/comic book files)](#xtc--xtcbz-mangacomic-book-files) below
for the pre-rendered-bitmap book formats (`.xtc`/`.xtch`/`.xtcbz`/`.xtcbzh`), which
live alongside books rather than in the cache dir and are read/written by
`lib/Xtc`.

## `book.bin`

### Version 10

`book.bin` stores EPUB metadata plus lookup tables for spine and TOC entries.
The current firmware writes this version from `BookMetadataCache`.

ImHex pattern:

```c++
import std.mem;
import std.string;
import std.core;

#define EXPECTED_VERSION 10
#define MAX_STRING_LENGTH 65535

struct String {
    u32 length [[hidden, comment("String byte length")]];
    if (length > MAX_STRING_LENGTH) {
        std::warning(std::format("Unusually large string length: {} bytes", length));
    }
    char data[length] [[comment("UTF-8 string data")]];
} [[sealed, format("format_string"), comment("Length-prefixed UTF-8 string")]];

fn format_string(String s) {
    return s.data;
};

struct Metadata {
    String title [[comment("Book title")]];
    String author [[comment("Book author")]];
    String language [[comment("Book language code")]];
    String coverItemHref [[comment("Path to cover image")]];
    String textReferenceHref [[comment("Path to guided first text reference")]];
};

struct SpineEntry {
    String href [[comment("Resource path")]];
    u32 cumulativeSize [[comment("Cumulative uncompressed spine size through this entry")]];
    s16 tocIndex [[comment("Index into TOC, or inherited/previous TOC index when no direct entry exists")]];
};

struct TocEntry {
    String title [[comment("Chapter/section title")]];
    String href [[comment("Resource path")]];
    String anchor [[comment("Fragment identifier")]];
    u8 level [[comment("Nesting level")]];
    s16 spineIndex [[comment("Index into spine (-1 if none)")]];
};

struct BookBin {
    u8 version;
    if (version != EXPECTED_VERSION) {
        std::error(std::format("Unsupported version: {} (expected {})", version, EXPECTED_VERSION));
    }

    u32 lutOffset [[comment("Offset to lookup tables")]];
    u16 spineCount;
    u16 tocCount;

    Metadata metadata;

    u32 currentOffset = $;
    if (currentOffset != lutOffset) {
        std::warning(std::format("LUT offset mismatch: expected 0x{:X}, got 0x{:X}", lutOffset, currentOffset));
    }

    u32 spineLut[spineCount] [[comment("Spine entry offsets")]];
    u32 tocLut[tocCount] [[comment("TOC entry offsets")]];

    SpineEntry spines[spineCount];
    TocEntry toc[tocCount];
};

BookBin book @ 0x00;

u32 fileSize = std::mem::size();
u32 parsedSize = $;
if (parsedSize != fileSize) {
    std::warning(std::format("Unparsed data detected: {} bytes remaining at offset 0x{:X}", fileSize - parsedSize, parsedSize));
}
```

## `section.bin`

### Version 41

Each file in `sections/*.bin` stores one laid-out spine section. The header is
also the cache-busting key: if any layout-affecting setting differs from the
current reader settings, the section is discarded and rebuilt.

Version 41 keeps the version 40 serialized layout unchanged. It was bumped
because simple HTML table rows are now laid out as positioned columns rather
than flattened paragraphs with synthetic row/cell labels.

Version 40 keeps the version 39 serialized layout unchanged. It was bumped
because ruby groups now remain intact when large text blocks are soft-flushed.

Version 39 keeps the version 38 serialized layout unchanged. It was bumped
because image top margins are now clamped to keep full-height images within the
page viewport.

Version 38 keeps the version 37 serialized layout unchanged. It was bumped
because Focus Reading now permits line breaks at visible hyphens and dashes
and hyphenates focus-split words as a whole, changing cached page layout.

Version 37 increases the fixed-size footnote href field from 96 to 256 bytes.
This changes each serialized footnote record from 128 to 288 bytes, so older
section caches must be discarded and rebuilt.

Version 36 keeps the version 35 serialized layout unchanged. It was bumped
because ruby and justified text positioning and CJK line breaking now use
corrected word measurements, so version 35 cached page layouts no longer match.

Version 35 adds a header offset and a `uint32_t` entry per page for the
visible-text offset LUT. The other section LUTs remain unchanged.

Version 34 is binary-identical to version 33. The version was bumped because
word-gap suppression was narrowed to tokens glued together in the source: v33
dropped the gap between any two words meeting at a CJK break opportunity, which
collapsed the spaces between Hangul words, so v33 word positions no longer match
what the layout engine now produces.

Version 30 is binary-identical to version 29. The version was bumped because
Arabic contextual shaping changed text measurement (`getTextAdvanceX` now
measures the shaped visual text), so word positions cached by v29 no longer
match what `drawText` renders.

Version 28 introduced serialized word style bits for underline, strikethrough,
superscript, and subscript. The format also includes:

- cache-busting fields for paragraph alignment, hyphenation, embedded CSS,
  image rendering mode, and Focus Reading
- page offset LUT
- per-page visible-text offset LUT (zero-based Unicode codepoints in `<body>`)
- anchor-to-page map for fragment and footnote navigation
- paragraph and list-item LUTs retained for navigation and legacy sync fallback
- optional per-word Focus Reading split metadata
- per-page footnote entries
- serialized word style bits for underline, strikethrough, superscript, and
  subscript
- flat TextBlock word storage (v29): per-word arrays plus one shared
  NUL-terminated text blob, replacing v28's length-prefixed word strings. The
  on-disk order mirrors the in-RAM arena so the firmware reads a whole block
  payload with a single allocation and a single SD read

ImHex pattern:

```c++
import std.mem;
import std.string;
import std.core;

#define EXPECTED_VERSION 41
#define MAX_STRING_LENGTH 65535
#define FOOTNOTE_NUMBER_LEN 32
#define FOOTNOTE_HREF_LEN 256

struct String {
    u32 length [[hidden, comment("String byte length")]];
    if (length > MAX_STRING_LENGTH) {
        std::warning(std::format("Unusually large string length: {} bytes", length));
    }
    char data[length] [[comment("UTF-8 string data")]];
} [[sealed, format("format_string"), comment("Length-prefixed UTF-8 string")]];

fn format_string(String s) {
    return s.data;
};

enum PageElementTag : u8 {
    TAG_PageLine = 1,
    TAG_PageImage = 2,
    TAG_PageHorizontalRule = 3
};

enum WordStyle : u8 {
    REGULAR = 0,
    BOLD = 1,
    ITALIC = 2,
    BOLD_ITALIC = 3,
    UNDERLINE = 4,
    STRIKETHROUGH = 8,
    SUP = 16,
    SUB = 32
};

enum TextAlign : u8 {
    JUSTIFIED = 0,
    LEFT_ALIGN = 1,
    CENTER_ALIGN = 2,
    RIGHT_ALIGN = 3,
    NONE = 4
};

struct BlockStyle {
    TextAlign alignment;
    bool textAlignDefined;
    s16 marginTop;
    s16 marginBottom;
    s16 marginLeft;
    s16 marginRight;
    s16 paddingTop;
    s16 paddingBottom;
    s16 paddingLeft;
    s16 paddingRight;
    s16 textIndent;
    bool textIndentDefined;
    bool isRtl;
    bool directionDefined;
};

struct TextBlock {
    u16 wordCount;
    u8 hasFocus;
    u16 textBytes [[comment("Total size of text[], including one NUL per word")]];

    if (wordCount > 0) {
        u16 textOff[wordCount] [[comment("Byte offset of word i's text within text[]")]];
        s16 wordXPos[wordCount];
        if (hasFocus != 0) {
            u16 wordFocusSuffixX[wordCount] [[comment("Suffix x offset from word start")]];
        }
        WordStyle wordStyle[wordCount];
        if (hasFocus != 0) {
            u8 wordFocusBoundary[wordCount] [[comment("UTF-8 byte boundary between bold prefix and suffix")]];
        }
        char text[textBytes] [[comment("All words back to back, each NUL-terminated")]];
    }

    BlockStyle blockStyle;
};

struct ImageBlock {
    String imagePath;
    String srcPath;
    s16 width;
    s16 height;
};

struct PageLine {
    s16 xPos;
    s16 yPos;
    TextBlock block;
};

struct PageImage {
    s16 xPos;
    s16 yPos;
    ImageBlock image;
};

struct PageHorizontalRule {
    s16 xPos;
    s16 yPos;
    u16 width;
    u8 thickness;
};

struct PageElement {
    PageElementTag pageElementType;
    if (pageElementType == TAG_PageLine) {
        PageLine pageLine [[inline]];
    } else if (pageElementType == TAG_PageImage) {
        PageImage pageImage [[inline]];
    } else if (pageElementType == TAG_PageHorizontalRule) {
        PageHorizontalRule horizontalRule [[inline]];
    } else {
        std::error(std::format("Unknown page element type: {}", pageElementType));
    }
};

struct FootnoteEntry {
    char number[FOOTNOTE_NUMBER_LEN];
    char href[FOOTNOTE_HREF_LEN];
};

struct Page {
    u16 elementCount;
    PageElement elements[elementCount] [[inline]];

    u16 footnoteCount;
    FootnoteEntry footnotes[footnoteCount];
};

struct AnchorEntry {
    String anchor;
    u16 page;
};

struct AnchorMap {
    u16 count;
    AnchorEntry entries[count];
};

struct ParagraphLut {
    u16 count;
    u16 paragraphIndex[count];
};

struct SectionBin {
    u8 version;
    if (version != EXPECTED_VERSION) {
        std::error(std::format("Unsupported version: {} (expected {})", version, EXPECTED_VERSION));
    }

    s32 fontId;
    float lineCompression;
    bool extraParagraphSpacing;
    u8 paragraphAlignment;
    u16 viewportWidth;
    u16 viewportHeight;
    bool hyphenationEnabled;
    bool embeddedStyle;
    u8 imageRendering;
    bool focusReadingEnabled;

    u16 pageCount;
    u32 pageLutOffset;
    u32 anchorMapOffset;
    u32 paragraphLutOffset;
    u32 listItemLutOffset;
    u32 visibleTextLutOffset;

    Page pages[pageCount];

    u32 currentOffset = $;
    if (currentOffset != pageLutOffset) {
        std::warning(std::format("Page LUT offset mismatch: expected 0x{:X}, got 0x{:X}", pageLutOffset, currentOffset));
    }

    u32 pageLut[pageCount] [[comment("Page data offsets")]];

    if (anchorMapOffset != 0) {
        AnchorMap anchorMap @ anchorMapOffset;
    }

    if (paragraphLutOffset != 0) {
        ParagraphLut paragraphLut @ paragraphLutOffset;
    }

    if (listItemLutOffset != 0 && paragraphLutOffset != 0) {
        u16 listItemIndex[paragraphLut.count] @ listItemLutOffset;
    }

    if (visibleTextLutOffset != 0) {
	u32 visibleTextOffset[pageCount] @ visibleTextLutOffset;
    }
};

SectionBin section @ 0x00;

u32 fileSize = std::mem::size();
u32 parsedSize = $;
if (parsedSize != fileSize) {
    std::warning(std::format("Unparsed data detected: {} bytes remaining at offset 0x{:X}", fileSize - parsedSize, parsedSize));
}
```

## XTC / XTCBZ (manga/comic book files)

Unlike the cache formats above, these are book files (typically dropped
straight onto the SD card, e.g. by `cbz2xteink`), parsed by `lib/Xtc`
(`XtcParser`). Both store pre-rendered page bitmaps -- no on-device text
layout or image decoding is needed to read them.

**XTC / XTCH** (`.xtc` / `.xtch`) is a third-party format (based on the
EPUB2XTC converter by Rafal-P-Mazur) that this project reads and writes
faithfully but does not own -- `XtcHeader` must not be extended with
CrossPoint-only fields.

**XTCBZ / XTCBZH** (`.xtcbz` / `.xtcbzh`) is CrossPoint's own container, produced by
`cbz2xteink` for manga/comics. It reuses XTC's page data verbatim (same
`XtgPageHeader`, same XTG/XTH bitmap encoding) and is parsed by the same
`XtcParser`, but has a distinct magic/extension and a grown header, so a
generic XTC-only reader simply won't recognize it rather than misparsing it.
It adds two things XTC has no room for:

- **A subpage table** -- groups of consecutive pages that are all views of one
  source page (e.g. cbz2xteink emits `[full page, panel crop 1, panel crop
  2, ...]` per manga page). This lets the reader page-turn by manga page while
  still letting the user drill into individual crops (see
  `XtcReaderActivity::stepSubpage`/`stepSubpageGroup`). Distinct from, and
  independent of, real book chapters (`ChapterInfo`) -- a subpage-table book
  can also have real chapters, or none at all.
- **Per-page deflate compression** -- `XtgPageHeader::compression = 1` means
  the bitmap that follows the header is raw-deflate compressed
  (decompressed via `InflateStream`, the same tinfl-backed decompressor used
  for EPUB zip entries and PNG IDAT chunks). This field already existed in
  XTC's page header (reserved at 0), so it's technically readable from a
  plain XTC/XTCH container too, but this project's own XTC/XTCH writer never
  sets it -- compression is an XTCBZ/XTCBZH-only convention going forward.

Magic numbers (little-endian `uint32_t`, first 4 bytes of the file):

| Format | Magic bytes | Value        | Page data | Extension |
|--------|-------------|--------------|-----------|-----------|
| XTC    | `X T C \0`  | `0x00435458` | XTG (1-bit) | `.xtc`  |
| XTCH   | `X T C H`   | `0x48435458` | XTH (2-bit) | `.xtch` |
| XTCBZ  | `X T Z \0`  | `0x005A5458` | XTG (1-bit) | `.xtcbz`  |
| XTCBZH | `X T Z H`   | `0x485A5458` | XTH (2-bit) | `.xtcbzh` |

XTCBZ/XTCBZH's magic bytes spell "XTZ\0"/"XTZH", not the format's own name --
"XTC" is already taken by the plain format above, and "XTCBZ" itself doesn't
fit a 4-byte magic; 'Z' (from "cb-Z") avoids colliding with any existing
magic's third byte (C/G/H).

ImHex pattern (header + subpage/chapter tables; page data is described inline
below rather than as a separate pattern, since it's identical for all four
formats):

```c++
import std.mem;
import std.string;
import std.core;

struct XtcHeader {
    u32 magic [[comment("XTC_MAGIC/XTCH_MAGIC/XTCBZ_MAGIC/XTCBZH_MAGIC")]];
    u8 versionMajor;
    u8 versionMinor;
    u16 pageCount;
    u8 readDirection [[comment("0=L->R, 1=R->L, 2=unset")]];
    u8 hasMetadata;
    u8 hasThumbnails [[comment("parsed but unused -- covers/thumbs are generated on-device")]];
    u8 hasChapters;
    u32 currentPage [[comment("overridden by progress.bin once opened")]];
    u64 metadataOffset [[comment("parsed but not used for the seek -- title/author are always read at a fixed "
                                  "offset right after the header: 0x38/0xB8 for XtcHeader, 0x40/0xC0 for XtcbzHeader "
                                  "(8 bytes later, since XtcbzHeader is 8 bytes bigger)")]];
    u64 pageTableOffset;
    u64 dataOffset [[comment("first page's data offset")]];
    u64 thumbOffset [[comment("unused by the reader")]];
    u64 chapterOffset [[comment("0 if hasChapters==0; the trailing 4 bytes here were originally a separate `padding` field, "
                                 "but readChapters() always re-reads this as one 64-bit LE value")]];
} [[static]];

struct XtcbzHeader {
    XtcHeader base [[comment("identical layout/semantics to XtcHeader above, except metadata/title/author "
                              "offsets shift by the 8 extra bytes below -- see metadataOffset comment")]];
    u64 subpageTableOffset [[comment("0 if unused")]];
} [[static]];

bool isExtended = false;
u32 magic @ 0x00;
if (magic == 0x005A5458 || magic == 0x485A5458) {
    isExtended = true;
}

if (isExtended) {
    XtcbzHeader header @ 0x00;
} else {
    XtcHeader header @ 0x00;
}

struct ChapterEntry {
    char name[80] [[comment("null-terminated, UTF-8")]];
    u16 startPage [[comment("1-based; 0 is only valid as an all-zero terminator entry")]];
    u16 endPage [[comment("1-based, inclusive")]];
    padding[12];
} [[static]]; // 96 bytes

struct SubpageGroupEntry {
    u16 startPage [[comment("1-based")]];
    u16 endPage [[comment("1-based, inclusive")]];
} [[static]]; // 4 bytes -- XTCBZ/XTCBZH only

struct PageTableEntry {
    u64 dataOffset [[comment("absolute file offset")]];
    u32 dataSize [[comment("bytes of the page blob on disk (header + bitmap, compressed if applicable)")]];
    u16 width;
    u16 height;
} [[static]]; // 16 bytes

struct XtgPageHeader {
    u32 magic [[comment("XTG_MAGIC for 1-bit pages, XTH_MAGIC for 2-bit pages")]];
    u16 width;
    u16 height;
    u8 colorMode [[comment("0=monochrome")]];
    u8 compression [[comment("0=raw, 1=raw deflate (XTCBZ/XTCBZH convention)")]];
    u32 dataSize [[comment("bytes following this header, as stored on disk: the deflated size when "
                            "compression=1, otherwise the raw/uncompressed bitmap size")]];
    u64 md5 [[comment("unused by the reader")]];
    // Followed by `dataSize` bytes of bitmap data, which decompress (if compression=1) to:
    //   XTG (1-bit): row-major, ((width+7)/8)*height bytes, 8 px/byte MSB-first
    //   XTH (2-bit): two bit planes, column-major (right-to-left), 8 vertical px/byte,
    //                ((width*height+7)/8)*2 bytes total; pixelValue = (bit1<<1)|bit2
} [[static]]; // 22 bytes + dataSize
```

