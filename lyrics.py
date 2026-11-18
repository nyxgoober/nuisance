#!/usr/bin/env python3
"""
lyrics.py — Time-synced lyric video renderer (TTML, LRC, and Enhanced LRC).
Outputs PNG frame sequences ready for ffmpeg.
"""

import argparse
import bisect
import math
import os
import re
import sys
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from lrc2ttml import parse_lrc, read_text_file

# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════

WIDTH      = 1920
HEIGHT     = 1080
FPS        = 60
OUTPUT_DIR = "frames"

FONT_BOLD    = os.path.join(_HERE, "ZenKakuGothicNew-Bold.ttf")
FONT_REGULAR = os.path.join(_HERE, "ZenKakuGothicNew-Regular.ttf")

MARGIN_LEFT   = 120
MARGIN_RIGHT  = 120
MARGIN_TOP    = 100
LINE_H_IDLE   = 58
LINE_H_ACTIVE = 150
LINE_GAP      = 24

SIZE_IDLE        = 36
SIZE_IDLE_MIN    = 20
SIZE_ACTIVE      = 96
SIZE_ACTIVE_MIN  = 48
SIZE_TITLE       = 80
SIZE_TITLE_MIN   = 36
SIZE_WATERMARK   = 22
SIZE_ROMAN_SCALE = 0.38
SIZE_ROMAN_IDLE  = 22

BG_COLOR      = (8,   8,  12)
GS_COLOR      = (0, 255,   0)   # green screen chroma key colour
TITLE_COLOR   = (180, 180, 190)
WATERMARK_COL = (60,  60,  70)
IDLE_COLOR    = (70,  70,  80)
DONE_COLOR    = (200, 200, 210)
ACTIVE_BASE   = (130, 130, 145)
ACTIVE_SUNG   = (255, 255, 255)
ROMAN_COLOR   = (160, 160, 175)

GROW_IN_DUR   = 0.25
GROW_OUT_DUR  = 0.30
WORD_FADE_DUR = 0.18

# Intermission dots
BREAK_THRESHOLD  = 2.5    # seconds of silence before showing dots
DOT_COUNT        = 3
DOT_RADIUS       = 14
DOT_SPACING      = 52     # center-to-center
DOT_PULSE_PERIOD = 1.40   # seconds for the bright spot to travel across all dots once
DOT_FADE_IN      = 0.50   # group fade-in
DOT_FADE_OUT     = 0.50   # group fade-out
DOT_LINE_H       = 60
DOT_COLOR        = (200, 200, 210)
DOT_DIM          = (55,  55,  65)

SHADOW_OFFSET = 3
SHADOW_BLUR   = 6

# ══════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════

def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else (hi if x > hi else x)

def ease_out(t):
    t = clamp(t)
    u = 1.0 - t
    return 1.0 - u * u * u

def ease_in_out(t):
    t = clamp(t)
    return t * t * (3.0 - 2.0 * t)

def lerp(a, b, t):
    return a + (b - a) * clamp(t)

def lerp_color(ca, cb, t):
    t = clamp(t)
    return (
        int(ca[0] + (cb[0] - ca[0]) * t),
        int(ca[1] + (cb[1] - ca[1]) * t),
        int(ca[2] + (cb[2] - ca[2]) * t),
    )

_OFFSET_TIME_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s|m|h|f)?\s*$")

def parse_time(ts):
    """
    Parse any valid TTML time expression into float seconds:
      - HH:MM:SS.mmm, HH:MM:SS, MM:SS.mm, MM:SS, HH:MM:SS:FF
      - Offset times: 12.345s, 500ms, 2.5m, 1h, 60f
    """
    if ts is None:
        return 0.0
    ts = ts.strip()
    if not ts:
        return 0.0

    m = _OFFSET_TIME_RE.match(ts)
    if m and ":" not in ts:
        val = float(m.group(1))
        unit = m.group(2) or "s"
        if unit == "ms":
            return val / 1000.0
        if unit == "s":
            return val
        if unit == "m":
            return val * 60.0
        if unit == "h":
            return val * 3600.0
        if unit == "f":
            return val / FPS

    parts = ts.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60.0 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600.0 + int(parts[1]) * 60.0 + float(parts[2])
    if len(parts) == 4:
        return (
            int(parts[0]) * 3600.0
            + int(parts[1]) * 60.0
            + int(parts[2])
            + float(parts[3]) / FPS
        )
    return float(ts)


