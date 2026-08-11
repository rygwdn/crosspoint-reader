# Canonical version constants for the .cpfont binary format and font manifest.
#
# These are the single source of truth for the build tooling. The CI workflow
# (release-fonts.yml) and both Python scripts (fontconvert_sdcard.py,
# generate-font-manifest.py) read from here.
#
# The firmware C++ headers (SdCardFont.h, FontDownloadActivity.h) carry their
# own copies — those must be bumped manually when the firmware is updated to
# support a new version.

# .cpfont binary format version. Bump when the on-disk struct layout changes.
# v5: glyph bitmaps are content-deduplicated into one pool shared by all
# styles in the file, instead of each style carrying its own private bitmap
# section (see the CPFONT_VERSION comment in lib/EpdFont/SdCardFont.h).
CPFONT_VERSION = 5

# JSON manifest schema version. Bump when the manifest shape changes.
FONTS_MANIFEST_VERSION = 1
