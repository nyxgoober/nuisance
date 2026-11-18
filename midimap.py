#!/usr/bin/env python3
"""
midimap.py — Synthesia-style MIDI visualiser
Renders falling note blocks onto a piano keyboard, one colour per track.
Output: PNG frame sequences → encode with ffmpeg.

Requirements: pip install mido pillow
"""

import argparse
import bisect
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import mido
from PIL import Image, ImageDraw, ImageFilter, ImageFont

_HERE = os.path.dirname(os.path.abspath(__file__))

# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════

WIDTH      = 1920
HEIGHT     = 1080
FPS        = 60
OUTPUT_DIR = "frames"

FONT_BOLD    = os.path.join(_HERE, "ZenKakuGothicNew-Bold.ttf")
FONT_REGULAR = os.path.join(_HERE, "ZenKakuGothicNew-Regular.ttf")

# How many seconds of notes are visible above the keyboard
LOOKAHEAD    = 3.0

# Keyboard layout
KEY_AREA_H   = 180      # total height of keyboard at bottom
WHITE_KEY_H  = KEY_AREA_H
BLACK_KEY_H  = int(KEY_AREA_H * 0.60)
NOTE_AREA_H  = HEIGHT - KEY_AREA_H   # falling note area height

# MIDI note range to display (A0=21 … C8=108 covers 88-key grand piano)
MIDI_LOW     = 21    # A0
MIDI_HIGH    = 108   # C8

# Note block visual
NOTE_RADIUS  = 6     # rounded corner radius
NOTE_MIN_H   = 8     # minimum block height in pixels
NOTE_GAP     = 4     # gap between stacked notes on same key
GLOW_BLUR    = 18    # gaussian blur radius for hit glow
GLOW_ALPHA   = 160   # max glow opacity (0–255)

# Colours
BG_COLOR      = (8,   8,  12)
GS_COLOR      = (0, 255,   0)
WK_COLOR      = (220, 220, 228)   # white key resting
BK_COLOR      = (18,  18,  24)    # black key resting
DIVIDER_COL   = (30,  30,  40)    # thin line between note area and keyboard
LABEL_COL     = (100, 100, 115)   # octave labels on keyboard
WATERMARK_COL = (60,  60,  70)
TITLE_COL     = (180, 180, 190)

SIZE_TITLE     = 52
SIZE_TITLE_MIN = 28
SIZE_WATERMARK = 22
SIZE_LABEL     = 18

SHADOW_OFFSET  = 3
SHADOW_BLUR    = 6

# Per-track colour palette — vivid, readable on dark bg
TRACK_PALETTE = [
    (100, 180, 255),   # soft blue
    (255, 130,  90),   # coral
    (120, 220, 140),   # mint green
    (220, 140, 255),   # lavender
    (255, 210,  70),   # amber
    (80,  210, 210),   # teal
    (255, 120, 160),   # pink
    (160, 200,  80),   # lime
]

# ══════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════

def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else (hi if x > hi else x)

def lerp(a, b, t):
    return a + (b - a) * clamp(t)

def lerp_color(ca, cb, t):
    t = clamp(t)
    return (
        int(ca[0] + (cb[0] - ca[0]) * t),
        int(ca[1] + (cb[1] - ca[1]) * t),
        int(ca[2] + (cb[2] - ca[2]) * t),
    )

def ease_out(t):
    t = clamp(t)
    u = 1.0 - t
    return 1.0 - u * u * u

def alpha_paste(dst, src, x, y):
    """In-place Porter-Duff alpha_composite with automatic bounds clipping."""
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


# ══════════════════════════════════════════
# MIDI PARSING  (O(log T) tempo map, accurate track names & note stacks)
# ══════════════════════════════════════════