def _local_tag(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# ══════════════════════════════════════════
# TTML & LRC / ENHANCED LRC PARSERS
# ══════════════════════════════════════════

def _is_xml_text(text, path):
    stripped = text.lstrip("\ufeff").lstrip()
    if stripped.startswith("<?xml") or stripped.lower().startswith("<tt"):
        return True
    ext = os.path.splitext(path)[1].lower()
    return ext in (".ttml", ".xml") and stripped.startswith("<")


def _extract_spans_from_p(p_el, p_begin, p_end):
    """
    Extract syllable dicts from a <p> element, preserving inline spaces
    between <span> elements and leading text before the first <span>.
    Returns (syls, is_synced).
    """
    span_children = [ch for ch in p_el if _local_tag(ch.tag) == "span"]
    if not span_children:
        text = "".join(p_el.itertext()).strip()
        if not text:
            return [], False
        return [{"text": text, "begin": p_begin, "end": p_end}], False

    syls = []
    # Leading text directly inside <p> before first <span>
    if p_el.text and p_el.text.strip():
        first_sb = (
            parse_time(span_children[0].attrib["begin"])
            if "begin" in span_children[0].attrib
            else p_begin
        )
        lead = p_el.text.lstrip("\r\n\t ")
        if lead:
            syls.append({"text": lead, "begin": p_begin, "end": max(p_begin, first_sb)})

    for idx, s in enumerate(span_children):
        raw_text = s.text if s.text is not None else ""
        # Fallback if nested tags exist inside <span>
        if not raw_text and len(s):
            raw_text = "".join(s.itertext())

        sb = parse_time(s.attrib["begin"]) if "begin" in s.attrib else p_begin
        if "end" in s.attrib:
            se = parse_time(s.attrib["end"])
        elif "dur" in s.attrib:
            se = sb + parse_time(s.attrib["dur"])
        elif idx + 1 < len(span_children) and "begin" in span_children[idx + 1].attrib:
            se = parse_time(span_children[idx + 1].attrib["begin"])
        else:
            se = p_end
        se = max(sb, se)

        # Inline tail space (e.g. </span> <span>, NOT pretty-printed newline indentation)
        tail_space = ""
        if s.tail and "\n" not in s.tail and "\r" not in s.tail:
            tail_space = s.tail

        if raw_text.strip():
            # Clean accidental newline indentation while preserving intentional single spaces
            clean_text = raw_text.replace("\r", "").replace("\n", "")
            syls.append({"text": clean_text + tail_space, "begin": sb, "end": se})
        elif (raw_text or tail_space) and syls and "\n" not in raw_text:
            # Whitespace-only span between words: attach to previous syllable
            syls[-1]["text"] += raw_text + tail_space

    if not syls:
        text = "".join(p_el.itertext()).strip()
        if text:
            return [{"text": text, "begin": p_begin, "end": p_end}], False
        return [], False

    return syls, True


def load_ttml_text(text):
    root = ET.fromstring(text.lstrip("\ufeff"))

    title = ""
    auto_dur = None
    for el in root.iter():
        lname = _local_tag(el.tag)
        if lname == "title" and el.text and not title:
            title = el.text.strip()
        if lname in ("tt", "body", "div") and auto_dur is None:
            if "dur" in el.attrib:
                auto_dur = parse_time(el.attrib["dur"])
            elif "end" in el.attrib:
                auto_dur = parse_time(el.attrib["end"])

    p_elements = [el for el in root.iter() if _local_tag(el.tag) == "p"]
    raw_p = []
    for p in p_elements:
        if "begin" not in p.attrib:
            continue
        b = parse_time(p.attrib["begin"])
        e = None
        if "end" in p.attrib:
            e = parse_time(p.attrib["end"])
        elif "dur" in p.attrib:
            e = b + parse_time(p.attrib["dur"])
        raw_p.append((b, e, p))

    phrases = []
    for idx, (begin, end, p) in enumerate(raw_p):
        if end is None:
            end = raw_p[idx + 1][0] if idx + 1 < len(raw_p) else begin + 5.0
        end = max(begin + 0.05, end)
        syls, synced = _extract_spans_from_p(p, begin, end)
        if syls:
            phrases.append(
                {
                    "begin": begin,
                    "end": end,
                    "syllables": syls,
                    "synced": synced,
                }
            )

    phrases.sort(key=lambda ph: ph["begin"])
    if phrases:
        inferred = phrases[-1]["end"] + 2.0
        auto_dur = max(auto_dur, inferred) if auto_dur else inferred
    return title, phrases, (auto_dur or 0.0), None


def load_lrc_text(text):
    meta, lines = parse_lrc(text)
    title = meta.get("ti", "").strip()
    phrases = []
    inline_roman = []
    has_any_sub = False

    for line in lines:
        phrases.append(
            {
                "begin": line["begin"],
                "end": line["end"],
                "syllables": line["words"],
                "synced": line.get("synced", len(line["words"]) > 1),
            }
        )
        sub = line.get("sub_words") or []
        if sub:
            has_any_sub = True
        inline_roman.append(
            {"syllables": sub, "synced": line.get("sub_synced", len(sub) > 1)}
            if sub
            else None
        )

    meta_dur = meta.get("_duration")
    if phrases:
        inferred = phrases[-1]["end"] + 2.0
        auto_dur = max(meta_dur, inferred) if meta_dur else inferred
    else:
        auto_dur = meta_dur or 0.0

    return title, phrases, auto_dur, (inline_roman if has_any_sub else None)


def load_lyrics(path):
    """
    Unified loader for TTML, LRC, and Enhanced LRC files.
    Returns (title, phrases, auto_duration, inline_roman_or_None, format_label).
    """
    text = read_text_file(path)
    if _is_xml_text(text, path):
        title, phrases, auto_dur, inline_roman = load_ttml_text(text)
        fmt = "TTML"
    else:
        title, phrases, auto_dur, inline_roman = load_lrc_text(text)
        fmt = "Enhanced LRC" if any(p.get("synced") for p in phrases) else "LRC"
    return title, phrases, auto_dur, inline_roman, fmt


def load_ttml(path):
    """Backward-compatible helper returning (title, phrases)."""
    title, phrases, _, _, _ = load_lyrics(path)
    return title, phrases


# ══════════════════════════════════════════
# ROMANIZATION / SECONDARY SUBTITLE LOADER
# ══════════════════════════════════════════

def load_roman_file(path):
    """
    Load romanization / secondary lyrics from either TTML, LRC, or Enhanced LRC.
    Returns dict: begin_time -> {"syllables": [...], "synced": bool}.
    """
    _, r_phrases, _, _, _ = load_lyrics(path)
    entries = {}
    for rp in r_phrases:
        entries[rp["begin"]] = {
            "syllables": rp["syllables"],
            "synced": rp.get("synced", len(rp["syllables"]) > 1),
        }
    return entries


def align_romanization(phrases, roman_entries):
    """
    Align romanization entries to phrases by begin timestamp (within 150ms tolerance).
    Returns list of dicts {"syllables": [...], "synced": bool, "full": str} or None.
    """
    if not roman_entries:
        return [None] * len(phrases)
    keys = sorted(roman_entries.keys())
    result = []
    for p in phrases:
        pb = p["begin"]
        idx = bisect.bisect_left(keys, pb)
        best_key = None
        best_diff = 0.15
        for cand in (idx - 1, idx):
            if 0 <= cand < len(keys):
                diff = abs(keys[cand] - pb)
                if diff <= best_diff:
                    best_diff = diff
                    best_key = keys[cand]
        if best_key is not None:
            ent = roman_entries[best_key]
            syls = ent["syllables"]
            result.append(
                {
                    "syllables": syls,
                    "synced": ent.get("synced", len(syls) > 1),
                    "full": "".join(s["text"] for s in syls),
                }
            )
        else:
            result.append(None)
    return result


# ══════════════════════════════════════════
# FONT CACHE (per-thread)
# ══════════════════════════════════════════

_local = threading.local()

def get_font(path, size):
    cache = getattr(_local, "font_cache", None)
    if cache is None:
        cache = {}
        _local.font_cache = cache
    sz = max(8, int(size))
    key = (path, sz)
    fnt = cache.get(key)
    if fnt is None:
        try:
            fnt = ImageFont.truetype(path, sz)
        except Exception:
            try:
                fnt = ImageFont.load_default(size=sz)
            except TypeError:
                fnt = ImageFont.load_default()
        cache[key] = fnt
    return fnt


def text_width(text, font):
    if not text:
        return 0
    bb = font.getbbox(text)
    return bb[2] - bb[0]


# ══════════════════════════════════════════
# BREAK DETECTION & FAST INDEXING
# ══════════════════════════════════════════

def detect_breaks(phrases, duration):
    breaks = []
    if phrases and phrases[0]["begin"] >= BREAK_THRESHOLD:
        breaks.append({"start": 0.0, "end": phrases[0]["begin"], "prev_idx": None})
    for i in range(len(phrases) - 1):
        gap_start = phrases[i]["end"]
        gap_end   = phrases[i + 1]["begin"]
        if gap_end - gap_start >= BREAK_THRESHOLD:
            breaks.append({"start": gap_start, "end": gap_end, "prev_idx": i})
    if phrases and duration - phrases[-1]["end"] >= BREAK_THRESHOLD:
        breaks.append({"start": phrases[-1]["end"], "end": duration, "prev_idx": len(phrases) - 1})
    return breaks


def in_break(breaks, break_starts, t):
    if not breaks:
        return None
    idx = bisect.bisect_right(break_starts, t) - 1
    if idx >= 0:
        b = breaks[idx]
        if b["start"] <= t < b["end"]:
            return b
    return None


def find_active(phrases, phrase_begins, t):
    """
    Find active phrase index at time t using O(log N) binary search.
    During a short non-break breath between phrases, the preceding phrase
    remains active until the next phrase begins; during breaks/outro,
    no phrase is active (returns last past phrase as -1-free past boundary).
    """
    if not phrases:
        return -1, False
    idx = bisect.bisect_right(phrase_begins, t) - 1
    if idx < 0:
        return -1, False
    p = phrases[idx]
    if t < p["active_end"]:
        return idx, True
    return idx, False


# ══════════════════════════════════════════
# WRAPPING & PRECOMPUTED PHRASE METRICS
# ══════════════════════════════════════════

def _split_oversized_syllable(syl, font, max_w, synced):
    """Split a single syllable wider than max_w across word or character boundaries."""
    txt = syl["text"]
    sb, se = syl["begin"], syl["end"]
    total_len = max(1, len(txt))

    # Tokenize into word chunks (keeping trailing spaces) if spaces exist, else characters
    if " " in txt or "\u3000" in txt:
        tokens = re.findall(r"\S+\s*|\s+", txt)
    else:
        tokens = list(txt)

    pieces = []
    cur = ""
    for tok in tokens:
        cand = cur + tok
        if cur and text_width(cand, font) > max_w:
            # If tok itself is still wider than max_w, split cur first then tok by char
            pieces.append(cur)
            cur = ""
            if text_width(tok, font) > max_w:
                for ch in tok:
                    if cur and text_width(cur + ch, font) > max_w:
                        pieces.append(cur)
                        cur = ch
                    else:
                        cur += ch
            else:
                cur = tok
        else:
            cur = cand
    if cur:
        pieces.append(cur)

    if len(pieces) <= 1:
        return [syl]

    out = []
    pos = 0
    dur = se - sb
    for piece in pieces:
        plen = len(piece)
        if synced:
            pb = sb + dur * (pos / total_len)
            pe = sb + dur * ((pos + plen) / total_len)
        else:
            pb, pe = sb, se
        out.append({"text": piece, "begin": pb, "end": pe})
        pos += plen
    return out


def wrap_syllables(syls, fnt, synced=True):
    max_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    rows, row = [], []
    w = 0
    for syl in syls:
        sw = text_width(syl["text"], fnt)
        if sw > max_w:
            sub_syls = _split_oversized_syllable(syl, fnt, max_w, synced)
            for ss in sub_syls:
                ssw = text_width(ss["text"], fnt)
                if row and w + ssw > max_w:
                    rows.append(row)
                    row, w = [], 0
                row.append(ss)
                w += ssw
            continue
        if row and w + sw > max_w:
            rows.append(row)
            row, w = [], 0
        row.append(syl)
        w += sw
    if row:
        rows.append(row)
    return rows


def measure_line_width(syls, fnt):
    return sum(text_width(syl["text"], fnt) for syl in syls)


def fit_font_size(syls, fnt_path, target_fs, min_fs=SIZE_ACTIVE_MIN):
    max_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    fs = int(target_fs)
    while fs >= min_fs:
        fnt = get_font(fnt_path, fs)
        if measure_line_width(syls, fnt) <= max_w:
            return fs, fnt, False
        fs -= 2
    return min_fs, get_font(fnt_path, min_fs), True


def get_active_layout_cached(phrase_idx, p, fs):
    """Per-thread cached active font size and wrapped rows for (phrase_idx, fs)."""
    lcache = getattr(_local, "layout_cache", None)
    if lcache is None:
        lcache = {}
        _local.layout_cache = lcache
    key = (phrase_idx, int(fs))
    res = lcache.get(key)
    if res is None:
        fs_actual, fnt, needs_wrap = fit_font_size(p["syllables"], FONT_BOLD, fs)
        ref = fnt.getbbox("あ")
        line_h = ref[3] - ref[1]
        synced = p.get("synced", len(p["syllables"]) > 1)
        rows = wrap_syllables(p["syllables"], fnt, synced) if needs_wrap else [p["syllables"]]
        res = (fs_actual, fnt, rows, ref[1], line_h)
        lcache[key] = res
    return res


def prepare_phrases(phrases, roman_lines):
    """Precompute strings, smooth active_end times, fitted font sizes, and slot heights."""
    max_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    n = len(phrases)
    for i, p in enumerate(phrases):
        p["full"] = "".join(s["text"] for s in p["syllables"])
        if "synced" not in p:
            p["synced"] = len(p["syllables"]) > 1

        # Smooth active_end: hold active across brief gaps (< BREAK_THRESHOLD),
        # end at p["end"] before intermission breaks or after the final line.
        if i + 1 < n and (phrases[i + 1]["begin"] - p["end"]) < BREAK_THRESHOLD:
            p["active_end"] = max(p["end"], phrases[i + 1]["begin"])
        else:
            p["active_end"] = p["end"]

        # Fit idle font size so long lines never clip horizontally
        idle_fs = SIZE_IDLE
        while idle_fs > SIZE_IDLE_MIN:
            if text_width(p["full"], get_font(FONT_REGULAR, idle_fs)) <= max_w:
                break
            idle_fs -= 2
        p["idle_fs"] = idle_fs

        # Fit romanization idle & active font sizes if present
        has_roman = bool(roman_lines and roman_lines[i] and roman_lines[i]["syllables"])
        if has_roman:
            rent = roman_lines[i]
            if "full" not in rent:
                rent["full"] = "".join(s["text"] for s in rent["syllables"])
            r_idle_fs = SIZE_ROMAN_IDLE
            while r_idle_fs > 14:
                if text_width(rent["full"], get_font(FONT_REGULAR, r_idle_fs)) <= max_w:
                    break
                r_idle_fs -= 1
            rent["idle_fs"] = r_idle_fs

        # Compute active slot height accounting for multi-row wrap and romanization
        fs_act, fnt_act, rows_act, _, lh_act = get_active_layout_cached(i, p, SIZE_ACTIVE)
        n_rows = len(rows_act)
        needed_active_h = n_rows * lh_act + max(0, n_rows - 1) * 8 + 30
        needed_idle_h = LINE_H_IDLE
        if has_roman:
            r_target = max(18, int(fs_act * SIZE_ROMAN_SCALE))
            rent = roman_lines[i]
            while r_target > 16:
                if text_width(rent["full"], get_font(FONT_BOLD, r_target)) <= max_w:
                    break
                r_target -= 2
            rent["active_max_fs"] = r_target
            needed_active_h += r_target + 22
            needed_idle_h += rent["idle_fs"] + 10

        p["idle_h"] = max(LINE_H_IDLE, needed_idle_h)
        p["active_h"] = max(LINE_H_ACTIVE, needed_active_h)


# ══════════════════════════════════════════
# LAYOUT & ANIMATION CURVES
# ══════════════════════════════════════════

def _phrase_visual_state(p, i, last_started_idx, is_currently_active, t):
    idle_h = p["idle_h"]
    active_h = p["active_h"]
    idle_fs = p["idle_fs"]

    if is_currently_active and i == last_started_idx:
        grow_t = ease_out(clamp((t - p["begin"]) / GROW_IN_DUR))
        h = lerp(idle_h, active_h, grow_t)
        fs = int(lerp(idle_fs, SIZE_ACTIVE, grow_t))
        return "active", h, fs, False

    if i <= last_started_idx:
        elapsed = t - p["active_end"]
        if 0.0 <= elapsed < GROW_OUT_DUR:
            shrink_t = ease_in_out(clamp(elapsed / GROW_OUT_DUR))
            h = lerp(active_h, idle_h, shrink_t)
            fs = int(lerp(SIZE_ACTIVE, idle_fs, shrink_t))
            return "past", h, fs, True
        return "past", idle_h, idle_fs, False

    return "idle", idle_h, idle_fs, False


def dot_slot_height(brk, t):
    if brk is None:
        return 0.0
    elapsed = t - brk["start"]
    remain  = brk["end"] - t
    fade_in  = ease_out(clamp(elapsed / DOT_FADE_IN))
    fade_out = ease_out(clamp(remain  / DOT_FADE_OUT))
    return DOT_LINE_H * fade_in * fade_out


def build_layout(phrases, last_started_idx, is_currently_active, brk, t):
    prev_phrase_idx = brk["prev_idx"] if brk is not None else None
    dot_h = dot_slot_height(brk, t)

    slots   = []
    heights = []

    if brk is not None and prev_phrase_idx is None and dot_h > 0:
        slots.append({"type": "dots", "brk": brk, "anchor_idx": 0})
        heights.append(dot_h + LINE_GAP)

    for i, p in enumerate(phrases):
        state, lh, fs, shrinking = _phrase_visual_state(
            p, i, last_started_idx, is_currently_active, t
        )
        slots.append(
            {
                "type": "phrase",
                "idx": i,
                "p": p,
                "state": state,
                "fs": fs,
                "shrinking": shrinking,
            }
        )
        heights.append(lh + LINE_GAP)

        if brk is not None and i == prev_phrase_idx and dot_h > 0:
            slots.append({"type": "dots", "brk": brk, "anchor_idx": i})
            heights.append(dot_h + LINE_GAP)

    cumulative = [float(MARGIN_TOP)]
    for h in heights[:-1]:
        cumulative.append(cumulative[-1] + h)

    return slots, heights, cumulative


# ══════════════════════════════════════════
# LAZY SCROLL — precomputed per-frame
# ══════════════════════════════════════════

def precompute_offsets(phrases, phrase_begins, breaks, break_starts, total_frames):
    ANCHOR_Y  = HEIGHT * 0.40
    n         = len(phrases)
    if n == 0:
        return [{"dots": 0.0} for _ in range(total_frames)]

    springs   = None
    dot_spring = 0.0
    TAU_BELOW = 0.10
    TAU_PAST  = GROW_OUT_DUR * 0.9
    k_below   = 1.0 - math.exp(-1.0 / (FPS * TAU_BELOW))
    k_past    = 1.0 - math.exp(-1.0 / (FPS * TAU_PAST))
    k_settled = 1.0 - math.exp(-1.0 / (FPS * (TAU_BELOW * 0.5)))

    offsets = []

    for frame_num in range(total_frames):
        t = frame_num / FPS
        last_idx, is_act = find_active(phrases, phrase_begins, t)
        brk = in_break(breaks, break_starts, t)

        slots, heights, cumulative = build_layout(phrases, last_idx, is_act, brk, t)

        # Target anchor: active phrase if singing, else dot slot if in break, else nearest phrase
        target = 0.0
        target_found = False
        if is_act:
            for si, slot in enumerate(slots):
                if slot["type"] == "phrase" and slot["idx"] == last_idx:
                    target = ANCHOR_Y - (cumulative[si] + heights[si] * 0.5)
                    target_found = True
                    break
        elif brk is not None:
            for si, slot in enumerate(slots):
                if slot["type"] == "dots":
                    target = ANCHOR_Y - (cumulative[si] + heights[si] * 0.5)
                    target_found = True
                    break

        if not target_found:
            focus_idx = max(0, last_idx)
            for si, slot in enumerate(slots):
                if slot["type"] == "phrase" and slot["idx"] == focus_idx:
                    target = ANCHOR_Y - (cumulative[si] + heights[si] * 0.5)
                    break

        if springs is None:
            springs = [target] * n
            dot_spring = target

        dot_spring += (target - dot_spring) * k_settled
        frame_offsets = {"dots": dot_spring}

        for i in range(n):
            p = phrases[i]
            if is_act and i == last_idx:
                springs[i] = target
            elif i > last_idx:
                springs[i] += (target - springs[i]) * k_below
            else:
                elapsed = t - p["active_end"]
                k = k_past if 0.0 <= elapsed < GROW_OUT_DUR else k_settled
                springs[i] += (target - springs[i]) * k
            frame_offsets[i] = springs[i]

        offsets.append(frame_offsets)

    return offsets


# ══════════════════════════════════════════
# COMPOSITING, SHADOW & WIPE HELPERS
# ══════════════════════════════════════════

def alpha_paste(dst, src, x, y):
    """In-place Porter-Duff alpha_composite with automatic canvas clipping."""
    x, y = int(x), int(y)
    sw, sh = src.size
    sx0 = max(0, -x)
    sy0 = max(0, -y)
    dx0 = max(0, x)
    dy0 = max(0, y)
    w = min(sw - sx0, dst.width - dx0)
    h = min(sh - sy0, dst.height - dy0)
    if w <= 0 or h <= 0:
        return
    if sx0 == 0 and sy0 == 0 and w == sw and h == sh:
        dst.alpha_composite(src, dest=(dx0, dy0))
    else:
        dst.alpha_composite(src.crop((sx0, sy0, sx0 + w, sy0 + h)), dest=(dx0, dy0))


def draw_text_opaque(draw, pos, text, font, fill, alpha_mul=1.0,
                     greenscreen=False, shadow_offset=SHADOW_OFFSET):
    """Hard drop-shadow + coloured text for RGB / green-screen mode."""
    if not text or alpha_mul <= 0.0:
        return
    x, y = int(pos[0]), int(pos[1])
    if greenscreen:
        sh_col = (0, 0, 0)
        col = tuple(int(c * alpha_mul) for c in fill[:3])
    else:
        sh_col = lerp_color(BG_COLOR, (0, 0, 0), alpha_mul)
        col = lerp_color(BG_COLOR, fill[:3], alpha_mul)
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=sh_col)
    draw.text((x, y), text, font=font, fill=col)


