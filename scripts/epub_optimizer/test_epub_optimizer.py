# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pillow>=10.0",
#   "numpy>=1.26",
#   "lxml>=5.0",
#   "tinycss2>=1.2",
#   "cssselect2>=0.7",
#   "pytest>=8.0",
# ]
# ///
"""Pytest suite for epub_optimizer.py.

Run: uv run --with pytest pytest test_epub_optimizer.py
(or, inside an env with the deps above installed: pytest test_epub_optimizer.py)
"""

from __future__ import annotations

import io
import zipfile

import pytest
from PIL import Image

import epub_optimizer as eo

# ============================================================================
# Utility / text functions
# ============================================================================


class TestResolvePath:
    def test_relative_sibling(self):
        assert eo.resolve_path("OEBPS/chapter1.xhtml", "images/cover.png") == "OEBPS/images/cover.png"

    def test_dot_slash_prefix(self):
        assert eo.resolve_path("OEBPS/chapter1.xhtml", "./style.css") == "OEBPS/style.css"

    def test_parent_dir_climbs_out(self):
        assert eo.resolve_path("OEBPS/text/chapter1.xhtml", "../images/cover.png") == "OEBPS/images/cover.png"

    def test_absolute_href_strips_leading_slash(self):
        assert eo.resolve_path("OEBPS/chapter1.xhtml", "/OEBPS/images/cover.png") == "OEBPS/images/cover.png"

    def test_no_base_dir(self):
        assert eo.resolve_path("content.opf", "images/cover.png") == "images/cover.png"

    def test_collapses_duplicate_slashes(self):
        assert eo.resolve_path("OEBPS//chapter1.xhtml", "images/cover.png") == "OEBPS/images/cover.png"


class TestDecodeHref:
    def test_decodes_percent_escapes(self):
        assert eo.decode_href("cover%20image.png") == "cover image.png"

    def test_invalid_escape_returns_original(self):
        assert eo.decode_href("100%") == "100%"


class TestIsInternalEpubLink:
    @pytest.mark.parametrize(
        "href,expected",
        [
            ("chapter2.xhtml#note1", True),
            ("#note1", True),
            ("https://example.com", False),
            ("mailto:a@b.com", False),
            ("javascript:void(0)", False),
            (None, False),
            ("", False),
        ],
    )
    def test_classification(self, href, expected):
        assert eo.is_internal_epub_link(href) is expected


class TestSafeReadText:
    def test_plain_utf8(self):
        assert eo.safe_read_text("hello".encode("utf-8")) == "hello"

    def test_strips_utf8_bom(self):
        assert eo.safe_read_text(b"\xef\xbb\xbf<html/>") == "<html/>"

    def test_decodes_non_ascii(self):
        assert eo.safe_read_text("café".encode("utf-8")) == "café"


# ============================================================================
# Image math
# ============================================================================


class TestJsRound:
    def test_half_rounds_up(self):
        assert eo.js_round(2.5) == 3

    def test_rounds_down_below_half(self):
        assert eo.js_round(2.4) == 2


class TestJsRound4:
    def test_rounds_to_four_places(self):
        assert eo._js_round4(1 / 3) == 0.3333

    def test_negative_rounds_away_from_zero(self):
        assert eo._js_round4(-0.00005) == -0.0001


class TestLengthAsEm:
    def test_converts_px_to_em_string(self):
        assert eo._length_as_em(24.0, 16.0) == "1.5em"

    def test_zero_px_is_zero_em(self):
        assert eo._length_as_em(0.0, 16.0) == "0em"

    def test_none_inputs_return_none(self):
        assert eo._length_as_em(None, 16.0) is None
        assert eo._length_as_em(12.0, 0.0) is None


