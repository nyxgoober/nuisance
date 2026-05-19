#!/usr/bin/env python3

import argparse
import os
import xml.etree.ElementTree as ET
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
SIZE_TITLE     = 38
SIZE_WATERMARK = 22

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
            text = (s.text or "").strip()
            if text:
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
# FONT CACHE
# ══════════════════════════════════════════

_fonts = {}

def get_font(path, size):
    key = (path, int(size))
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(path, int(size))
        except Exception:
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]

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

        ref    = draw.textbbox((0, 0), "あ", font=fnt)
        draw_y = y - ref[1]
        x      = float(MARGIN_LEFT)

        if state == 'active':
            for syl in p["syllables"]:
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
                draw.text((x, draw_y), text, font=fnt, fill=col)
                bb  = draw.textbbox((0, 0), text, font=fnt)
                x  += bb[2] - bb[0]
        else:
            full = "".join(s["text"] for s in p["syllables"])
            base = DONE_COLOR if state == 'past' else IDLE_COLOR
            col  = tuple(int(c * alpha) for c in base)
            draw.text((x, draw_y), full, font=fnt, fill=col)

    # title
    if title:
        tf = get_font(FONT_BOLD, SIZE_TITLE)
        draw.text((MARGIN_LEFT, 36), title, font=tf, fill=TITLE_COLOR)

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

def progress(i, total):
    bar = 40
    f   = int(bar * i / total)
    pct = i / total * 100
    print(f"\r[{'█'*f}{'░'*(bar-f)}] {i}/{total}  {pct:.1f}%", end="", flush=True)

# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",    required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--start",    type=int,   default=0)
    args = parser.parse_args()

    title, phrases = load_ttml(args.input)
    print(f"\n  ♪ {title or '(no title)'}")
    print(f"  {len(phrases)} lines · {args.duration}s")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total = int(args.duration * FPS)
    print(f"  {total} frames @ {FPS}fps · single-threaded")
    print(f"  output → {OUTPUT_DIR}/\n")

    if input("  Render? (y/n): ").lower() != "y":
        return

    for f in range(args.start, total):
        render_frame(phrases, title, f)
        if f % 10 == 0:
            progress(f + 1, total)

    progress(total, total)
    print(f"\n\n  Done. Encode with:")
    print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png -i audio.mp3 \\")
    print(f"         -c:v h264_mediacodec -c:a aac -b:a 192k -shortest out.mp4\n")

if __name__ == "__main__":
    main()
