#!/usr/bin/env python3
"""
romini.py — TTML Japanese romanizer
Romanizes span text in a TTML file while preserving all timestamps.
Romanization is done at the <p> block level so compounds read correctly.

Usage:
    romini.py input.ttml [output.ttml]
    If output is omitted, writes to input_roman.ttml

Requirements:
    pip install pykakasi
"""

import sys
import xml.etree.ElementTree as ET
import pykakasi
from pathlib import Path

kks = pykakasi.kakasi()

# ── Correction tables ──────────────────────────────────────────────────────────

# Per-chunk corrections: fix kanji that pykakasi misreads in context.
# Called once per pykakasi output chunk with access to prev/next chunk.
def fix_chunk(orig, roman, nxt_orig, prev_orig):
    if orig == "孕":                            return "hara"     # 孕む → はらむ
    if orig == "哭"  and "い" in nxt_orig:      return "na"       # 哭く → 泣く
    if orig == "囁"  and "い" in nxt_orig:      return "sasaya"   # 囁く → ささやく
    if orig == "世"  and nxt_orig == "迷言":    return "yomai"    # 世迷言 (pt.1)
    if orig == "迷言" and prev_orig == "世":    return "goto"     # 世迷言 (pt.2)
    return roman

# String-level corrections applied to each span's final roman output.
# Catches anything fix_chunk can't handle due to cross-span chunk splits.
ROMAN_CORRECTIONS = [
    # add more ("wrong", "right") pairs here as needed
]

# ── Core romanization ──────────────────────────────────────────────────────────

def romanize_block(full_text):
    """Romanize a full <p> text as one unit. Returns list of (orig, roman)."""
    items = kks.convert(full_text)
    result = []
    for i, item in enumerate(items):
        orig  = item["orig"]
        roman = item["hepburn"]
        nxt   = items[i + 1]["orig"] if i + 1 < len(items) else ""
        prev  = items[i - 1]["orig"] if i > 0              else ""
        result.append((orig, fix_chunk(orig, roman, nxt, prev)))
    return result

def assign_to_spans(span_texts, chunks):
    """
    Assign each pykakasi chunk's roman to the span that contains the
    chunk's first character. Multi-char chunks land on their lead span;
    subsequent spans in the group stay empty (correct for left-to-right wipe).
    """
    results = [""] * len(span_texts)
    pos_to_span = {}
    pos = 0
    for si, text in enumerate(span_texts):
        for _ in text:
            pos_to_span[pos] = si
            pos += 1
    chunk_pos = 0
    for orig, roman in chunks:
        si = pos_to_span.get(chunk_pos)
        if si is not None:
            results[si] += roman
        chunk_pos += len(orig)
    return results

def apply_corrections(roman):
    for src, dst in ROMAN_CORRECTIONS:
        roman = roman.replace(src, dst)
    return roman

# ── TTML processing ────────────────────────────────────────────────────────────

NS   = {"tt": "http://www.w3.org/ns/ttml"}
TTML = "http://www.w3.org/ns/ttml"
TTM  = "http://www.w3.org/ns/ttml#metadata"
XML  = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("",    TTML)
ET.register_namespace("ttm", TTM)
ET.register_namespace("xml", XML)

def romanize_ttml(input_path, output_path):
    tree = ET.parse(input_path)
    root = tree.getroot()

    for p in root.findall(".//tt:p", NS):
        spans = p.findall("tt:span", NS)
        if not spans:
            continue
        span_texts = [(sp.text or "").strip() for sp in spans]
        full = "".join(span_texts)
        if not full:
            continue
        chunks   = romanize_block(full)
        assigned = assign_to_spans(span_texts, chunks)
        for sp, roman in zip(spans, assigned):
            sp.text = apply_corrections(roman) or None

    root.set(f"{{{XML}}}lang", "ja-Latn")

    title_el = root.find(f"tt:head/{{{TTM}}}title", NS)
    if title_el is not None and title_el.text:
        orig = title_el.text.strip()
        title_el.text = f"{orig} (Romanized)"

    tree.write(output_path, xml_declaration=True, encoding="UTF-8")
    print(f"romini: {input_path} → {output_path}")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    inp = Path(sys.argv[1])
    if not inp.exists():
        print(f"romini: file not found: {inp}", file=sys.stderr)
        sys.exit(1)

    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp.with_stem(inp.stem + "_roman")
    romanize_ttml(inp, out)