def draw_text_transparent(img, pos, text, font, fill, alpha_mul=1.0,
                          shadow_blur=SHADOW_BLUR, shadow_offset=SHADOW_OFFSET):
    """Blurred shadow + RGBA text composited onto img via a tight bounding box."""
    if not text or alpha_mul <= 0.0:
        return
    x, y = int(pos[0]), int(pos[1])
    bb = font.getbbox(text)
    pad = shadow_blur * 2 + shadow_offset + 4
    bx0 = max(0, x + bb[0] - pad)
    by0 = max(0, y + bb[1] - pad)
    bx1 = min(img.width,  x + bb[2] + pad)
    by1 = min(img.height, y + bb[3] + pad)
    bw, bh = bx1 - bx0, by1 - by0
    if bw <= 0 or bh <= 0:
        return

    lx = x - bx0
    ly = y - by0

    tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text(
        (lx + shadow_offset, ly + shadow_offset),
        text, font=font, fill=(0, 0, 0, int(200 * alpha_mul))
    )
    tile = tile.filter(ImageFilter.GaussianBlur(radius=shadow_blur))

    text_tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    r, g, b = fill[:3]
    ImageDraw.Draw(text_tile).text(
        (lx, ly), text, font=font, fill=(r, g, b, int(255 * alpha_mul))
    )
    tile.alpha_composite(text_tile)
    alpha_paste(img, tile, bx0, by0)