class TestNormalizeImageLength:
    """Unlike _length_as_em, image width/height must stay in a unit the
    firmware's CssLength natively resolves per-orientation (px/em/rem/pt/%,
    see CssStyle.h:CssLength.toPixels) rather than being pre-resolved to a
    fixed em/px number at optimize time."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("100%", "100%"),
            ("33.333%", "33.333%"),
            ("1.8em", "1.8em"),
            ("2rem", "2rem"),
            ("12pt", "12pt"),
            ("240px", "240px"),
        ],
    )
    def test_native_units_pass_through_verbatim(self, value, expected):
        assert eo._normalize_image_length(value) == expected

    def test_inches_resolved_to_absolute_px(self):
        assert eo._normalize_image_length("1in") == "96px"

    def test_cm_resolved_to_absolute_px(self):
        assert eo._normalize_image_length("2.54cm") == "96px"

    def test_mm_resolved_to_absolute_px(self):
        assert eo._normalize_image_length("25.4mm") == "96px"

    @pytest.mark.parametrize("value", ["auto", "0", "0%", "0px", "-5px", "calc(50% - 10px)", "inherit"])
    def test_unsupported_or_non_positive_returns_none(self, value):
        assert eo._normalize_image_length(value) is None


class TestBoldItalicWeight:
    @pytest.mark.parametrize("weight,expected", [("700", True), ("400", False), ("bold", True), ("bolder", True), ("normal", False)])
    def test_is_bold_weight(self, weight, expected):
        assert eo.is_bold_weight(weight) is expected

    @pytest.mark.parametrize("style,expected", [("italic", True), ("oblique 10deg", True), ("normal", False)])
    def test_is_italic_style(self, style, expected):
        assert eo.is_italic_style(style) is expected


class TestApplyGrayscale:
    def test_luminance_formula(self):
        img = Image.new("RGB", (1, 1), (255, 0, 0))  # pure red
        gray = eo.apply_grayscale(img)
        expected = round(255 * 0.299)
        px = gray.getpixel((0, 0))
        assert abs(px[0] - expected) <= 1
        assert px[0] == px[1] == px[2]

    def test_white_stays_white(self):
        img = Image.new("RGB", (2, 2), (255, 255, 255))
        gray = eo.apply_grayscale(img)
        assert all(lo == hi == 255 for lo, hi in gray.getextrema())


class TestSplitPositions:
    def test_multiple_parts_overlap_and_cover_total_width(self):
        positions = eo._split_positions(900, 480, 5, ltr=True)
        assert len(positions) >= 2
        assert positions[0] == 0
        assert positions[-1] + 480 >= 900

    def test_rtl_reverses_order(self):
        ltr = eo._split_positions(900, 480, 5, ltr=True)
        rtl = eo._split_positions(900, 480, 5, ltr=False)
        assert rtl == list(reversed(ltr))


class TestShouldSkipAutoCrop:
    def test_skipped_when_auto_crop_disabled(self):
        opts = eo.OptimizeOptions(auto_crop=False)
        assert eo.should_skip_auto_crop("cover.png", 600, 800, opts) is True

    def test_skipped_for_protected_path(self):
        opts = eo.OptimizeOptions(auto_crop=True, protected_paths={"cover.png"})
        assert eo.should_skip_auto_crop("cover.png", 600, 800, opts) is True

    def test_skipped_below_min_dimension(self):
        opts = eo.OptimizeOptions(auto_crop=True)
        assert eo.should_skip_auto_crop("icon.png", 100, 100, opts) is True

    def test_allowed_for_large_unprotected_image(self):
        opts = eo.OptimizeOptions(auto_crop=True)
        assert eo.should_skip_auto_crop("figure.png", 600, 800, opts) is False


class TestContainerMaxDimensions:
    """opts.container_max_width/height bound the on-device text/image
    container at any reachable screen-margin setting (SCREEN_MARGIN_MIN_PX
    mirrors CrossPointSettings::SCREEN_MARGIN_MIN) -- narrower than the raw
    device box in DEVICE_PROFILES."""

    def test_x4_subtracts_min_margin_from_both_sides(self):
        opts = eo.OptimizeOptions(device="X4")
        assert opts.container_max_width == opts.max_width - 2 * eo.SCREEN_MARGIN_MIN_PX
        assert opts.container_max_height == opts.max_height - 2 * eo.SCREEN_MARGIN_MIN_PX

    def test_x3_subtracts_min_margin_from_both_sides(self):
        opts = eo.OptimizeOptions(device="X3")
        assert opts.container_max_width == opts.max_width - 2 * eo.SCREEN_MARGIN_MIN_PX
        assert opts.container_max_height == opts.max_height - 2 * eo.SCREEN_MARGIN_MIN_PX


# ============================================================================
# CSS squash
# ============================================================================


class TestRelevantFacetsForElement:
    def test_header_tags_exclude_font_weight(self):
        el = eo.parse_xml("<h1/>")
        facets = eo.relevant_facets_for_element(el, "h1")
        assert facets is not None
        assert "fontWeight" not in facets
        assert facets["textAlign"] is True

    def test_block_tags_include_font_weight(self):
        el = eo.parse_xml("<p/>")
        facets = eo.relevant_facets_for_element(el, "p")
        assert facets["fontWeight"] is True

    def test_bold_tags_exclude_font_weight(self):
        el = eo.parse_xml("<strong/>")
        facets = eo.relevant_facets_for_element(el, "strong")
        assert "fontWeight" not in facets

    def test_internal_anchor_returns_none(self):
        el = eo.parse_xml('<a href="#note1"/>')
        assert eo.relevant_facets_for_element(el, "a") is None

    def test_external_anchor_returns_generic_facets(self):
        el = eo.parse_xml('<a href="https://example.com"/>')
        facets = eo.relevant_facets_for_element(el, "a")
        assert facets is not None
        assert facets["fontWeight"] is True


class TestSquashCssToClasses:
    def _xhtml(self, css_link: str = "", inline_style: str = "", body: str = "") -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title>'
            f"{css_link}{inline_style}</head><body>{body}</body></html>"
        )

    def test_bakes_declaration_and_strips_class(self):
        css = {"OEBPS/style.css": "p.note { font-weight: bold; }"}
        xhtml = self._xhtml(
            css_link='<link rel="stylesheet" href="style.css"/>',
            body='<p class="note">hi</p>',
        )
        out = eo.squash_css_to_classes(xhtml, "OEBPS/chapter1.xhtml", css)
        assert 'style="font-weight:bold"' in out
        assert "class=" not in out
        assert "<link" not in out

    def test_bakes_image_width_percent_verbatim_not_resolved_to_em(self):
        # Percent must stay a percent (resolves against the *live*
        # per-orientation container width on-device); resolving it to `em`
        # like block-level box props would freeze the wrong physical size.
        # vertical-align:super also confirms img is no longer shadowed by
        # SKIP_TAGS (it used to get zero baked style at all, ever).
        css = {"OEBPS/style.css": ".bar { width: 100%; height: auto; vertical-align: super; }"}
        xhtml = self._xhtml(
            css_link='<link rel="stylesheet" href="style.css"/>',
            body='<img class="bar" src="images/bar.jpg"/>',
        )
        out = eo.squash_css_to_classes(xhtml, "OEBPS/chapter1.xhtml", css)
        assert "width:100%" in out
        assert "vertical-align:super" in out
        assert "height:auto" not in out  # "auto" isn't a length token; _normalize_image_length drops it
        assert "class=" not in out

    def test_bakes_image_width_em_verbatim(self):
        css = {"OEBPS/style.css": ".icon { width: 1.8em; height: 1.8em; }"}
        xhtml = self._xhtml(
            css_link='<link rel="stylesheet" href="style.css"/>',
            body='<img class="icon" src="images/icon.jpg"/>',
        )
        out = eo.squash_css_to_classes(xhtml, "OEBPS/chapter1.xhtml", css)
        assert "width:1.8em" in out
        assert "height:1.8em" in out

    def test_same_asset_keeps_its_own_per_chapter_css_size(self):
        # The offline pixel pre-bake (compute_image_render_targets) unions
        # every usage of a shared asset into one raster, but each chapter's
        # own squashed <img> style must keep *its own* requested size, not
        # get collapsed to the largest usage anywhere in the book.
        css_small = {"OEBPS/style.css": ".icon { width: 1.8em; }"}
        css_large = {"OEBPS/style.css": ".hero { width: 100%; }"}
        small_out = eo.squash_css_to_classes(
            self._xhtml(css_link='<link rel="stylesheet" href="style.css"/>', body='<img class="icon" src="images/shared.jpg"/>'),
            "OEBPS/chapter1.xhtml",
            css_small,
        )
        large_out = eo.squash_css_to_classes(
            self._xhtml(css_link='<link rel="stylesheet" href="style.css"/>', body='<img class="hero" src="images/shared.jpg"/>'),
            "OEBPS/chapter2.xhtml",
            css_large,
        )
        assert "width:1.8em" in small_out and "width:100%" not in small_out
        assert "width:100%" in large_out and "width:1.8em" not in large_out

    def test_drops_style_tag(self):
        xhtml = self._xhtml(inline_style="<style>p{font-style:italic}</style>", body="<p>hi</p>")
        out = eo.squash_css_to_classes(xhtml, "OEBPS/chapter1.xhtml", {})
        assert "<style" not in out
        assert 'style="font-style:italic"' in out

    def test_fails_open_on_malformed_markup(self):
        broken = "<html><head><title>t</title></head><body><p>unclosed"
        out = eo.squash_css_to_classes(broken, "OEBPS/chapter1.xhtml", {})
        assert out == broken

    def test_noop_without_head_or_rules(self):
        xhtml = self._xhtml(body="<p>hi</p>")
        out = eo.squash_css_to_classes(xhtml, "OEBPS/chapter1.xhtml", {})
        assert "style=" not in out

    def test_strips_class_from_body_itself(self):
        # `elements` only walks body's *descendants* - body's own class
        # attribute needs an explicit strip or it survives as dead cruft
        # once the stylesheet that declared it is gone.
        xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title>'
            '<link rel="stylesheet" href="style.css"/></head>'
            '<body class="class6"><p>hi</p></body></html>'
        )
        out = eo.squash_css_to_classes(xhtml, "OEBPS/chapter1.xhtml", {"OEBPS/style.css": ".class6 { color: red; }"})
        assert "class=" not in out


class TestComputeImageRenderTargets:
    """Regression coverage for the horizontal-divider scenario: an <img>
    styled `width:100%; height:auto` must pre-bake to the margin-aware
    container box (opts.container_max_width/height), not the raw device box
    -- css-squash strips the CSS before the firmware ever sees it, so every
    image renders through ChapterHtmlSlimParser's CSS-less scale-to-fit-
    container fallback regardless of what the source book requested."""

    def _xhtml(self, css: str, img: str) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title>'
            f"<style>{css}</style></head><body>{img}</body></html>"
        )

    def test_percent_width_image_bounded_by_container_not_device_box(self):
        opts = eo.OptimizeOptions(device="X4")
        xhtml = self._xhtml(".bar{width:100%;height:auto}", '<img class="bar" src="images/bar.jpg"/>')
        targets = eo.compute_image_render_targets({"OEBPS/chapter1.xhtml": xhtml}, {}, opts)
        assert targets["OEBPS/images/bar.jpg"] == (opts.container_max_width, opts.container_max_height)
        assert targets["OEBPS/images/bar.jpg"][0] != opts.max_width  # would be 480 with the old, non-margin-aware box

    def test_image_with_no_css_box_also_bounded_by_container(self):
        opts = eo.OptimizeOptions(device="X4")
        xhtml = self._xhtml("", '<img src="images/plain.jpg"/>')
        targets = eo.compute_image_render_targets({"OEBPS/chapter1.xhtml": xhtml}, {}, opts)
        assert targets["OEBPS/images/plain.jpg"] == (opts.container_max_width, opts.container_max_height)


class TestExpandBoxShorthand:
    def test_single_value_applies_to_all_sides(self):
        assert eo._expand_box_shorthand("1em") == ("1em", "1em", "1em", "1em")

    def test_two_values_vertical_horizontal(self):
        assert eo._expand_box_shorthand("1em 2em") == ("1em", "2em", "1em", "2em")

    def test_four_values_top_right_bottom_left(self):
        assert eo._expand_box_shorthand("1em 2em 3em 4em") == ("1em", "2em", "3em", "4em")

    def test_calc_expands_structurally_but_does_not_resolve_to_a_length(self):
        # calc() gets a longhand slot like a real CSSOM would, but it can't be
        # resolved to a px length without evaluating arithmetic - downstream
        # `_parse_length_px` is what actually drops it, not this function.
        expanded = eo._expand_box_shorthand("calc(1em + 2px)")
        assert expanded == ("calc(1em + 2px)",) * 4
        assert eo._parse_length_px(expanded[0], 16.0, None) is None

    def test_auto_keeps_sibling_lengths_resolvable(self):
        # `margin: 1.4em auto` (vertical margin + horizontal auto-centering)
        # must still expose top/bottom as real lengths; only left/right (the
        # 'auto' side) fail to resolve downstream. Regression test: an
        # earlier version rejected the whole shorthand whenever any side
        # wasn't a plain length, silently dropping valid margin-top/bottom.
        expanded = eo._expand_box_shorthand("1.4em auto")
        assert expanded == ("1.4em", "auto", "1.4em", "auto")
        assert eo._parse_length_px(expanded[0], 16.0, None) == 22.4
        assert eo._parse_length_px(expanded[1], 16.0, None) is None


class TestParseLengthPxPercentAsRawNumber:
    def test_text_indent_percentage_treated_as_raw_px(self):
        # Browser quirk (verified against real Chrome): getComputedStyle()
        # does not resolve a percentage text-indent to a used px value the
        # way it does for margin/padding - it hands back the specified
        # percentage string unchanged, and the browser version's
        # `lengthAsEm` then does `parseFloat(cs.textIndent)`, which strips
        # the '%' and divides the bare number by font-size-px as if it were
        # already in px. This is the only path that lets text-indent
        # percentages (a common hanging-indent pattern) bake at all, since
        # we never run real layout to resolve a percentage against a
        # container width.
        assert eo._parse_length_px("-5.769%", 16.0, None, percent_as_raw_number=True) == -5.769

    def test_percentage_without_raw_number_mode_is_unresolvable(self):
        assert eo._parse_length_px("-5.769%", 16.0, None) is None


# ============================================================================
# OPF / NCX fixups
# ============================================================================


class TestExtractIdentifier:
    def test_extracts_unique_identifier(self):
        opf = (
            '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="bookid">urn:uuid:abc-123</dc:identifier>'
            "</metadata></package>"
        )
        assert eo.extract_identifier(opf) == "urn:uuid:abc-123"

    def test_missing_identifier_returns_none(self):
        opf = '<package xmlns="http://www.idpf.org/2007/opf"><metadata/></package>'
        assert eo.extract_identifier(opf) is None


class TestSyncNcxIdentifier:
    def test_updates_stale_dtb_uid(self):
        ncx = '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><head><meta name="dtb:uid" content="old"/></head></ncx>'
        out = eo.sync_ncx_identifier(ncx, "urn:uuid:new-id")
        assert 'content="urn:uuid:new-id"' in out
        assert 'content="old"' not in out

    def test_none_identifier_is_noop(self):
        ncx = '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><head><meta name="dtb:uid" content="old"/></head></ncx>'
        assert eo.sync_ncx_identifier(ncx, None) == ncx


class TestFixOpf:
    def test_split_image_rewrites_manifest_item(self):
        opf = (
            '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">u</dc:identifier></metadata>'
            '<manifest><item id="img1" href="images/wide.png" media-type="image/png"/></manifest>'
            "</package>"
        )
        split_images = {
            "OEBPS/images/wide.png": {
                "origName": "wide.png",
                "origDir": "OEBPS/images",
                "parts": [
                    {"path": "OEBPS/images/wide_part1.jpg", "imgName": "wide_part1.jpg", "id": "wide_part1", "suffix": "_part1"},
                    {"path": "OEBPS/images/wide_part2.jpg", "imgName": "wide_part2.jpg", "id": "wide_part2", "suffix": "_part2"},
                ],
            }
        }
        out = eo.fix_opf(opf, opf, "OEBPS", split_images, drop_css_items=False)
        assert 'href="images/wide_part1.jpg"' in out
        assert 'href="images/wide_part2.jpg"' in out
        assert 'href="images/wide.png"' not in out

    def test_drop_css_items_removes_stylesheet_manifest_entries(self):
        opf = (
            '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">u</dc:identifier></metadata>'
            '<manifest><item id="css1" href="style.css" media-type="text/css"/>'
            '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>'
            "</package>"
        )
        out = eo.fix_opf(opf, opf, "", {}, drop_css_items=True)
        assert "style.css" not in out
        assert "chapter1.xhtml" in out


# ============================================================================
# End-to-end synthetic EPUB
# ============================================================================


class TestConvertEpubBytesEndToEnd:
    def test_self_test_suite_passes(self):
        assert eo._run_self_test() is True

    def test_wide_image_gets_split_into_multiple_parts(self):
        # H-Split scales width to MAX_HEIGHT then rotates; only images whose
        # rotated width still exceeds MAX_WIDTH actually split (see
        # process_image / FilesPage.html:4613-4662), hence height/width > 0.6.
        img = Image.new("RGB", (1600, 1200), "white")
        png_buf = io.BytesIO()
        img.save(png_buf, format="PNG")


        opf = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">u</dc:identifier></metadata>'
            "<manifest>"
            '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="img1" href="images/wide.png" media-type="image/png"/>'
            "</manifest>"
            '<spine><itemref idref="ch1"/></spine></package>'
        )
        xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
            '<body><img src="images/wide.png" width="1600" height="1200"/></body></html>'
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", (
                '<?xml version="1.0"?><container version="1.0" '
                'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>'
            ))
            z.writestr("OEBPS/content.opf", opf)
            z.writestr("OEBPS/chapter1.xhtml", xhtml)
            z.writestr("OEBPS/images/wide.png", png_buf.getvalue())

        opts = eo.OptimizeOptions(device="X4", split_states={"OEBPS/images/wide.png": eo.STATE_HSPLIT})
        out, stats = eo.convert_epub_bytes(buf.getvalue(), opts)
        assert stats.images_split == 1

        zout = zipfile.ZipFile(io.BytesIO(out))
        part_names = [n for n in zout.namelist() if "wide_part" in n]
        assert len(part_names) >= 2

        xhtml_out = zout.read("OEBPS/chapter1.xhtml").decode("utf-8")
        assert xhtml_out.count("<img") == len(part_names)
        assert 'width="1600"' not in xhtml_out

        opf_out = zout.read("OEBPS/content.opf").decode("utf-8")
        for name in part_names:
            assert eo._basename(name) in opf_out
        assert "wide.png" not in opf_out

    def test_output_is_valid_zip_with_mimetype_first(self):
        src = eo._build_synthetic_epub()
        out, _ = eo.convert_epub_bytes(src, eo.OptimizeOptions())
        zout = zipfile.ZipFile(io.BytesIO(out))
        assert zout.testzip() is None
        assert zout.namelist()[0] == "mimetype"
        assert zout.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