def _build_tempo_converter(tempo_events, tpb):
    """
    Given [(abs_tick, tempo_us), ...], build an O(log T) tick->seconds function.
    """
    tpb = max(1, abs(tpb))
    # Sort by tick, keeping the last tempo change at identical ticks
    dedup = {0: 500_000}
    for tick, tempo in sorted(tempo_events, key=lambda x: x[0]):
        dedup[max(0, tick)] = tempo

    ticks = sorted(dedup.keys())
    tempos = [dedup[tk] for tk in ticks]
    cum_sec = [0.0]
    for i in range(1, len(ticks)):
        dtick = ticks[i] - ticks[i - 1]
        cum_sec.append(cum_sec[-1] + (dtick / tpb) * (tempos[i - 1] / 1_000_000.0))

    def ticks_to_sec(tick):
        idx = bisect.bisect_right(ticks, tick) - 1
        return cum_sec[idx] + ((tick - ticks[idx]) / tpb) * (tempos[idx] / 1_000_000.0)

    return ticks_to_sec


def load_midi(path):
    """
    Returns:
      tracks   — list of lists of note dicts: {pitch, start, end, velocity}
      names    — matching list of track name strings (aligned 1-to-1 with tracks)
      duration — float seconds (last note-off + 1s tail)
    """
    mid = mido.MidiFile(path)
    tpb = mid.ticks_per_beat

    global_tempos = []
    if mid.type != 2:
        for tr in mid.tracks:
            abs_tick = 0
            for msg in tr:
                abs_tick += getattr(msg, "time", 0)
                if msg.type == "set_tempo":
                    global_tempos.append((abs_tick, msg.tempo))
    global_t2s = _build_tempo_converter(global_tempos, tpb)

    tracks   = []
    names    = []
    duration = 0.0

    for tr in mid.tracks:
        if mid.type == 2:
            tr_tempos = []
            tk = 0
            for msg in tr:
                tk += getattr(msg, "time", 0)
                if msg.type == "set_tempo":
                    tr_tempos.append((tk, msg.tempo))
            ticks_to_sec = _build_tempo_converter(tr_tempos, tpb)
        else:
            ticks_to_sec = global_t2s

        abs_tick   = 0
        open_notes = {}   # (channel, pitch) -> list of (start_sec, velocity)
        notes      = []

        for msg in tr:
            abs_tick += getattr(msg, "time", 0)
            if msg.type not in ("note_on", "note_off"):
                continue

            t_sec = ticks_to_sec(abs_tick)
            ch = getattr(msg, "channel", 0)
            key = (ch, msg.note)

            if msg.type == "note_on" and msg.velocity > 0:
                open_notes.setdefault(key, []).append((t_sec, msg.velocity))
            else:
                stack = open_notes.get(key)
                if stack:
                    start, vel = stack.pop(0)
                    end = max(start + 0.02, t_sec)
                    notes.append({
                        "pitch":    msg.note,
                        "start":    start,
                        "end":      end,
                        "velocity": vel,
                    })
                    duration = max(duration, end)

        # Close any notes still open at end of track
        for (_, pitch), stack in open_notes.items():
            for start, vel in stack:
                end = start + 0.1
                notes.append({
                    "pitch": pitch, "start": start,
                    "end": end, "velocity": vel,
                })
                duration = max(duration, end)

        if notes:
            raw_name = tr.name.strip() if getattr(tr, "name", None) else ""
            name = raw_name or f"Track {len(tracks) + 1}"
            tracks.append(notes)
            names.append(name)

    return tracks, names, (duration + 1.0 if duration > 0 else 0.0)


# ══════════════════════════════════════════
# KEYBOARD GEOMETRY
# ══════════════════════════════════════════

_BLACK = {1, 3, 6, 8, 10}   # semitone offsets within octave

def is_black(pitch):
    return (pitch % 12) in _BLACK

def white_keys_in_range(lo, hi):
    return [p for p in range(lo, hi + 1) if not is_black(p)]

def get_actual_range(tracks):
    """
    Return (lo, hi) MIDI pitch range to display.
    Ensures valid ordering, white-key boundaries, and at least a 2-octave span.
    """
    lo, hi = 127, 0
    for track in tracks:
        for n in track:
            p = n["pitch"]
            if p < lo:
                lo = p
            if p > hi:
                hi = p
    if lo > hi:
        return MIDI_LOW, MIDI_HIGH

    # Clamp to 88-key range only when notes overlap [MIDI_LOW, MIDI_HIGH]
    if hi >= MIDI_LOW and lo <= MIDI_HIGH:
        lo = max(lo, MIDI_LOW)
        hi = min(hi, MIDI_HIGH)
        if lo > hi:
            lo, hi = MIDI_LOW, MIDI_HIGH

    # Ensure at least 24 semitones (2 octaves) so keys aren't absurdly wide
    if hi - lo < 24:
        mid_p = (lo + hi) // 2
        lo = max(0, mid_p - 12)
        hi = min(127, lo + 24)
        lo = max(0, hi - 24)

    # Expand to white-key boundaries so edge black keys always have white neighbours
    if is_black(lo) and lo > 0:
        lo -= 1
    if is_black(hi) and hi < 127:
        hi += 1

    return lo, hi