def draw_text(img, draw, pos, text, font, fill, alpha_mul=1.0,
              transparent=False, greenscreen=False,
              shadow_blur=SHADOW_BLUR, shadow_offset=SHADOW_OFFSET):
    if transparent:
        draw_text_transparent(img, pos, text, font, fill, alpha_mul,
                              shadow_blur=shadow_blur, shadow_offset=shadow_offset)
    else:
        draw_text_opaque(draw, pos, text, font, fill, alpha_mul,
                         greenscreen=greenscreen, shadow_offset=shadow_offset)


def draw_wipe_opaque(img, draw, pos, text, font,
                     fill_base, fill_sung, wipe_frac, alpha_mul, greenscreen=False):
    """
    Fast left-to-right wipe for RGB / green-screen mode.
    Zero full-frame allocations: fast-paths 0% and 100%, and crops only
    the single mid-wipe glyph bounding box when 0 < wipe_frac < 1.
    """
    if not text or alpha_mul <= 0.0:
        return
    if wipe_frac <= 0.0:
        draw_text_opaque(draw, pos, text, font, fill_base, alpha_mul, greenscreen=greenscreen)
        return
    if wipe_frac >= 1.0:
        draw_text_opaque(draw, pos, text, font, fill_sung, alpha_mul, greenscreen=greenscreen)
        return

    bb = font.getbbox(text)
    tw = bb[2] - bb[0]
    wipe_px = int(tw * clamp(wipe_frac))
    draw_text_opaque(draw, pos, text, font, fill_base, alpha_mul, greenscreen=greenscreen)
    if wipe_px <= 0 or tw <= 0:
        return

    x, y = int(pos[0]), int(pos[1])
    bx0 = max(0, x + bb[0])
    by0 = max(0, y + bb[1] - 2)
    bx1 = min(img.width,  x + bb[0] + wipe_px)
    by1 = min(img.height, y + bb[3] + 2)
    if bx1 <= bx0 or by1 <= by0:
        return

    sung_col = (
        tuple(int(c * alpha_mul) for c in fill_sung[:3])
        if greenscreen
        else lerp_color(BG_COLOR, fill_sung[:3], alpha_mul)
    )
    patch = img.crop((bx0, by0, bx1, by1))
    ImageDraw.Draw(patch).text((x - bx0, y - by0), text, font=font, fill=sung_col)
    img.paste(patch, (bx0, by0))


