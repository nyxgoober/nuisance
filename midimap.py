#!/usr/bin/env python3
"""
midimap.py — Synthesia-style MIDI visualiser
Renders falling note blocks onto a piano keyboard, one colour per track.
Output: PNG frame sequences → encode with ffmpeg (same pattern as nuisance.py)

Requirements: pip install mido pillow
"""

import argparse
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import mido
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

# How many seconds of notes are visible above the keyboard
LOOKAHEAD    = 3.0

# Keyboard layout
KEY_AREA_H   = 180      # total height of keyboard at bottom
WHITE_KEY_H  = KEY_AREA_H
BLACK_KEY_H  = int(KEY_AREA_H * 0.60)
NOTE_AREA_H  = HEIGHT - KEY_AREA_H   # falling note area height

# MIDI note range to display (C1=24 … C8=108 covers grand piano)
MIDI_LOW     = 21    # A0
MIDI_HIGH    = 108   # C8

# Note block visual
NOTE_RADIUS  = 6     # rounded corner radius
NOTE_MIN_H   = 8     # minimum block height in pixels
NOTE_GAP     = 2     # gap between stacked notes on same key
GLOW_BLUR    = 18    # gaussian blur radius for hit glow
GLOW_ALPHA   = 160   # max glow opacity (0–255)

# Colours
BG_COLOR     = (8,   8,  12)
GS_COLOR     = (0, 255,   0)
WK_COLOR     = (220, 220, 228)   # white key resting
WK_HIT_COL  = None               # filled from track colour at render time
BK_COLOR     = (18,  18,  24)   # black key resting
DIVIDER_COL  = (30,  30,  40)   # thin line between note area and keyboard
LABEL_COL    = (100, 100, 115)   # octave labels on keyboard
WATERMARK_COL= (60,  60,  70)
TITLE_COL    = (180, 180, 190)

SIZE_TITLE     = 52
SIZE_WATERMARK = 22
SIZE_LABEL     = 18

SHADOW_OFFSET  = 3
SHADOW_BLUR    = 6

# Per-track colour palette — vivid but not garish, readable on dark bg
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
    return max(lo, min(hi, x))

def lerp(a, b, t):
    return a + (b - a) * clamp(t)

def lerp_color(ca, cb, t):
    t = clamp(t)
    return tuple(int(a + (b - a) * t) for a, b in zip(ca, cb))

def ease_out(t):
    t = clamp(t)
    return 1.0 - (1.0 - t) ** 3

# ══════════════════════════════════════════
# MIDI PARSING
# ══════════════════════════════════════════

def load_midi(path):
    """
    Returns:
      tracks  — list of lists of note dicts: {pitch, start, end, velocity}
      names   — list of track name strings
      duration — float seconds (last note-off)
    """
    mid = mido.MidiFile(path)
    tpb = mid.ticks_per_beat

    # Build tempo map: list of (abs_tick, tempo_us)
    tempo_map = [(0, 500000)]   # default 120 BPM
    abs_tick  = 0
    for msg in mid.tracks[0] if mid.type != 2 else []:
        abs_tick += getattr(msg, "time", 0)
        if msg.type == "set_tempo":
            tempo_map.append((abs_tick, msg.tempo))

    def ticks_to_sec(tick):
        sec = 0.0
        prev_tick, prev_tempo = 0, 500000
        for tm_tick, tm_tempo in tempo_map:
            if tm_tick >= tick:
                break
            seg = min(tick, tm_tick) - prev_tick
            sec += seg / tpb * (prev_tempo / 1_000_000)
            prev_tick, prev_tempo = tm_tick, tm_tempo
        sec += (tick - prev_tick) / tpb * (prev_tempo / 1_000_000)
        return sec

    # collect note-on/off events per track
    tracks   = []
    names    = []
    duration = 0.0

    for tr in mid.tracks:
        name = tr.name.strip() or f"Track {len(tracks)+1}"
        names.append(name)

        abs_tick  = 0
        open_notes = {}   # pitch -> (start_sec, velocity)
        notes     = []

        for msg in tr:
            abs_tick += msg.time
            t_sec = ticks_to_sec(abs_tick)

            if msg.type == "note_on" and msg.velocity > 0:
                open_notes[msg.note] = (t_sec, msg.velocity)
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                if msg.note in open_notes:
                    start, vel = open_notes.pop(msg.note)
                    end = t_sec
                    notes.append({
                        "pitch":    msg.note,
                        "start":    start,
                        "end":      end,
                        "velocity": vel,
                    })
                    duration = max(duration, end)

        # close any notes still open at end of track
        for pitch, (start, vel) in open_notes.items():
            notes.append({"pitch": pitch, "start": start,
                          "end": start + 0.1, "velocity": vel})
            duration = max(duration, start + 0.1)

        if notes:
            tracks.append(notes)

    return tracks, names, duration + 1.0   # +1s tail