def build_key_geometry(lo, hi):
    whites = white_keys_in_range(lo, hi)
    n_white = len(whites)
    if n_white == 0:
        return {}

    margin  = 40
    total_w = WIDTH - 2 * margin
    wk_w    = total_w / n_white

    white_x = {p: margin + idx * wk_w for idx, p in enumerate(whites)}

    geom = {}
    for p in range(lo, hi + 1):
        if not is_black(p):
            geom[p] = {"x": white_x[p], "w": max(2.0, wk_w - 1.0), "is_black": False}
        else:
            lx = white_x.get(p - 1)
            rx = white_x.get(p + 1)
            bw = max(2.0, wk_w * 0.55)
            if lx is not None:
                bx = lx + wk_w - bw * 0.5
            elif rx is not None:
                bx = rx - bw * 0.5
            else:
                continue
            geom[p] = {"x": bx, "w": bw, "is_black": True}

    return geom


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
# SPATIAL INDEX — O(log N + K) lookup per frame
# ══════════════════════════════════════════

def build_index(tracks):
    """
    Build per-track binary-searchable index:
      (sorted_notes, starts, prefix_max_end)
    prefix_max_end is monotonically non-decreasing, allowing O(log N) lower bound
    even when long sustained notes overlap shorter subsequent notes.
    """
    indexed = []
    for notes in tracks:
        s = sorted(notes, key=lambda n: (n["start"], n["end"]))
        starts = [n["start"] for n in s]
        pmax = []
        cur_max = -1.0
        for n in s:
            if n["end"] > cur_max:
                cur_max = n["end"]
            pmax.append(cur_max)
        indexed.append((s, starts, pmax))
    return indexed


def notes_visible(indexed_track, t_now, lookahead):
    notes, starts, pmax = indexed_track
    hi = bisect.bisect_right(starts, t_now + lookahead)
    if hi == 0:
        return ()
    cutoff = t_now - 0.1
    lo = bisect.bisect_left(pmax, cutoff, 0, hi)
    return [notes[i] for i in range(lo, hi) if notes[i]["end"] >= cutoff]


# ══════════════════════════════════════════
# DRAW HELPERS & PRE-RENDERED STATIC LAYERS
# ══════════════════════════════════════════

