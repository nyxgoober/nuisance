#!/usr/bin/env python3

import argparse
import os
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw, ImageFont

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

SIZE_IDLE      = 36
SIZE_ACTIVE    = 96
SIZE_TITLE     = 56
SIZE_WATERMARK = 22
SIZE_ACTIVE_MIN = 48

MARGIN_RIGHT  = 120
TITLE_SHADOW  = (0, 0, 0)

BG_COLOR       = (8,   8,  12)
TITLE_COLOR    = (180, 180, 190)
WATERMARK_COL  = (60,  60,  70)
IDLE_COLOR     = (70,  70,  80)
DONE_COLOR     = (200, 200, 210)
ACTIVE_BASE    = (130, 130, 145)
ACTIVE_SUNG    = (255, 255, 255)

GROW_IN_DUR   = 0.25
GROW_OUT_DUR  = 0.30
WORD_FADE_DUR = 0.18

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
# LAYOUT HELPERS
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

# ══════════════════════════════════════════
# ACTIVE LINE LAYOUT
# ══════════════════════════════════════════

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

def draw_shadow(draw, pos, text, font, fill, offset=3):
    draw.text((pos[0] + offset, pos[1] + offset), text, font=font, fill=(0, 0, 0))
    draw.text(pos, text, font=font, fill=fill)

# ══════════════════════════════════════════
# RENDER ONE FRAME
# ══════════════════════════════════════════

def render_frame(phrases, title, frame_num):
    t = frame_num / FPS

    img  = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    active_idx = find_active(phrases, t)

    heights = [line_height(phrases[i], i, active_idx, t) + LINE_GAP
               for i in range(len(phrases))]

    cumulative = [float(MARGIN_TOP)]
    for i in range(len(phrases) - 1):
        cumulative.append(cumulative[-1] + heights[i])

    ANCHOR_Y = HEIGHT * 0.40
    if 0 <= active_idx < len(phrases):
        mid    = cumulative[active_idx] + heights[active_idx] * 0.5
        offset = ANCHOR_Y - mid
    else:
        offset = 0.0

    for i, p in enumerate(phrases):
        y = cumulative[i] + offset
        h = heights[i]

        if y + h < -20 or y > HEIGHT + 20:
            continue

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

        if state == 'active':
            fs_actual, fnt, needs_wrap = fit_font_size(p["syllables"], FONT_BOLD, fs, draw)
            ref    = draw.textbbox((0, 0), "あ", font=fnt)
            draw_y = y - ref[1]
            line_h = ref[3] - ref[1]

            if needs_wrap:
                rows = wrap_syllables(p["syllables"], fnt, draw)
            else:
                rows = [p["syllables"]]

            row_y = draw_y
            for row in rows:
                x = float(MARGIN_LEFT)
                for syl in row:
                    sb   = syl["begin"]
                    se   = syl["end"]
                    text = syl["text"]
                    wf   = ease_out(clamp((t - sb) / WORD_FADE_DUR))

                    if t < sb:
                        col = ACTIVE_BASE
                    elif t <= se:
                        col = lerp_color(ACTIVE_BASE, ACTIVE_SUNG, wf)
                    else:
                        col = DONE_COLOR

                    col = tuple(int(c * alpha) for c in col)
                    draw.text((x, row_y), text, font=fnt, fill=col)
                    bb  = draw.textbbox((0, 0), text, font=fnt)
                    x  += bb[2] - bb[0]
                row_y += line_h + 8
        else:
            ref    = draw.textbbox((0, 0), "あ", font=fnt)
            draw_y = y - ref[1]
            x      = float(MARGIN_LEFT)
            full = "".join(s["text"] for s in p["syllables"])
            base = DONE_COLOR if state == 'past' else IDLE_COLOR
            col  = tuple(int(c * alpha) for c in base)
            draw.text((x, draw_y), full, font=fnt, fill=col)

    # title
    if title:
        tf = get_font(FONT_BOLD, SIZE_TITLE)
        draw_shadow(draw, (MARGIN_LEFT, 32), title, tf, TITLE_COLOR, offset=3)

    # watermark
    wf  = get_font(FONT_REGULAR, SIZE_WATERMARK)
    wm  = "made with Nuisance"
    wbb = draw.textbbox((0, 0), wm, font=wf)
    wx  = WIDTH  - (wbb[2] - wbb[0]) - 40
    wy  = HEIGHT - (wbb[3] - wbb[1]) - 30
    draw.text((wx, wy), wm, font=wf, fill=WATERMARK_COL)

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",    required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--start",    type=int,   default=0)
    parser.add_argument("--workers",  type=int,   default=os.cpu_count())
    args = parser.parse_args()

    title, phrases = load_ttml(args.input)
    print(f"\n  ♪ {title or '(no title)'}")
    print(f"  {len(phrases)} lines · {args.duration}s")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total = int(args.duration * FPS)
    print(f"  {total} frames @ {FPS}fps · {args.workers} workers")
    print(f"  output → {OUTPUT_DIR}/\n")

    if input("  Render? (y/n): ").lower() != "y":
        return

    _done_count = 0
    frames = range(args.start, total)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(render_frame, phrases, title, f): f for f in frames}
        for fut in as_completed(futs):
            fut.result()
            on_frame_done(total)

    print(f"\n\n  Done. Encode with:")
    print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png -i audio.mp3 \\")
    print(f"         -c:v h264_mediacodec -c:a aac -b:a 192k -shortest out.mp4\n")

if __name__ == "__main__":
    main()
