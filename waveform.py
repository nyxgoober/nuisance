#!/usr/bin/env python3
"""
waveform.py — Audio waveform / spectrum visualiser
Renders a scrolling waveform or FFT spectrum bar in the same visual style
as Nuisance. Outputs PNG frame sequences → encode with ffmpeg.

Requirements: pip install pillow numpy soundfile
"""

import argparse
import math
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
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

# Visual layout
VIZ_H        = 340                      # height of the visualiser area (centred vertically)
VIZ_Y        = (HEIGHT - VIZ_H) // 2    # top of visualiser

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
SIZE_TITLE_MIN = 28
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

def lerp_color3(ca, cm, cb, t):
    """Three-stop colour lerp: ca→cm over [0,0.5], cm→cb over [0.5,1]."""
    if t <= 0.5:
        return lerp_color(ca, cm, t * 2.0)
    return lerp_color(cm, cb, (t - 0.5) * 2.0)

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
# AUDIO LOADING
# ══════════════════════════════════════════

def load_audio(path):
    """
    Load audio file → (samples_mono float32, sample_rate).
    Tries soundfile first, then pydub, then direct ffmpeg pipe.
    """
    sf_err     = None
    pydub_err  = None
    ffmpeg_err = None

    try:
        import soundfile as sf
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        mono = data.mean(axis=1, dtype=np.float32)
        return np.ascontiguousarray(mono, dtype=np.float32), int(sr)
    except ImportError:
        sf_err = "soundfile not installed  →  pip install soundfile"
    except Exception as e:
        sf_err = str(e)

    try:
        from pydub import AudioSegment
        seg  = AudioSegment.from_file(path)
        seg  = seg.set_channels(1).set_sample_width(2)
        sr   = int(seg.frame_rate)
        raw  = np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float32)
        mono = raw / 32768.0
        return np.ascontiguousarray(mono, dtype=np.float32), sr
    except ImportError:
        pydub_err = "pydub not installed  →  pip install pydub"
    except Exception as e:
        pydub_err = str(e)

    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        try:
            sr = 44100
            proc = subprocess.run(
                [
                    ffmpeg_bin, "-v", "error", "-i", path,
                    "-ac", "1", "-ar", str(sr), "-f", "f32le", "-",
                ],
                capture_output=True,
                check=True,
            )
            mono = np.frombuffer(proc.stdout, dtype=np.float32).copy()
            if len(mono) > 0:
                return mono, sr
            ffmpeg_err = "decoded 0 samples"
        except Exception as e:
            ffmpeg_err = str(e)
    else:
        ffmpeg_err = "ffmpeg not found on PATH"

    raise RuntimeError(
        f"Could not load '{path}'.\n"
        f"  soundfile : {sf_err}\n"
        f"  pydub     : {pydub_err}\n"
        f"  ffmpeg    : {ffmpeg_err}\n\n"
        f"Install soundfile (`pip install soundfile`) or ffmpeg."
    )


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
# DRAWING HELPERS & STATIC BASE CANVAS
# ══════════════════════════════════════════

def draw_text_opaque(draw, pos, text, font, fill, alpha_mul=1.0, greenscreen=False):
    if not text or alpha_mul <= 0.0:
        return
    x, y = int(pos[0]), int(pos[1])
    if greenscreen:
        sh_col = (0, 0, 0)
        col = tuple(int(c * alpha_mul) for c in fill[:3])
    else:
        sh_col = lerp_color(BG_COLOR, (0, 0, 0), alpha_mul)
        col = lerp_color(BG_COLOR, fill[:3], alpha_mul)
    draw.text((x + SHADOW_OFFSET, y + SHADOW_OFFSET), text, font=font, fill=sh_col)
    draw.text((x, y), text, font=font, fill=col)

def draw_text_transparent(img, pos, text, font, fill, alpha_mul=1.0):
    if not text or alpha_mul <= 0.0:
        return
    x, y = int(pos[0]), int(pos[1])
    bb  = font.getbbox(text)
    pad = SHADOW_BLUR * 2 + SHADOW_OFFSET + 4
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
        (lx + SHADOW_OFFSET, ly + SHADOW_OFFSET), text, font=font,
        fill=(0, 0, 0, int(200 * alpha_mul))
    )
    tile = tile.filter(ImageFilter.GaussianBlur(radius=SHADOW_BLUR))
    text_tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    r, g, b = fill[:3]
    ImageDraw.Draw(text_tile).text(
        (lx, ly), text, font=font, fill=(r, g, b, int(255 * alpha_mul))
    )
    tile.alpha_composite(text_tile)
    alpha_paste(img, tile, bx0, by0)