def rounded_rect(draw, bbox, radius, fill):
    x0, y0, x1, y1 = bbox
    if x1 < x0 or y1 < y0:
        return
    r = max(0, min(radius, (x1 - x0) // 2, (y1 - y0) // 2))
    if r == 0:
        draw.rectangle([x0, y0, x1, y1], fill=fill)
    else:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill)


def draw_text_simple(draw, pos, text, font, fill, shadow=True):
    if not text:
        return
    x, y = int(pos[0]), int(pos[1])
    if shadow:
        draw.text((x + SHADOW_OFFSET, y + SHADOW_OFFSET), text,
                  font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=fill)


def draw_text_transparent(img, pos, text, font, fill, alpha_mul=1.0,
                          shadow_blur=SHADOW_BLUR):
    if not text or alpha_mul <= 0.0:
        return
    x, y = int(pos[0]), int(pos[1])
    bb = font.getbbox(text)
    pad = shadow_blur * 2 + SHADOW_OFFSET + 4
    bx0 = max(0, x + bb[0] - pad)
    by0 = max(0, y + bb[1] - pad)
    bx1 = min(img.width,  x + bb[2] + pad)
    by1 = min(img.height, y + bb[3] + pad)
    bw, bh = bx1 - bx0, by1 - by0
    if bw <= 0 or bh <= 0:
        return
    lx, ly = x - bx0, y - by0

    tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text(
        (lx + SHADOW_OFFSET, ly + SHADOW_OFFSET), text,
        font=font, fill=(0, 0, 0, int(200 * alpha_mul))
    )
    tile = tile.filter(ImageFilter.GaussianBlur(radius=shadow_blur))

    text_t = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    r, g, b = fill[:3]
    ImageDraw.Draw(text_t).text(
        (lx, ly), text, font=font, fill=(r, g, b, int(255 * alpha_mul))
    )
    tile.alpha_composite(text_t)
    alpha_paste(img, tile, bx0, by0)


def _draw_white_key(draw, pitch, g, key_fill, lbl_font):
    key_top = NOTE_AREA_H
    x0 = int(g["x"])
    x1 = int(g["x"] + g["w"])
    y0 = key_top
    y1 = key_top + WHITE_KEY_H - 1
    draw.rectangle([x0, y0, x1, y1], fill=key_fill)
    draw.line([(x1, y0), (x1, y1)], fill=(160, 160, 170))
    if pitch % 12 == 0:
        octave = pitch // 12 - 1
        lbl = f"C{octave}"
        lx = int(g["x"] + 3)
        ly = key_top + WHITE_KEY_H - SIZE_LABEL - 6
        draw.text((lx, ly), lbl, font=lbl_font, fill=LABEL_COL)


def _draw_black_key(draw, g, key_fill):
    key_top = NOTE_AREA_H
    x0 = int(g["x"])
    x1 = int(g["x"] + g["w"])
    y0 = key_top
    y1 = key_top + BLACK_KEY_H
    draw.rectangle([x0, y0, x1, y1], fill=key_fill)
    draw.line([(x0, y0), (x1, y0)], fill=(50, 50, 60))


def build_static_scene(geom, title, track_names, track_colors, transparent, greenscreen):
    """
    Pre-render static background + resting keyboard + watermark once,
    plus header metadata (title & wrapped track legend) so render_frame
    only copies base_canvas and draws active elements.
    """
    key_top = NOTE_AREA_H
    if transparent:
        base = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    elif greenscreen:
        base = Image.new("RGB",  (WIDTH, HEIGHT), GS_COLOR)
    else:
        base = Image.new("RGB",  (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(base)

    # Background vertical gradient + scanlines (opaque mode)
    if not transparent and not greenscreen:
        for y in range(0, key_top, 2):
            frac = y / key_top
            r = int(lerp(BG_COLOR[0], BG_COLOR[0] + 6, frac))
            g = int(lerp(BG_COLOR[1], BG_COLOR[1] + 6, frac))
            b = int(lerp(BG_COLOR[2], BG_COLOR[2] + 8, frac))
            draw.line([(0, y), (WIDTH, y)], fill=(r, g, b))
            draw.line([(0, y + 1), (WIDTH, y + 1)], fill=(r, g, b))
        for y in range(0, key_top, 60):
            draw.line([(0, y), (WIDTH, y)], fill=(20, 20, 28))

    # Divider & resting keyboard
    draw.rectangle([0, key_top - 1, WIDTH, key_top + 1], fill=DIVIDER_COL)
    lbl_font = get_font(FONT_REGULAR, SIZE_LABEL)
    for pitch, g in geom.items():
        if not g["is_black"]:
            _draw_white_key(draw, pitch, g, WK_COLOR, lbl_font)
    for pitch, g in geom.items():
        if g["is_black"]:
            _draw_black_key(draw, g, BK_COLOR)

    # Watermark (sits on bottom-right of keyboard, never overlapped by falling notes)
    wf  = get_font(FONT_REGULAR, SIZE_WATERMARK)
    wm  = "made with Nuisance"
    wbb = wf.getbbox(wm)
    wx  = WIDTH  - (wbb[2] - wbb[0]) - 40
    wy  = HEIGHT - (wbb[3] - wbb[1]) - 30
    if transparent:
        draw_text_transparent(base, (wx, wy), wm, wf, WATERMARK_COL, alpha_mul=0.6)
    else:
        draw_text_simple(draw, (wx, wy), wm, wf, WATERMARK_COL)

    # Pre-layout title and wrapped track legend (drawn over falling notes)
    title_fs = SIZE_TITLE
    if title:
        while title_fs > SIZE_TITLE_MIN:
            if text_width(title, get_font(FONT_BOLD, title_fs)) <= WIDTH - 100:
                break
            title_fs -= 2

    lf = get_font(FONT_REGULAR, 20)
    legend_items = []
    lx = 50
    ly = (28 + title_fs + 12) if title else 28
    for ti, name in enumerate(track_names):
        tw = text_width(name, lf)
        item_w = tw + 40
        if lx > 50 and lx + item_w > WIDTH - 50:
            lx = 50
            ly += 28
        legend_items.append((lx, ly, name, track_colors[ti]))
        lx += item_w

    header_strip = None
    if transparent and (title or legend_items):
        hdr_h = min(NOTE_AREA_H, max(140, ly + 48))
        header_strip = Image.new("RGBA", (WIDTH, hdr_h), (0, 0, 0, 0))
        hdr_draw = ImageDraw.Draw(header_strip)
        if title:
            tf = get_font(FONT_BOLD, title_fs)
            draw_text_transparent(header_strip, (50, 28), title, tf, TITLE_COL)
        for ix, iy, name, col in legend_items:
            hdr_draw.ellipse([ix, iy + 4, ix + 12, iy + 16], fill=(*col, 255))
            draw_text_transparent(header_strip, (ix + 18, iy), name, lf, TITLE_COL, alpha_mul=0.75)

    return {
        "base": base,
        "title": title,
        "title_fs": title_fs,
        "legend_items": legend_items,
        "header_strip": header_strip,
    }


# ══════════════════════════════════════════
# NOTE → SCREEN POSITION & FRAME RENDER
# ══════════════════════════════════════════

def note_y(note_time, t_now, lookahead):
    key_top = NOTE_AREA_H
    dt      = note_time - t_now
    frac    = dt / lookahead
    return key_top * (1.0 - frac)


def render_frame(tracks_indexed, track_colors, geom, scene,
                 lookahead, frame_num, transparent, greenscreen,
                 output_dir=OUTPUT_DIR):
    t = frame_num / FPS
    img = scene["base"].copy()
    draw = ImageDraw.Draw(img)
    key_top = NOTE_AREA_H

    # Fetch visible notes once per track (reused for both active keys and note blocks)
    visible_per_track = [
        notes_visible(indexed, t, lookahead) for indexed in tracks_indexed
    ]

    # ── collect active pitches for keyboard highlighting ──────────────
    active = {}   # pitch -> (color, velocity, held_sec)
    for ti, vis_notes in enumerate(visible_per_track):
        col = track_colors[ti]
        for n in vis_notes:
            if n["start"] <= t < n["end"]:
                held = t - n["start"]
                p = n["pitch"]
                if p not in active or n["velocity"] > active[p][1]:
                    active[p] = (col, n["velocity"], held)

    # ── update only active keys (and overlapping neighbour black keys) ─
    if active:
        lbl_font = get_font(FONT_REGULAR, SIZE_LABEL)
        black_to_redraw = set()
        for pitch, (col, _, held) in active.items():
            g = geom.get(pitch)
            if g is None:
                continue
            flash = ease_out(clamp(1.0 - held * 8.0))
            if not g["is_black"]:
                key_fill = lerp_color(WK_COLOR, col, lerp(0.35, 0.70, flash))
                _draw_white_key(draw, pitch, g, key_fill, lbl_font)
                # Redraw adjacent black keys whose edges sit atop this white key
                if (pitch - 1) in geom and geom[pitch - 1]["is_black"]:
                    black_to_redraw.add(pitch - 1)
                if (pitch + 1) in geom and geom[pitch + 1]["is_black"]:
                    black_to_redraw.add(pitch + 1)
            else:
                black_to_redraw.add(pitch)

        for bp in black_to_redraw:
            bg = geom[bp]
            if bp in active:
                col, _, held = active[bp]
                flash = ease_out(clamp(1.0 - held * 8.0))
                key_fill = lerp_color(BK_COLOR, col, lerp(0.50, 0.90, flash))
            else:
                key_fill = BK_COLOR
            _draw_black_key(draw, bg, key_fill)

    # ── falling note blocks + narrow glow strip ───────────────────────
    glow_ellipses = []
    max_glow_r = 0

    for ti, vis_notes in enumerate(visible_per_track):
        col = track_colors[ti]
        for n in vis_notes:
            pitch = n["pitch"]
            gm = geom.get(pitch)
            if gm is None:
                continue

            cx = gm["x"] + gm["w"] * 0.5
            bw = max(2.0, gm["w"] - NOTE_GAP * 2)

            y_bottom = note_y(n["start"], t, lookahead)
            y_top    = note_y(n["end"],   t, lookahead)

            y_top    = max(0.0, y_top)
            y_bottom = min(float(key_top - 1), y_bottom)
            if y_bottom <= y_top:
                continue

            block_h = max(NOTE_MIN_H, y_bottom - y_top)
            x0 = int(cx - bw * 0.5)
            x1 = max(x0 + 1, int(cx + bw * 0.5))
            y0 = int(y_top)
            y1 = min(key_top - 1, int(y_top + block_h))
            if y1 <= y0:
                continue

            is_active = n["start"] <= t < n["end"]
            v_bright  = lerp(0.55, 1.0, n["velocity"] / 127.0)

            if is_active:
                fill_col = tuple(int(c * v_bright) for c in col)
                hi_col   = lerp_color(col, (255, 255, 255), 0.5)
            else:
                fill_col = tuple(int(lerp(c, 180, 0.35) * v_bright) for c in col)
                hi_col   = fill_col

            if transparent:
                tw, th = x1 - x0 + 1, y1 - y0 + 1
                note_tile = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
                nt_draw   = ImageDraw.Draw(note_tile)
                rounded_rect(nt_draw, [0, 0, tw - 1, th - 1], NOTE_RADIUS, (*fill_col, 220))
                if tw > 3 and th > 3:
                    rounded_rect(
                        nt_draw,
                        [1, 1, tw - 2, min(6, th - 2)],
                        NOTE_RADIUS,
                        (*hi_col, 180),
                    )
                alpha_paste(img, note_tile, x0, y0)
            else:
                rounded_rect(draw, [x0, y0, x1, y1], NOTE_RADIUS, fill_col)
                if x1 - x0 > 2 and y1 - y0 > 2:
                    rounded_rect(draw, [x0 + 1, y0 + 1, x1 - 1, min(y0 + 6, y1 - 1)],
                                 NOTE_RADIUS, hi_col)

            if is_active:
                gr = max(4, int(bw * (0.8 if transparent else 1.0)))
                if gr > max_glow_r:
                    max_glow_r = gr
                glow_ellipses.append((int(cx), y1, gr, col))

    # Composite active hit glow on a tight horizontal strip (only when notes are active!)
    if glow_ellipses:
        pad = max_glow_r + GLOW_BLUR * 2 + 4
        sy0 = max(0, key_top - pad)
        sy1 = min(HEIGHT, key_top + pad)
        sh  = sy1 - sy0
        if sh > 0:
            glow_strip = Image.new("RGBA", (WIDTH, sh), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow_strip)
            for gcx, gy, gr, gcol in glow_ellipses:
                ly = gy - sy0
                gd.ellipse([gcx - gr, ly - gr, gcx + gr, ly + gr], fill=(*gcol, GLOW_ALPHA))
            glow_strip = glow_strip.filter(ImageFilter.GaussianBlur(radius=GLOW_BLUR))
            if transparent:
                img.alpha_composite(glow_strip, dest=(0, sy0))
            else:
                region = img.crop((0, sy0, WIDTH, sy1)).convert("RGBA")
                region.alpha_composite(glow_strip)
                img.paste(region.convert("RGB"), (0, sy0))
                draw = ImageDraw.Draw(img)

    # ── header overlay (title + track legend) ─────────────────────────
    if transparent:
        if scene["header_strip"] is not None:
            img.alpha_composite(scene["header_strip"], dest=(0, 0))
    else:
        if scene["title"]:
            tf = get_font(FONT_BOLD, scene["title_fs"])
            draw_text_simple(draw, (50, 28), scene["title"], tf, TITLE_COL)
        lf = get_font(FONT_REGULAR, 20)
        for ix, iy, name, col in scene["legend_items"]:
            draw.ellipse([ix, iy + 4, ix + 12, iy + 16], fill=col)
            draw_text_simple(
                draw, (ix + 18, iy), name, lf,
                tuple(int(c * 0.8) for c in col), shadow=False
            )

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
    global _done_count, LOOKAHEAD

    parser = argparse.ArgumentParser(
        prog="midimap.py",
        description="Nuisance — Synthesia-style MIDI visualiser. Outputs PNG frame sequences ready for ffmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # quick preview (1 frame/sec, skips confirmation)
  python midimap.py --input song.mid --preview

  # full render
  python midimap.py --input song.mid

  # transparent with custom lookahead
  python midimap.py --input song.mid --transparent --lookahead 4.0

encode output:
  ffmpeg -framerate 60 -i frames/frame_%06d.png -i audio.mp3 \\
         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4
        """
    )
    parser.add_argument("--input",       required=True,
                        help="Path to .mid / .midi file")
    parser.add_argument("--duration",    type=float, default=None,
                        help="Override duration in seconds (auto-detected if omitted)")
    parser.add_argument("--start",       type=int,   default=0,
                        help="Resume from this frame index (default: 0)")
    parser.add_argument("--workers",     type=int,   default=os.cpu_count() or 1,
                        help="Thread count for parallel rendering (default: CPU count)")
    parser.add_argument("--lookahead",   type=float, default=LOOKAHEAD,
                        help=f"Seconds of notes visible above keyboard (default {LOOKAHEAD})")
    parser.add_argument("--transparent", action="store_true",
                        help="RGBA output with blurred shadows (slower, for alpha compositing)")
    parser.add_argument("--greenscreen", action="store_true",
                        help="Solid green (#00ff00) background for chroma key (fast)")
    parser.add_argument("--title",       default=None,
                        help="Override title shown on screen")
    parser.add_argument("--preview",     action="store_true",
                        help="Render 1 frame per second only — fast visual check, skips confirmation prompt")
    parser.add_argument("--yes", "-y",   action="store_true",
                        help="Skip interactive confirmation prompt")
    args = parser.parse_args()

    if args.transparent and args.greenscreen:
        print("  error: --transparent and --greenscreen are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    if args.lookahead <= 0:
        print("  error: --lookahead must be positive.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.input):
        print(f"  error: file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    LOOKAHEAD = args.lookahead

    print(f"\n  Loading {args.input} …", end="", flush=True)
    tracks, names, auto_dur = load_midi(args.input)
    print(" done.")

    if not tracks:
        print("  No note tracks found in MIDI file.")
        return

    duration = args.duration if (args.duration is not None and args.duration > 0) else auto_dur
    if duration <= 0:
        print("  error: duration must be positive.", file=sys.stderr)
        sys.exit(1)

    title = args.title if args.title is not None else os.path.splitext(os.path.basename(args.input))[0]

    pitch_lo, pitch_hi = get_actual_range(tracks)
    geom = build_key_geometry(pitch_lo, pitch_hi)
    if not geom:
        print("  error: could not construct keyboard geometry.", file=sys.stderr)
        sys.exit(1)

    track_colors   = [TRACK_PALETTE[i % len(TRACK_PALETTE)] for i in range(len(tracks))]
    tracks_indexed = build_index(tracks)
    workers        = max(1, args.workers)
    start_frame    = max(0, args.start)

    mode = "transparent" if args.transparent else ("green screen" if args.greenscreen else "opaque")
    print(f"  ♪ {title}")
    print(f"  {len(tracks)} track(s): {', '.join(names)}")
    print(f"  pitch range: MIDI {pitch_lo}–{pitch_hi}")
    print(f"  duration: {duration:.1f}s · mode: {mode}")

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

    scene = build_static_scene(
        geom, title, names, track_colors, args.transparent, args.greenscreen
    )
    _done_count = 0
    num_to_render = len(frames)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                render_frame,
                tracks_indexed,
                track_colors,
                geom,
                scene,
                LOOKAHEAD,
                f,
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
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -c:v prores_ks -pix_fmt yuva444p10le -profile:v 4444 out.mov")
    elif args.greenscreen:
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -c:v libx264 -pix_fmt yuv420p -crf 0 out_gs.mp4")
    else:
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png -i audio.mp3 \\")
        print(f"         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4")
    print()


if __name__ == "__main__":
    main()
