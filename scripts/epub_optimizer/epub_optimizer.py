# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pillow>=10.0",
#   "numpy>=1.26",
#   "lxml>=5.0",
#   "tinycss2>=1.2",
#   "cssselect2>=0.7",
# ]
# ///
"""Pure-Python port of CrossPoint's browser-side EPUB Optimizer.

Reference implementation: src/network/html/FilesPage.html (JS, runs client-side
in the File Transfer web UI). This module ports the deterministic, non-DOM
parts of that pipeline function-for-function (same file/line comments point
back at the JS source) so the two can be diffed and kept in sync:

  - Image pipeline (resize/rotate/split/auto-crop/grayscale/JPEG re-encode)
  - XHTML fixups (SVG-wrapped cover/images, split-image rewiring, defensive CSS)
  - OPF/NCX manifest fixups
  - "Simplify CSS Styling" cascade squash (bakes CSS into style="" attributes)

Known deviations from the browser version (see also each function's docstring):
  - JPEG bytes are not identical (libjpeg via Pillow vs. the browser's own
    encoder); dimensions, split counts and structural output are.
  - Image resampling uses Pillow's LANCZOS filter, not the browser's canvas
    smoothing algorithm; pixel values differ slightly, dimensions do not.
  - CSS squash resolves the cascade directly (matched declarations only, no
    inheritance walk needed -- see squash_css_to_classes docstring) instead of
    a real browser layout/cascade engine. Percentage margin/padding lengths
    can't be resolved without running layout and are dropped rather than
    baked in. `text-indent` percentages ARE baked (see
    `_parse_length_px`'s `percent_as_raw_number`): the browser's
    getComputedStyle never resolves a percentage text-indent to a used px
    value the way it does margin/padding, so the JS version's naive
    `parseFloat()` on that percentage string effectively treats the bare
    number as px, which this port reproduces exactly. Baked em values can
    differ from the browser by ~0.0001em (last decimal place) on rare
    occasions due to the browser's own subpixel layout rounding of
    getComputedStyle px values, which a pure arithmetic round-trip doesn't
    reproduce; visually a no-op on an 800x480 e-ink panel.
  - No manual per-image split-state picker; every image defaults to Normal
    (state 0) unless overridden via --split-state, matching the JS default
    before a user opens the image picker.

Usage:
    uv run epub_optimizer.py input.epub -o output.epub --device X4 --quality 85
    uv run epub_optimizer.py input.epub -o output.epub --auto-crop --no-css-squash
    uv run epub_optimizer.py --self-test
"""

from __future__ import annotations

import argparse
import dataclasses
import html.entities
import io
import math
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from lxml import etree
from PIL import Image

import tinycss2
import cssselect2

# ============================================================================
# Constants -- mirrors FilesPage.html:3302-3324, :5057-:5074, :3828
# ============================================================================

DEVICE_PROFILES = {
    "X4": (480, 800),
    "X3": (528, 792),
}
DEFAULT_DEVICE = "X4"

# Mirrors CrossPointSettings::SCREEN_MARGIN_MIN (src/CrossPointSettings.h) -- the
# smallest screen margin a user can configure. EpubReaderActivity always adds
# SETTINGS.screenMargin to every viewport edge (top/left/right unconditionally,
# bottom via max(screenMargin, statusBarHeight)) on top of the device bezel, so
# the on-device text/image container is *never* wider or taller than
# device_dim - 2*SCREEN_MARGIN_MIN_PX, regardless of the live margin setting.
# Used as a safe upper bound when pre-baking image pixel dimensions: images
# always fit inside this box at any margin the user could ever choose, since
# margin only ever grows from this minimum.
SCREEN_MARGIN_MIN_PX = 5

CROP_WHITE_THRESHOLD = 245
CROP_BACKGROUND_TOLERANCE = 28
CROP_BACKGROUND_MAX_SPREAD = 24
CROP_EDGE_SAMPLE_SIZE = 12
CROP_PADDING_PX = 8
MIN_CROP_SAVINGS_RATIO = 0.08
MIN_COLOR_CROP_SAVINGS_RATIO = 0.20
MIN_CROP_DIMENSION = 240

STATE_NORMAL, STATE_HSPLIT, STATE_VSPLIT, STATE_ROTATE = 0, 1, 2, 3

DEFENSIVE_STYLE = (
    '<style type="text/css">img,svg{max-width:100%;height:auto}'
    "body{overflow-wrap:break-word}table{max-width:100%;table-layout:fixed}"
    "pre,code{white-space:pre-wrap;word-wrap:break-word}*{box-sizing:border-box}</style>"
)

HEADER_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK_TAGS = {"p", "div", "li", "blockquote"}
BOLD_TAGS = {"b", "strong"}
ITALIC_TAGS = {"i", "em"}
UNDERLINE_TAGS = {"u", "ins"}
LINETHROUGH_TAGS = {"del", "s", "strike"}
SKIP_TAGS = {
    "html", "head", "body", "title", "meta", "link", "style", "script", "base",
    "table", "tr", "td", "th", "noscript", "br", "img", "image", "hr",
    "area", "col", "colgroup", "object", "embed", "iframe", "video", "audio",
    "source", "track",
}
DECORATION_PROPS = ["text-decoration", "text-decoration-line"]
LENGTH_PROPS = [
    "margin-top", "margin-bottom", "margin-left", "margin-right",
    "padding-top", "padding-bottom", "padding-left", "padding-right",
    "text-indent",
]

IMAGE_EXT_RE = re.compile(r"\.(png|gif|webp|bmp|jpeg)$", re.IGNORECASE)
ANY_IMAGE_EXT_RE = re.compile(r"\.(png|gif|webp|bmp|jpg|jpeg)$", re.IGNORECASE)
XHTML_EXT_RE = re.compile(r"\.(xhtml|html|htm)$", re.IGNORECASE)

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
OPF_NS = "http://www.idpf.org/2007/opf"

XML_PARSER = etree.XMLParser(resolve_entities=True, recover=False, no_network=True, huge_tree=True)
# recover=True repairs the same unescaped-amp/mismatched-tag class of malformed
# markup that the browser DOMParser would reject and the original JS handled
# via hand-rolled regex fallbacks (now removed). Used only as a second attempt
# after XML_PARSER fails in parse_xml_recover().
RECOVER_PARSER = etree.XMLParser(resolve_entities=True, recover=True, no_network=True, huge_tree=True)

# XML has only 5 predefined entities; real epub XHTML relies on the HTML
# named-entity set (&nbsp;, &mdash;, ...) being predefined too, which
# real browsers do for application/xhtml+xml but lxml's bare XML parser
# does not -- rewrite named entities to numeric ones before parsing so
# parses succeed the same way a browser's DOMParser would.
_XML_PREDEFINED = {"amp", "lt", "gt", "quot", "apos"}
_NAMED_ENTITY_RE = re.compile(r"&(#?\w+);")


