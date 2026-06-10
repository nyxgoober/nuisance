#!/usr/bin/env python3

import argparse
import math
import os
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════

WIDTH      = 1920
HEIGHT     = 1080
FPS        = 60
OUTPUT_DIR = "frames"

FONT_BOLD    = "ZenKakuGothicNew-Bold.ttf"
FONT_REGULAR = "ZenKakuGothicNew-Regular.ttf"

MARGIN_LEFT   = 120
MARGIN_TOP    = 100
LINE_H_IDLE   = 58
LINE_H_ACTIVE = 150
LINE_GAP      = 24

SIZE_IDLE        = 36
SIZE_ACTIVE      = 96
SIZE_TITLE       = 80
SIZE_WATERMARK   = 22
SIZE_ACTIVE_MIN  = 48
SIZE_ROMAN_SCALE = 0.38
SIZE_ROMAN_IDLE  = 22

MARGIN_RIGHT = 120

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
# Height the dot row occupies in the layout (slides in/out like a phrase line)
DOT_LINE_H       = 60
DOT_COLOR        = (200, 200, 210)
DOT_DIM          = (55,  55,  65)

# Lazy scroll
SHADOW_OFFSET = 3
SHADOW_BLUR   = 6

# ══════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))

def ease_out(t):
    t = clamp(t)
    return 1.0 - (1.0 - t) ** 3

def ease_in_out(t):
    t = clamp(t)
    return t * t * (3.0 - 2.0 * t)

def lerp(a, b, t):
    return a + (b - a) * clamp(t)

def lerp_color(ca, cb, t):
    t = clamp(t)
    return tuple(int(a + (b - a) * t) for a, b in zip(ca, cb))

def parse_time(ts):
    h, m, s = ts.split(":")
    sec, ms = s.split(".")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000

# ══════════════════════════════════════════
# TTML PARSER
# ══════════════════════════════════════════

def load_ttml(path):
    tree = ET.parse(path)
    root = tree.getroot()
    ns   = {"tt":  "http://www.w3.org/ns/ttml",
            "ttm": "http://www.w3.org/ns/ttml#metadata"}

    title_el = root.find(".//ttm:title", ns)
    title    = title_el.text.strip() if title_el is not None and title_el.text else ""

    phrases = []
    for p in root.findall(".//tt:p", ns):
        begin = parse_time(p.attrib["begin"])
        end   = parse_time(p.attrib["end"])
        syls  = []
        for s in p.findall("tt:span", ns):
            text = s.text or ""
            if text.strip():
                syls.append({
                    "text":  text,
                    "begin": parse_time(s.attrib["begin"]),
                    "end":   parse_time(s.attrib["end"]),
                })
        if not syls:
            text = "".join(p.itertext()).strip()
            if text:
                syls = [{"text": text, "begin": begin, "end": end}]
        if syls:
            phrases.append({"begin": begin, "end": end, "syllables": syls})

    return title, phrases

# ══════════════════════════════════════════
# ROMANIZATION
# ══════════════════════════════════════════

