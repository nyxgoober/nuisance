#!/usr/bin/env python3
"""
romini.py — Japanese romanizer for TTML, LRC, and Enhanced LRC
Romanizes lyric text while preserving all line and syllable/word timestamps.
Romanization is done at the line/<p> block level so kanji compounds read accurately.

Usage:
    python romini.py input.ttml [output.ttml]
    python romini.py input.lrc  [output.lrc]
    If output is omitted, writes to <stem>_roman.<ext>

Requirements:
    pip install pykakasi
"""

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from lrc2ttml import LINE_TAG, META_TAG, WORD_TAG, read_text_file

_kks = None

def _get_kakasi():
    global _kks
    if _kks is None:
        try:
            import pykakasi
        except ImportError:
            print(
                "romini: pykakasi is not installed. Install with:\n"
                "  pip install pykakasi",
                file=sys.stderr,
            )
            sys.exit(1)
        _kks = pykakasi.kakasi()
    return _kks


# ── Correction tables ──────────────────────────────────────────────────────────

def fix_chunk(orig, roman, nxt_orig, prev_orig):
    if orig == "孕":                            return "hara"     # 孕む → はらむ
    if orig == "哭"  and "い" in nxt_orig:      return "na"       # 哭く → 泣く
    if orig == "囁"  and "い" in nxt_orig:      return "sasaya"   # 囁く → ささやく
    if orig == "世"  and nxt_orig == "迷言":    return "yomai"    # 世迷言 (pt.1)
    if orig == "迷言" and prev_orig == "世":    return "goto"     # 世迷言 (pt.2)
    return roman


ROMAN_CORRECTIONS = [
    # add ("wrong", "right") pairs here as needed
]

_NO_SPACE_BEFORE = set(".,!?;:)]}、。！？」）』】…・〜~ ")
_NO_SPACE_AFTER  = set("([{「（『【 ")


def _needs_space(cur_roman, nxt_roman):
    if not cur_roman or not nxt_roman:
        return False
    if cur_roman[-1] in _NO_SPACE_AFTER or cur_roman[-1] == " ":
        return False
    if nxt_roman[0] in _NO_SPACE_BEFORE:
        return False
    return True


# ── Core romanization ──────────────────────────────────────────────────────────

def romanize_block(full_text):
    """Romanize a full line/<p> text as one unit. Returns list of (orig, roman_with_space)."""
    kks = _get_kakasi()
    items = kks.convert(full_text)
    raw_chunks = []
    for i, item in enumerate(items):
        orig  = item["orig"]
        roman = item["hepburn"]
        nxt   = items[i + 1]["orig"] if i + 1 < len(items) else ""
        prev  = items[i - 1]["orig"] if i > 0              else ""
        raw_chunks.append((orig, apply_corrections(fix_chunk(orig, roman, nxt, prev))))

    result = []
    for i, (orig, roman) in enumerate(raw_chunks):
        nxt_roman = raw_chunks[i + 1][1] if i + 1 < len(raw_chunks) else ""
        if _needs_space(roman, nxt_roman):
            roman = roman + " "
        result.append((orig, roman))
    return result


def assign_to_spans(span_texts, chunks):
    """
    Assign each pykakasi chunk's roman to the span containing its first character.
    Returns (assigned_texts, last_span_indices) so multi-span compounds can extend
    their lead span's end timestamp across absorbed empty spans.
    """
    n = len(span_texts)
    results = [""] * n
    end_span = list(range(n))

    pos_to_span = {}
    pos = 0
    for si, text in enumerate(span_texts):
        for _ in text:
            pos_to_span[pos] = si
            pos += 1

    chunk_pos = 0
    for orig, roman in chunks:
        si = pos_to_span.get(chunk_pos)
        last_si = pos_to_span.get(max(chunk_pos, chunk_pos + len(orig) - 1), si)
        if si is not None:
            results[si] += roman
            if last_si is not None and last_si > end_span[si]:
                end_span[si] = last_si
        chunk_pos += len(orig)
    return results, end_span


def apply_corrections(roman):
    for src, dst in ROMAN_CORRECTIONS:
        roman = roman.replace(src, dst)
    return roman


def romanize_plain_line(text):
    chunks = romanize_block(text)
    return "".join(r for _, r in chunks).strip()


# ── TTML processing ────────────────────────────────────────────────────────────

TTML = "http://www.w3.org/ns/ttml"
TTM  = "http://www.w3.org/ns/ttml#metadata"
XML  = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("",    TTML)
ET.register_namespace("ttm", TTM)
ET.register_namespace("xml", XML)