def _numericize_named_entities(text: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name.startswith("#") or name in _XML_PREDEFINED:
            return m.group(0)
        codepoint = html.entities.html5.get(name + ";") or html.entities.html5.get(name)
        if codepoint is None:
            return m.group(0)
        return "".join(f"&#{ord(c)};" for c in codepoint)

    return _NAMED_ENTITY_RE.sub(repl, text)


def parse_xml(content: str) -> etree._Element:
    """Parse XHTML/XML text into an lxml tree, tolerating HTML named entities."""
    return etree.fromstring(_numericize_named_entities(content).encode("utf-8"), XML_PARSER)


def parse_xml_recover(content: str) -> etree._Element:
    """Recover-mode parse: repairs malformed markup into the best-effort tree
    lxml can build, rather than rejecting it. Use as a fallback after parse_xml
    raises XMLSyntaxError -- the repaired tree replaces the hand-rolled regex
    fallbacks the JS version needed because the browser DOMParser has no
    repair mode."""
    return etree.fromstring(_numericize_named_entities(content).encode("utf-8"), RECOVER_PARSER)


def local_name(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


# ============================================================================
# Small pure utilities -- mirrors FilesPage.html:3999-4046, :3841-3895
# ============================================================================


def js_round(x: float) -> int:
    """Math.round semantics for the non-negative values used throughout: half rounds up."""
    return math.floor(x + 0.5)


def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def decode_href(href: str) -> str:
    from urllib.parse import unquote

    try:
        return unquote(href)
    except Exception:
        return href


def resolve_path(base_path: str, href: str) -> str:
    """Port of resolvePath (FilesPage.html:3999-4011)."""
    if href.startswith("/"):
        return href[1:]
    href = re.sub(r"^\./", "", href)
    base_dir = base_path.rsplit("/", 1)[0] if "/" in base_path else ""
    base_parts = base_dir.split("/") if base_dir else []
    href_parts = href.split("/")
    while href_parts and href_parts[0] == "..":
        href_parts.pop(0)
        if base_parts:
            base_parts.pop()
    resolved = "/".join(base_parts + href_parts)
    return re.sub(r"/+", "/", resolved)


def is_internal_epub_link(href: Optional[str]) -> bool:
    if not href:
        return False
    return not re.match(r"^(https?:|mailto:|ftp:|tel:|javascript:)", href, re.IGNORECASE)


def safe_read_text(raw: bytes) -> str:
    """Port of safeReadText (FilesPage.html:3850-3877): BOM strip + encoding sniff."""
    offset = 3 if raw[:3] == b"\xef\xbb\xbf" else 0
    body = raw[offset:]
    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    ascii_peek = raw[offset : offset + 512].decode("ascii", errors="ignore")
    m = re.search(r'encoding=["\']([^"\']+)["\']', ascii_peek, re.IGNORECASE) or re.search(
        r'charset=["\']?([^"\'\s;]+)', ascii_peek, re.IGNORECASE
    )
    encoding = m.group(1).lower() if m else "windows-1252"
    try:
        return body.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return body.decode("iso-8859-1", errors="replace")


def find_opf_path(names: list[str], read: Callable[[str], bytes]) -> Optional[str]:
    """Port of findOPFPath (FilesPage.html:3883-3895)."""
    container_path = next((p for p in names if p.lower() == "meta-inf/container.xml"), None)
    if container_path:
        try:
            xml = safe_read_text(read(container_path))
            m = re.search(r'<rootfile[^>]+full-path=["\']([^"\']+)["\']', xml, re.IGNORECASE)
            if m and m.group(1) in names:
                return m.group(1)
        except Exception:
            pass
    return next((p for p in names if p.lower().endswith(".opf")), None)


def serialize_xml(tree: etree._Element, original_content: str) -> str:
    """Approximate port of safeSerialize (FilesPage.html:4017-4046): re-attach the
    original <?xml?> declaration lxml's serializer drops by default."""
    result = etree.tostring(tree, encoding="unicode", method="xml")
    if re.match(r"^\s*<\?xml\b", original_content) and not re.match(r"^\s*<\?xml\b", result):
        m = re.match(r"^\s*(<\?xml[^?]*\?>)", original_content)
        if m:
            result = m.group(1) + "\n" + result
    return result


# ============================================================================
# Options
# ============================================================================


@dataclass
class OptimizeOptions:
    device: str = DEFAULT_DEVICE
    jpeg_quality: int = 85
    grayscale: bool = True
    auto_crop: bool = False
    css_squash: bool = True
    css_aware_resize: bool = True
    handedness: str = "right"  # 'right' = CW, 'left' = CCW
    overlap_percent: int = 5
    split_states: dict[str, int] = field(default_factory=dict)  # image path -> 0..3
    protected_paths: set[str] = field(default_factory=set)  # never auto-cropped (cover etc.)

    @property
    def max_width(self) -> int:
        return DEVICE_PROFILES[self.device][0]

    @property
    def max_height(self) -> int:
        return DEVICE_PROFILES[self.device][1]

    @property
    def container_max_width(self) -> int:
        """Upper bound on the on-device text/image container width at *any*
        reachable screen-margin setting -- see SCREEN_MARGIN_MIN_PX. This is
        what CSS-aware resizing should target, since css-squash strips every
        <img>'s CSS box before the firmware ever sees it (relevant_facets_for_
        element never bakes width/height onto <img>), so every image renders
        through ChapterHtmlSlimParser's CSS-less scale-to-fit-container
        fallback regardless of the source book's own CSS."""
        return max(1, self.max_width - 2 * SCREEN_MARGIN_MIN_PX)

    @property
    def container_max_height(self) -> int:
        """Upper bound on the on-device text/image container height at any
        reachable screen-margin setting; see container_max_width."""
        return max(1, self.max_height - 2 * SCREEN_MARGIN_MIN_PX)

    def image_state(self, path: str) -> int:
        return self.split_states.get(path, STATE_NORMAL)


# ============================================================================
# Image pipeline -- mirrors FilesPage.html:4412-4941
# ============================================================================


@dataclass
class ImagePart:
    data: bytes
    suffix: str
    width: int
    height: int
    size: int


@dataclass
class ImageResult:
    parts: list[ImagePart]
    meta: dict


def apply_grayscale(img: Image.Image) -> Image.Image:
    """Port of applyGrayscale (FilesPage.html:4412-4426). Pillow's mode-L
    conversion uses the same ITU-R 601-2 luma coefficients (0.299/0.587/0.114)
    as the JS version; the channel-wise round-half-up of the original is lost
    on the last bit, invisibly after JPEG re-encode."""
    return img.convert("L").convert("RGB")


def should_skip_auto_crop(image_path: str, width: int, height: int, opts: OptimizeOptions) -> bool:
    """Port of shouldSkipAutoCrop (FilesPage.html:4428-4433)."""
    if not opts.auto_crop:
        return True
    if image_path in opts.protected_paths:
        return True
    if width < MIN_CROP_DIMENSION or height < MIN_CROP_DIMENSION:
        return True
    return bool(re.search(r"(^|/)(cover|thumbnail|thumb|icon)[^/]*\.(jpe?g|png|gif|webp|bmp)$", image_path or "", re.IGNORECASE))


def estimate_crop_background(arr: np.ndarray) -> Optional[tuple[float, float, float]]:
    """Port of estimateCropBackground (FilesPage.html:4444-4491)."""
    height, width = arr.shape[:2]
    sample_size = min(CROP_EDGE_SAMPLE_SIZE, width // 8, height // 8)
    if sample_size < 2:
        return None

    points = [
        (0, 0),
        (width - sample_size, 0),
        (0, height - sample_size),
        (width - sample_size, height - sample_size),
        ((width - sample_size) // 2, 0),
        ((width - sample_size) // 2, height - sample_size),
        (0, (height - sample_size) // 2),
        (width - sample_size, (height - sample_size) // 2),
    ]

    samples = []
    for sx, sy in points:
        block = arr[sy : sy + sample_size, sx : sx + sample_size].astype(np.float64)
        samples.append(block.reshape(-1, 3).mean(axis=0))
    samples = np.array(samples)
    avg = samples.mean(axis=0)
    max_spread = float(np.max(np.abs(samples - avg)))
    if max_spread > CROP_BACKGROUND_MAX_SPREAD:
        return None
    return tuple(avg)


def is_near_white_background(background: Optional[tuple[float, float, float]]) -> bool:
    return background is not None and all(c >= CROP_WHITE_THRESHOLD for c in background)


def find_non_white_bounds(arr: np.ndarray) -> Optional[dict]:
    """Port of findNonWhiteBounds (FilesPage.html:4509-4547)."""
    height, width = arr.shape[:2]
    background = estimate_crop_background(arr)
    fa = arr.astype(np.float64)

    if background is not None:
        bg = np.array(background)
        content_mask = np.any(np.abs(fa - bg) > CROP_BACKGROUND_TOLERANCE, axis=-1)
    else:
        content_mask = np.any(fa < CROP_WHITE_THRESHOLD, axis=-1)

    ys, xs = np.nonzero(content_mask)
    if xs.size == 0:
        return None

    left, right = int(xs.min()), int(xs.max())
    top, bottom = int(ys.min()), int(ys.max())

    left = max(0, left - CROP_PADDING_PX)
    top = max(0, top - CROP_PADDING_PX)
    right = min(width - 1, right + CROP_PADDING_PX)
    bottom = min(height - 1, bottom + CROP_PADDING_PX)

    crop_w = right - left + 1
    crop_h = bottom - top + 1
    saved_ratio = 1 - ((crop_w * crop_h) / (width * height))
    min_saved_ratio = (
        MIN_COLOR_CROP_SAVINGS_RATIO
        if background is not None and not is_near_white_background(background)
        else MIN_CROP_SAVINGS_RATIO
    )
    if saved_ratio < min_saved_ratio:
        return None
    return {"x": left, "y": top, "width": crop_w, "height": crop_h}


def create_auto_cropped_canvas(
    img: Image.Image, image_path: str, width: int, height: int, opts: OptimizeOptions
) -> tuple[Image.Image, int, int, Optional[dict]]:
    """Port of createAutoCroppedCanvas (FilesPage.html:4549-4581). Returns an
    opaque RGB canvas composited over white -- alpha is fully resolved here,
    so every downstream step operates on plain RGB (matches the JS canvas,
    whose getImageData is always opaque after fillRect(white)+drawImage)."""
    rgba = img.convert("RGBA") if img.mode != "RGBA" else img
    white_bg = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    white_bg.alpha_composite(rgba)
    source = white_bg.convert("RGB")

    if should_skip_auto_crop(image_path, width, height, opts):
        return source, width, height, None

    arr = np.asarray(source)
    crop = find_non_white_bounds(arr)
    if crop is None:
        return source, width, height, None

    box = (crop["x"], crop["y"], crop["x"] + crop["width"], crop["y"] + crop["height"])
    cropped = source.crop(box)
    return cropped, crop["width"], crop["height"], crop


def _split_positions(total_w: int, max_w: int, overlap_percent: int, ltr: bool) -> list[int]:
    """Shared centered-overlap split geometry used by both H-Split (rtl when
    clockwise) and V-Split (always ltr) -- FilesPage.html:4664-4705 (H-Split)
    and :4764-4793 (V-Split), which implement the identical algorithm."""
    min_overlap_px = js_round(max_w * (overlap_percent / 100))
    max_step = max_w - min_overlap_px
    num_parts = math.ceil((total_w - min_overlap_px) / max_step)
    if num_parts < 2:
        num_parts = 2
    step = js_round((total_w - max_w) / (num_parts - 1))
    overlap_px = max_w - step
    if overlap_px < min_overlap_px:
        overlap_px = min_overlap_px
        step = max_w - overlap_px

    positions = []
    for i in range(num_parts):
        x = i * step if ltr else total_w - max_w - i * step
        x = max(0, min(x, total_w - max_w))
        positions.append(x)
    if ltr:
        positions[0] = 0
        positions[-1] = total_w - max_w
    else:
        positions[0] = total_w - max_w
        positions[-1] = 0
    return positions


def _to_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _rotate(img: Image.Image, clockwise: bool) -> Image.Image:
    return img.transpose(Image.Transpose.ROTATE_270 if clockwise else Image.Transpose.ROTATE_90)


def _split_canvas(canvas: Image.Image, positions: list[int], part_w: int, part_h: int) -> list[Image.Image]:
    parts = []
    for x in positions:
        part = Image.new("RGB", (part_w, part_h), (255, 255, 255))
        part.paste(canvas.crop((x, 0, x + part_w, part_h)), (0, 0))
        parts.append(part)
    return parts


def process_image(
    data: bytes,
    image_state: int,
    image_path: str,
    opts: OptimizeOptions,
    target_w: Optional[int] = None,
    target_h: Optional[int] = None,
) -> ImageResult:
    """Port of processImage (FilesPage.html:4585-4941). `target_w`/`target_h`
    override the device-profile box for STATE_NORMAL images only, when the
    firmware's CSS cascade resolves an explicit width/height smaller than the
    full screen for every place this image is used (see
    compute_image_render_targets) -- manual split/rotate states always use
    the full device box, since those are a deliberate manga-panel override."""
    orig_size = len(data)
    img = Image.open(io.BytesIO(data))
    img.load()
    if getattr(img, "is_animated", False):
        img.seek(0)  # canvas drawImage() on an <img> only ever paints frame 0
    orig_w, orig_h = img.size

    source, source_w, source_h, crop = create_auto_cropped_canvas(img, image_path, orig_w, orig_h, opts)
    was_cropped = crop is not None
    if image_state == STATE_NORMAL and target_w is not None and target_h is not None:
        max_w, max_h = target_w, target_h
    else:
        max_w, max_h = opts.max_width, opts.max_height
    quality = opts.jpeg_quality
    clockwise = opts.handedness == "right"

    def gray(canvas: Image.Image) -> Image.Image:
        if not opts.grayscale:
            return canvas
        return apply_grayscale(canvas)

    if image_state == STATE_HSPLIT:
        scale = max_h / source_w
        scaled_w, scaled_h = max_h, js_round(source_h * scale)
        scaled = source.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
        rot = _rotate(scaled, clockwise)
        rot = gray(rot)
        rot_w, rot_h = rot.size

        if rot_w <= max_w:
            part_bytes = _to_jpeg(rot, quality)
            return ImageResult(
                parts=[ImagePart(part_bytes, "", rot_w, rot_h, len(part_bytes))],
                meta=dict(origW=orig_w, origH=orig_h, origSize=orig_size, wasSplit=False, rotated=True,
                          finalW=rot_w, finalH=rot_h, finalSize=len(part_bytes), imageState=STATE_HSPLIT),
            )
        positions = _split_positions(rot_w, max_w, opts.overlap_percent, ltr=not clockwise)
        canvases = _split_canvas(rot, positions, max_w, rot_h)
        parts = []
        for i, c in enumerate(canvases):
            b = _to_jpeg(c, quality)
            parts.append(ImagePart(b, f"_part{i + 1}", max_w, rot_h, len(b)))
        return ImageResult(
            parts=parts,
            meta=dict(origW=orig_w, origH=orig_h, origSize=orig_size, wasSplit=True, splitCount=len(parts),
                      rotated=True, finalW=parts[0].width, finalH=parts[0].height,
                      finalSize=sum(p.size for p in parts), imageState=STATE_HSPLIT),
        )

    if image_state == STATE_VSPLIT:
        scale = max_h / source_h
        scaled_w, scaled_h = js_round(source_w * scale), max_h
        scaled = source.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
        scaled = gray(scaled)

        if scaled_w <= max_w:
            part_bytes = _to_jpeg(scaled, quality)
            return ImageResult(
                parts=[ImagePart(part_bytes, "", scaled_w, scaled_h, len(part_bytes))],
                meta=dict(origW=orig_w, origH=orig_h, origSize=orig_size, wasSplit=False, rotated=False,
                          finalW=scaled_w, finalH=scaled_h, finalSize=len(part_bytes), imageState=STATE_VSPLIT),
            )
        positions = _split_positions(scaled_w, max_w, opts.overlap_percent, ltr=True)
        canvases = _split_canvas(scaled, positions, max_w, scaled_h)
        parts = []
        for i, c in enumerate(canvases):
            b = _to_jpeg(c, quality)
            parts.append(ImagePart(b, f"_part{i + 1}", max_w, scaled_h, len(b)))
        return ImageResult(
            parts=parts,
            meta=dict(origW=orig_w, origH=orig_h, origSize=orig_size, wasSplit=True, splitCount=len(parts),
                      rotated=False, finalW=parts[0].width, finalH=parts[0].height,
                      finalSize=sum(p.size for p in parts), imageState=STATE_VSPLIT),
        )

    if image_state == STATE_ROTATE:
        rot = _rotate(source, clockwise)
        rot_w, rot_h = rot.size
        fits = rot_w <= max_w and rot_h <= max_h
        if fits and not was_cropped:
            rot = gray(rot)
            part_bytes = _to_jpeg(rot, quality)
            return ImageResult(
                parts=[ImagePart(part_bytes, "", rot_w, rot_h, len(part_bytes))],
                meta=dict(origW=orig_w, origH=orig_h, origSize=orig_size, wasSplit=False, rotated=True,
                          finalW=rot_w, finalH=rot_h, finalSize=len(part_bytes), imageState=STATE_ROTATE),
            )
        scale = min(max_w / rot_w, max_h / rot_h)
        new_w, new_h = js_round(rot_w * scale), js_round(rot_h * scale)
        scaled = rot.resize((new_w, new_h), Image.Resampling.LANCZOS)
        scaled = gray(scaled)
        part_bytes = _to_jpeg(scaled, quality)
        return ImageResult(
            parts=[ImagePart(part_bytes, "", new_w, new_h, len(part_bytes))],
            meta=dict(origW=orig_w, origH=orig_h, origSize=orig_size, wasSplit=False, rotated=True,
                      finalW=new_w, finalH=new_h, finalSize=len(part_bytes), imageState=STATE_ROTATE),
        )

    # STATE_NORMAL
    fits = source_w <= max_w and source_h <= max_h
    if fits and not was_cropped:
        canvas = gray(source)
        part_bytes = _to_jpeg(canvas, quality)
        return ImageResult(
            parts=[ImagePart(part_bytes, "", source_w, source_h, len(part_bytes))],
            meta=dict(origW=orig_w, origH=orig_h, origSize=orig_size, wasSplit=False, rotated=False,
                      finalW=source_w, finalH=source_h, finalSize=len(part_bytes), imageState=STATE_NORMAL),
        )
    scale = min(max_w / source_w, max_h / source_h)
    new_w, new_h = js_round(source_w * scale), js_round(source_h * scale)
    scaled = source.resize((new_w, new_h), Image.Resampling.LANCZOS)
    scaled = gray(scaled)
    part_bytes = _to_jpeg(scaled, quality)
    return ImageResult(
        parts=[ImagePart(part_bytes, "", new_w, new_h, len(part_bytes))],
        meta=dict(origW=orig_w, origH=orig_h, origSize=orig_size, wasSplit=False, rotated=False,
                  finalW=new_w, finalH=new_h, finalSize=len(part_bytes), imageState=STATE_NORMAL),
    )


# ============================================================================
# XHTML fixups -- mirrors FilesPage.html:4208-4337
# ============================================================================

_COVER_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
<head><meta content="text/html; charset=UTF-8" http-equiv="default-style"/><title>Cover</title></head>
<body><section epub:type="cover"><img style="max-width:100%;height:auto" alt="Cover" src="{href}"/></section></body>
</html>"""


def fix_svg_cover(content: str) -> tuple[str, bool, int]:
    """Port of fixSvgCover (FilesPage.html:4209-4284)."""
    has_svg = "<svg" in content or "<svg:" in content
    if not has_svg or "xlink:href" not in content:
        return content, False, 0
    if not any(m in content for m in ("calibre:cover", 'name="cover"', "<title>Cover</title>")):
        return content, False, 0

    href = None
    for parse in (parse_xml, parse_xml_recover):
        try:
            tree = parse(content)
        except etree.XMLSyntaxError:
            continue
        for svg in tree.iter():
            if local_name(svg.tag) != "svg":
                continue
            for image_el in svg.iter():
                if local_name(image_el.tag) != "image":
                    continue
                href = image_el.get(f"{{{XLINK_NS}}}href") or image_el.get("xlink:href") or image_el.get("href")
                if href:
                    break
            if href:
                break
        if href:
            break

    if not href:
        m = re.search(r'xlink:href=["\']([^"\']+)["\']', content)
        if not m:
            return content, False, 0
        href = m.group(1)

    return _COVER_TEMPLATE.format(href=xml_escape(href)), True, 1


def fix_svg_wrapped_images(content: str) -> tuple[str, bool, int]:
    """Port of fixSvgWrappedImages (FilesPage.html:4287-4337)."""
    has_svg = "<svg" in content or "<svg:" in content
    if not has_svg or "xlink:href" not in content:
        return content, False, 0

    try:
        tree = parse_xml(content)
    except etree.XMLSyntaxError:
        tree = parse_xml_recover(content)

    fixed_count = 0
    for svg in [el for el in tree.iter() if local_name(el.tag) == "svg"]:
        image_el = next((el for el in svg.iter() if local_name(el.tag) == "image"), None)
        if image_el is None:
            continue
        href = image_el.get(f"{{{XLINK_NS}}}href") or image_el.get("xlink:href") or image_el.get("href")
        if not href:
            continue
        width = image_el.get("width") or svg.get("width")
        height = image_el.get("height") or svg.get("height")
        img = etree.SubElement(svg.getparent(), f"{{{XHTML_NS}}}img")
        svg.getparent().replace(svg, img)
        img.set("src", href)
        img.set("alt", "")
        img.set("style", "max-width:100%;height:auto")
        if width:
            img.set("width", width)
        if height:
            img.set("height", height)
        fixed_count += 1

    if fixed_count == 0:
        return content, False, 0
    return serialize_xml(tree, content), True, fixed_count


SAFE_CONTAINERS = {"div", "p", "figure", "aside", "section", "body"}
ROOT_FOLDERS = {"ops", "oebps", "epub", "content"}


def _iter_imgs(tree: etree._Element) -> list[etree._Element]:
    return [el for el in tree.iter() if local_name(el.tag) == "img"]


def rewrite_images_in_xhtml(
    tree: etree._Element, xhtml_path: str, renamed: dict[str, str], split_images: dict[str, dict]
) -> bool:
    """Port of the img-fixup DOMParser pass in convertEpubFile (FilesPage.html:5422-5552):
    strips stale width/height, rewrites renamed image extensions, and rewires
    src/inserts sibling wrappers for images that were split into parts."""
    modified = False
    for img in _iter_imgs(tree):
        if "width" in img.attrib:
            del img.attrib["width"]
            modified = True
        if "height" in img.attrib:
            del img.attrib["height"]
            modified = True
        src = img.get("src")
        if src:
            decoded_src = decode_href(src)
            resolved_src = resolve_path(xhtml_path, decoded_src)
            match = next(((old, new) for old, new in renamed.items() if resolved_src == old), None)
            if match:
                old_path, new_path = match
                img.set(
                    "src",
                    decoded_src.replace(old_path.rsplit("/", 1)[-1], new_path.rsplit("/", 1)[-1], 1),
                )
                modified = True

    if not split_images:
        return modified

    xhtml_dir = xhtml_path.rsplit("/", 1)[0] if "/" in xhtml_path else ""
    xhtml_dir_parts = [p for p in xhtml_dir.split("/") if p]

    for split_info in split_images.values():
        orig_name = split_info["origName"]
        orig_dir = split_info["origDir"]
        parts = split_info["parts"]
        new_name = IMAGE_EXT_RE.sub(".jpg", orig_name)

        split_dir_parts = [p for p in orig_dir.split("/") if p]
        last_dir = split_dir_parts[-1].lower() if split_dir_parts else None
        immediate_parent = split_dir_parts[-1] if (last_dir and last_dir not in ROOT_FOLDERS) else None

        matching_imgs = []
        for img in _iter_imgs(tree):
            src = img.get("src") or ""
            src_parts = [p for p in src.split("/") if p and p not in ("..", ".")]
            src_name = src_parts.pop() if src_parts else ""
            if src_name != orig_name and src_name != new_name:
                continue
            if immediate_parent:
                if not src_parts:
                    xhtml_last_dir = xhtml_dir_parts[-1] if xhtml_dir_parts else None
                    if xhtml_last_dir != immediate_parent:
                        continue
                elif src_parts[-1] != immediate_parent:
                    continue
            elif src_parts and src_parts[-1].lower() not in ROOT_FOLDERS:
                continue
            matching_imgs.append(img)

        for img in matching_imgs:
            src = img.get("src") or ""
            new_src = src.replace(orig_name, parts[0]["imgName"], 1).replace(new_name, parts[0]["imgName"], 1)
            img.set("src", new_src)

            if len(parts) > 1:
                for attr in ("width", "height", "class"):
                    img.attrib.pop(attr, None)
                img.set("style", "max-width:100%;height:auto")

                container = img.getparent()
                while container is not None and local_name(container.tag) not in SAFE_CONTAINERS:
                    container = container.getparent()
                insert_target = container if container is not None else img.getparent()
                if insert_target is not None and local_name(insert_target.tag) != "body":
                    insert_target.attrib.pop("class", None)
                    insert_target.attrib.pop("style", None)
                insert_parent = insert_target.getparent() if insert_target is not None else None
                ns = tree.nsmap.get(None) or XHTML_NS

                if insert_parent is not None:
                    insert_index = list(insert_parent).index(insert_target) + 1
                    for pi in range(1, len(parts)):
                        wrapper = etree.Element(f"{{{ns}}}div")
                        new_img = etree.SubElement(wrapper, f"{{{ns}}}img")
                        part_src = src.replace(orig_name, parts[pi]["imgName"], 1).replace(
                            new_name, parts[pi]["imgName"], 1
                        )
                        new_img.set("src", part_src)
                        new_img.set("alt", "")
                        new_img.set("style", "max-width:100%;height:auto")
                        insert_parent.insert(insert_index, wrapper)
                        insert_index += 1
            modified = True

    return modified


# ============================================================================
# OPF / NCX fixups -- mirrors FilesPage.html:4052-4206, :4339-4409
# ============================================================================


def _local_findall(tree: etree._Element, name: str) -> list[etree._Element]:
    return tree.xpath(f".//*[local-name()='{name}']")


def extract_identifier(opf_content: str) -> Optional[str]:
    """Port of extractIdentifier (FilesPage.html:4052-4082)."""
    tree = None
    for parse in (parse_xml, parse_xml_recover):
        try:
            tree = parse(opf_content)
            break
        except etree.XMLSyntaxError:
            continue
    if tree is None:
        return None
    pkg = next((el for el in tree.iter() if local_name(el.tag) == "package"), None)
    uid = pkg.get("unique-identifier") if pkg is not None else None
    identifiers = _local_findall(tree, "identifier")
    if uid:
        el = next((e for e in identifiers if e.get("id") == uid), None)
        if el is not None and (el.text or "").strip():
            return (el.text or "").strip()
    if identifiers and (identifiers[0].text or "").strip():
        return (identifiers[0].text or "").strip()
    return None


def sync_ncx_identifier(ncx_text: str, main_identifier: Optional[str]) -> str:
    """Port of syncNCXIdentifier (FilesPage.html:4087-4103)."""
    if not main_identifier:
        return ncx_text
    tree = None
    for parse in (parse_xml, parse_xml_recover):
        try:
            tree = parse(ncx_text)
            break
        except etree.XMLSyntaxError:
            continue
    if tree is None:
        return ncx_text
    meta = next(
        (
            el
            for el in tree.iter()
            if local_name(el.tag) == "meta" and el.get("name") == "dtb:uid"
        ),
        None,
    )
    if meta is None:
        return ncx_text
    meta.set("content", main_identifier)
    return serialize_xml(tree, ncx_text)


def ensure_cover_meta(opf_string: str) -> tuple[str, bool]:
    """Port of ensureCoverMeta / ensureCoverMetaRegex (FilesPage.html:4340-4409)."""
    tree = None
    for parse in (parse_xml, parse_xml_recover):
        try:
            tree = parse(opf_string)
            break
        except etree.XMLSyntaxError:
            continue
    if tree is None:
        return opf_string, False
    items = _local_findall(tree, "item")
    cover_id = None
    for item in items:
        if (item.get("media-type") or "").startswith("image/") and "cover-image" in (
            item.get("properties") or ""
        ):
            cover_id = item.get("id")
            break
    if not cover_id:
        for item in items:
            if not (item.get("media-type") or "").startswith("image/"):
                continue
            ident, href = item.get("id") or "", item.get("href") or ""
            if "cover" in ident.lower() or "cover" in href.lower():
                cover_id = ident
                break
    if not cover_id:
        return opf_string, False

    metas = _local_findall(tree, "meta")
    cover_meta = next((m for m in metas if m.get("name") == "cover"), None)
    if cover_meta is not None:
        if cover_meta.get("content") == cover_id:
            return opf_string, False
        cover_meta.set("content", cover_id)
    else:
        metadata = next((el for el in tree.iter() if local_name(el.tag) == "metadata"), None)
        if metadata is None:
            return opf_string, False
        ns = metadata.nsmap.get(None) or OPF_NS
        new_meta = etree.SubElement(metadata, f"{{{ns}}}meta")
        new_meta.set("name", "cover")
        new_meta.set("content", cover_id)
    return serialize_xml(tree, opf_string), True


def _fix_opf_tree(
    tree: etree._Element,
    split_images: dict[str, dict],
    opf_dir: str,
    drop_css_items: bool,
) -> None:
    if drop_css_items:
        for item in _local_findall(tree, "item"):
            if (item.get("media-type") or "") == "text/css":
                item.getparent().remove(item)

    items = _local_findall(tree, "item")
    manifest_el = next((el for el in tree.iter() if local_name(el.tag) == "manifest"), None)

    for item in items:
        href = item.get("href") or ""
        media_type = item.get("media-type") or ""
        if href.endswith(".jpg") and re.match(r"^image/(png|gif|webp|bmp)$", media_type):
            item.set("media-type", "image/jpeg")

    for item in items:
        props = item.get("properties") or ""
        if "svg" in props:
            new_props = " ".join(p for p in props.split() if p != "svg")
            if new_props:
                item.set("properties", new_props)
            else:
                item.attrib.pop("properties", None)

    for split_key, split_info in split_images.items():
        parts = split_info.get("parts") or split_info
        orig_href = (
            split_key[len(opf_dir) + 1 :] if opf_dir and split_key.startswith(opf_dir + "/") else split_key
        )
        orig_href_jpg = IMAGE_EXT_RE.sub(".jpg", orig_href)
        part1_href = re.sub(r"\.jpg$", "_part1.jpg", orig_href_jpg, flags=re.IGNORECASE)

        for item in items:
            h = item.get("href") or ""
            if h in (orig_href, orig_href_jpg) or decode_href(h) in (orig_href, orig_href_jpg):
                item.set("href", part1_href)
                break

        if manifest_el is not None:
            ns = manifest_el.nsmap.get(None) or OPF_NS
            for p in parts[1:]:
                href = p["path"][len(opf_dir) + 1 :] if opf_dir and p["path"].startswith(opf_dir + "/") else p["path"]
                new_item = etree.SubElement(manifest_el, f"{{{ns}}}item")
                new_item.set("id", f"img-{p['id']}")
                new_item.set("href", href)
                new_item.set("media-type", "image/jpeg")


def fix_opf(
    opf_text: str, opf_original: str, opf_dir: str, split_images: dict[str, dict], drop_css_items: bool
) -> str:
    """Port of fixOPF (FilesPage.html:4110-4206)."""
    tree = None
    for parse in (parse_xml, parse_xml_recover):
        try:
            tree = parse(opf_text)
            break
        except etree.XMLSyntaxError:
            continue
    t = opf_text
    if tree is not None:
        _fix_opf_tree(tree, split_images, opf_dir, drop_css_items)
        t = serialize_xml(tree, opf_original)
    fixed_t, fixed = ensure_cover_meta(t)
    if fixed:
        t = fixed_t
    return t


OPTIMIZER_META_PREFIX = "crosspoint:optimiz"  # matches crosspoint:optimized* and crosspoint:optimizer*


def add_optimizer_metadata(opf_text: str, opf_original: str, opts: OptimizeOptions, stats: ConversionStats) -> str:
    """Records this optimization pass in the OPF <metadata> block (device
    profile, quality, and which passes ran) so a book's optimization history
    survives independent of any app-side state. Idempotent: strips any
    `crosspoint:optimiz*` meta entries a previous pass added before adding
    the current run's, so re-optimizing a file doesn't accumulate stale
    entries."""
    tree = None
    for parse in (parse_xml, parse_xml_recover):
        try:
            tree = parse(opf_text)
            break
        except etree.XMLSyntaxError:
            continue
    if tree is None:
        return opf_text
    metadata = next((el for el in tree.iter() if local_name(el.tag) == "metadata"), None)
    if metadata is None:
        return opf_text

    for meta in _local_findall(tree, "meta"):
        if meta.getparent() is metadata and (meta.get("name") or "").startswith(OPTIMIZER_META_PREFIX):
            metadata.remove(meta)

    ns = metadata.nsmap.get(None) or OPF_NS

    def add_meta(name: str, content: str) -> None:
        el = etree.SubElement(metadata, f"{{{ns}}}meta")
        el.set("name", name)
        el.set("content", content)

    add_meta("crosspoint:optimized", "true")
    add_meta(
        "crosspoint:optimizer-settings",
        f"device={opts.device};quality={opts.jpeg_quality};grayscale={int(opts.grayscale)};"
        f"autoCrop={int(opts.auto_crop)};cssSquash={int(opts.css_squash)};"
        f"cssAwareResize={int(opts.css_aware_resize)}",
    )
    add_meta("crosspoint:optimizer-date", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    add_meta(
        "crosspoint:optimizer-stats",
        f"imagesProcessed={stats.images_processed};imagesSplit={stats.images_split};"
        f"imagesResizedByCss={stats.images_resized_by_css};cssSquashed={stats.css_squashed}",
    )
    return serialize_xml(tree, opf_original)


# ============================================================================
# CSS cascade squash -- mirrors FilesPage.html:5004-5279
# ============================================================================
#
# The browser version resolves the cascade with a real hidden-iframe
# getComputedStyle() call. Ported here with tinycss2 (parsing) + cssselect2
# (selector matching/specificity) instead. This is *not* a general CSS
# engine -- it only resolves the exact property set squashCssToClasses reads,
# and it can do so without a full inheritance walk: `elementHasOwnDeclaration`
# gates every bake on "some rule or the inline style directly targets this
# element for this property", and per the cascade spec a direct match on an
# element always wins over anything inherited from an ancestor regardless of
# specificity -- inheritance is only the fallback when the cascade yields no
# value at all for that element. So resolving "the winning declaration among
# those that directly match this element" is sufficient; no ancestor walk is
# needed except to resolve `font-size` itself (needed to convert
# margin/padding/text-indent into em, see _resolve_font_size_px).

HTML_ROOT_FONT_PX = 16.0
_PT_TO_PX = 96.0 / 72.0
_DECORATION_LINE_KEYWORDS = {"underline", "overline", "line-through", "blink"}
_UNRESOLVABLE_KEYWORDS = {"inherit", "initial", "unset", "revert"}

BOX_SHORTHANDS = {
    "margin": ("margin-top", "margin-right", "margin-bottom", "margin-left"),
    "padding": ("padding-top", "padding-right", "padding-bottom", "padding-left"),
}


def _serialize_tokens(tokens) -> str:
    return tinycss2.serialize(tokens).strip()


def _significant_tokens(tokens) -> list:
    return [t for t in tokens if t.type not in ("whitespace", "comment")]


def _expand_box_shorthand(value_str: str) -> Optional[tuple[str, str, str, str]]:
    """1-4 value box shorthand (margin/padding) -> (top, right, bottom, left)
    value strings, CSS-spec value-count expansion. Mirrors what a browser's
    CSSOM does when parsing `margin`/`padding` shorthand: every side gets a
    longhand value regardless of what kind of token it is (length, 'auto',
    a keyword, calc()...). Resolving that value to an actual em length
    happens downstream in `_parse_length_px`, which returns None for
    anything that isn't a plain length/percentage/zero (auto, calc(), var()
    all naturally drop out there instead of poisoning the whole shorthand -
    e.g. `margin: 1.4em auto` still bakes margin-top/bottom while leaving
    margin-left/right unresolved, exactly like `1.4em auto` would with a
    real cascade). None only for a structurally invalid value (wrong
    component count)."""
    tokens = _significant_tokens(tinycss2.parse_component_value_list(value_str))
    if not (1 <= len(tokens) <= 4):
        return None
    parts = [_serialize_tokens([t]) for t in tokens]
    if len(parts) == 1:
        top = right = bottom = left = parts[0]
    elif len(parts) == 2:
        top, right = parts
        bottom, left = top, right
    elif len(parts) == 3:
        top, right, bottom = parts
        left = right
    else:
        top, right, bottom, left = parts
    return top, right, bottom, left


def _extract_decoration_line(value_str: str) -> Optional[str]:
    """Pulls the line keywords (underline/overline/line-through/blink/none)
    out of a `text-decoration` shorthand value, ignoring style/color/
    thickness components, mirroring what the CSSOM sets text-decoration-line
    to when the shorthand is parsed."""
    tokens = _significant_tokens(tinycss2.parse_component_value_list(value_str))
    lines = []
    for t in tokens:
        if t.type == "ident":
            kw = t.lower_value
            if kw == "none":
                return "none"
            if kw in _DECORATION_LINE_KEYWORDS and kw not in lines:
                lines.append(kw)
    return " ".join(lines) if lines else None


def _parse_length_px(
    value_str: str,
    em_base_px: float,
    pct_base_px: Optional[float],
    percent_as_raw_number: bool = False,
) -> Optional[float]:
    """Resolve a single length/percentage token to px. `em_base_px` is the
    font-size em/rem is relative to; `pct_base_px` is what `%` is relative to
    (None = treat percentages as unresolvable, since we don't run layout).

    `percent_as_raw_number` replicates a browser quirk: unlike margin/
    padding, `getComputedStyle().textIndent` does NOT resolve a percentage
    to a used px value - it returns the specified value back as a percentage
    string (e.g. "-5.769%"). The browser version's `lengthAsEm` then does
    `parseFloat(cs.textIndent)`, which strips the trailing '%' and divides
    the bare number by font-size-px as if it were already in px. Passing
    `percent_as_raw_number=True` (text-indent only) reproduces that exact
    arithmetic instead of resolving the percentage against a container
    width we don't have (and that this call site never used anyway - see
    the `text-indent` special-case in `compute_element_declarations`)."""
    tokens = _significant_tokens(tinycss2.parse_component_value_list(value_str))
    if len(tokens) != 1:
        return None
    tok = tokens[0]
    if tok.type == "dimension":
        unit = tok.lower_unit
        if unit == "px":
            return float(tok.value)
        if unit == "em":
            return float(tok.value) * em_base_px
        if unit == "rem":
            return float(tok.value) * HTML_ROOT_FONT_PX
        if unit == "pt":
            return float(tok.value) * _PT_TO_PX
        if unit == "pc":
            return float(tok.value) * 12.0 * _PT_TO_PX
        if unit == "in":
            return float(tok.value) * 96.0
        if unit == "cm":
            return float(tok.value) * 96.0 / 2.54
        if unit == "mm":
            return float(tok.value) * 96.0 / 25.4
        return None
    if tok.type == "percentage":
        if percent_as_raw_number:
            return float(tok.value)
        return None if pct_base_px is None else float(tok.value) / 100.0 * pct_base_px
    if tok.type == "number" and tok.value == 0:
        return 0.0
    return None


def _js_round4(x: float) -> float:
    """Math.round(x * 10000) / 10000, JS round-half-away-from-zero semantics."""
    scaled = x * 10000.0
    rounded = math.floor(scaled + 0.5) if scaled >= 0 else -math.floor(-scaled + 0.5)
    return rounded / 10000.0


def _js_number_str(x: float) -> str:
    """Matches JS Number->String conversion for values already snapped to
    <=4 decimal places by _js_round4 (e.g. 2.0 -> "2", 0.6667 -> "0.6667"),
    since the browser version interpolates a JS number directly (`${em}em`)
    rather than a fixed-decimal string."""
    if x == int(x):
        return str(int(x))
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _length_as_em(px: Optional[float], font_size_px: Optional[float]) -> Optional[str]:
    """Port of lengthAsEm (FilesPage.html:5120-5126)."""
    if px is None or font_size_px is None or font_size_px <= 0:
        return None
    return f"{_js_number_str(_js_round4(px / font_size_px))}em"


_IMAGE_LENGTH_NATIVE_UNITS = {"px", "em", "rem", "pt"}


def _normalize_image_length(value_str: str) -> Optional[str]:
    """Normalizes a CSS `width`/`height` value for baking verbatim onto an
    `<img style="">` attribute. Unlike margin/padding/text-indent (baked as
    a resolved `em`, see `_length_as_em`), image percentages must stay
    percentages: the firmware's `CssLength.toPixels(emSize, containerWidth)`
    (CssStyle.h) resolves `%` against the *live* per-orientation container
    width at render time (ChapterHtmlSlimParser.cpp:666-672), exactly like a
    real CSS engine, so baking a fixed px number here would freeze the image
    at whatever orientation the optimizer happened to target. `px`/`em`/
    `rem`/`pt` are also natively understood by CssParser::tryInterpretLength
    and pass through verbatim; every other unit (`in`/`cm`/`mm`/`pc`, all
    viewport-independent) is pre-resolved to an absolute px number here,
    since the firmware would otherwise silently misparse an unrecognized
    unit suffix as bare pixels (CssParser.cpp:292-302)."""
    tokens = _significant_tokens(tinycss2.parse_component_value_list(value_str))
    if len(tokens) != 1:
        return None
    tok = tokens[0]
    if tok.type == "percentage":
        if float(tok.value) <= 0:
            return None
        return f"{_js_number_str(_js_round4(float(tok.value)))}%"
    if tok.type == "dimension":
        unit = tok.lower_unit
        if unit in _IMAGE_LENGTH_NATIVE_UNITS:
            if float(tok.value) <= 0:
                return None
            return f"{_js_number_str(_js_round4(float(tok.value)))}{unit}"
        px = _parse_length_px(value_str, em_base_px=1.0, pct_base_px=None)
        if px is None or px <= 0:
            return None
        return f"{_js_number_str(_js_round4(px))}px"
    return None


def _parse_declarations(content) -> list[tuple[str, str, bool]]:
    """Parses a rule's declaration block into (prop, value, important) triples
    in source order, with box/text-decoration shorthands expanded into their
    longhands (appended right after the shorthand, like CSSOM shorthand
    expansion) so `elementHasOwnDeclaration`-equivalent gating on a longhand
    name also fires when only the shorthand was authored."""
    decls: list[tuple[str, str, bool]] = []
    for d in tinycss2.parse_declaration_list(content, skip_comments=True, skip_whitespace=True):
        if d.type != "declaration":
            continue
        name = d.lower_name
        value_str = _serialize_tokens(d.value)
        if not value_str:
            continue
        decls.append((name, value_str, d.important))
        if name in BOX_SHORTHANDS:
            expanded = _expand_box_shorthand(value_str)
            if expanded:
                for longhand, v in zip(BOX_SHORTHANDS[name], expanded):
                    decls.append((longhand, v, d.important))
        elif name == "text-decoration":
            line = _extract_decoration_line(value_str)
            if line is not None:
                decls.append(("text-decoration-line", line, d.important))
    return decls


class ChapterCascade:
    """A CSS cascade for one chapter's combined author stylesheet text, plus
    per-element inline `style=""` attributes."""

    def __init__(self, css_text: str):
        self.matcher = cssselect2.Matcher()
        self._inline_cache: dict[int, list[tuple[str, str, bool]]] = {}
        self._prop_cache: dict[tuple[int, str], Optional[str]] = {}
        self._font_size_cache: dict[int, float] = {}
        if css_text.strip():
            self._consume_rules(tinycss2.parse_stylesheet(css_text, skip_comments=True, skip_whitespace=True))

    def _consume_rules(self, rules) -> None:
        for rule in rules:
            if rule.type == "qualified-rule":
                self._add_style_rule(rule)
            elif rule.type == "at-rule" and rule.content is not None:
                keyword = (rule.lower_at_keyword or "")
                if keyword == "media":
                    prelude_text = _serialize_tokens(rule.prelude).lower()
                    # The iframe renders as normal page content (screen media);
                    # skip blocks that are print-only.
                    if "print" in prelude_text and "screen" not in prelude_text and "all" not in prelude_text:
                        continue
                    self._consume_rules(
                        tinycss2.parse_stylesheet(rule.content, skip_comments=True, skip_whitespace=True)
                    )
                # @font-face/@import/@page/@namespace etc: not consulted by
                # squashCssToClasses's DECORATION_PROPS/LENGTH_PROPS/facet set.

    def _add_style_rule(self, rule) -> None:
        prelude_text = _serialize_tokens(rule.prelude)
        if not prelude_text:
            return
        try:
            selectors = cssselect2.compile_selector_list(prelude_text)
        except cssselect2.SelectorError:
            return
        decls = _parse_declarations(rule.content)
        if not decls:
            return
        for sel in selectors:
            self.matcher.add_selector(sel, decls)

    def _inline_declarations(self, element: etree._Element) -> list[tuple[str, str, bool]]:
        key = id(element)
        cached = self._inline_cache.get(key)
        if cached is None:
            style_attr = element.get("style")
            cached = _parse_declarations(style_attr) if style_attr else []
            self._inline_cache[key] = cached
        return cached

    def resolve_property(self, el: cssselect2.ElementWrapper, prop: str) -> Optional[str]:
        """The winning value string for `prop` among declarations that
        directly match `el` (inline style + matched author rules), or None if
        none of them declare it. Mirrors `elementHasOwnDeclaration` (gate) +
        `getComputedStyle` (value) for FilesPage.html's DECORATION_PROPS/
        LENGTH_PROPS/facet properties -- see the module-level cascade note."""
        cache_key = (id(el.etree_element), prop)
        if cache_key in self._prop_cache:
            return self._prop_cache[cache_key]

        candidates: list[tuple[int, int, tuple, int, str]] = []

        for name, value, important in self._inline_declarations(el.etree_element):
            if name == prop:
                candidates.append((1 if important else 0, 1, (0, 0, 0), 0, value))

        for specificity, order, pseudo, decls in self.matcher.match(el):
            if pseudo is not None:
                continue
            value = None
            important = False
            for name, v, imp in decls:
                if name == prop:
                    value, important = v, imp
            if value is not None:
                candidates.append((1 if important else 0, 0, specificity, order, value))

        candidates.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
        result = None
        for c in reversed(candidates):
            v = c[4].strip()
            if v.lower() in _UNRESOLVABLE_KEYWORDS or v.lower().startswith("var("):
                continue
            result = v
            break

        self._prop_cache[cache_key] = result
        return result

    def font_size_px(self, el: cssselect2.ElementWrapper) -> float:
        """This element's own computed font-size in px (inherits from parent
        when not directly declared on the element itself)."""
        key = id(el.etree_element)
        cached = self._font_size_cache.get(key)
        if cached is not None:
            return cached
        parent_px = self.font_size_px(el.parent) if el.parent is not None else HTML_ROOT_FONT_PX
        px = parent_px
        own = self.resolve_property(el, "font-size")
        if own is not None:
            resolved = _parse_length_px(own, em_base_px=parent_px, pct_base_px=parent_px)
            if resolved is not None and resolved > 0:
                px = resolved
        self._font_size_cache[key] = px
        return px


def is_bold_weight(computed_font_weight: str) -> bool:
    """Port of isBoldWeight (FilesPage.html:5004-5008)."""
    try:
        return int(computed_font_weight.strip()) >= 700
    except ValueError:
        return bool(re.match(r"^(bold|bolder)$", computed_font_weight.strip(), re.IGNORECASE))


def is_italic_style(computed_font_style: str) -> bool:
    """Port of isItalicStyle (FilesPage.html:5010-5012)."""
    return bool(re.match(r"^(italic|oblique)", computed_font_style.strip(), re.IGNORECASE))


def is_internal_epub_link(href: Optional[str]) -> bool:
    """Port of isInternalEpubLink (FilesPage.html:5078-5081)."""
    if not href:
        return False
    return not re.match(r"^(https?:|mailto:|ftp:|tel:|javascript:)", href, re.IGNORECASE)


def relevant_facets_for_element(el: etree._Element, tag: str) -> Optional[dict]:
    """Port of relevantFacetsForElement (FilesPage.html:5086-5110)."""
    if tag == "a" and is_internal_epub_link(el.get("href")):
        return None
    if tag == "img":
        # Deviation from the FilesPage.html port: a live browser/live cascade
        # never needs this (its layout engine reads the image's own CSS
        # width/height directly), but css-squash strips every class/<link>/
        # <style> off the document, so without baking width/height here an
        # <img> reused at different CSS sizes across chapters would collapse
        # to a single pre-baked pixel size on-device (see
        # compute_image_render_targets: images_by_href is a union of every
        # chapter that references the asset). Checked before SKIP_TAGS,
        # which otherwise excludes "img" entirely (no style was ever baked
        # onto <img> before this, including verticalAlign).
        return {
            "fontWeight": True, "fontStyle": True, "textDecoration": True,
            "direction": True, "verticalAlign": True, "imageBox": True,
        }
    if tag in SKIP_TAGS or tag in ("sup", "sub"):
        return None
    if tag in HEADER_TAGS:
        return {"textAlign": True, "fontStyle": True, "textDecoration": True, "direction": True, "box": True}
    if tag in BLOCK_TAGS:
        return {
            "textAlign": True, "fontWeight": True, "fontStyle": True,
            "textDecoration": True, "direction": True, "box": True,
        }
    if tag in BOLD_TAGS:
        return {"fontStyle": True, "textDecoration": True, "direction": True, "verticalAlign": True}
    if tag in ITALIC_TAGS:
        return {"fontWeight": True, "textDecoration": True, "direction": True, "verticalAlign": True}
    if tag in UNDERLINE_TAGS:
        return {
            "fontWeight": True, "fontStyle": True, "direction": True, "verticalAlign": True,
            "textDecoration": True, "decorationMask": ["underline"],
        }
    if tag in LINETHROUGH_TAGS:
        return {
            "fontWeight": True, "fontStyle": True, "direction": True, "verticalAlign": True,
            "textDecoration": True, "decorationMask": ["line-through"],
        }
    return {"fontWeight": True, "fontStyle": True, "textDecoration": True, "direction": True, "verticalAlign": True}


def compute_element_declarations(
    cascade: ChapterCascade, el: cssselect2.ElementWrapper, facets: dict
) -> list[str]:
    """Port of computeElementDeclarations (FilesPage.html:5132-5175)."""
    decls: list[str] = []

    if facets.get("textAlign"):
        v = cascade.resolve_property(el, "text-align")
        if v is not None:
            vl = v.strip().lower()
            norm = "right" if vl in ("right", "end") else "center" if vl == "center" else "justify" if vl == "justify" else "left"
            decls.append(f"text-align:{norm}")

    if facets.get("fontWeight"):
        v = cascade.resolve_property(el, "font-weight")
        if v is not None:
            decls.append(f"font-weight:{'bold' if is_bold_weight(v) else 'normal'}")

    if facets.get("fontStyle"):
        v = cascade.resolve_property(el, "font-style")
        if v is not None:
            decls.append(f"font-style:{'italic' if is_italic_style(v) else 'normal'}")

    if facets.get("textDecoration"):
        v = cascade.resolve_property(el, "text-decoration-line")
        if v is not None:
            mask = set(facets.get("decorationMask") or [])
            lines = [l for l in v.strip().lower().split() if l in ("underline", "line-through") and l not in mask]
            if lines:
                decls.append(f"text-decoration:{' '.join(lines)}")

    if facets.get("direction"):
        v = cascade.resolve_property(el, "direction")
        if v is not None:
            decls.append(f"direction:{'rtl' if v.strip().lower() == 'rtl' else 'ltr'}")

    if facets.get("verticalAlign"):
        v = cascade.resolve_property(el, "vertical-align")
        if v is not None:
            vl = v.strip().lower()
            if vl == "super":
                decls.append("vertical-align:super")
            elif vl == "sub":
                decls.append("vertical-align:sub")

    if facets.get("box"):
        font_size_px = cascade.font_size_px(el)
        for prop in LENGTH_PROPS:
            v = cascade.resolve_property(el, prop)
            if v is None:
                continue
            px = _parse_length_px(
                v, em_base_px=font_size_px, pct_base_px=None, percent_as_raw_number=(prop == "text-indent")
            )
            if px is None:
                continue
            em = _length_as_em(px, font_size_px)
            if not em:
                continue
            if prop != "text-indent" and float(em[:-2]) == 0:
                continue
            decls.append(f"{prop}:{em}")

    if facets.get("imageBox"):
        # width/height (not part of LENGTH_PROPS/`box`): baked in their
        # original unit via _normalize_image_length, not resolved to `em`,
        # so a percentage width stays responsive to the live per-orientation
        # container width on-device instead of freezing at optimize time.
        for prop in ("width", "height"):
            v = cascade.resolve_property(el, prop)
            if v is None:
                continue
            norm = _normalize_image_length(v)
            if norm is None:
                continue
            decls.append(f"{prop}:{norm}")

    return decls


def collect_chapter_css(head: Optional[etree._Element], chapter_path: str, css_text_by_path: dict[str, str]) -> str:
    """Port of collectChapterCss (FilesPage.html:5182-5196)."""
    combined = ""
    if head is None:
        return combined
    for link in head.iter():
        if local_name(link.tag) != "link" or link.get("rel") != "stylesheet":
            continue
        href = link.get("href")
        if not href:
            continue
        resolved = resolve_path(chapter_path, decode_href(href))
        css = css_text_by_path.get(resolved)
        if css:
            combined += css + "\n"
    for style_el in head.iter():
        if local_name(style_el.tag) == "style":
            combined += (style_el.text or "") + "\n"
    return combined


def squash_css_to_classes(xhtml_string: str, chapter_path: str, css_text_by_path: dict[str, str]) -> str:
    """Port of squashCssToClasses (FilesPage.html:5207-5279): bakes the
    resolved CSS cascade directly onto each element as a style="" attribute
    and strips the original CSS references. Fails open (returns the input
    unchanged) on any parse error, matching the browser version's try/catch."""
    try:
        tree = parse_xml(xhtml_string)
    except etree.XMLSyntaxError:
        return xhtml_string

    head = next((el for el in tree.iter() if local_name(el.tag) == "head"), None)
    body = next((el for el in tree.iter() if local_name(el.tag) == "body"), None)
    if head is None or body is None:
        return xhtml_string

    combined_css = collect_chapter_css(head, chapter_path, css_text_by_path)
    cascade = ChapterCascade(combined_css)

    try:
        root_wrapper = cssselect2.ElementWrapper.from_xml_root(tree)
        elements = [
            w for w in root_wrapper.iter_subtree()
            if any(a.local_name == "body" for a in w.ancestors)
        ]

        changed = False
        for w in elements:
            el = w.etree_element
            tag = local_name(el.tag)
            display = cascade.resolve_property(w, "display")
            if display is not None and display.strip().lower() == "none":
                new_style = "display:none"
            else:
                facets = relevant_facets_for_element(el, tag)
                new_style = ";".join(compute_element_declarations(cascade, w, facets)) if facets else ""

            old_style = el.get("style") or ""
            if new_style != old_style:
                if new_style:
                    el.set("style", new_style)
                else:
                    el.attrib.pop("style", None)
                changed = True

        for w in elements:
            if "class" in w.etree_element.attrib:
                del w.etree_element.attrib["class"]
                changed = True

        # `elements` only walks body's descendants (see the comment above
        # its construction) - body's own class attribute is otherwise never
        # visited, leaving dead cruft behind once the original stylesheet
        # it referenced is gone.
        if "class" in body.attrib:
            del body.attrib["class"]
            changed = True

        for link in [el for el in head.iter() if local_name(el.tag) == "link" and el.get("rel") == "stylesheet"]:
            link.getparent().remove(link)
            changed = True
        for style_el in [el for el in head.iter() if local_name(el.tag) == "style"]:
            style_el.getparent().remove(style_el)
            changed = True

        if not changed:
            return xhtml_string

        return serialize_xml(tree, xhtml_string)
    except Exception:
        return xhtml_string


def _resolve_image_css_box(
    cascade: ChapterCascade, el: cssselect2.ElementWrapper, opts: OptimizeOptions
) -> Optional[tuple[int, int]]:
    """Resolves the on-device render box for one <img> from its CSS `width`/
    `height` alone -- mirrors ChapterHtmlSlimParser::onImg's four branches
    (both set / height-only / width-only / neither), which are the only two
    properties CssParser.cpp parses for images at all (`max-width` and
    `max-height`, including this optimizer's own injected DEFENSIVE_STYLE,
    are silently ignored by the firmware parser). Percentages, and the final
    clamp on absolute em/px sizes, resolve against opts.container_max_width/
    height rather than the raw device box: css-squash always strips <img>'s
    CSS before the firmware sees it (see squash_css_to_classes), so every
    image renders through the CSS-less scale-to-fit-container fallback,
    whose container is always margin-reduced -- container_max_width/height
    is a safe upper bound on that container at any margin the user could
    ever pick (SCREEN_MARGIN_MIN_PX), so an image is never sized smaller
    than it can actually render at. Returns None when neither property is
    declared, meaning "no override, use the full container box"."""
    width_v = cascade.resolve_property(el, "width")
    height_v = cascade.resolve_property(el, "height")
    if width_v is None and height_v is None:
        return None

    font_size_px = cascade.font_size_px(el)
    width_px = (
        _parse_length_px(width_v, em_base_px=font_size_px, pct_base_px=float(opts.container_max_width))
        if width_v is not None
        else None
    )
    height_px = (
        _parse_length_px(height_v, em_base_px=font_size_px, pct_base_px=float(opts.container_max_height))
        if height_v is not None
        else None
    )
    if width_px is None and height_px is None:
        return None

    # An unresolved side (e.g. only height given) is left uncapped so
    # process_image's own min(w-ratio, h-ratio) scale derives it from the
    # image's aspect ratio, exactly like the firmware's aspect-derived branches.
    target_w = js_round(width_px) if width_px is not None else opts.container_max_width
    target_h = js_round(height_px) if height_px is not None else opts.container_max_height
    return max(1, min(target_w, opts.container_max_width)), max(1, min(target_h, opts.container_max_height))


def compute_image_render_targets(
    xhtml_files: dict[str, str], css_text_by_path: dict[str, str], opts: OptimizeOptions
) -> dict[str, tuple[int, int]]:
    """Resolves every chapter's CSS cascade to find <img> elements with an
    explicit `width`/`height`, keyed by the resolved on-disk image path. An
    image referenced more than once takes the largest box across every
    chapter that uses it; any usage with no explicit size at all forces the
    full container box for that image (opts.container_max_width/height),
    since it can render at that size on-device regardless of what a
    different chapter requests."""
    targets: dict[str, tuple[int, int]] = {}
    full_box = (opts.container_max_width, opts.container_max_height)
    for chapter_path, content in xhtml_files.items():
        if "<img" not in content:
            continue
        tree = None
        for parse in (parse_xml, parse_xml_recover):
            try:
                tree = parse(content)
                break
            except etree.XMLSyntaxError:
                continue
        if tree is None:
            continue
        imgs = [el for el in tree.iter() if local_name(el.tag) == "img"]
        if not imgs:
            continue
        try:
            head = next((el for el in tree.iter() if local_name(el.tag) == "head"), None)
            combined_css = collect_chapter_css(head, chapter_path, css_text_by_path)
            cascade = ChapterCascade(combined_css)
            root_wrapper = cssselect2.ElementWrapper.from_xml_root(tree)
            wrapper_by_el = {w.etree_element: w for w in root_wrapper.iter_subtree()}
        except Exception:
            continue
        for img in imgs:
            src = img.get("src")
            if not src:
                continue
            href = resolve_path(chapter_path, decode_href(src))
            wrapper = wrapper_by_el.get(img)
            if wrapper is None:
                continue
            try:
                box = _resolve_image_css_box(cascade, wrapper, opts)
            except Exception:
                box = None
            if box is None:
                targets[href] = full_box
                continue
            prev = targets.get(href)
            targets[href] = box if prev is None else (max(prev[0], box[0]), max(prev[1], box[1]))
    return targets


# ============================================================================
# Orchestration -- mirrors convertEpubFile (FilesPage.html:5283-5645)
# ============================================================================


@dataclass
class ConversionStats:
    original_size: int
    new_size: int = 0
    images_processed: int = 0
    images_split: int = 0
    images_resized_by_css: int = 0
    xhtml_fixed: int = 0
    css_squashed: int = 0
    opf_updated: bool = False
    ncx_synced: bool = False
    warnings: list[str] = field(default_factory=list)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def convert_epub_bytes(data: bytes, opts: OptimizeOptions) -> tuple[bytes, ConversionStats]:
    """Port of convertEpubFile (FilesPage.html:5283-5645): resize/split/rotate
    every image, fix up XHTML/OPF/NCX references to match, optionally squash
    CSS into inline style="" attributes, and repackage as a new EPUB."""
    stats = ConversionStats(original_size=len(data))
    zin = zipfile.ZipFile(io.BytesIO(data))
    names = zin.namelist()
    dirset = {n for n in names if n.endswith("/")}

    def is_dir(name: str) -> bool:
        return name in dirset or name.endswith("/")

    # renamed: EVERY image path in the archive with a non-.jpg raster
    # extension -> its .jpg-renamed path, computed up front regardless of
    # split state (FilesPage.html: `zip.forEach` renamed-building loop just
    # above convertEpubFile). Split images get a *further* _partN rename
    # inside fixOPF/rewrite_images_in_xhtml; this dict only covers the
    # single-part case and the blind filename-replace passes below.
    renamed: dict[str, str] = {}
    for p in names:
        if is_dir(p):
            continue
        if IMAGE_EXT_RE.search(p):
            renamed[p] = IMAGE_EXT_RE.sub(".jpg", p)

    out_entries: list[tuple[str, bytes, int]] = []

    if "mimetype" in names:
        out_entries.append(("mimetype", zin.read("mimetype"), zipfile.ZIP_STORED))

    split_images: dict[str, dict] = {}
    xhtml_files: dict[str, str] = {}
    css_text_by_path: dict[str, str] = {}
    opf_path: Optional[str] = None
    opf_content: Optional[str] = None

    # First pass: read (but don't yet transform) XHTML/CSS/OPF text, and collect
    # image paths for the second sub-pass below. Text must be read for every
    # chapter before any image is resized, since compute_image_render_targets
    # needs every chapter's CSS cascade to know how small an image can be.
    image_paths: list[str] = []
    for path in names:
        if is_dir(path) or path == "mimetype":
            continue
        low = path.lower()

        if ANY_IMAGE_EXT_RE.search(low):
            image_paths.append(path)
        elif XHTML_EXT_RE.search(low):
            xhtml_files[path] = safe_read_text(zin.read(path))
        elif low.endswith(".css"):
            if opts.css_squash or opts.css_aware_resize:
                css_text_by_path[path] = safe_read_text(zin.read(path))
        elif low.endswith(".opf"):
            opf_path = path
            opf_content = safe_read_text(zin.read(path))

    image_targets = (
        compute_image_render_targets(xhtml_files, css_text_by_path, opts) if opts.css_aware_resize else {}
    )

    # Second sub-pass: process every image now that render targets are known.
    for path in image_paths:
        raw = zin.read(path)
        image_state = opts.image_state(path)
        target = image_targets.get(path)
        target_w, target_h = target if target is not None else (None, None)
        try:
            result = process_image(raw, image_state, path, opts, target_w=target_w, target_h=target_h)
            parts = result.parts
        except Exception as e:
            stats.warnings.append(f"Failed to process {_basename(path)}, using original: {e}")
            parts = [ImagePart(raw, "", 0, 0, len(raw))]

        stats.images_processed += 1
        # Compare against the container box (compute_image_render_targets' own
        # "no CSS at all" default), not the raw device box -- otherwise every
        # image would count as "sized from CSS" merely for being margin-trimmed.
        if image_state == STATE_NORMAL and target is not None and target != (
            opts.container_max_width,
            opts.container_max_height,
        ):
            stats.images_resized_by_css += 1
        base_name = re.sub(r"\.[^.]+$", "", path)

        if len(parts) == 1 and parts[0].suffix == "":
            new_path = renamed.get(path) or re.sub(r"\.[^.]+$", ".jpg", path)
            out_entries.append((new_path, parts[0].data, zipfile.ZIP_STORED))
        else:
            stats.images_split += 1
            path_dir = _dirname(path)
            split_images[path] = {"origName": _basename(path), "origDir": path_dir, "parts": []}
            prefix = path_dir + "/" if path_dir else ""
            for part in parts:
                part_name = _basename(base_name) + part.suffix + ".jpg"
                part_path = prefix + part_name
                out_entries.append((part_path, part.data, zipfile.ZIP_STORED))
                split_images[path]["parts"].append(
                    {
                        "path": part_path,
                        "imgName": part_name,
                        "id": _basename(base_name) + part.suffix,
                        "suffix": part.suffix,
                    }
                )

    # Second pass: XHTML fixups (SVG cover/images, img src rewiring, CSS squash).
    for xhtml_path, content in xhtml_files.items():
        t = content
        t, fixed, _ = fix_svg_cover(t)
        if fixed:
            stats.xhtml_fixed += 1
        t2, fixed2, _ = fix_svg_wrapped_images(t)
        if fixed2:
            t = t2
            stats.xhtml_fixed += 1

        tree = None
        for parse in (parse_xml, parse_xml_recover):
            try:
                tree = parse(t)
                break
            except etree.XMLSyntaxError as e:
                last_err = e
                continue
        if tree is None:
            stats.warnings.append(f"DOMParser error for {xhtml_path}: {last_err}")
        elif rewrite_images_in_xhtml(tree, xhtml_path, renamed, split_images):
            t = serialize_xml(tree, content)

        if opts.css_squash:
            before = t
            t = squash_css_to_classes(t, xhtml_path, css_text_by_path)
            if t != before:
                stats.css_squashed += 1

        if "</head>" in t:
            t = t.replace("</head>", DEFENSIVE_STYLE + "</head>", 1)

        out_entries.append((xhtml_path, t.encode("utf-8"), zipfile.ZIP_DEFLATED))

    main_identifier = extract_identifier(opf_content) if opf_content else None

    # Third pass: OPF.
    if opf_content is not None and opf_path is not None:
        t = opf_content
        for o, n in renamed.items():
            t = t.replace(_basename(o), _basename(n))
        opf_dir = _dirname(opf_path)
        t = fix_opf(t, opf_content, opf_dir, split_images, opts.css_squash)
        t = add_optimizer_metadata(t, opf_content, opts, stats)
        if t != opf_content:
            stats.opf_updated = True
        out_entries.append((opf_path, t.encode("utf-8"), zipfile.ZIP_DEFLATED))

    # Copy remaining files (fonts, NCX, nav resources, and CSS when squash is off).
    for path in names:
        if is_dir(path) or path == "mimetype":
            continue
        low = path.lower()
        if ANY_IMAGE_EXT_RE.search(low) or XHTML_EXT_RE.search(low) or low.endswith(".opf"):
            continue
        if opts.css_squash and low.endswith(".css"):
            continue

        raw = zin.read(path)
        if low.endswith(".css"):
            t = safe_read_text(raw)
            for o, n in renamed.items():
                t = t.replace(_basename(o), _basename(n))
            out_entries.append((path, t.encode("utf-8"), zipfile.ZIP_DEFLATED))
        elif low.endswith(".ncx"):
            t = safe_read_text(raw)
            for o, n in renamed.items():
                t = t.replace(_basename(o), _basename(n))
            old_t = t
            t = sync_ncx_identifier(t, main_identifier)
            if t != old_t:
                stats.ncx_synced = True
            out_entries.append((path, t.encode("utf-8"), zipfile.ZIP_DEFLATED))
        else:
            out_entries.append((path, raw, zipfile.ZIP_DEFLATED))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zout:
        for entry_path, entry_data, compress_type in out_entries:
            info = zipfile.ZipInfo(entry_path)
            info.compress_type = compress_type
            info.external_attr = 0o644 << 16
            zout.writestr(info, entry_data, compresslevel=8 if compress_type == zipfile.ZIP_DEFLATED else None)

    new_bytes = buf.getvalue()
    stats.new_size = len(new_bytes)
    return new_bytes, stats


def convert_epub_file(input_path: Path, output_path: Path, opts: OptimizeOptions) -> ConversionStats:
    data = input_path.read_bytes()
    new_bytes, stats = convert_epub_bytes(data, opts)
    output_path.write_bytes(new_bytes)
    return stats


# ============================================================================
# CLI
# ============================================================================

_STATE_NAMES = {"normal": STATE_NORMAL, "hsplit": STATE_HSPLIT, "vsplit": STATE_VSPLIT, "rotate": STATE_ROTATE}


def _parse_split_state(raw: str) -> tuple[str, int]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"expected PATH=STATE, got {raw!r}")
    path, state_str = raw.rsplit("=", 1)
    state_str = state_str.strip().lower()
    if state_str in _STATE_NAMES:
        return path, _STATE_NAMES[state_str]
    try:
        state = int(state_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid state {state_str!r}: expected 0-3 or one of {sorted(_STATE_NAMES)}"
        ) from None
    if state not in (STATE_NORMAL, STATE_HSPLIT, STATE_VSPLIT, STATE_ROTATE):
        raise argparse.ArgumentTypeError(f"state must be 0-3, got {state}")
    return path, state


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="epub_optimizer.py",
        description="Pure-Python port of CrossPoint's browser-side EPUB Optimizer (FilesPage.html).",
    )
    p.add_argument("input", nargs="?", type=Path, help="input .epub file")
    p.add_argument("-o", "--output", type=Path, help="output .epub path (default: <input>.optimized.epub)")
    p.add_argument("--device", choices=sorted(DEVICE_PROFILES), default=DEFAULT_DEVICE, help="target device profile")
    p.add_argument("--quality", type=int, default=85, metavar="30-95", help="JPEG quality (default: 85)")
    p.add_argument("--no-grayscale", dest="grayscale", action="store_false", help="keep original image color")
    p.add_argument("--auto-crop", action="store_true", help="auto-crop uniform margins from images")
    p.add_argument(
        "--no-css-squash", dest="css_squash", action="store_false",
        help="keep CSS classes/stylesheets instead of baking resolved styles onto elements (default: squash)",
    )
    p.add_argument(
        "--no-css-aware-resize", dest="css_aware_resize", action="store_false",
        help="ignore CSS width/height when sizing images, always fill the device screen (default: CSS-aware)",
    )
    p.add_argument("--rotation", choices=["cw", "ccw"], default="cw", help="rotation direction for split images")
    p.add_argument("--overlap", type=int, default=5, metavar="0-100", help="overlap percent for split images (default: 5)")
    p.add_argument(
        "--split-state",
        action="append",
        default=[],
        metavar="PATH=STATE",
        type=_parse_split_state,
        help="per-image split state override; STATE is 0-3 or normal|hsplit|vsplit|rotate (repeatable)",
    )
    p.add_argument("--protect", action="append", default=[], metavar="PATH", help="never auto-crop this image path (repeatable)")
    p.add_argument("--self-test", action="store_true", help="run an in-memory synthetic-EPUB smoke test and exit")
    return p


def _opts_from_args(args: argparse.Namespace) -> OptimizeOptions:
    return OptimizeOptions(
        device=args.device,
        jpeg_quality=args.quality,
        grayscale=args.grayscale,
        auto_crop=args.auto_crop,
        css_squash=args.css_squash,
        css_aware_resize=args.css_aware_resize,
        handedness="right" if args.rotation == "cw" else "left",
        overlap_percent=args.overlap,
        split_states=dict(args.split_state),
        protected_paths=set(args.protect),
    )


def _build_synthetic_epub() -> bytes:
    """Minimal but structurally valid EPUB used by --self-test: one XHTML
    chapter with an inline-styled + linked-stylesheet element, one PNG cover
    image, and an NCX whose dc:identifier is deliberately stale."""
    img = Image.new("RGB", (40, 60), "white")
    for x in range(10, 30):
        img.putpixel((x, 20), (200, 30, 30))
    png_buf = io.BytesIO()
    img.save(png_buf, format="PNG")

    container_xml = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="bookid">urn:uuid:self-test-0001</dc:identifier>'
        "<dc:title>Self Test Book</dc:title>"
        "</metadata>"
        "<manifest>"
        '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="css1" href="style.css" media-type="text/css"/>'
        '<item id="cover-img" href="images/cover.png" media-type="image/png" properties="cover-image"/>'
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        "</manifest>"
        '<spine toc="ncx"><itemref idref="ch1"/></spine>'
        "</package>"
    )
    css = ".title { text-align: center; } p.note { font-style: italic; font-weight: bold; }"
    xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ch1</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>'
        '<body><h1 class="title">Chapter One</h1>'
        '<p class="note">A note.</p>'
        '<img src="images/cover.png" width="40" height="60" alt="cover"/>'
        "</body></html>"
    )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        '<head><meta name="dtb:uid" content="urn:uuid:stale-id"/></head>'
        "<docTitle><text>Self Test Book</text></docTitle>"
        '<navMap><navPoint id="np1"><navLabel><text>Chapter One</text></navLabel>'
        '<content src="chapter1.xhtml"/></navPoint></navMap>'
        "</ncx>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container_xml)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/style.css", css)
        z.writestr("OEBPS/chapter1.xhtml", xhtml)
        z.writestr("OEBPS/images/cover.png", png_buf.getvalue())
        z.writestr("OEBPS/toc.ncx", ncx)
    return buf.getvalue()