def draw_wipe_transparent(img, pos, text, font,
                          fill_base, fill_sung, wipe_frac, alpha_mul):
    """Fast tile-based left-to-right wipe for RGBA transparent mode."""
    if not text or alpha_mul <= 0.0:
        return
    if wipe_frac <= 0.0:
        draw_text_transparent(img, pos, text, font, fill_base, alpha_mul)
        return
    if wipe_frac >= 1.0:
        draw_text_transparent(img, pos, text, font, fill_sung, alpha_mul)
        return

    x, y = int(pos[0]), int(pos[1])
    bb  = font.getbbox(text)
    tw  = bb[2] - bb[0]
    pad = SHADOW_BLUR * 2 + SHADOW_OFFSET + 4
    bx0 = max(0, x + bb[0] - pad)
    by0 = max(0, y + bb[1] - pad)
    bx1 = min(img.width,  x + bb[2] + pad)
    by1 = min(img.height, y + bb[3] + pad)
    bw, bh = bx1 - bx0, by1 - by0
    if bw <= 0 or bh <= 0:
        return
    lx = x - bx0
    ly = y - by0

    tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text(
        (lx + SHADOW_OFFSET, ly + SHADOW_OFFSET), text, font=font,
        fill=(0, 0, 0, int(180 * alpha_mul))
    )
    tile = tile.filter(ImageFilter.GaussianBlur(radius=SHADOW_BLUR))

    wipe_px = int(tw * clamp(wipe_frac))
    split_x = clamp(lx + bb[0] + wipe_px, 0, bw)
    a_val = int(255 * alpha_mul)

    text_layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(text_layer).text((lx, ly), text, font=font, fill=(*fill_base[:3], a_val))

    if split_x > 0:
        sung_part = Image.new("RGBA", (int(split_x), bh), (0, 0, 0, 0))
        ImageDraw.Draw(sung_part).text((lx, ly), text, font=font, fill=(*fill_sung[:3], a_val))
        text_layer.paste(sung_part, (0, 0))

    tile.alpha_composite(text_layer)
    alpha_paste(img, tile, bx0, by0)