def load_roman_ttml(path):
    """
    Returns a dict mapping phrase begin-time → list of syllable dicts
    [{text, begin, end}, ...].  Falls back to a single-syllable list
    spanning the whole phrase if no <span> children are present.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    ns   = {"tt": "http://www.w3.org/ns/ttml"}
    entries = {}
    for p in root.findall(".//tt:p", ns):
        p_begin = parse_time(p.attrib["begin"])
        p_end   = parse_time(p.attrib["end"])
        spans   = p.findall("tt:span", ns)
        if spans:
            syls = []
            for s in spans:
                text = (s.text or "").strip()
                if text:
                    syls.append({
                        "text":  text,
                        "begin": parse_time(s.attrib.get("begin", p.attrib["begin"])),
                        "end":   parse_time(s.attrib.get("end",   p.attrib["end"])),
                    })
            if syls:
                entries[p_begin] = syls
        else:
            text = "".join(p.itertext()).strip()
            if text:
                entries[p_begin] = [{"text": text, "begin": p_begin, "end": p_end}]
    return entries

def align_romanization(phrases, roman_entries):
    """
    Returns list[list[syl]] — one syllable list per phrase.
    Each syl is {text, begin, end}.  Empty list means no romanization.
    """
    result = []
    keys   = sorted(roman_entries.keys())
    for p in phrases:
        best = []
        for k in keys:
            if abs(k - p["begin"]) < 0.05:
                best = roman_entries[k]
                break
        result.append(best)
    return result

# ══════════════════════════════════════════
# BREAK DETECTION
# ══════════════════════════════════════════

def detect_breaks(phrases, duration):
    breaks = []
    if phrases and phrases[0]["begin"] >= BREAK_THRESHOLD:
        breaks.append({"start": 0.0, "end": phrases[0]["begin"]})
    for i in range(len(phrases) - 1):
        gap_start = phrases[i]["end"]
        gap_end   = phrases[i + 1]["begin"]
        if gap_end - gap_start >= BREAK_THRESHOLD:
            breaks.append({"start": gap_start, "end": gap_end})
    if phrases and duration - phrases[-1]["end"] >= BREAK_THRESHOLD:
        breaks.append({"start": phrases[-1]["end"], "end": duration})
    return breaks

def in_break(breaks, t):
    for b in breaks:
        if b["start"] <= t < b["end"]:
            return b
    return None

# ══════════════════════════════════════════
# FONT CACHE (per-thread)
# ══════════════════════════════════════════

_local = threading.local()

def get_font(path, size):
    if not hasattr(_local, "cache"):
        _local.cache = {}
    key = (path, int(size))
    if key not in _local.cache:
        try:
            _local.cache[key] = ImageFont.truetype(path, int(size))
        except Exception:
            _local.cache[key] = ImageFont.load_default()
    return _local.cache[key]

# ══════════════════════════════════════════
# LAYOUT
# ══════════════════════════════════════════

def find_active(phrases, t):
    for i, p in enumerate(phrases):
        if p["begin"] <= t < p["end"]:
            return i
    last = -1
    for i, p in enumerate(phrases):
        if t >= p["end"]:
            last = i
    return last

def line_height(p, i, active_idx, t):
    if i == active_idx:
        grow_t = ease_out(clamp((t - p["begin"]) / GROW_IN_DUR))
        return lerp(LINE_H_IDLE, LINE_H_ACTIVE, grow_t)
    elif i < active_idx:
        elapsed  = t - p["end"]
        shrink_t = ease_in_out(clamp(elapsed / GROW_OUT_DUR))
        return lerp(LINE_H_ACTIVE, LINE_H_IDLE, shrink_t) if elapsed < GROW_OUT_DUR else LINE_H_IDLE
    else:
        return LINE_H_IDLE

def line_fontsize(p, i, active_idx, t):
    if i == active_idx:
        grow_t = ease_out(clamp((t - p["begin"]) / GROW_IN_DUR))
        return int(lerp(SIZE_IDLE, SIZE_ACTIVE, grow_t))
    elif i < active_idx:
        elapsed  = t - p["end"]
        shrink_t = ease_in_out(clamp(elapsed / GROW_OUT_DUR))
        return int(lerp(SIZE_ACTIVE, SIZE_IDLE, shrink_t)) if elapsed < GROW_OUT_DUR else SIZE_IDLE
    else:
        return SIZE_IDLE

def dot_slot_height(brk, t):
    """
    Returns the height the dot row contributes to the layout at time t.
    Slides in from 0 → DOT_LINE_H over DOT_FADE_IN, slides out over DOT_FADE_OUT.
    Returns 0 when there is no active break.
    """
    if brk is None:
        return 0.0
    elapsed = t - brk["start"]
    remain  = brk["end"] - t
    fade_in  = ease_out(clamp(elapsed / DOT_FADE_IN))
    fade_out = ease_out(clamp(remain  / DOT_FADE_OUT))
    return DOT_LINE_H * fade_in * fade_out

def measure_line_width(syls, fnt, draw):
    total = 0
    for syl in syls:
        bb = draw.textbbox((0, 0), syl["text"], font=fnt)
        total += bb[2] - bb[0]
    return total

def fit_font_size(syls, fnt_path, target_fs, draw):
    max_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    fs = target_fs
    while fs >= SIZE_ACTIVE_MIN:
        fnt = get_font(fnt_path, fs)
        if measure_line_width(syls, fnt, draw) <= max_w:
            return fs, fnt, False
        fs -= 2
    return fs, get_font(fnt_path, SIZE_ACTIVE_MIN), True

def wrap_syllables(syls, fnt, draw):
    max_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    rows, row = [], []
    w = 0
    for syl in syls:
        bb = draw.textbbox((0, 0), syl["text"], font=fnt)
        sw = bb[2] - bb[0]
        if row and w + sw > max_w:
            rows.append(row)
            row, w = [], 0
        row.append(syl)
        w += sw
    if row:
        rows.append(row)
    return rows

# ══════════════════════════════════════════
# LAYOUT WITH DOT SLOT
# The dot slot is injected between prev_idx and next_idx in the layout.
# We build a virtual "slot list" of (type, data) entries.
# ══════════════════════════════════════════

def build_layout(phrases, active_idx, brk, t):
    """
    Returns:
      slots      — list of dicts: {type: 'phrase'|'dots', ...}
      heights    — matching list of heights
      cumulative — list of top-Y for each slot (before scroll offset)
    """
    # find which phrases the break sits between
    prev_phrase_idx = None
    next_phrase_idx = None
    if brk is not None:
        for i, p in enumerate(phrases):
            if p["end"] <= brk["start"] + 0.01:
                prev_phrase_idx = i
            if next_phrase_idx is None and p["begin"] >= brk["end"] - 0.01:
                next_phrase_idx = i

    dot_h = dot_slot_height(brk, t)

    slots   = []
    heights = []

    for i, p in enumerate(phrases):
        slots.append({"type": "phrase", "idx": i, "p": p})
        heights.append(line_height(p, i, active_idx, t) + LINE_GAP)

        # insert dot slot immediately after the phrase that precedes the break
        if brk is not None and i == prev_phrase_idx and dot_h > 0:
            slots.append({"type": "dots", "brk": brk})
            heights.append(dot_h + LINE_GAP)

    # if break is before all phrases (intro)
    if brk is not None and prev_phrase_idx is None and dot_h > 0:
        slots.insert(0, {"type": "dots", "brk": brk})
        heights.insert(0, dot_h + LINE_GAP)

    cumulative = [float(MARGIN_TOP)]
    for h in heights[:-1]:
        cumulative.append(cumulative[-1] + h)

    return slots, heights, cumulative

# ══════════════════════════════════════════
# LAZY SCROLL — precomputed per-frame
# ══════════════════════════════════════════

def precompute_offsets(phrases, breaks, total_frames):
    # Returns list of per-frame dicts: {phrase_index -> y_offset}.
    # Every line gets its own spring so recently-past lines ease upward
    # in sync with their slot shrink rather than snapping.
    ANCHOR_Y    = HEIGHT * 0.40
    n           = len(phrases)
    springs     = [0.0] * n
    TAU_BELOW   = 0.10
    TAU_PAST    = GROW_OUT_DUR * 0.9
    offsets     = []

    for frame_num in range(total_frames):
        t          = frame_num / FPS
        active_idx = find_active(phrases, t)
        brk        = in_break(breaks, t)

        slots, heights, cumulative = build_layout(phrases, active_idx, brk, t)

        # shared layout target: offset that places active line at anchor
        target = 0.0
        for si, slot in enumerate(slots):
            if slot["type"] == "phrase" and slot["idx"] == active_idx:
                mid    = cumulative[si] + heights[si] * 0.5
                target = ANCHOR_Y - mid
                break

        frame_offsets = {}
        for i in range(n):
            p = phrases[i]
            if i == active_idx:
                springs[i] = target          # snap active line
            elif i > active_idx:
                tau = TAU_BELOW              # not yet sung — lag below
                k   = 1.0 - math.exp(-1.0 / (FPS * tau))
                springs[i] += (target - springs[i]) * k
            else:
                elapsed = t - p["end"]
                if elapsed < GROW_OUT_DUR:
                    tau = TAU_PAST           # just finished — slow spring upward
                else:
                    tau = TAU_BELOW * 0.5   # settled — follow closely
                k = 1.0 - math.exp(-1.0 / (FPS * tau))
                springs[i] += (target - springs[i]) * k
            frame_offsets[i] = springs[i]

        offsets.append(frame_offsets)

    return offsets

# ══════════════════════════════════════════
# SHADOW / DRAWING HELPERS
# ══════════════════════════════════════════

def _text_bbox_tight(text, font):
    """Return (w, h) of text using a throwaway draw."""
    tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
    bb  = tmp.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]

def draw_text_opaque(draw, pos, text, font, fill, alpha_mul=1.0):
    """Hard drop-shadow + coloured text for RGB/green-screen mode."""
    x, y = int(pos[0]), int(pos[1])
    draw.text((x + SHADOW_OFFSET, y + SHADOW_OFFSET), text, font=font, fill=(0, 0, 0))
    col = tuple(int(c * alpha_mul) for c in fill[:3])
    draw.text((x, y), text, font=font, fill=col)

def draw_text_transparent(img, pos, text, font, fill, alpha_mul=1.0,
                           shadow_blur=SHADOW_BLUR, shadow_offset=SHADOW_OFFSET):
    """
    Blurred shadow + RGBA text composited onto img.
    Works on a tight bounding box to avoid allocating full-canvas images per glyph.
    """
    x, y = int(pos[0]), int(pos[1])

    # measure tight bbox
    tmp_draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    bb = tmp_draw.textbbox((0, 0), text, font=font)
    # add padding for shadow blur + offset
    pad = shadow_blur * 2 + shadow_offset + 4
    bx0 = max(0, x + bb[0] - pad)
    by0 = max(0, y + bb[1] - pad)
    bx1 = min(img.width,  x + bb[2] + pad)
    by1 = min(img.height, y + bb[3] + pad)
    bw, bh = bx1 - bx0, by1 - by0
    if bw <= 0 or bh <= 0:
        return

    # local origin within the tile
    lx = x - bx0
    ly = y - by0

    # shadow tile
    shadow_tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(shadow_tile).text(
        (lx + shadow_offset, ly + shadow_offset),
        text, font=font, fill=(0, 0, 0, int(200 * alpha_mul))
    )
    shadow_tile = shadow_tile.filter(ImageFilter.GaussianBlur(radius=shadow_blur))

    # text tile
    text_tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    r, g, b = fill[:3]
    ImageDraw.Draw(text_tile).text(
        (lx, ly), text, font=font, fill=(r, g, b, int(255 * alpha_mul))
    )

    # composite both tiles onto img at (bx0, by0)
    region = img.crop((bx0, by0, bx1, by1))
    region.alpha_composite(shadow_tile)
    region.alpha_composite(text_tile)
    img.paste(region, (bx0, by0))


def draw_text(img, draw, pos, text, font, fill, alpha_mul=1.0,
              transparent=False, greenscreen=False,
              shadow_blur=SHADOW_BLUR, shadow_offset=SHADOW_OFFSET):
    """Unified text drawing: delegates to transparent or opaque helper."""
    if transparent:
        draw_text_transparent(img, pos, text, font, fill, alpha_mul,
                              shadow_blur=shadow_blur, shadow_offset=shadow_offset)
    else:
        draw_text_opaque(draw, pos, text, font, fill, alpha_mul)


# ══════════════════════════════════════════
# WIPE HELPERS
# ══════════════════════════════════════════

def draw_wipe_opaque(img, draw, pos, text, font,
                     fill_base, fill_sung, wipe_frac, alpha_mul):
    """Left-to-right wipe for RGB/green-screen mode."""
    bb      = draw.textbbox((0, 0), text, font=font)
    tw      = bb[2] - bb[0]
    wipe_px = int(tw * clamp(wipe_frac))
    x, y    = int(pos[0]), int(pos[1])

    draw.text((x + SHADOW_OFFSET, y + SHADOW_OFFSET), text, font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font,
              fill=tuple(int(c * alpha_mul) for c in fill_base[:3]))

    if wipe_px > 0 and tw > 0:
        sung_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        r, g, b = fill_sung[:3]
        ImageDraw.Draw(sung_layer).text(
            (x, y), text, font=font,
            fill=(int(r * alpha_mul), int(g * alpha_mul), int(b * alpha_mul), 255)
        )
        alpha_ch = sung_layer.getchannel("A")
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rectangle([x, 0, x + wipe_px, img.height], fill=255)
        sung_layer.putalpha(ImageChops.multiply(alpha_ch, mask))
        base_rgba = img.convert("RGBA")
        base_rgba.alpha_composite(sung_layer)
        img.paste(base_rgba.convert("RGB"))


def draw_wipe_transparent(img, pos, text, font,
                           fill_base, fill_sung, wipe_frac, alpha_mul):
    """Left-to-right wipe for RGBA transparent mode, tile-optimised."""
    x, y = int(pos[0]), int(pos[1])

    tmp_draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    bb  = tmp_draw.textbbox((0, 0), text, font=font)
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

    # shadow
    shadow_tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(shadow_tile).text(
        (lx + SHADOW_OFFSET, ly + SHADOW_OFFSET), text, font=font,
        fill=(0, 0, 0, int(180 * alpha_mul))
    )
    shadow_tile = shadow_tile.filter(ImageFilter.GaussianBlur(radius=SHADOW_BLUR))

    # base (un-sung) text tile
    base_tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(base_tile).text(
        (lx, ly), text, font=font,
        fill=(*fill_base[:3], int(255 * alpha_mul))
    )

    wipe_px = int(tw * clamp(wipe_frac))

    region = img.crop((bx0, by0, bx1, by1))
    region.alpha_composite(shadow_tile)
    region.alpha_composite(base_tile)

    if wipe_px > 0:
        sung_tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        ImageDraw.Draw(sung_tile).text(
            (lx, ly), text, font=font,
            fill=(*fill_sung[:3], int(255 * alpha_mul))
        )
        # mask: only keep columns up to wipe_px relative to text origin
        clip_x1_local = lx + wipe_px - bb[0]
        mask_img = Image.new("L", (bw, bh), 0)
        c0 = max(0, lx + bb[0] - bb[0])   # = lx (text start in tile)
        c1 = min(bw, lx + wipe_px)
        ImageDraw.Draw(mask_img).rectangle([c0, 0, c1, bh], fill=255)
        sung_tile.putalpha(ImageChops.multiply(sung_tile.getchannel("A"), mask_img))
        region.alpha_composite(sung_tile)

    img.paste(region, (bx0, by0))


# ══════════════════════════════════════════
# INTERMISSION DOTS  (sequential travelling pulse)
# ══════════════════════════════════════════

def draw_dots(img, draw, cy, brk, t, transparent, greenscreen):
    """
    Three dots at vertical position cy, left-aligned with text margin.
    A single bright peak travels dot-by-dot across the three, then repeats.
    """
    elapsed = t - brk["start"]
    remain  = brk["end"] - t
    fade_in  = ease_out(clamp(elapsed / DOT_FADE_IN))
    fade_out = ease_out(clamp(remain  / DOT_FADE_OUT))
    group_alpha = fade_in * fade_out
    if group_alpha <= 0.0:
        return

    # travelling peak: position 0→DOT_COUNT cycles over DOT_PULSE_PERIOD
    peak_pos = (elapsed % DOT_PULSE_PERIOD) / DOT_PULSE_PERIOD * DOT_COUNT

    cx_start = MARGIN_LEFT + 18

    for d in range(DOT_COUNT):
        # distance of this dot from the travelling peak (wrap around)
        dist  = abs(peak_pos - d)
        dist  = min(dist, DOT_COUNT - dist)   # wrap
        # brightness falls off with distance; peak width ≈ 0.7 dots
        pulse = clamp(1.0 - dist / 0.9)
        pulse = ease_in_out(pulse)

        dot_alpha = group_alpha * lerp(0.15, 1.0, pulse)
        cx = cx_start + d * DOT_SPACING
        r  = int(DOT_RADIUS * lerp(0.55, 1.0, pulse))
        col = lerp_color(DOT_DIM, DOT_COLOR, pulse)

        if transparent:
            dot_tile = Image.new("RGBA", (r * 2 + 2, r * 2 + 2), (0, 0, 0, 0))
            ImageDraw.Draw(dot_tile).ellipse(
                [0, 0, r * 2, r * 2],
                fill=(*col, int(255 * dot_alpha))
            )
            tx = cx - r
            ty = int(cy) - r
            if 0 <= tx < img.width and 0 <= ty < img.height:
                region = img.crop((tx, ty, tx + r*2 + 2, ty + r*2 + 2))
                region.alpha_composite(dot_tile)
                img.paste(region, (tx, ty))
        else:
            fill = tuple(int(c * dot_alpha) for c in col)
            draw.ellipse([cx - r, int(cy) - r, cx + r, int(cy) + r], fill=fill)


# ══════════════════════════════════════════
# RENDER ONE FRAME
# ══════════════════════════════════════════

def render_frame(phrases, roman_lines, title, frame_num,
                 line_offsets,
                 breaks, transparent, greenscreen):
    t = frame_num / FPS

    if transparent:
        img  = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    elif greenscreen:
        img  = Image.new("RGB",  (WIDTH, HEIGHT), GS_COLOR)
    else:
        img  = Image.new("RGB",  (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    active_idx = find_active(phrases, t)
    brk        = in_break(breaks, t)

    slots, heights, cumulative = build_layout(phrases, active_idx, brk, t)

    for si, slot in enumerate(slots):
        # each line has its own sprung offset; dot slots borrow the offset of
        # the phrase immediately before them (prev_phrase_idx in the break).
        phrase_idx_of_slot = slot.get("idx", active_idx)
        offset = line_offsets.get(phrase_idx_of_slot, line_offsets.get(active_idx, 0.0))

        y = cumulative[si] + offset
        h = heights[si]

        if y + h < -20 or y > HEIGHT + 20:
            continue

        # ── dot slot ─────────────────────────────────────────────────
        if slot["type"] == "dots":
            cy = y + h * 0.18
            draw_dots(img, draw, cy, brk, t, transparent, greenscreen)
            continue

        # ── phrase slot ───────────────────────────────────────────────
        i = slot["idx"]
        p = slot["p"]

        fs    = line_fontsize(p, i, active_idx, t)
        state = 'active' if i == active_idx else ('past' if i < active_idx else 'idle')

        dist = abs(i - max(0, active_idx))
        if state == 'past':
            alpha = lerp(1.0, 0.15, clamp((dist - 1) / 5.0))
        elif state == 'idle':
            alpha = lerp(0.85, 0.1, clamp((dist - 1) / 6.0))
        else:
            alpha = 1.0

        shrinking = state == 'past' and (t - p["end"]) < GROW_OUT_DUR
        use_bold  = state == 'active' or shrinking
        fnt       = get_font(FONT_BOLD if use_bold else FONT_REGULAR, fs)

        roman_syls = roman_lines[i] if roman_lines else []

        if state == 'active':
            fs_actual, fnt, needs_wrap = fit_font_size(p["syllables"], FONT_BOLD, fs, draw)
            ref    = draw.textbbox((0, 0), "あ", font=fnt)
            draw_y = y - ref[1]
            line_h = ref[3] - ref[1]
            is_syllable_synced = len(p["syllables"]) > 1

            rows = wrap_syllables(p["syllables"], fnt, draw) if needs_wrap else [p["syllables"]]

            row_y = draw_y
            for row in rows:
                x = float(MARGIN_LEFT)
                for syl in row:
                    sb, se = syl["begin"], syl["end"]
                    text   = syl["text"]
                    bb     = draw.textbbox((0, 0), text, font=fnt)
                    sw     = bb[2] - bb[0]

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
                                             wipe_frac, alpha)
                    else:
                        wf  = ease_out(clamp((t - sb) / WORD_FADE_DUR))
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
                roman_fs  = max(18, int(fs_actual * SIZE_ROMAN_SCALE))
                roman_fnt = get_font(FONT_BOLD, roman_fs)
                roman_y   = row_y + 14
                rx        = float(MARGIN_LEFT + 6)
                roman_str = "".join(s["text"] for s in roman_syls)
                if is_syllable_synced:
                    for rsyl in roman_syls:
                        rtext = rsyl["text"]
                        rbb   = draw.textbbox((0, 0), rtext, font=roman_fnt)
                        rsw   = rbb[2] - rbb[0]
                        rb, re = rsyl["begin"], rsyl["end"]
                        if t < rb:
                            wipe_frac = 0.0
                        elif t <= re:
                            wipe_frac = (t - rb) / max(re - rb, 0.01)
                        else:
                            wipe_frac = 1.0
                        if transparent:
                            draw_wipe_transparent(img, (rx, roman_y), rtext, roman_fnt,
                                                  ROMAN_COLOR, (255, 255, 255),
                                                  wipe_frac, alpha)
                        else:
                            draw_wipe_opaque(img, draw, (rx, roman_y), rtext, roman_fnt,
                                             ROMAN_COLOR, (255, 255, 255),
                                             wipe_frac, alpha)
                        rx += rsw
                else:
                    # line-by-line — no fake wipe, just draw sung colour
                    draw_text(img, draw, (rx, roman_y), roman_str, roman_fnt,
                              ACTIVE_SUNG, alpha,
                              transparent=transparent, greenscreen=greenscreen,
                              shadow_blur=4, shadow_offset=2)

        else:
            ref       = draw.textbbox((0, 0), "あ", font=fnt)
            glyph_h   = ref[3] - ref[1]
            # Vertically centre the glyph within the animated slot height (minus LINE_GAP).
            # This means the text smoothly rides down to its resting position as the
            # slot shrinks from LINE_H_ACTIVE → LINE_H_IDLE, rather than snapping.
            slot_inner = h - LINE_GAP
            draw_y     = y + (slot_inner - glyph_h) * 0.5 - ref[1]
            full       = "".join(s["text"] for s in p["syllables"])
            base       = DONE_COLOR if state == 'past' else IDLE_COLOR

            draw_text(img, draw, (MARGIN_LEFT, int(draw_y)), full, fnt, base, alpha,
                      transparent=transparent, greenscreen=greenscreen)

            if roman_syls:
                roman_fnt = get_font(FONT_REGULAR, SIZE_ROMAN_IDLE)
                roman_y   = draw_y + glyph_h + 8
                roman_col = IDLE_COLOR if state == 'idle' else DONE_COLOR
                roman_str = "".join(s["text"] for s in roman_syls)
                draw_text(img, draw, (MARGIN_LEFT + 6, int(roman_y)), roman_str, roman_fnt,
                          roman_col, alpha * 0.75,
                          transparent=transparent, greenscreen=greenscreen,
                          shadow_blur=3, shadow_offset=2)

    _draw_chrome(img, draw, title, transparent, greenscreen)
    _save_frame(img, frame_num, transparent)


# ══════════════════════════════════════════
# CHROME + SAVE
# ══════════════════════════════════════════

def _draw_chrome(img, draw, title, transparent, greenscreen):
    if title:
        tf  = get_font(FONT_BOLD, SIZE_TITLE)
        pos = (MARGIN_LEFT, 28)
        draw_text(img, draw, pos, title, tf, TITLE_COLOR, 1.0,
                  transparent=transparent, greenscreen=greenscreen,
                  shadow_blur=10, shadow_offset=4)

    wf  = get_font(FONT_REGULAR, SIZE_WATERMARK)
    wm  = "made with Nuisance"
    wbb = draw.textbbox((0, 0), wm, font=wf)
    wx  = WIDTH  - (wbb[2] - wbb[0]) - 40
    wy  = HEIGHT - (wbb[3] - wbb[1]) - 30
    draw_text(img, draw, (wx, wy), wm, wf, WATERMARK_COL, 0.6,
              transparent=transparent, greenscreen=greenscreen,
              shadow_blur=3, shadow_offset=2)


def _save_frame(img, frame_num, transparent):
    path = os.path.join(OUTPUT_DIR, f"frame_{frame_num:06d}.png")
    img.save(path)


# ══════════════════════════════════════════
# PROGRESS
# ══════════════════════════════════════════

_progress_lock = threading.Lock()
_done_count    = 0

def on_frame_done(total):
    global _done_count
    with _progress_lock:
        _done_count += 1
        i = _done_count
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
        prog="nuisance.py",
        description="Nuisance — time-synced lyric video renderer. Outputs PNG frame sequences ready for ffmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # quick preview (renders only 1 frame per second, skips render prompt)
  python nuisance.py --input lyrics.ttml --duration 217 --preview

  # full render, opaque
  python nuisance.py --input lyrics.ttml --duration 217

  # transparent RGBA (for compositing)
  python nuisance.py --input lyrics.ttml --duration 217 --transparent

  # green screen + romanization
  python nuisance.py --input lyrics.ttml --duration 217 --greenscreen --romanise roman.ttml

encode output:
  ffmpeg -framerate 60 -i frames/frame_%06d.png -i audio.mp3 \\\\
         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4
        """
    )
    parser.add_argument("--input",        required=True,
                        help="Path to TTML lyrics file")
    parser.add_argument("--duration",     type=float, required=True,
                        help="Track length in seconds")
    parser.add_argument("--start",        type=int,   default=0,
                        help="Resume from this frame index (default: 0)")
    parser.add_argument("--workers",      type=int,   default=os.cpu_count(),
                        help="Thread count for parallel rendering (default: CPU count)")
    parser.add_argument("--transparent",  action="store_true",
                        help="Export RGBA PNGs with blurred text shadows (slower, for alpha compositing)")
    parser.add_argument("--greenscreen",  action="store_true",
                        help="Solid green (#00ff00) background for chroma key (fast)")
    parser.add_argument("--romanise",     default=None,
                        help="Path to a second TTML file with romanized lyrics (shown below each line)")
    parser.add_argument("--preview",      action="store_true",
                        help="Render 1 frame per second only — fast visual check, skips confirmation prompt")
    args = parser.parse_args()
    if args.transparent and args.greenscreen:
        print("  error: --transparent and --greenscreen are mutually exclusive.")
        return

    title, phrases = load_ttml(args.input)

    roman_lines = None
    if args.romanise:
        roman_entries = load_roman_ttml(args.romanise)
        roman_lines   = align_romanization(phrases, roman_entries)
        print(f"  romanization: {sum(1 for r in roman_lines if r)} / {len(roman_lines)} lines matched"  )

    breaks = detect_breaks(phrases, args.duration)

    mode = "transparent RGBA" if args.transparent else ("green screen" if args.greenscreen else "opaque")
    print(f"\n  ♪ {title or '(no title)'}")
    print(f"  {len(phrases)} lines · {args.duration}s · mode: {mode}")
    if breaks:
        print(f"  {len(breaks)} intermission break(s) detected")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total = int(args.duration * FPS)

    if args.preview:
        frames = list(range(args.start, total, FPS))
        print(f"  PREVIEW MODE — {len(frames)} frames (1/sec) · {args.workers} workers")
    else:
        frames = list(range(args.start, total))
        print(f"  {total} frames @ {FPS}fps · {args.workers} workers")

    print(f"  output → {OUTPUT_DIR}/\n")

    if not args.preview:
        if input("  Render? (y/n): ").lower() != "y":
            return

    print("  Pre-computing scroll offsets …", end="", flush=True)
    offsets = precompute_offsets(phrases, breaks, total)
    print(" done.")

    _done_count = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(
                render_frame,
                phrases,
                roman_lines,
                title,
                f,
                offsets[f],
                breaks,
                args.transparent,
                args.greenscreen,
            ): f
            for f in frames
        }
        for fut in as_completed(futs):
            fut.result()
            on_frame_done(total)

    print(f"\n\n  Done. Encode with:")
    if args.transparent:
        print(f"  # lossless with alpha (ProRes 4444):")
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -c:v prores_ks -pix_fmt yuva444p10le -profile:v 4444 out.mov")
        print(f"")
        print(f"  # composite over background video:")
        print(f"  ffmpeg -i background.mp4 -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -i audio.mp3 -filter_complex \"[0:v][1:v]overlay=0:0\" \\")
        print(f"         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4")
    elif args.greenscreen:
        print(f"  # encode green-screen video:")
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -c:v libx264 -pix_fmt yuv420p -crf 0 out_gs.mp4")
        print(f"")
        print(f"  # chroma-key composite in ffmpeg (replace green with background.mp4):")
        print(f"  ffmpeg -i background.mp4 -i out_gs.mp4 \\")
        print(f"         -filter_complex \"[1:v]colorkey=0x00ff00:0.3:0.1[ov];[0:v][ov]overlay\" \\")
        print(f"         -i audio.mp3 -c:a aac -b:a 192k -shortest out.mp4")
    else:
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png -i audio.mp3 \\")
        print(f"         -c:v h264_mediacodec -c:a aac -b:a 192k -shortest out.mp4")
    print()


if __name__ == "__main__":
    main()


