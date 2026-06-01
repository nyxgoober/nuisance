#!/usr/bin/env python3
"""
waveform.py — Audio waveform / spectrum visualiser
Renders a scrolling waveform or FFT spectrum bar in the same visual style
as Nuisance. Outputs PNG frame sequences → encode with ffmpeg.

Requirements: pip install pillow numpy soundfile
  soundfile handles WAV, FLAC, OGG, AIFF.
  For MP3 support also install: pip install soundfile[mp3]   (uses mpg123)
  or: pip install pydub && apt install ffmpeg  (fallback path used automatically)

Usage:
  python waveform.py --input audio.wav --duration 217
  python waveform.py --input audio.flac --mode spectrum --bars 80
  python waveform.py --input audio.mp3  --transparent --preview
"""

import argparse
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════

WIDTH      = 1920
HEIGHT     = 1080
FPS        = 60
OUTPUT_DIR = "frames"

FONT_BOLD    = "ZenKakuGothicNew-Bold.ttf"
FONT_REGULAR = "ZenKakuGothicNew-Regular.ttf"

# Visual layout
VIZ_H        = 340      # height of the visualiser area (centred vertically)
VIZ_Y        = (HEIGHT - VIZ_H) // 2   # top of visualiser

# Colours (match nuisance/midimap dark palette)
BG_COLOR      = (8,   8,  12)
GS_COLOR      = (0, 255,   0)
WAVEFORM_COL  = (100, 180, 255)    # soft blue — same as midimap track 0
WAVEFORM_GLOW = (60,  120, 200)
SPECTRUM_COLS = [                  # colour gradient low→high
    (100, 180, 255),   # blue  (bass)
    (120, 220, 140),   # mint  (mids)
    (255, 210,  70),   # amber (highs)
]
TITLE_COL     = (180, 180, 190)
WATERMARK_COL = (60,  60,  70)

SIZE_TITLE     = 52
SIZE_WATERMARK = 22

SHADOW_OFFSET  = 3
SHADOW_BLUR    = 6

# Waveform mode
WAVE_WINDOW_S  = 0.08   # seconds of audio visible in waveform at once
GLOW_BLUR      = 14

# Spectrum mode
SPEC_BARS      = 64     # number of frequency bars
SPEC_MIN_FREQ  = 40     # Hz
SPEC_MAX_FREQ  = 16000  # Hz
SPEC_SMOOTHING = 0.72   # temporal smoothing factor (0=none, <1=smooth)
SPEC_BAR_GAP   = 3      # pixels between bars
SPEC_CORNER    = 5      # rounded corner radius
SPEC_PEAK_HOLD = 45     # frames a peak indicator holds before dropping

# ══════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))

def ease_out(t):
    t = clamp(t)
    return 1.0 - (1.0 - t) ** 3

def lerp(a, b, t):
    return a + (b - a) * clamp(t)

def lerp_color(ca, cb, t):
    t = clamp(t)
    return tuple(int(a + (b - a) * t) for a, b in zip(ca, cb))

def lerp_color3(ca, cm, cb, t):
    """Three-stop colour lerp: ca→cm over [0,0.5], cm→cb over [0.5,1]."""
    if t <= 0.5:
        return lerp_color(ca, cm, t * 2)
    return lerp_color(cm, cb, (t - 0.5) * 2)

# ══════════════════════════════════════════
# AUDIO LOADING
# ══════════════════════════════════════════

def load_audio(path):
    """
    Load audio file → (samples_mono float32, sample_rate).
    Tries soundfile first; falls back to pydub if soundfile can't read the format.
    """
    sf_err    = None
    pydub_err = None

    try:
        import soundfile as sf
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
        return mono, sr
    except ImportError:
        sf_err = "soundfile not installed  →  pip install soundfile"
    except Exception as e:
        sf_err = str(e)

    try:
        from pydub import AudioSegment
        seg  = AudioSegment.from_file(path)
        seg  = seg.set_channels(1).set_sample_width(2)
        sr   = seg.frame_rate
        raw  = np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float32)
        mono = raw / 32768.0
        return mono, sr
    except ImportError:
        pydub_err = "pydub not installed  →  pip install pydub"
    except Exception as e:
        pydub_err = str(e)

    raise RuntimeError(
        f"Could not load '{path}'.\n"
        f"  soundfile : {sf_err}\n"
        f"  pydub     : {pydub_err}\n\n"
        f"Install at least one:  pip install soundfile\n"
        f"MP3 support also needs ffmpeg on PATH."
    )

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
# DRAWING HELPERS
# ══════════════════════════════════════════