# ══════════════════════════════════════════
# INTERMISSION DOTS  (sequential travelling pulse)
# ══════════════════════════════════════════

def draw_dots(img, draw, cy, brk, t, transparent, greenscreen):
    elapsed = t - brk["start"]
    remain  = brk["end"] - t
    fade_in  = ease_out(clamp(elapsed / DOT_FADE_IN))
    fade_out = ease_out(clamp(remain  / DOT_FADE_OUT))
    group_alpha = fade_in * fade_out
    if group_alpha <= 0.0:
        return

    peak_pos = (elapsed % DOT_PULSE_PERIOD) / DOT_PULSE_PERIOD * DOT_COUNT
    cx_start = MARGIN_LEFT + 18

    for d in range(DOT_COUNT):
        dist  = abs(peak_pos - d)
        dist  = min(dist, DOT_COUNT - dist)
        pulse = ease_in_out(clamp(1.0 - dist / 0.9))

        dot_alpha = group_alpha * lerp(0.15, 1.0, pulse)
        cx = cx_start + d * DOT_SPACING
        r  = max(1, int(DOT_RADIUS * lerp(0.55, 1.0, pulse)))
        col = lerp_color(DOT_DIM, DOT_COLOR, pulse)

        if transparent:
            dot_tile = Image.new("RGBA", (r * 2 + 2, r * 2 + 2), (0, 0, 0, 0))
            ImageDraw.Draw(dot_tile).ellipse(
                [0, 0, r * 2, r * 2],
                fill=(*col, int(255 * dot_alpha))
            )
            alpha_paste(img, dot_tile, cx - r, int(cy) - r)
        else:
            if greenscreen:
                fill = tuple(int(c * dot_alpha) for c in col)
            else:
                fill = lerp_color(BG_COLOR, col, dot_alpha)
            draw.ellipse([cx - r, int(cy) - r, cx + r, int(cy) + r], fill=fill)


# ══════════════════════════════════════════
# PRE-RENDERED CHROME OVERLAY
# ══════════════════════════════════════════

def build_chrome_overlay(title, transparent, greenscreen):
    """
    Pre-render title and watermark tiles once (avoiding per-frame GaussianBlur).
    Returns list of (tile_rgba, x, y) for transparent mode, or a draw callback
    using pre-fitted fonts/positions.
    """
    max_title_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    title_fs = SIZE_TITLE
    title_fnt = None
    if title:
        while title_fs >= SIZE_TITLE_MIN:
            title_fnt = get_font(FONT_BOLD, title_fs)
            if text_width(title, title_fnt) <= max_title_w:
                break
            title_fs -= 4

    wm = "made with Nuisance"
    wf = get_font(FONT_REGULAR, SIZE_WATERMARK)
    wbb = wf.getbbox(wm)
    wx = WIDTH  - (wbb[2] - wbb[0]) - 40
    wy = HEIGHT - (wbb[3] - wbb[1]) - 30

    if transparent:
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        if title and title_fnt is not None:
            draw_text_transparent(
                overlay, (MARGIN_LEFT, 28), title, title_fnt, TITLE_COLOR, 1.0,
                shadow_blur=10, shadow_offset=4
            )
        draw_text_transparent(
            overlay, (wx, wy), wm, wf, WATERMARK_COL, 0.6,
            shadow_blur=3, shadow_offset=2
        )
        # Extract non-empty top and bottom strips so per-frame composite is tiny
        strips = []
        if title:
            t_strip = overlay.crop((0, 0, WIDTH, 160))
            strips.append((t_strip, 0, 0))
        b_strip = overlay.crop((max(0, wx - 20), max(0, wy - 20), WIDTH, HEIGHT))
        strips.append((b_strip, max(0, wx - 20), max(0, wy - 20)))
        return {"transparent": True, "strips": strips}

    return {
        "transparent": False,
        "greenscreen": greenscreen,
        "title": title,
        "title_fs": title_fs,
        "wm": wm,
        "wm_pos": (wx, wy),
    }


def apply_chrome(img, draw, chrome):
    if chrome["transparent"]:
        for strip, sx, sy in chrome["strips"]:
            img.alpha_composite(strip, dest=(sx, sy))
    else:
        gs = chrome["greenscreen"]
        if chrome["title"]:
            tf = get_font(FONT_BOLD, chrome["title_fs"])
            draw_text_opaque(
                draw, (MARGIN_LEFT, 28), chrome["title"], tf, TITLE_COLOR, 1.0,
                greenscreen=gs, shadow_offset=4
            )
        wf = get_font(FONT_REGULAR, SIZE_WATERMARK)
        draw_text_opaque(
            draw, chrome["wm_pos"], chrome["wm"], wf, WATERMARK_COL, 0.6,
            greenscreen=gs, shadow_offset=2
        )


# ══════════════════════════════════════════
# RENDER ONE FRAME
# ══════════════════════════════════════════

