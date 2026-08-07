#!/usr/bin/env python3
"""Generates a test EPUB with spine items of deliberately different sizes,
to exercise crosspoint-reader's per-section page-build cache across the
full range: a single-page item, a few small chapters, one giant chapter
(enough text to lay out several hundred pages -- forces the bounded
page-LUT RAM window to actually evict and read back from its spill file),
and a normal chapter again afterward.

Structure/requirements confirmed against crosspoint-reader's own parser
(lib/Epub/Epub/parsers/{ContainerParser,ContentOpfParser,TocNavParser,
TocNcxParser}.cpp): manifest must precede spine in content.opf; NCX
navPoint needs navLabel before content; both TOC formats are optional
but included here for robustness.
"""
import zipfile
import random
import sys

OUT_PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/section-size-test.epub"

random.seed(42)

# Varied sentence fragments -- avoids one repeated string so layout/hyphenation
# see realistic variety, not a single degenerate pattern.
SUBJECTS = ["The keeper", "A distant bell", "Her old notebook", "The harbor light",
            "Every winter", "The council", "A stray dog", "The river", "His father",
            "The last train", "A quiet argument", "The garden wall", "Their letters"]
VERBS = ["returned to", "forgot about", "argued over", "waited beside", "circled",
         "measured", "abandoned", "rebuilt", "misread", "carried", "outlived",
         "questioned", "photographed"]
OBJECTS = ["the empty square", "a promise no one wrote down", "the second bridge",
           "an unopened letter", "the north road", "three broken clocks",
           "the harbor at dusk", "a debt no one mentioned", "the family's name",
           "the last of the candles", "a map with no legend", "the schoolhouse"]
TAILS = ["and nothing came of it.", "though few believed it.", "as the light failed.",
         "for reasons no one recorded.", "and never spoke of it again.",
         "while the town slept.", "against every warning given.",
         "as if it mattered.", "and called it settled."]


def make_paragraph(rng):
    sentence_count = rng.randint(3, 6)
    sentences = []
    for _ in range(sentence_count):
        s = f"{rng.choice(SUBJECTS)} {rng.choice(VERBS)} {rng.choice(OBJECTS)} {rng.choice(TAILS)}"
        sentences.append(s)
    return " ".join(sentences)


def make_chapter_html(title, paragraph_count, rng):
    paras = "\n".join(f"    <p>{make_paragraph(rng)}</p>" for _ in range(paragraph_count))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title></head>
<body>
    <h1>{title}</h1>
{paras}
</body>
</html>"""


# (id, filename, title, paragraph_count, in_toc)
# Paragraph counts are rough (~1-2 short paragraphs per rendered page at
# typical reader settings) -- the giant chapter is sized generously past
# any plausible page-LUT RAM window to force spill-file eviction/read-back.
CHAPTERS = [
    ("titlepage", "titlepage.xhtml", "Section Size Test", 1, True),
    ("preface", "preface.xhtml", "Preface", 4, True),
    ("ch1", "chapter1.xhtml", "Chapter 1: A Small Beginning", 12, True),
    ("ch2", "chapter2.xhtml", "Chapter 2: The Long Middle", 400, True),
    ("ch3", "chapter3.xhtml", "Chapter 3: A Normal Chapter Again", 20, True),
    ("afterword", "afterword.xhtml", "Afterword", 1, True),
]

NAV_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Table of Contents</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <ol>
{items}
    </ol>
  </nav>
</body>
</html>"""

NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:section-size-test"/></head>
  <docTitle><text>Section Size Test</text></docTitle>
  <navMap>
{navpoints}
  </navMap>
</ncx>"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:section-size-test</dc:identifier>
    <dc:title>Section Size Test</dc:title>
    <dc:creator>Test Generator</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
{manifest_items}
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx">
{spine_items}
  </spine>
</package>"""


def build():
    rng = random.Random(42)
    manifest_items = []
    spine_items = []
    nav_items = []
    navpoints = []
    chapter_files = {}

    for i, (item_id, filename, title, para_count, in_toc) in enumerate(CHAPTERS):
        html = make_chapter_html(title, para_count, rng)
        chapter_files[filename] = html
        manifest_items.append(f'    <item id="{item_id}" href="{filename}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'    <itemref idref="{item_id}"/>')
        if in_toc:
            nav_items.append(f'      <li><a href="{filename}">{title}</a></li>')
            navpoints.append(
                f'    <navPoint id="navpoint-{i}" playOrder="{i + 1}">\n'
                f'      <navLabel><text>{title}</text></navLabel>\n'
                f'      <content src="{filename}"/>\n'
                f'    </navPoint>'
            )
        size_kb = len(html) / 1024
        print(f"  {filename}: {para_count} paragraphs, {size_kb:.1f} KB")

    opf = OPF.format(manifest_items="\n".join(manifest_items), spine_items="\n".join(spine_items))
    nav = NAV_XHTML.format(items="\n".join(nav_items))
    ncx = NCX.format(navpoints="\n".join(navpoints))

    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

    with zipfile.ZipFile(OUT_PATH, "w") as zf:
        # mimetype first, uncompressed -- not required by this parser (it reads the
        # zip central directory, not sequential order) but keeps the file valid
        # against the wider EPUB spec / other tools.
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", nav)
        zf.writestr("OEBPS/toc.ncx", ncx)
        for filename, html in chapter_files.items():
            zf.writestr(f"OEBPS/{filename}", html)

    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    build()