def draw_text_opaque(draw, pos, text, font, fill, alpha_mul=1.0):
    x, y = int(pos[0]), int(pos[1])
    draw.text((x + SHADOW_OFFSET, y + SHADOW_OFFSET), text, font=font, fill=(0, 0, 0))
    col = tuple(int(c * alpha_mul) for c in fill[:3])
    draw.text((x, y), text, font=font, fill=col)

def draw_text_transparent(img, pos, text, font, fill, alpha_mul=1.0):
    x, y = int(pos[0]), int(pos[1])
    tmp_draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    bb  = tmp_draw.textbbox((0, 0), text, font=font)
    pad = SHADOW_BLUR * 2 + SHADOW_OFFSET + 4
    bx0 = max(0, x + bb[0] - pad)
    by0 = max(0, y + bb[1] - pad)
    bx1 = min(img.width,  x + bb[2] + pad)
    by1 = min(img.height, y + bb[3] + pad)
    bw, bh = bx1 - bx0, by1 - by0
    if bw <= 0 or bh <= 0:
        return
    lx, ly = x - bx0, y - by0
    shadow_tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(shadow_tile).text(
        (lx + SHADOW_OFFSET, ly + SHADOW_OFFSET), text, font=font,
        fill=(0, 0, 0, int(200 * alpha_mul))
    )
    shadow_tile = shadow_tile.filter(ImageFilter.GaussianBlur(radius=SHADOW_BLUR))
    text_tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    r, g, b = fill[:3]
    ImageDraw.Draw(text_tile).text(
        (lx, ly), text, font=font, fill=(r, g, b, int(255 * alpha_mul))
    )
    region = img.crop((bx0, by0, bx1, by1))
    region.alpha_composite(shadow_tile)
    region.alpha_composite(text_tile)
    img.paste(region, (bx0, by0))

def draw_text(img, draw, pos, text, font, fill, alpha_mul=1.0, transparent=False):
    if transparent:
        draw_text_transparent(img, pos, text, font, fill, alpha_mul)
    else:
        draw_text_opaque(draw, pos, text, font, fill, alpha_mul)

def rounded_rect(draw, bbox, radius, fill, alpha=255):
    x0, y0, x1, y1 = bbox
    r = min(radius, (x1 - x0) // 2, (y1 - y0) // 2)
    if isinstance(fill, tuple) and len(fill) == 3:
        fill = (*fill, alpha)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill)

def draw_glow_rect(img, bbox, color, blur_radius=GLOW_BLUR, alpha=100):
    """Draw a soft glow behind a rectangle."""
    x0, y0, x1, y1 = bbox
    pad = blur_radius * 2
    w = (x1 - x0) + pad * 2
    h = (y1 - y0) + pad * 2
    if w <= 0 or h <= 0:
        return
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rectangle([pad, pad, pad + (x1-x0), pad + (y1-y0)],
                                   fill=(*color, alpha))
    tile = tile.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    tx, ty = x0 - pad, y0 - pad
    cx0 = max(0, tx);  cy0 = max(0, ty)
    cx1 = min(img.width, tx + w);  cy1 = min(img.height, ty + h)
    if cx1 <= cx0 or cy1 <= cy0:
        return
    crop = img.crop((cx0, cy0, cx1, cy1))
    tile_crop = tile.crop((cx0 - tx, cy0 - ty, cx1 - tx, cy1 - ty))
    crop.alpha_composite(tile_crop)
    img.paste(crop, (cx0, cy0))

# ══════════════════════════════════════════
# CHROME (title + watermark)
# ══════════════════════════════════════════

def draw_chrome(img, draw, title, transparent, greenscreen):
    if title:
        tf  = get_font(FONT_BOLD, SIZE_TITLE)
        draw_text(img, draw, (120, 28), title, tf, TITLE_COL, 1.0, transparent)

    wf  = get_font(FONT_REGULAR, SIZE_WATERMARK)
    wm  = "made with Nuisance"
    tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
    wbb = tmp.textbbox((0, 0), wm, font=wf)
    wx  = WIDTH  - (wbb[2] - wbb[0]) - 40
    wy  = HEIGHT - (wbb[3] - wbb[1]) - 30
    draw_text(img, draw, (wx, wy), wm, wf, WATERMARK_COL, 0.6, transparent)