def rounded_rect(draw, bbox, radius, fill, alpha=255):
    x0, y0, x1, y1 = bbox
    if x1 < x0 or y1 < y0:
        return
    r = max(0, min(radius, (x1 - x0) // 2, (y1 - y0) // 2))
    if isinstance(fill, tuple) and len(fill) == 3 and draw._image.mode == "RGBA":
        fill = (*fill, alpha)
    if r == 0:
        draw.rectangle([x0, y0, x1, y1], fill=fill)
    else:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill)


def build_base_canvas(title, transparent, greenscreen):
    """
    Pre-render background, title, and watermark once before parallel frame rendering.
    Since the visualiser sits inside [VIZ_Y, VIZ_Y + VIZ_H] (370..710),
    chrome at top (y=28) and bottom (y=1020) never overlaps the visualiser!
    """
    if transparent:
        base = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    elif greenscreen:
        base = Image.new("RGB",  (WIDTH, HEIGHT), GS_COLOR)
    else:
        base = Image.new("RGB",  (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(base)

    if title:
        title_fs = SIZE_TITLE
        while title_fs > SIZE_TITLE_MIN:
            tf = get_font(FONT_BOLD, title_fs)
            if text_width(title, tf) <= WIDTH - 240:
                break
            title_fs -= 2
        tf = get_font(FONT_BOLD, title_fs)
        if transparent:
            draw_text_transparent(base, (120, 28), title, tf, TITLE_COL, 1.0)
        else:
            draw_text_opaque(draw, (120, 28), title, tf, TITLE_COL, 1.0, greenscreen=greenscreen)

    wf  = get_font(FONT_REGULAR, SIZE_WATERMARK)
    wm  = "made with Nuisance"
    wbb = wf.getbbox(wm)
    wx  = WIDTH  - (wbb[2] - wbb[0]) - 40
    wy  = HEIGHT - (wbb[3] - wbb[1]) - 30
    if transparent:
        draw_text_transparent(base, (wx, wy), wm, wf, WATERMARK_COL, 0.6)
    else:
        draw_text_opaque(draw, (wx, wy), wm, wf, WATERMARK_COL, 0.6, greenscreen=greenscreen)

    return base


# ══════════════════════════════════════════
# WAVEFORM MODE  (vectorized RMS + faithful global peak scaling)
# ══════════════════════════════════════════

def compute_waveform_ref_peak(samples, sr):
    """Compute a stable track-wide RMS reference so silence stays flat."""
    if len(samples) == 0:
        return 1.0
    block = max(1, int(sr * 0.005))
    n_blocks = len(samples) // block
    if n_blocks >= 4:
        trimmed = samples[: n_blocks * block].reshape(n_blocks, block)
        rms = np.sqrt(np.mean(trimmed * trimmed, axis=1))
        ref = float(np.percentile(rms, 99.5))
    else:
        ref = float(np.max(np.abs(samples)))
    return max(ref, 1e-4)


def render_waveform_frame(samples, sr, ref_peak, base_canvas, frame_num,
                          transparent, greenscreen, output_dir=OUTPUT_DIR):
    t   = frame_num / FPS
    img = base_canvas.copy()
    draw = ImageDraw.Draw(img)

    half_w  = WAVE_WINDOW_S * 0.5
    s_start = max(0, int((t - half_w) * sr))
    s_end   = min(len(samples), int((t + half_w) * sr))
    window  = samples[s_start:s_end]

    cy      = HEIGHT // 2
    draw_w  = WIDTH - 240
    draw_h  = VIZ_H
    x0_draw = (WIDTH - draw_w) // 2

    if len(window) > 1:
        if len(window) >= draw_w:
            chunk = len(window) // draw_w
            n_chunks = min(draw_w, len(window) // chunk)
            reshaped = window[: n_chunks * chunk].reshape(n_chunks, chunk)
            rms_arr = np.sqrt(np.mean(reshaped * reshaped, axis=1))
        else:
            rms_arr = np.abs(window)
            n_chunks = len(rms_arr)

        norm = np.clip(rms_arr / ref_peak, 0.0, 1.0)

        if n_chunks > 1:
            xs_local = (np.arange(n_chunks) * (draw_w / n_chunks)).astype(np.int32)
            dy_vals  = (norm * (draw_h * 0.45)).astype(np.int32)

            pad_y   = GLOW_BLUR * 2 + 6
            band_h  = draw_h + pad_y * 2
            band_cy = band_h // 2
            band_y0 = cy - band_cy

            pts_up_local = list(zip(xs_local.tolist(), (band_cy - dy_vals).tolist()))
            pts_dn_local = list(zip(xs_local.tolist(), (band_cy + dy_vals).tolist()))

            # Blurred glow pass (both upper & mirrored lower envelopes, properly aligned!)
            glow_tile = Image.new("RGBA", (draw_w, band_h), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow_tile)
            gd.line(pts_up_local, fill=(*WAVEFORM_GLOW, 180), width=6)
            gd.line(pts_dn_local, fill=(*WAVEFORM_GLOW, 180), width=6)
            glow_tile = glow_tile.filter(ImageFilter.GaussianBlur(radius=GLOW_BLUR))

            if transparent:
                # Draw crisp waveform lines onto a tight band tile and composite once
                line_tile = Image.new("RGBA", (draw_w, band_h), (0, 0, 0, 0))
                ld = ImageDraw.Draw(line_tile)
                ld.line(pts_up_local, fill=(*WAVEFORM_COL, 220), width=3)
                ld.line(pts_dn_local, fill=(*WAVEFORM_COL, 220), width=3)
                glow_tile.alpha_composite(line_tile)
                alpha_paste(img, glow_tile, x0_draw, band_y0)
            else:
                region = img.crop((x0_draw, band_y0, x0_draw + draw_w, band_y0 + band_h)).convert("RGBA")
                region.alpha_composite(glow_tile)
                img.paste(region.convert("RGB"), (x0_draw, band_y0))
                draw = ImageDraw.Draw(img)

                pts_up = [(x0_draw + lx, band_y0 + ly) for lx, ly in pts_up_local]
                pts_dn = [(x0_draw + lx, band_y0 + ly) for lx, ly in pts_dn_local]
                draw.line(pts_up, fill=WAVEFORM_COL, width=3)
                draw.line(pts_dn, fill=WAVEFORM_COL, width=3)
                draw.line([(x0_draw, cy), (x0_draw + draw_w, cy)],
                          fill=tuple(c // 4 for c in WAVEFORM_COL), width=1)

    path = os.path.join(output_dir, f"frame_{frame_num:06d}.png")
    img.save(path, format="PNG", compress_level=1)


# ══════════════════════════════════════════
# SPECTRUM MODE  (deterministic batched FFT + thread-safe timeline)
# ══════════════════════════════════════════

def _build_spectrum_bin_matrix(sr, n_bars, fft_size=4096):
    """
    Build a (n_bars, fft_size // 2 + 1) weight matrix mapping squared FFT
    magnitudes to log-spaced frequency bands. Interpolates narrow low-frequency
    bands so no bass bar ever sits between FFT bins with 0 weight.
    """
    freqs  = np.fft.rfftfreq(fft_size, 1.0 / sr)
    n_bins = len(freqs)
    lo_hz  = max(1.0, float(SPEC_MIN_FREQ))
    hi_hz  = min(float(SPEC_MAX_FREQ), max(lo_hz * 2.0, sr * 0.48))
    edges  = np.logspace(math.log10(lo_hz), math.log10(hi_hz), n_bars + 1)

    W = np.zeros((n_bars, n_bins), dtype=np.float32)
    df = freqs[1] - freqs[0] if n_bins > 1 else 1.0

    for b in range(n_bars):
        mask = (freqs >= edges[b]) & (freqs < edges[b + 1])
        cnt = int(mask.sum())
        if cnt > 0:
            W[b, mask] = 1.0 / cnt
        else:
            # Center frequency falls strictly between two adjacent FFT bins: interpolate
            center_hz = 0.5 * (edges[b] + edges[b + 1])
            f_idx = center_hz / df
            i0 = max(0, min(n_bins - 1, int(math.floor(f_idx))))
            i1 = min(n_bins - 1, i0 + 1)
            frac = f_idx - i0
            W[b, i0] += 1.0 - frac
            W[b, i1] += frac

    return W


def precompute_spectrum_timeline(samples, sr, total_frames, n_bars, fft_size=4096):
    """
    Precompute (smoothed_bars, peak_bars) of shape (total_frames, n_bars)
    deterministically using batched NumPy rFFTs before launching worker threads.
    Eliminates multi-threaded temporal smoothing races and flickering!
    """
    if total_frames <= 0:
        return np.zeros((0, n_bars), dtype=np.float32), np.zeros((0, n_bars), dtype=np.float32)

    raw_all = np.zeros((total_frames, n_bars), dtype=np.float32)
    n_samp  = len(samples)

    if n_samp >= 64:
        W         = _build_spectrum_bin_matrix(sr, n_bars, fft_size)
        WT        = np.ascontiguousarray(W.T)
        win       = np.hanning(fft_size).astype(np.float32)
        half_win  = fft_size / sr / 2.0
        log_denom = math.log1p(500.0)
        offsets   = np.arange(fft_size, dtype=np.int64)

        # Zero-pad samples at both ends so batch slicing never goes out of bounds
        pad_len = fft_size
        padded_samples = np.pad(samples, (pad_len, pad_len), mode="constant")

        batch_size = 512
        for f0 in range(0, total_frames, batch_size):
            f1 = min(total_frames, f0 + batch_size)
            ts = np.arange(f0, f1, dtype=np.float64) / FPS
            s0 = np.clip(((ts - half_win) * sr).astype(np.int64), 0, max(0, n_samp - 1))
            # Validity mask for frames completely past audio end
            valid_len = np.clip(n_samp - s0, 0, fft_size)
            idx_mat = (s0[:, None] + pad_len) + offsets[None, :]
            frames_win = padded_samples[idx_mat]
            # Mask samples beyond n_samp when near end of file
            if np.any(valid_len < fft_size):
                col_mask = offsets[None, :] < valid_len[:, None]
                frames_win = frames_win * col_mask
            frames_win *= win[None, :]

            spec = np.fft.rfft(frames_win, axis=1)
            mag_sq = (spec.real * spec.real + spec.imag * spec.imag).astype(np.float32)
            bars = np.sqrt(mag_sq @ WT)
            bars = np.log1p(bars * 500.0) / log_denom
            np.clip(bars, 0.0, 1.0, out=bars)
            bars[valid_len < 64] = 0.0
            raw_all[f0:f1] = bars

    # Exact sequential causal smoothing & peak hold across all frames
    smoothed_all = np.empty_like(raw_all)
    peaks_all    = np.empty_like(raw_all)
    prev         = np.zeros(n_bars, dtype=np.float32)
    peaks        = np.zeros(n_bars, dtype=np.float32)
    peak_hold    = np.zeros(n_bars, dtype=np.int32)
    alpha        = float(SPEC_SMOOTHING)
    one_minus_a  = 1.0 - alpha

    for f in range(total_frames):
        prev = alpha * prev + one_minus_a * raw_all[f]
        smoothed_all[f] = prev

        risen = prev >= peaks
        peaks = np.where(risen, prev, peaks)
        peak_hold = np.where(risen, SPEC_PEAK_HOLD, peak_hold)

        falling = ~risen
        holding = falling & (peak_hold > 0)
        peak_hold = np.where(holding, peak_hold - 1, peak_hold)
        dropping = falling & ~holding
        peaks = np.where(dropping, np.maximum(prev, peaks - 0.015), peaks)
        peaks_all[f] = peaks

    return smoothed_all, peaks_all


def render_spectrum_frame(smoothed_row, peaks_row, n_bars, base_canvas, frame_num,
                          transparent, greenscreen, output_dir=OUTPUT_DIR):
    img  = base_canvas.copy()
    draw = ImageDraw.Draw(img)

    margin  = 120
    total_w = WIDTH - margin * 2
    bar_w   = max(2, (total_w - SPEC_BAR_GAP * max(0, n_bars - 1)) // n_bars)
    max_h   = VIZ_H
    base_y  = VIZ_Y + max_h

    glow_blur = 8
    strip_pad = glow_blur * 2 + 8
    strip_y0  = max(0, VIZ_Y - strip_pad)
    strip_y1  = min(HEIGHT, base_y + strip_pad)
    strip_h   = strip_y1 - strip_y0

    glow_strip = Image.new("RGBA", (WIDTH, strip_h), (0, 0, 0, 0))
    glow_draw  = ImageDraw.Draw(glow_strip)

    if transparent:
        bar_layer = Image.new("RGBA", (WIDTH, strip_h), (0, 0, 0, 0))
        bar_draw  = ImageDraw.Draw(bar_layer)

    for b in range(n_bars):
        v  = float(smoothed_row[b])
        bh = max(4, int(v * max_h))
        x0 = margin + b * (bar_w + SPEC_BAR_GAP)
        x1 = x0 + bar_w
        if x0 >= WIDTH - margin + bar_w:
            break
        y0 = base_y - bh
        y1 = base_y

        col = lerp_color3(*SPECTRUM_COLS, v)

        # Bar body
        if transparent:
            rounded_rect(
                bar_draw,
                [x0, y0 - strip_y0, x1, y1 - strip_y0],
                SPEC_CORNER, col, alpha=220,
            )
        else:
            rounded_rect(draw, [x0, y0, x1, y1], SPEC_CORNER, col)

        # Bar-top glow accumulated on shared strip (1 blur per frame instead of n_bars!)
        glow_draw.rectangle(
            [x0, y0 - strip_y0, x1, min(y0 + 16, y1) - strip_y0],
            fill=(*col, 85),
        )

        # Peak indicator line
        ph = float(peaks_row[b])
        py = base_y - int(ph * max_h) - 2
        if strip_y0 <= py < strip_y1 - 2:
            peak_col = lerp_color3(*SPECTRUM_COLS, ph)
            if transparent:
                bar_draw.rectangle(
                    [x0, py - strip_y0, x1, py - strip_y0 + 2],
                    fill=(*peak_col, 200),
                )
            else:
                draw.rectangle([x0, py, x1, py + 2], fill=peak_col)

    glow_strip = glow_strip.filter(ImageFilter.GaussianBlur(radius=glow_blur))

    if transparent:
        bar_layer.alpha_composite(glow_strip)
        img.alpha_composite(bar_layer, dest=(0, strip_y0))
    else:
        region = img.crop((0, strip_y0, WIDTH, strip_y1)).convert("RGBA")
        region.alpha_composite(glow_strip)
        img.paste(region.convert("RGB"), (0, strip_y0))

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
                        help="Path to audio file (WAV, FLAC, OGG, AIFF, MP3, M4A, AAC, OPUS)")
    parser.add_argument("--duration",    type=float, default=None,
                        help="Override duration in seconds (auto-detected from file if omitted)")
    parser.add_argument("--mode",        choices=["waveform", "spectrum"], default="waveform",
                        help="Visualiser mode: waveform (default) or spectrum")
    parser.add_argument("--bars",        type=int, default=SPEC_BARS,
                        help=f"Number of frequency bars in spectrum mode (default: {SPEC_BARS})")
    parser.add_argument("--start",       type=int, default=0,
                        help="Resume from this frame index (default: 0)")
    parser.add_argument("--workers",     type=int, default=os.cpu_count() or 1,
                        help="Thread count for parallel rendering (default: CPU count)")
    parser.add_argument("--transparent", action="store_true",
                        help="RGBA output with blurred shadows (slower, for alpha compositing)")
    parser.add_argument("--greenscreen", action="store_true",
                        help="Solid green (#00ff00) background for chroma key (fast)")
    parser.add_argument("--title",       default=None,
                        help="Title shown in the top-left (defaults to filename stem)")
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

    print(f"\n  Loading {args.input} …", end="", flush=True)
    try:
        samples, sr = load_audio(args.input)
    except RuntimeError as err:
        print(f"\n  error: {err}", file=sys.stderr)
        sys.exit(1)

    if len(samples) == 0 or sr <= 0:
        print("  error: audio file contains no samples.", file=sys.stderr)
        sys.exit(1)

    print(f" done.  ({len(samples)/sr:.1f}s @ {sr}Hz)")

    duration = args.duration if (args.duration is not None and args.duration > 0) else len(samples) / sr
    if duration <= 0:
        print("  error: duration must be positive.", file=sys.stderr)
        sys.exit(1)

    title       = args.title if args.title is not None else os.path.splitext(os.path.basename(args.input))[0]
    n_bars      = max(1, args.bars)
    workers     = max(1, args.workers)
    start_frame = max(0, args.start)

    mode_str = "transparent RGBA" if args.transparent else ("green screen" if args.greenscreen else "opaque")
    print(f"  ♪ {title}")
    print(f"  mode: {args.mode} · output: {mode_str} · duration: {duration:.1f}s")

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

    base_canvas = build_base_canvas(title, args.transparent, args.greenscreen)

    if args.mode == "spectrum":
        print("  Pre-computing FFT spectrum & peak envelopes …", end="", flush=True)
        smoothed_all, peaks_all = precompute_spectrum_timeline(samples, sr, total, n_bars)
        print(" done.")
    else:
        ref_peak = compute_waveform_ref_peak(samples, sr)

    _done_count = 0
    num_to_render = len(frames)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        if args.mode == "waveform":
            futs = {
                ex.submit(
                    render_waveform_frame,
                    samples, sr, ref_peak, base_canvas, f,
                    args.transparent, args.greenscreen, OUTPUT_DIR,
                ): f
                for f in frames
            }
        else:
            futs = {
                ex.submit(
                    render_spectrum_frame,
                    smoothed_all[f], peaks_all[f], n_bars, base_canvas, f,
                    args.transparent, args.greenscreen, OUTPUT_DIR,
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