def render_frame(phrases, phrase_begins, roman_lines, chrome, frame_num,
                 line_offsets, breaks, break_starts, transparent, greenscreen,
                 output_dir=OUTPUT_DIR):
    t = frame_num / FPS

    if transparent:
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    elif greenscreen:
        img = Image.new("RGB",  (WIDTH, HEIGHT), GS_COLOR)
    else:
        img = Image.new("RGB",  (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    last_idx, is_act = find_active(phrases, phrase_begins, t)
    brk = in_break(breaks, break_starts, t)

    slots, heights, cumulative = build_layout(phrases, last_idx, is_act, brk, t)
    focus_idx = max(0, last_idx)

    for si, slot in enumerate(slots):
        if slot["type"] == "dots":
            offset = line_offsets.get("dots", 0.0)
        else:
            offset = line_offsets.get(slot["idx"], 0.0)

        y = cumulative[si] + offset
        h = heights[si]

        if y + h < -30 or y > HEIGHT + 30:
            continue

        # ── dot slot ─────────────────────────────────────────────────
        if slot["type"] == "dots":
            cy = y + h * 0.18
            draw_dots(img, draw, cy, brk, t, transparent, greenscreen)
            continue

        # ── phrase slot ───────────────────────────────────────────────
        i     = slot["idx"]
        p     = slot["p"]
        state = slot["state"]
        fs    = slot["fs"]

        dist = abs(i - focus_idx)
        if state == "past":
            alpha = lerp(1.0, 0.15, clamp((dist - 1) / 5.0))
        elif state == "idle":
            alpha = lerp(0.85, 0.10, clamp((dist - 1) / 6.0))
        else:
            alpha = 1.0

        use_bold = state == "active" or slot["shrinking"]
        roman_ent = roman_lines[i] if roman_lines else None
        roman_syls = roman_ent["syllables"] if roman_ent else None

        if state == "active":
            fs_actual, fnt, rows, ref_top, line_h = get_active_layout_cached(i, p, fs)
            draw_y = y - ref_top
            is_syllable_synced = p.get("synced", len(p["syllables"]) > 1)

            row_y = draw_y
            for row in rows:
                x = float(MARGIN_LEFT)
                for syl in row:
                    sb, se = syl["begin"], syl["end"]
                    text   = syl["text"]
                    sw     = text_width(text, fnt)

                    if is_syllable_synced:
                        if t < sb:
                            wipe_frac = 0.0
                        elif t <= se:
                            wipe_frac = (t - sb) / max(se - sb, 0.01)
                        else:
                            wipe_frac = 1.0

                        if transparent:
                            draw_wipe_transparent(img, (x, row_y), text, fnt,
                                                  ACTIVE_BASE, ACTIVE_SUNG,
                                                  wipe_frac, alpha)
                        else:
                            draw_wipe_opaque(img, draw, (x, row_y), text, fnt,
                                             ACTIVE_BASE, ACTIVE_SUNG,
                                             wipe_frac, alpha, greenscreen=greenscreen)
                    else:
                        wf = ease_out(clamp((t - sb) / WORD_FADE_DUR))
                        if t < sb:
                            col = ACTIVE_BASE
                        elif t <= se:
                            col = lerp_color(ACTIVE_BASE, ACTIVE_SUNG, wf)
                        else:
                            col = DONE_COLOR

                        draw_text(img, draw, (x, row_y), text, fnt, col, alpha,
                                  transparent=transparent, greenscreen=greenscreen)
                    x += sw
                row_y += line_h + 8

            if roman_syls:
                max_r_fs  = roman_ent.get("active_max_fs", max(18, int(fs_actual * SIZE_ROMAN_SCALE)))
                roman_fs  = min(max_r_fs, max(16, int(fs_actual * SIZE_ROMAN_SCALE)))
                roman_fnt = get_font(FONT_BOLD, roman_fs)
                roman_y   = row_y + 6
                rx        = float(MARGIN_LEFT + 6)
                r_synced  = roman_ent.get("synced", is_syllable_synced and len(roman_syls) > 1)

                if r_synced:
                    for rsyl in roman_syls:
                        rtext = rsyl["text"]
                        rsw   = text_width(rtext, roman_fnt)
                        rb, re_t = rsyl["begin"], rsyl["end"]
                        if t < rb:
                            wipe_frac = 0.0
                        elif t <= re_t:
                            wipe_frac = (t - rb) / max(re_t - rb, 0.01)
                        else:
                            wipe_frac = 1.0
                        if transparent:
                            draw_wipe_transparent(img, (rx, roman_y), rtext, roman_fnt,
                                                  ROMAN_COLOR, (255, 255, 255),
                                                  wipe_frac, alpha)
                        else:
                            draw_wipe_opaque(img, draw, (rx, roman_y), rtext, roman_fnt,
                                             ROMAN_COLOR, (255, 255, 255),
                                             wipe_frac, alpha, greenscreen=greenscreen)
                        rx += rsw
                else:
                    draw_text(img, draw, (rx, roman_y), roman_ent["full"], roman_fnt,
                              ACTIVE_SUNG, alpha,
                              transparent=transparent, greenscreen=greenscreen,
                              shadow_blur=4, shadow_offset=2)

        else:
            fnt        = get_font(FONT_BOLD if use_bold else FONT_REGULAR, fs)
            ref        = fnt.getbbox("あ")
            glyph_h    = ref[3] - ref[1]
            slot_inner = h - LINE_GAP
            if roman_syls:
                r_idle_fs = roman_ent.get("idle_fs", SIZE_ROMAN_IDLE)
                group_h   = glyph_h + 8 + r_idle_fs
                draw_y    = y + (slot_inner - group_h) * 0.5 - ref[1]
            else:
                draw_y    = y + (slot_inner - glyph_h) * 0.5 - ref[1]

            base = DONE_COLOR if state == "past" else IDLE_COLOR
            draw_text(img, draw, (MARGIN_LEFT, int(draw_y)), p["full"], fnt, base, alpha,
                      transparent=transparent, greenscreen=greenscreen)

            if roman_syls:
                r_idle_fs = roman_ent.get("idle_fs", SIZE_ROMAN_IDLE)
                roman_fnt = get_font(FONT_REGULAR, r_idle_fs)
                roman_y   = draw_y + ref[1] + glyph_h + 8
                roman_col = IDLE_COLOR if state == "idle" else DONE_COLOR
                draw_text(img, draw, (MARGIN_LEFT + 6, int(roman_y)), roman_ent["full"], roman_fnt,
                          roman_col, alpha * 0.75,
                          transparent=transparent, greenscreen=greenscreen,
                          shadow_blur=3, shadow_offset=2)

    apply_chrome(img, draw, chrome)
    path = os.path.join(output_dir, f"frame_{frame_num:06d}.png")
    img.save(path, format="PNG", compress_level=1)


# ══════════════════════════════════════════
# PROGRESS
# ══════════════════════════════════════════

_progress_lock = threading.Lock()
_done_count    = 0

def on_frame_done(total):
    global _done_count
    if total <= 0:
        return
    with _progress_lock:
        _done_count += 1
        i = _done_count
    step = max(1, total // 200)
    if i < total and (i % step) != 0:
        return
    bar = 40
    f   = int(bar * i / total)
    pct = i / total * 100
    print(f"\r[{'█'*f}{'░'*(bar-f)}] {i}/{total}  {pct:.1f}%", end="", flush=True)


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

def main():
    global _done_count

    parser = argparse.ArgumentParser(
        prog="lyrics.py",
        description="Nuisance — time-synced lyric video renderer (TTML, LRC, Enhanced LRC). "
                    "Outputs PNG frame sequences ready for ffmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # quick preview from TTML, LRC, or Enhanced LRC
  python lyrics.py --input lyrics.lrc --preview

  # full render with explicit duration override
  python lyrics.py --input lyrics.ttml --duration 217

  # transparent RGBA (for compositing)
  python lyrics.py --input song.lrc --transparent

  # green screen + romanization (TTML or LRC)
  python lyrics.py --input lyrics.ttml --greenscreen --romanise roman.ttml

encode output:
  ffmpeg -framerate 60 -i frames/frame_%06d.png -i audio.mp3 \\
         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4
        """
    )
    parser.add_argument("--input",       required=True,
                        help="Path to lyrics file (TTML, LRC, or Enhanced LRC)")
    parser.add_argument("--duration",    type=float, default=None,
                        help="Track length in seconds (auto-detected if omitted)")
    parser.add_argument("--start",       type=int,   default=0,
                        help="Resume from this frame index (default: 0)")
    parser.add_argument("--workers",     type=int,   default=os.cpu_count() or 1,
                        help="Thread count for parallel rendering (default: CPU count)")
    parser.add_argument("--transparent", action="store_true",
                        help="Export RGBA PNGs with blurred text shadows (slower, for alpha compositing)")
    parser.add_argument("--greenscreen", action="store_true",
                        help="Solid green (#00ff00) background for chroma key (fast)")
    parser.add_argument("--romanise",    default=None,
                        help="Path to secondary TTML or LRC file with romanized/translated lyrics")
    parser.add_argument("--title",       default=None,
                        help="Override song title shown on screen")
    parser.add_argument("--preview",     action="store_true",
                        help="Render 1 frame per second only — fast visual check, skips confirmation prompt")
    parser.add_argument("--yes", "-y",   action="store_true",
                        help="Skip interactive confirmation prompt")
    args = parser.parse_args()

    if args.transparent and args.greenscreen:
        print("  error: --transparent and --greenscreen are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.input):
        print(f"  error: file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    title, phrases, auto_dur, inline_roman, fmt_label = load_lyrics(args.input)
    if args.title is not None:
        title = args.title

    if not phrases:
        print(f"  No lyric lines found in {args.input}.")
        return

    duration = args.duration if (args.duration is not None and args.duration > 0) else auto_dur
    if duration <= 0:
        print("  error: duration must be positive.", file=sys.stderr)
        sys.exit(1)

    roman_lines = None
    if args.romanise:
        if not os.path.exists(args.romanise):
            print(f"  error: romanization file not found: {args.romanise}", file=sys.stderr)
            sys.exit(1)
        roman_entries = load_roman_file(args.romanise)
        roman_lines   = align_romanization(phrases, roman_entries)
        print(f"  romanization: {sum(1 for r in roman_lines if r)} / {len(roman_lines)} lines matched")
    elif inline_roman:
        roman_lines = inline_roman
        print(f"  co-timed sub-lines: {sum(1 for r in roman_lines if r)} / {len(roman_lines)} lines")

    prepare_phrases(phrases, roman_lines)
    phrase_begins = [p["begin"] for p in phrases]

    breaks = detect_breaks(phrases, duration)
    break_starts = [b["start"] for b in breaks]

    workers = max(1, args.workers)
    start_frame = max(0, args.start)

    mode = "transparent RGBA" if args.transparent else ("green screen" if args.greenscreen else "opaque")
    print(f"\n  ♪ {title or '(no title)'}  [{fmt_label}]")
    print(f"  {len(phrases)} lines · {duration:.1f}s · mode: {mode}")
    if breaks:
        print(f"  {len(breaks)} intermission break(s) detected")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total = int(duration * FPS)
    if args.preview:
        frames = list(range(start_frame, total, FPS))
        print(f"  PREVIEW MODE — {len(frames)} frames (1/sec) · {workers} workers")
    else:
        frames = list(range(start_frame, total))
        print(f"  {len(frames)} frames @ {FPS}fps · {workers} workers")

    if not frames:
        print("  Nothing to render (0 frames).")
        return

    print(f"  output → {OUTPUT_DIR}/\n")

    if not args.preview and not args.yes:
        try:
            ans = input("  Render? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ans != "y":
            return

    print("  Pre-computing scroll offsets …", end="", flush=True)
    offsets = precompute_offsets(phrases, phrase_begins, breaks, break_starts, total)
    chrome  = build_chrome_overlay(title, args.transparent, args.greenscreen)
    print(" done.")

    _done_count = 0
    num_to_render = len(frames)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                render_frame,
                phrases,
                phrase_begins,
                roman_lines,
                chrome,
                f,
                offsets[f],
                breaks,
                break_starts,
                args.transparent,
                args.greenscreen,
                OUTPUT_DIR,
            ): f
            for f in frames
        }
        for fut in as_completed(futs):
            fut.result()
            on_frame_done(num_to_render)

    print(f"\n\n  Done. Encode with:")
    if args.transparent:
        print(f"  # lossless with alpha (ProRes 4444):")
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -c:v prores_ks -pix_fmt yuva444p10le -profile:v 4444 out.mov")
        print()
        print(f"  # composite over background video:")
        print(f"  ffmpeg -i background.mp4 -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -i audio.mp3 -filter_complex \"[0:v][1:v]overlay=0:0\" \\")
        print(f"         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4")
    elif args.greenscreen:
        print(f"  # encode green-screen video:")
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -c:v libx264 -pix_fmt yuv420p -crf 0 out_gs.mp4")
        print()
        print(f"  # chroma-key composite in ffmpeg (replace green with background.mp4):")
        print(f"  ffmpeg -i background.mp4 -i out_gs.mp4 \\")
        print(f"         -filter_complex \"[1:v]colorkey=0x00ff00:0.3:0.1[ov];[0:v][ov]overlay\" \\")
        print(f"         -i audio.mp3 -c:a aac -b:a 192k -shortest out.mp4")
    else:
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png -i audio.mp3 \\")
        print(f"         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4")
    print()


if __name__ == "__main__":
    main()