# ══════════════════════════════════════════
# WAVEFORM MODE
# ══════════════════════════════════════════

def render_waveform_frame(samples, sr, frame_num, title,
                          transparent, greenscreen):
    t = frame_num / FPS

    if transparent:
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    elif greenscreen:
        img = Image.new("RGB",  (WIDTH, HEIGHT), GS_COLOR)
    else:
        img = Image.new("RGB",  (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # window of samples to display
    half_w  = WAVE_WINDOW_S / 2
    s_start = max(0, int((t - half_w) * sr))
    s_end   = min(len(samples), int((t + half_w) * sr))
    window  = samples[s_start:s_end]

    cx   = WIDTH  // 2
    cy   = HEIGHT // 2
    draw_w = WIDTH - 240   # leave margins
    draw_h = VIZ_H

    if len(window) > 1:
        # downsample to pixel columns
        n_cols    = draw_w
        chunk     = max(1, len(window) // n_cols)
        # compute RMS per chunk for a smoother envelope
        n_chunks  = len(window) // chunk
        rms_cols  = []
        for ci in range(n_chunks):
            seg = window[ci*chunk:(ci+1)*chunk]
            rms_cols.append(float(np.sqrt(np.mean(seg**2))))

        if rms_cols:
            peak = max(max(rms_cols), 1e-6)
            rms_cols = [v / peak for v in rms_cols]

        # --- glow pass (RGBA tile) ---
        x0_draw = (WIDTH - draw_w) // 2

        if len(rms_cols) > 1:
            glow_tile = Image.new("RGBA", (draw_w, draw_h + 40), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow_tile)
            prev_gx, prev_gy = None, None
            for ci, v in enumerate(rms_cols):
                gx = int(ci * draw_w / len(rms_cols))
                gy = draw_h // 2 - int(v * draw_h * 0.45)
                if prev_gx is not None:
                    gd.line([(prev_gx, prev_gy + draw_h//2 - draw_h//2),
                              (gx,     gy     + draw_h//2 - draw_h//2)],
                            fill=(*WAVEFORM_GLOW, 180), width=6)
                prev_gx, prev_gy = gx, gy - draw_h // 2 + draw_h // 2
            glow_tile = glow_tile.filter(ImageFilter.GaussianBlur(radius=GLOW_BLUR))

            if transparent:
                region = img.crop((x0_draw, cy - draw_h//2 - 20,
                                   x0_draw + draw_w, cy + draw_h//2 + 20))
                region.alpha_composite(glow_tile)
                img.paste(region, (x0_draw, cy - draw_h//2 - 20))
            else:
                img_rgba = img.convert("RGBA")
                img_rgba.alpha_composite(glow_tile, dest=(x0_draw, cy - draw_h//2 - 20))
                img.paste(img_rgba.convert("RGB"))

        # --- main waveform line ---
        pts = []
        for ci, v in enumerate(rms_cols):
            wx = x0_draw + int(ci * draw_w / len(rms_cols))
            wy = cy - int(v * draw_h * 0.45)
            pts.append((wx, wy))
            # mirror below centre
        pts_mirror = [(x, cy + (cy - y)) for x, y in pts]

        col_main = WAVEFORM_COL

        def draw_poly_line(point_list, color, width):
            if len(point_list) < 2:
                return
            if transparent:
                tmp_tile = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
                ImageDraw.Draw(tmp_tile).line(point_list, fill=(*color, 220), width=width)
                img.alpha_composite(tmp_tile)
            else:
                draw.line(point_list, fill=color, width=width)

        draw_poly_line(pts, col_main, 3)
        draw_poly_line(pts_mirror, col_main, 3)

        # centre line
        if not transparent:
            draw.line([(x0_draw, cy), (x0_draw + draw_w, cy)],
                      fill=tuple(c // 4 for c in WAVEFORM_COL), width=1)

    draw_chrome(img, draw, title, transparent, greenscreen)
    path = os.path.join(OUTPUT_DIR, f"frame_{frame_num:06d}.png")
    img.save(path)

# ══════════════════════════════════════════
# SPECTRUM MODE
# ══════════════════════════════════════════

# Per-thread smoothed spectrum state
_spec_state = threading.local()

def _get_spec_state(n_bars):
    if not hasattr(_spec_state, "prev") or len(_spec_state.prev) != n_bars:
        _spec_state.prev      = np.zeros(n_bars)
        _spec_state.peaks     = np.zeros(n_bars)
        _spec_state.peak_hold = np.zeros(n_bars, dtype=int)
    return _spec_state.prev, _spec_state.peaks, _spec_state.peak_hold

def compute_spectrum(samples, sr, t, n_bars, fft_size=4096):
    """Return normalised bar heights [0,1] for n_bars frequency bands."""
    half_win = fft_size / sr / 2
    s0 = max(0, int((t - half_win) * sr))
    s1 = min(len(samples), s0 + fft_size)
    window = samples[s0:s1]
    if len(window) < 64:
        return np.zeros(n_bars)

    # zero-pad to fft_size
    padded = np.zeros(fft_size)
    padded[:len(window)] = window * np.hanning(len(window))
    mag = np.abs(np.fft.rfft(padded))
    freqs = np.fft.rfftfreq(fft_size, 1.0 / sr)

    # log-spaced frequency bins
    log_lo  = math.log10(max(SPEC_MIN_FREQ, 1))
    log_hi  = math.log10(SPEC_MAX_FREQ)
    edges   = np.logspace(log_lo, log_hi, n_bars + 1)

    bars = np.zeros(n_bars)
    for b in range(n_bars):
        mask = (freqs >= edges[b]) & (freqs < edges[b + 1])
        if mask.any():
            bars[b] = float(np.sqrt(np.mean(mag[mask] ** 2)))

    # normalise to dB-ish scale
    bars = np.log1p(bars * 500) / math.log1p(500)
    bars = np.clip(bars, 0, 1)
    return bars

def render_spectrum_frame(samples, sr, frame_num, n_bars, title,
                          transparent, greenscreen):
    t = frame_num / FPS

    if transparent:
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    elif greenscreen:
        img = Image.new("RGB",  (WIDTH, HEIGHT), GS_COLOR)
    else:
        img = Image.new("RGB",  (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    raw_bars = compute_spectrum(samples, sr, t, n_bars)

    # temporal smoothing (per-thread state)
    prev, peaks, peak_hold = _get_spec_state(n_bars)
    smoothed = SPEC_SMOOTHING * prev + (1 - SPEC_SMOOTHING) * raw_bars
    prev[:] = smoothed

    # peak hold
    for b in range(n_bars):
        if smoothed[b] >= peaks[b]:
            peaks[b]     = smoothed[b]
            peak_hold[b] = SPEC_PEAK_HOLD
        else:
            if peak_hold[b] > 0:
                peak_hold[b] -= 1
            else:
                peaks[b] = max(smoothed[b], peaks[b] - 0.015)

    # layout
    margin   = 120
    total_w  = WIDTH - margin * 2
    bar_w    = (total_w - SPEC_BAR_GAP * (n_bars - 1)) // n_bars
    bar_w    = max(bar_w, 2)
    max_h    = VIZ_H
    base_y   = VIZ_Y + max_h    # bottom of bars

    for b in range(n_bars):
        v     = float(smoothed[b])
        bh    = max(4, int(v * max_h))
        x0    = margin + b * (bar_w + SPEC_BAR_GAP)
        x1    = x0 + bar_w
        y0    = base_y - bh
        y1    = base_y

        col = lerp_color3(*SPECTRUM_COLS, v)

        if transparent:
            bar_tile = Image.new("RGBA", (bar_w, bh), (0, 0, 0, 0))
            rounded_rect(ImageDraw.Draw(bar_tile),
                         [0, 0, bar_w, bh], SPEC_CORNER,
                         col, alpha=220)
            img.alpha_composite(bar_tile, dest=(x0, y0))
        else:
            rounded_rect(draw, [x0, y0, x1, y1], SPEC_CORNER, col)

        # glow at top of bar
        draw_glow_rect(img.convert("RGBA") if not transparent else img,
                       [x0, y0, x1, min(y0 + 16, y1)], col,
                       blur_radius=8, alpha=80)

        # peak indicator line
        ph  = float(peaks[b])
        py  = base_y - int(ph * max_h) - 2
        px0, px1 = x0, x1
        if 0 <= py < HEIGHT:
            peak_col = lerp_color3(*SPECTRUM_COLS, ph)
            if transparent:
                pk_tile = Image.new("RGBA", (bar_w, 3), (*peak_col, 200))
                img.alpha_composite(pk_tile, dest=(px0, py))
            else:
                draw.rectangle([px0, py, px1, py + 2], fill=peak_col)

    draw_chrome(img, draw, title, transparent, greenscreen)
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
        prog="waveform.py",
        description="Nuisance — audio waveform / spectrum visualiser. Outputs PNG frame sequences ready for ffmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
modes:
  waveform   Scrolling RMS waveform centred on playback position (default)
  spectrum   Frequency spectrum bar chart with peak hold indicators

examples:
  # quick preview
  python waveform.py --input audio.wav --preview

  # full spectrum render
  python waveform.py --input audio.flac --mode spectrum --bars 80

  # transparent waveform for compositing
  python waveform.py --input audio.wav --transparent

encode output:
  ffmpeg -framerate 60 -i frames/frame_%06d.png -i audio.mp3 \\
         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4
        """
    )
    parser.add_argument("--input",       required=True,
                        help="Path to audio file (WAV, FLAC, OGG, AIFF, MP3)")
    parser.add_argument("--duration",    type=float, default=None,
                        help="Override duration in seconds (auto-detected from file if omitted)")
    parser.add_argument("--mode",        choices=["waveform", "spectrum"], default="waveform",
                        help="Visualiser mode: waveform (default) or spectrum")
    parser.add_argument("--bars",        type=int, default=SPEC_BARS,
                        help=f"Number of frequency bars in spectrum mode (default: {SPEC_BARS})")
    parser.add_argument("--start",       type=int, default=0,
                        help="Resume from this frame index (default: 0)")
    parser.add_argument("--workers",     type=int, default=os.cpu_count(),
                        help="Thread count for parallel rendering (default: CPU count)")
    parser.add_argument("--transparent", action="store_true",
                        help="RGBA output with blurred shadows (slower, for alpha compositing)")
    parser.add_argument("--greenscreen", action="store_true",
                        help="Solid green (#00ff00) background for chroma key (fast)")
    parser.add_argument("--title",       default=None,
                        help="Title shown in the top-left (defaults to filename stem)")
    parser.add_argument("--preview",     action="store_true",
                        help="Render 1 frame per second only — fast visual check, skips confirmation prompt")
    args = parser.parse_args()

    if args.transparent and args.greenscreen:
        print("  error: --transparent and --greenscreen are mutually exclusive.")
        return

    print(f"\n  Loading {args.input} …", end="", flush=True)
    samples, sr = load_audio(args.input)
    print(f" done.  ({len(samples)/sr:.1f}s @ {sr}Hz)")

    duration = args.duration if args.duration else len(samples) / sr
    title    = args.title or os.path.splitext(os.path.basename(args.input))[0]

    mode_str = "transparent RGBA" if args.transparent else ("green screen" if args.greenscreen else "opaque")
    print(f"  ♪ {title}")
    print(f"  mode: {args.mode} · output: {mode_str} · duration: {duration:.1f}s")

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
        if args.mode == "waveform":
            futs = {
                ex.submit(
                    render_waveform_frame,
                    samples, sr, f, title,
                    args.transparent, args.greenscreen,
                ): f
                for f in frames
            }
        else:
            futs = {
                ex.submit(
                    render_spectrum_frame,
                    samples, sr, f, args.bars, title,
                    args.transparent, args.greenscreen,
                ): f
                for f in frames
            }

        for fut in as_completed(futs):
            fut.result()
            on_frame_done(len(frames))

    print(f"\n\n  Done. Encode with:")
    if args.transparent:
        print(f"  # lossless with alpha (ProRes 4444):")
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -c:v prores_ks -pix_fmt yuva444p10le -profile:v 4444 out.mov")
        print()
        print(f"  # composite over background video:")
        print(f"  ffmpeg -i background.mp4 -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -i {args.input} -filter_complex \"[0:v][1:v]overlay=0:0\" \\")
        print(f"         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4")
    elif args.greenscreen:
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png \\")
        print(f"         -c:v libx264 -pix_fmt yuv420p -crf 0 out_gs.mp4")
        print()
        print(f"  # chroma-key composite:")
        print(f"  ffmpeg -i background.mp4 -i out_gs.mp4 \\")
        print(f"         -filter_complex \"[1:v]colorkey=0x00ff00:0.3:0.1[ov];[0:v][ov]overlay\" \\")
        print(f"         -i {args.input} -c:a aac -b:a 192k -shortest out.mp4")
    else:
        print(f"  ffmpeg -framerate {FPS} -i {OUTPUT_DIR}/frame_%06d.png -i {args.input} \\")
        print(f"         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4")
    print()


if __name__ == "__main__":
    main()