def _build_synthetic_epub_with_sized_image() -> bytes:
    """A synthetic EPUB whose one image is explicitly sized far below the
    device screen via CSS `width`, used to test compute_image_render_targets."""
    img = Image.new("RGB", (1200, 800), "white")
    jpg_buf = io.BytesIO()
    img.save(jpg_buf, format="JPEG")

    container_xml = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="bookid">urn:uuid:self-test-0002</dc:identifier>'
        "</metadata>"
        "<manifest>"
        '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="img1" href="images/wide.jpg" media-type="image/jpeg"/>'
        "</manifest>"
        '<spine><itemref idref="ch1"/></spine>'
        "</package>"
    )
    xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ch1</title>'
        "<style>.small { width: 100px; }</style></head>"
        '<body><img class="small" src="images/wide.jpg" alt="wide"/></body></html>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container_xml)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/chapter1.xhtml", xhtml)
        z.writestr("OEBPS/images/wide.jpg", jpg_buf.getvalue())
    return buf.getvalue()


def _run_self_test() -> bool:
    src = _build_synthetic_epub()
    failures: list[str] = []

    def check(label: str, cond: bool) -> None:
        if not cond:
            failures.append(label)

    # Plain conversion (squash explicitly off): image renamed, xhtml src
    # updated, css preserved.
    out, stats = convert_epub_bytes(src, OptimizeOptions(device="X4", css_squash=False))
    zout = zipfile.ZipFile(io.BytesIO(out))
    names = zout.namelist()
    check("mimetype is first entry", names[0] == "mimetype")
    check("mimetype stored uncompressed", zout.getinfo("mimetype").compress_type == zipfile.ZIP_STORED)
    check("mimetype content preserved", zout.read("mimetype") == b"application/epub+zip")
    check("image renamed to .jpg", "OEBPS/images/cover.jpg" in names and "OEBPS/images/cover.png" not in names)
    xhtml_out = zout.read("OEBPS/chapter1.xhtml").decode("utf-8")
    check("img src rewritten to .jpg", "cover.jpg" in xhtml_out and "cover.png" not in xhtml_out)
    check("img width/height stripped", 'width="40"' not in xhtml_out and 'height="60"' not in xhtml_out)
    check("defensive style injected", "max-width:100%" in xhtml_out)
    check("stylesheet kept when squash is off", "OEBPS/style.css" in names)
    ncx_out = zout.read("OEBPS/toc.ncx").decode("utf-8")
    check("NCX identifier synced to OPF", "urn:uuid:self-test-0001" in ncx_out and "stale-id" not in ncx_out)
    check("images_processed counted", stats.images_processed == 1)
    opf_out = zout.read("OEBPS/content.opf").decode("utf-8")
    check("optimizer metadata recorded", "crosspoint:optimized" in opf_out and 'content="true"' in opf_out)
    check("optimizer settings recorded css off", "cssSquash=0" in opf_out)

    # Default conversion: css-squash and css-aware-resize both on by default.
    out2, _ = convert_epub_bytes(src, OptimizeOptions(device="X4"))
    zout2 = zipfile.ZipFile(io.BytesIO(out2))
    names2 = zout2.namelist()
    check("stylesheet dropped by default", "OEBPS/style.css" not in names2)
    xhtml_out2 = zout2.read("OEBPS/chapter1.xhtml").decode("utf-8")
    check("squash bakes bold weight inline", "font-weight" in xhtml_out2 and "bold" in xhtml_out2)
    check("squash drops <link rel=stylesheet>", "rel=\"stylesheet\"" not in xhtml_out2)
    opf_out2 = zout2.read("OEBPS/content.opf").decode("utf-8")
    check("squash drops CSS manifest item", "style.css" not in opf_out2)
    check("optimizer settings recorded css on", "cssSquash=1" in opf_out2)

    # CSS-aware resize: an image with an explicit CSS width smaller than the
    # device screen must not be upscaled to fill the screen.
    small_css_src = _build_synthetic_epub_with_sized_image()
    out3, stats3 = convert_epub_bytes(small_css_src, OptimizeOptions(device="X4", css_squash=False))
    zout3 = zipfile.ZipFile(io.BytesIO(out3))
    with zout3.open("OEBPS/images/wide.jpg") as f:
        resized = Image.open(f)
        resized.load()
    check("css width shrinks stored image below device width", resized.size[0] <= 100)
    check("images_resized_by_css counted", stats3.images_resized_by_css == 1)

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return False
    print(f"SELF-TEST PASSED ({len(names)} entries plain, {len(names2)} entries squashed)")
    return True


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.self_test:
        return 0 if _run_self_test() else 1

    if args.input is None:
        build_arg_parser().error("input .epub is required unless --self-test is given")

    if not (30 <= args.quality <= 95):
        build_arg_parser().error("--quality must be between 30 and 95")
    if not (0 <= args.overlap <= 100):
        build_arg_parser().error("--overlap must be between 0 and 100")

    output_path = args.output or args.input.with_suffix(".optimized.epub")
    opts = _opts_from_args(args)
    stats = convert_epub_file(args.input, output_path, opts)

    print(f"{args.input.name} ({stats.original_size:,} bytes) -> {output_path.name} ({stats.new_size:,} bytes)")
    print(
        f"images: {stats.images_processed} processed, {stats.images_split} split, "
        f"{stats.images_resized_by_css} sized from CSS | "
        f"xhtml fixed: {stats.xhtml_fixed} | css squashed: {stats.css_squashed} | "
        f"opf updated: {stats.opf_updated} | ncx synced: {stats.ncx_synced}"
    )
    for w in stats.warnings:
        print(f"warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