def get_actual_range(tracks):
    """Return (lo, hi) MIDI pitch actually used, clamped to display range."""
    lo, hi = 127, 0
    for track in tracks:
        for n in track:
            lo = min(lo, n["pitch"])
            hi = max(hi, n["pitch"])
    return max(lo, MIDI_LOW), min(hi, MIDI_HIGH)

# ══════════════════════════════════════════
# KEYBOARD GEOMETRY
# ══════════════════════════════════════════

# MIDI note → is it a black key?
_BLACK = {1, 3, 6, 8, 10}   # offsets within octave

def is_black(pitch):
    return (pitch % 12) in _BLACK

def white_keys_in_range(lo, hi):
    """Return list of MIDI pitches that are white keys within [lo, hi]."""
    return [p for p in range(lo, hi + 1) if not is_black(p)]

def build_key_geometry(lo, hi):
    """
    Returns a dict: pitch -> {x, w, is_black}
    x, w are pixel positions within a virtual keyboard of width WIDTH
    (minus small margins).
    """
    whites = white_keys_in_range(lo, hi)
    n_white = len(whites)
    if n_white == 0:
        return {}

    margin  = 40
    total_w = WIDTH - 2 * margin
    wk_w    = total_w / n_white

    white_x = {}
    for idx, p in enumerate(whites):
        white_x[p] = margin + idx * wk_w

    geom = {}
    for p in range(lo, hi + 1):
        if not is_black(p):
            geom[p] = {"x": white_x[p], "w": wk_w - 1, "is_black": False}
        else:
            # black key sits between its left and right white neighbours
            left_w  = p - 1
            right_w = p + 1
            # find closest white neighbours in range
            lx = white_x.get(left_w)
            rx = white_x.get(right_w)
            if lx is not None and rx is not None:
                bw = wk_w * 0.55
                bx = lx + wk_w - bw * 0.5
            elif lx is not None:
                bw = wk_w * 0.55
                bx = lx + wk_w - bw * 0.5
            elif rx is not None:
                bw = wk_w * 0.55
                bx = rx - bw * 0.5
            else:
                continue
            geom[p] = {"x": bx, "w": bw, "is_black": True}

    return geom

def pitch_to_x_center(pitch, geom):
    if pitch not in geom:
        return None
    g = geom[pitch]
    return g["x"] + g["w"] / 2

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
# SPATIAL INDEX — fast note lookup per frame
# ══════════════════════════════════════════

def build_index(tracks):
    """
    Returns list (one per track) of notes sorted by start time.
    Caller uses binary search to find notes visible in [t - LOOKAHEAD, t].
    """
    import bisect
    indexed = []
    for notes in tracks:
        s = sorted(notes, key=lambda n: n["start"])
        indexed.append(s)
    return indexed

def notes_visible(indexed_track, t_now):
    """
    Notes whose block is visible in the falling window.
    A note is visible if:
      - its start time < t_now + LOOKAHEAD  (block top is still on screen)
      - its end time   > t_now - small_tail (block hasn't fully passed the keyboard)
    Notes fall from top to bottom; start hits keyboard at t_now.
    """
    result = []
    for n in indexed_track:
        # note starts after the visible window top — not on screen yet
        if n["start"] > t_now + LOOKAHEAD:
            break
        # note ended before now — already passed the keyboard
        if n["end"] < t_now - 0.1:
            continue
        result.append(n)
    return result

# ══════════════════════════════════════════
# DRAW HELPERS
# ══════════════════════════════════════════