def _local_tag(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def romanize_ttml(input_path, output_path):
    text = read_text_file(input_path)
    root = ET.fromstring(text.lstrip("\ufeff"))
    tree = ET.ElementTree(root)

    for p in root.iter():
        if _local_tag(p.tag) != "p":
            continue
        spans = [ch for ch in p if _local_tag(ch.tag) == "span"]
        if spans:
            span_texts = [(sp.text or "").replace("\r", "").replace("\n", "") for sp in spans]
            full = "".join(span_texts)
            if not full.strip():
                continue
            chunks = romanize_block(full)
            assigned, end_span = assign_to_spans(span_texts, chunks)
            orig_ends = [sp.attrib.get("end") for sp in spans]
            for idx, (sp, roman) in enumerate(zip(spans, assigned)):
                if roman:
                    sp.text = roman
                    sp.set(f"{{{XML}}}space", "preserve")
                    target_end = orig_ends[end_span[idx]]
                    if target_end:
                        sp.set("end", target_end)
                else:
                    sp.text = None
        else:
            full = "".join(p.itertext()).strip()
            if full:
                p.text = romanize_plain_line(full)

    root.set(f"{{{XML}}}lang", "ja-Latn")

    for el in root.iter():
        if _local_tag(el.tag) == "title" and el.text:
            orig = el.text.strip()
            if "(Romanized)" not in orig:
                el.text = f"{orig} (Romanized)"
            break

    tree.write(output_path, xml_declaration=True, encoding="UTF-8")
    print(f"romini: {input_path} → {output_path}")


# ── LRC & Enhanced LRC processing ──────────────────────────────────────────────

def _romanize_lrc_content(content):
    word_tags = list(WORD_TAG.finditer(content))
    if not word_tags:
        stripped = content.strip()
        return romanize_plain_line(stripped) if stripped else ""

    # Build segments: optional leading text before first <tag>, then each <tag>text
    segs = []  # list of (tag_str_or_empty, seg_text)
    lead = content[: word_tags[0].start()]
    if lead:
        segs.append(("", lead))
    trailing_end_tag = ""
    for wi, wt in enumerate(word_tags):
        t_str = wt.group(0)
        s0 = wt.end()
        s1 = word_tags[wi + 1].start() if wi + 1 < len(word_tags) else len(content)
        seg_txt = content[s0:s1]
        if wi + 1 == len(word_tags) and not seg_txt.strip():
            trailing_end_tag = t_str
        else:
            segs.append((t_str, seg_txt))

    span_texts = [txt for _, txt in segs]
    full = "".join(span_texts)
    if not full.strip():
        return content

    chunks = romanize_block(full)
    assigned, _ = assign_to_spans(span_texts, chunks)
    out_parts = []
    for (t_str, _), rom in zip(segs, assigned):
        if rom:
            out_parts.append(f"{t_str}{rom}")
    if trailing_end_tag:
        out_parts.append(trailing_end_tag)
    return "".join(out_parts)


def romanize_lrc(input_path, output_path):
    text = read_text_file(input_path)
    out_lines = []

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            out_lines.append("")
            continue

        tags = list(LINE_TAG.finditer(stripped))
        if tags and tags[0].start() == 0:
            prefix = stripped[: tags[-1].end()]
            content = stripped[tags[-1].end() :]
            out_lines.append(prefix + _romanize_lrc_content(content))
            continue

        mm = META_TAG.match(stripped)
        if mm and mm.group(1).lower() == "ti":
            ti = mm.group(2).strip()
            if ti and "(Romanized)" not in ti:
                out_lines.append(f"[ti:{ti} (Romanized)]")
                continue

        out_lines.append(raw)

    Path(output_path).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"romini: {input_path} → {output_path}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="romini.py",
        description="Romanize Japanese lyrics in TTML, LRC, or Enhanced LRC files."
    )
    parser.add_argument("input", help="Input .ttml or .lrc file")
    parser.add_argument("output", nargs="?", default=None,
                        help="Output file (default: <input_stem>_roman.<ext>)")
    args = parser.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        print(f"romini: file not found: {inp}", file=sys.stderr)
        sys.exit(1)

    out = Path(args.output) if args.output else inp.with_stem(inp.stem + "_roman")

    sample = read_text_file(inp).lstrip("\ufeff").lstrip()
    if sample.startswith("<?xml") or sample.lower().startswith("<tt") or inp.suffix.lower() in (".ttml", ".xml"):
        romanize_ttml(inp, out)
    else:
        romanize_lrc(inp, out)


if __name__ == "__main__":
    main()
