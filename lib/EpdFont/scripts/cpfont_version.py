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
# v5: interval entries widened 12->16 bytes to carry a per-interval uniform
# advance (0 = non-uniform), eliminating per-glyph advance reads for fixed-width
# ranges such as CJK.
CPFONT_VERSION = 5

# JSON manifest schema version. Bump when the manifest shape changes.
FONTS_MANIFEST_VERSION = 1