def rounded_rect(draw, bbox, radius, fill):
    x0, y0, x1, y1 = bbox
    radius = min(radius, (x1 - x0) // 2, (y1 - y0) // 2)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)

def draw_text_simple(draw, pos, text, font, fill, shadow=True):
    x, y = int(pos[0]), int(pos[1])
    if shadow:
        draw.text((x + SHADOW_OFFSET, y + SHADOW_OFFSET), text,
                  font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=fill)

def draw_text_transparent(img, pos, text, font, fill, alpha_mul=1.0,
                           shadow_blur=SHADOW_BLUR):
    x, y = int(pos[0]), int(pos[1])
    tmp_draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    bb = tmp_draw.textbbox((0, 0), text, font=font)
    pad = shadow_blur * 2 + SHADOW_OFFSET + 4
    bx0 = max(0, x + bb[0] - pad)
    by0 = max(0, y + bb[1] - pad)
    bx1 = min(img.width,  x + bb[2] + pad)
    by1 = min(img.height, y + bb[3] + pad)
    bw, bh = bx1 - bx0, by1 - by0
    if bw <= 0 or bh <= 0:
        return
    lx, ly = x - bx0, y - by0

    shadow_t = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(shadow_t).text(
        (lx + SHADOW_OFFSET, ly + SHADOW_OFFSET), text,
        font=font, fill=(0, 0, 0, int(200 * alpha_mul))
    )
    shadow_t = shadow_t.filter(ImageFilter.GaussianBlur(radius=shadow_blur))

    text_t = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    r, g, b = fill[:3]
    ImageDraw.Draw(text_t).text(
        (lx, ly), text, font=font, fill=(r, g, b, int(255 * alpha_mul))
    )
    region = img.crop((bx0, by0, bx1, by1))
    region.alpha_composite(shadow_t)
    region.alpha_composite(text_t)
    img.paste(region, (bx0, by0))

# ══════════════════════════════════════════
# NOTE → SCREEN POSITION
# ══════════════════════════════════════════

def note_y(note_time, t_now):
    """
    Maps a note timestamp to a Y pixel in the note area.
    note_time == t_now            → key_top (hitting the keyboard right now)
    note_time == t_now + LOOKAHEAD → 0       (top of screen, not yet arrived)
    """
    key_top = NOTE_AREA_H
    dt      = note_time - t_now          # positive = still in the future
    frac    = dt / LOOKAHEAD             # 0 = at keyboard, 1 = top of screen
    return key_top * (1.0 - frac)

# ══════════════════════════════════════════
# RENDER ONE FRAME
# ══════════════════════════════════════════

def render_frame(tracks_indexed, track_colors, track_names,
                 geom, pitch_lo, pitch_hi,
                 title, frame_num, transparent, greenscreen):
    t = frame_num / FPS

    if transparent:
        img  = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    elif greenscreen:
        img  = Image.new("RGB",  (WIDTH, HEIGHT), GS_COLOR)
    else:
        img  = Image.new("RGB",  (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    key_top = NOTE_AREA_H   # y-coordinate of the top of the keyboard strip

    # ── background gradient for note area ─────────────────────────────
    if not transparent and not greenscreen:
        for y in range(0, key_top, 2):
            frac = y / key_top
            r = int(lerp(BG_COLOR[0], BG_COLOR[0] + 6, frac))
            g = int(lerp(BG_COLOR[1], BG_COLOR[1] + 6, frac))
            b = int(lerp(BG_COLOR[2], BG_COLOR[2] + 8, frac))
            draw.line([(0, y), (WIDTH, y)], fill=(r, g, b))
            draw.line([(0, y+1), (WIDTH, y+1)], fill=(r, g, b))

    # ── collect active pitches per track for keyboard highlighting ─────
    active = {}   # pitch -> (color, velocity, how long held in seconds)
    for ti, indexed in enumerate(tracks_indexed):
        col = track_colors[ti]
        for n in notes_visible(indexed, t):
            if n["start"] <= t < n["end"]:
                held = t - n["start"]
                # if multiple tracks play same pitch, brighter one wins
                if n["pitch"] not in active or n["velocity"] > active[n["pitch"]][1]:
                    active[n["pitch"]] = (col, n["velocity"], held)

    # ── draw keyboard ──────────────────────────────────────────────────
    # divider line
    draw.rectangle([0, key_top - 1, WIDTH, key_top + 1], fill=DIVIDER_COL)

    # white keys first
    for pitch, g in geom.items():
        if g["is_black"]:
            continue
        x0 = int(g["x"])
        x1 = int(g["x"] + g["w"])
        y0 = key_top
        y1 = key_top + WHITE_KEY_H - 1

        if pitch in active:
            col, vel, held = active[pitch]
            flash = ease_out(clamp(1.0 - held * 8))   # fast flash, fades to tint
            key_fill = lerp_color(WK_COLOR, col, lerp(0.35, 0.70, flash))
        else:
            key_fill = WK_COLOR

        draw.rectangle([x0, y0, x1, y1], fill=key_fill)
        # subtle right border
        draw.line([(x1, y0), (x1, y1)], fill=(160, 160, 170))

    # black keys on top
    for pitch, g in geom.items():
        if not g["is_black"]:
            continue
        x0 = int(g["x"])
        x1 = int(g["x"] + g["w"])
        y0 = key_top
        y1 = key_top + BLACK_KEY_H

        if pitch in active:
            col, vel, held = active[pitch]
            flash    = ease_out(clamp(1.0 - held * 8))
            key_fill = lerp_color(BK_COLOR, col, lerp(0.5, 0.9, flash))
        else:
            key_fill = BK_COLOR

        draw.rectangle([x0, y0, x1, y1], fill=key_fill)
        # highlight top edge
        draw.line([(x0, y0), (x1, y0)], fill=(50, 50, 60))

    # octave labels (C notes)
    lbl_font = get_font(FONT_REGULAR, SIZE_LABEL)
    for pitch, g in geom.items():
        if pitch % 12 == 0 and not g["is_black"]:
            octave = pitch // 12 - 1
            lbl    = f"C{octave}"
            lx     = int(g["x"] + 3)
            ly     = key_top + WHITE_KEY_H - SIZE_LABEL - 6
            draw.text((lx, ly), lbl, font=lbl_font, fill=LABEL_COL)

    # ── draw falling note blocks ───────────────────────────────────────
    # Glow layer (transparent mode: composite per note; opaque: one shared layer)
    if not transparent:
        glow_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        glow_draw  = ImageDraw.Draw(glow_layer)

    for ti, indexed in enumerate(tracks_indexed):
        col = track_colors[ti]
        r, g, b = col

        for n in notes_visible(indexed, t):
            pitch = n["pitch"]
            if pitch not in geom:
                continue

            gm    = geom[pitch]
            cx    = gm["x"] + gm["w"] / 2
            bw    = gm["w"] - NOTE_GAP * 2

            # Y coordinates: start hits keyboard (bottom), end is above (top)
            y_bottom = note_y(n["start"], t)
            y_top    = note_y(n["end"],   t)

            # clamp to note area
            y_top    = max(0.0, y_top)
            y_bottom = min(float(key_top) - 1, y_bottom)

            if y_bottom <= y_top:
                continue

            block_h = max(NOTE_MIN_H, y_bottom - y_top)
            x0 = int(cx - bw / 2)
            x1 = int(cx + bw / 2)
            y0 = int(y_top)
            y1 = int(y_top + block_h)

            # is this note currently being played?
            is_active = n["start"] <= t < n["end"]
            held      = (t - n["start"]) if is_active else -1.0

            # velocity brightness
            v_bright = lerp(0.55, 1.0, n["velocity"] / 127)

            if is_active:
                # bright fill + glow
                fill_col = tuple(int(c * v_bright) for c in col)
                # white highlight on top edge
                hi_col   = lerp_color(col, (255, 255, 255), 0.5)
            else:
                # not yet hit: dimmer, slightly desaturated
                fill_col = tuple(int(lerp(c, 180, 0.35) * v_bright) for c in col)
                hi_col   = fill_col

            if transparent:
                # draw directly onto RGBA img
                note_tile = Image.new("RGBA", (x1 - x0 + 1, y1 - y0 + 1), (0, 0, 0, 0))
                nt_draw   = ImageDraw.Draw(note_tile)
                nt_draw.rounded_rectangle(
                    [0, 0, x1 - x0, y1 - y0],
                    radius=NOTE_RADIUS,
                    fill=(*fill_col, 220)
                )
                # top highlight strip
                nt_draw.rounded_rectangle(
                    [1, 1, x1 - x0 - 1, min(6, y1 - y0 - 1)],
                    radius=NOTE_RADIUS,
                    fill=(*hi_col, 180)
                )
                img.paste(note_tile, (x0, y0), note_tile)

                if is_active:
                    # glow: draw blurred ellipse
                    gr = int(bw * 0.8)
                    glow_t = Image.new("RGBA", (gr * 2 + 1, gr * 2 + 1), (0, 0, 0, 0))
                    ImageDraw.Draw(glow_t).ellipse(
                        [0, 0, gr * 2, gr * 2],
                        fill=(*col, GLOW_ALPHA)
                    )
                    glow_t = glow_t.filter(ImageFilter.GaussianBlur(radius=GLOW_BLUR))
                    gx = int(cx) - gr
                    gy = y1 - gr
                    img.paste(glow_t, (gx, gy), glow_t)
            else:
                # opaque / greenscreen
                rounded_rect(draw, [x0, y0, x1, y1], NOTE_RADIUS, fill_col)
                # top highlight
                rounded_rect(draw, [x0+1, y0+1, x1-1, min(y0+6, y1-1)],
                             NOTE_RADIUS, hi_col)

                if is_active:
                    glow_draw.ellipse(
                        [int(cx) - int(bw), y1 - int(bw),
                         int(cx) + int(bw), y1 + int(bw)],
                        fill=(*col, GLOW_ALPHA)
                    )

    # composite glow (opaque mode)
    if not transparent:
        glow_blurred = glow_layer.filter(ImageFilter.GaussianBlur(radius=GLOW_BLUR))
        base_rgba    = img.convert("RGBA")
        base_rgba.alpha_composite(glow_blurred)
        img = base_rgba.convert("RGB") if not transparent else base_rgba
        draw = ImageDraw.Draw(img)   # refresh draw handle after paste

    # ── scanline grid overlay (subtle, helps depth perception) ────────
    if not transparent and not greenscreen:
        for y in range(0, key_top, 60):
            draw.line([(0, y), (WIDTH, y)], fill=(255, 255, 255, 8) if transparent else (20, 20, 28))

    # ── title ─────────────────────────────────────────────────────────
    if title:
        tf  = get_font(FONT_BOLD, SIZE_TITLE)
        pos = (50, 28)
        if transparent:
            draw_text_transparent(img, pos, title, tf, TITLE_COL)
        else:
            draw_text_simple(draw, pos, title, tf, TITLE_COL)

    # ── track legend ──────────────────────────────────────────────────
    lf   = get_font(FONT_REGULAR, 20)
    lx   = 50
    ly   = 28 + SIZE_TITLE + 12
    for ti, name in enumerate(track_names[:len(tracks_indexed)]):
        col = track_colors[ti]
        # coloured dot
        draw.ellipse([lx, ly + 4, lx + 12, ly + 16], fill=col)
        lbl = name
        if transparent:
            draw_text_transparent(img, (lx + 18, ly), lbl, lf, TITLE_COL, alpha_mul=0.75)
        else:
            draw_text_simple(draw, (lx + 18, ly), lbl, lf,
                             tuple(int(c * 0.8) for c in col), shadow=False)
        lx += draw.textbbox((0, 0), lbl, font=lf)[2] + 40

    # ── watermark ─────────────────────────────────────────────────────
    wf  = get_font(FONT_REGULAR, SIZE_WATERMARK)
    wm  = "made with Nuisance"
    wbb = draw.textbbox((0, 0), wm, font=wf)
    wx  = WIDTH  - (wbb[2] - wbb[0]) - 40
    wy  = HEIGHT - (wbb[3] - wbb[1]) - 30
    if transparent:
        draw_text_transparent(img, (wx, wy), wm, wf, WATERMARK_COL, alpha_mul=0.6)
    else:
        draw_text_simple(draw, (wx, wy), wm, wf, WATERMARK_COL)

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
    parser.add_argument("--workers",     type=int,   default=os.cpu_count(),
                        help="Thread count for parallel rendering (default: CPU count)")
    global LOOKAHEAD
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
    args = parser.parse_args()

    if args.transparent and args.greenscreen:
        print("error: --transparent and --greenscreen are mutually exclusive.")
        return

    LOOKAHEAD = args.lookahead

    print(f"\n  Loading {args.input} …", end="", flush=True)
    tracks, names, auto_dur = load_midi(args.input)
    print(f" done.")

    if not tracks:
        print("  No note tracks found in MIDI file.")
        return

    duration = args.duration if args.duration else auto_dur
    title    = args.title or os.path.splitext(os.path.basename(args.input))[0]

    pitch_lo, pitch_hi = get_actual_range(tracks)
    geom = build_key_geometry(pitch_lo, pitch_hi)

    track_colors  = [TRACK_PALETTE[i % len(TRACK_PALETTE)] for i in range(len(tracks))]
    tracks_indexed = build_index(tracks)

    mode = "transparent" if args.transparent else ("green screen" if args.greenscreen else "opaque")
    print(f"  ♪ {title}")
    print(f"  {len(tracks)} track(s): {', '.join(names[:len(tracks)])}")
    print(f"  pitch range: MIDI {pitch_lo}–{pitch_hi}")
    print(f"  duration: {duration:.1f}s · mode: {mode}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    total = int(duration * FPS)

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

    _done_count = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(
                render_frame,
                tracks_indexed,
                track_colors,
                names[:len(tracks)],
                geom,
                pitch_lo, pitch_hi,
                title,
                f,
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
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -c:v prores_ks -pix_fmt yuva444p10le -profile:v 4444 out.mov")
    elif args.greenscreen:
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -c:v libx264 -pix_fmt yuv420p -crf 0 out_gs.mp4")
    else:
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png -i audio.mp3 \\")
        print(f"         -c:v h264_mediacodec -c:a aac -b:a 192k -shortest out.mp4")
    print()


if __name__ == "__main__":
    main()