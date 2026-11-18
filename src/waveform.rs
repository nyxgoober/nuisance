use std::fs::{self, File};
use std::io::Write;
use std::path::Path;
use std::process::Command;

use rayon::prelude::*;
use realfft::RealFftPlanner;
use symphonia::core::audio::SampleBuffer;
use symphonia::core::codecs::{DecoderOptions, CODEC_TYPE_NULL};
use symphonia::core::errors::Error as SymphoniaError;
use symphonia::core::formats::FormatOptions;
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;
use symphonia::core::probe::Hint;

use crate::canvas::{
    lerp_color3, Canvas, Color, BG_COLOR, FPS, GS_COLOR, HEIGHT, WIDTH,
};
use crate::progress::{build_pool, confirm_render, print_ffmpeg_hint, ProgressBar};
use crate::text::{draw_text, line_metrics, text_width, FontWeight};

const VIZ_H: i32 = 340;
const VIZ_Y: i32 = (HEIGHT as i32 - VIZ_H) / 2;

const WAVEFORM_COL: Color = [100, 180, 255];
const WAVEFORM_GLOW: Color = [60, 120, 200];
const SPECTRUM_COLS: [Color; 3] = [
    [100, 180, 255], // bass
    [120, 220, 140], // mids
    [255, 210, 70],  // highs
];
const TITLE_COL: Color = [180, 180, 190];
const WATERMARK_COL: Color = [60, 60, 70];

const SIZE_TITLE: u32 = 52;
const SIZE_TITLE_MIN: u32 = 28;
const SIZE_WATERMARK: u32 = 22;

const SHADOW_OFFSET: i32 = 3;
const SHADOW_BLUR: u32 = 6;

const WAVE_WINDOW_S: f64 = 0.08;
const GLOW_BLUR: u32 = 14;

pub const DEFAULT_SPEC_BARS: usize = 64;
const SPEC_MIN_FREQ: f64 = 40.0;
const SPEC_MAX_FREQ: f64 = 16_000.0;
const SPEC_SMOOTHING: f32 = 0.72;
const SPEC_BAR_GAP: i32 = 3;
const SPEC_CORNER: i32 = 5;
const SPEC_PEAK_HOLD: i32 = 45;

#[derive(Copy, Clone, Debug, PartialEq, Eq, clap::ValueEnum)]
pub enum VizMode {
    Waveform,
    Spectrum,
}

pub struct WaveformOptions<'a> {
    pub input: &'a Path,
    pub duration: Option<f64>,
    pub mode: VizMode,
    pub bars: usize,
    pub start: usize,
    pub workers: usize,
    pub transparent: bool,
    pub greenscreen: bool,
    pub title: Option<&'a str>,
    pub preview: bool,
    pub yes: bool,
    pub output_dir: &'a Path,
}

fn decode_with_symphonia(path: &Path) -> Result<(Vec<f32>, u32), String> {
    let file = File::open(path).map_err(|e| format!("open error: {}", e))?;
    let mss = MediaSourceStream::new(Box::new(file), Default::default());

    let mut hint = Hint::new();
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }

    let probed = symphonia::default::get_probe()
        .format(
            &hint,
            mss,
            &FormatOptions::default(),
            &MetadataOptions::default(),
        )
        .map_err(|e| format!("unsupported container/format: {}", e))?;

    let mut format = probed.format;
    let track = format
        .tracks()
        .iter()
        .find(|t| t.codec_params.codec != CODEC_TYPE_NULL)
        .ok_or_else(|| "no audio stream found".to_owned())?;

    let track_id = track.id;
    let mut sample_rate = track.codec_params.sample_rate.unwrap_or(0);

    let mut decoder = symphonia::default::get_codecs()
        .make(&track.codec_params, &DecoderOptions::default())
        .map_err(|e| format!("unsupported codec: {}", e))?;

    let mut mono: Vec<f32> = Vec::new();
    let mut sample_buf: Option<SampleBuffer<f32>> = None;

    loop {
        let packet = match format.next_packet() {
            Ok(p) => p,
            Err(SymphoniaError::IoError(ref err))
                if err.kind() == std::io::ErrorKind::UnexpectedEof =>
            {
                break;
            }
            Err(SymphoniaError::ResetRequired) => {
                decoder.reset();
                continue;
            }
            Err(_) => break,
        };

        if packet.track_id() != track_id {
            continue;
        }

        match decoder.decode(&packet) {
            Ok(decoded) => {
                let spec = *decoded.spec();
                if sample_rate == 0 {
                    sample_rate = spec.rate;
                }
                let channels = spec.channels.count().max(1);
                let cap = decoded.capacity() as u64;
                let sbuf = sample_buf.get_or_insert_with(|| SampleBuffer::<f32>::new(cap, spec));
                if sbuf.capacity() < decoded.frames() * channels {
                    *sbuf = SampleBuffer::<f32>::new(cap, spec);
                }
                sbuf.copy_interleaved_ref(decoded);
                let samples = sbuf.samples();
                if channels == 1 {
                    mono.extend_from_slice(samples);
                } else {
                    let inv_ch = 1.0 / (channels as f32);
                    for frame in samples.chunks_exact(channels) {
                        let sum: f32 = frame.iter().sum();
                        mono.push(sum * inv_ch);
                    }
                }
            }
            Err(SymphoniaError::DecodeError(_)) => continue,
            Err(SymphoniaError::ResetRequired) => {
                decoder.reset();
                continue;
            }
            Err(_) => break,
        }
    }

    if mono.is_empty() || sample_rate == 0 {
        return Err("decoded 0 audio samples".to_owned());
    }
    Ok((mono, sample_rate))
}

fn decode_with_ffmpeg(path: &Path) -> Result<(Vec<f32>, u32), String> {
    let sr = 44_100u32;
    let out = Command::new("ffmpeg")
        .args(["-v", "error", "-i"])
        .arg(path)
        .args(["-ac", "1", "-ar", "44100", "-f", "f32le", "-"])
        .output()
        .map_err(|e| format!("ffmpeg not available: {}", e))?;

    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        return Err(format!("ffmpeg failed: {}", stderr.trim()));
    }
    let n = out.stdout.len() / 4;
    if n == 0 {
        return Err("ffmpeg produced 0 samples".to_owned());
    }
    let mut mono = Vec::with_capacity(n);
    for chunk in out.stdout.chunks_exact(4) {
        mono.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
    }
    Ok((mono, sr))
}

fn load_audio(path: &Path) -> Result<(Vec<f32>, u32), String> {
    match decode_with_symphonia(path) {
        Ok(res) => Ok(res),
        Err(sym_err) => match decode_with_ffmpeg(path) {
            Ok(res) => Ok(res),
            Err(ff_err) => Err(format!(
                "Could not load '{}'.\n  symphonia : {}\n  ffmpeg    : {}",
                path.display(),
                sym_err,
                ff_err
            )),
        },
    }
}

fn build_base_canvas(title: &str, transparent: bool, greenscreen: bool) -> Canvas {
    let bg = if transparent {
        [0, 0, 0, 0]
    } else if greenscreen {
        [GS_COLOR[0], GS_COLOR[1], GS_COLOR[2], 255]
    } else {
        [BG_COLOR[0], BG_COLOR[1], BG_COLOR[2], 255]
    };
    let mut base = Canvas::new(WIDTH, HEIGHT, bg);

    if !title.is_empty() {
        let mut title_fs = SIZE_TITLE;
        while title_fs > SIZE_TITLE_MIN {
            if text_width(title, FontWeight::Bold, title_fs) <= WIDTH as f32 - 240.0 {
                break;
            }
            title_fs -= 2;
        }
        draw_text(
            &mut base,
            120.0,
            28.0,
            title,
            FontWeight::Bold,
            title_fs,
            TITLE_COL,
            1.0,
            transparent,
            greenscreen,
            SHADOW_BLUR,
            SHADOW_OFFSET,
        );
    }

    let wm = "made with Nuisance";
    let wm_w = text_width(wm, FontWeight::Regular, SIZE_WATERMARK);
    let wm_lm = line_metrics(FontWeight::Regular, SIZE_WATERMARK);
    let wx = WIDTH as f32 - wm_w - 40.0;
    let wy = HEIGHT as f32 - wm_lm.line_height - 30.0;
    draw_text(
        &mut base,
        wx,
        wy,
        wm,
        FontWeight::Regular,
        SIZE_WATERMARK,
        WATERMARK_COL,
        0.6,
        transparent,
        greenscreen,
        SHADOW_BLUR,
        SHADOW_OFFSET,
    );

    base
}

fn compute_waveform_ref_peak(samples: &[f32], sr: u32) -> f32 {
    if samples.is_empty() {
        return 1.0;
    }
    let block = ((sr as f64 * 0.005) as usize).max(1);
    let n_blocks = samples.len() / block;
    let peak = if n_blocks >= 4 {
        let mut rms_vals: Vec<f32> = samples[..n_blocks * block]
            .chunks_exact(block)
            .map(|seg| {
                let mean_sq = seg.iter().map(|&x| x * x).sum::<f32>() / (block as f32);
                mean_sq.sqrt()
            })
            .collect();
        rms_vals.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let idx = ((rms_vals.len() as f64 * 0.995) as usize).min(rms_vals.len() - 1);
        rms_vals[idx]
    } else {
        samples.iter().map(|x| x.abs()).fold(0.0f32, f32::max)
    };
    peak.max(1e-4)
}

#[allow(clippy::too_many_arguments)]
fn render_waveform_frame(
    canvas: &mut Canvas,
    glow_tile: &mut Canvas,
    blur_scratch: &mut Vec<u8>,
    mask_scratch: &mut Vec<u8>,
    rgb_scratch: &mut Vec<u8>,
    samples: &[f32],
    sr: u32,
    ref_peak: f32,
    base_canvas: &Canvas,
    frame_num: usize,
    transparent: bool,
    output_dir: &Path,
) -> Result<(), String> {
    let t = frame_num as f64 / FPS;
    canvas.copy_from(base_canvas);

    let half_w = WAVE_WINDOW_S * 0.5;
    let s_start = (((t - half_w) * sr as f64) as isize).max(0) as usize;
    let s_end = (((t + half_w) * sr as f64) as usize).min(samples.len());
    if s_end > s_start + 1 {
        let window = &samples[s_start..s_end];
        let cy = HEIGHT as i32 / 2;
        let draw_w = (WIDTH as i32 - 240) as usize;
        let target_pts = 420usize; // 4px horizontal step — smooth envelope, 4x faster AA raster
        let draw_h = VIZ_H as f32;
        let x0_draw = (WIDTH as i32 - draw_w as i32) / 2;

        let rms_cols: Vec<f32> = if window.len() >= target_pts {
            let chunk = window.len() / target_pts;
            let n_chunks = target_pts.min(window.len() / chunk);
            window[..n_chunks * chunk]
                .chunks_exact(chunk)
                .map(|seg| {
                    let msq = seg.iter().map(|&v| v * v).sum::<f32>() / (chunk as f32);
                    (msq.sqrt() / ref_peak).clamp(0.0, 1.0)
                })
                .collect()
        } else {
            window
                .iter()
                .map(|&v| (v.abs() / ref_peak).clamp(0.0, 1.0))
                .collect()
        };

        if rms_cols.len() > 1 {
            let n_c = rms_cols.len();
            let pad_y = (GLOW_BLUR as i32) * 2 + 6;
            let band_h = VIZ_H + pad_y * 2;
            let band_cy = band_h as f32 * 0.5;
            let band_y0 = cy - (band_h / 2);

            let mut pts_up_local = Vec::with_capacity(n_c);
            let mut pts_dn_local = Vec::with_capacity(n_c);
            let denom = (n_c - 1).max(1) as f32;
            for (ci, &v) in rms_cols.iter().enumerate() {
                let lx = (ci as f32) * ((draw_w - 1) as f32 / denom);
                let dy = v * (draw_h * 0.45);
                pts_up_local.push((lx, band_cy - dy));
                pts_dn_local.push((lx, band_cy + dy));
            }

            glow_tile.fill([0, 0, 0, 0]);
            glow_tile.draw_polyline(
                &pts_up_local,
                6.0,
                [WAVEFORM_GLOW[0], WAVEFORM_GLOW[1], WAVEFORM_GLOW[2], 180],
                mask_scratch,
            );
            glow_tile.draw_polyline(
                &pts_dn_local,
                6.0,
                [WAVEFORM_GLOW[0], WAVEFORM_GLOW[1], WAVEFORM_GLOW[2], 180],
                mask_scratch,
            );
            glow_tile.gaussian_blur_with_scratch(GLOW_BLUR, blur_scratch);

            let main_a = if transparent { 220 } else { 255 };
            glow_tile.draw_polyline(
                &pts_up_local,
                3.0,
                [WAVEFORM_COL[0], WAVEFORM_COL[1], WAVEFORM_COL[2], main_a],
                mask_scratch,
            );
            glow_tile.draw_polyline(
                &pts_dn_local,
                3.0,
                [WAVEFORM_COL[0], WAVEFORM_COL[1], WAVEFORM_COL[2], main_a],
                mask_scratch,
            );
            canvas.alpha_composite(glow_tile, x0_draw, band_y0);

            if !transparent {
                canvas.draw_hline(
                    x0_draw,
                    x0_draw + draw_w as i32 - 1,
                    cy,
                    [
                        WAVEFORM_COL[0] / 4,
                        WAVEFORM_COL[1] / 4,
                        WAVEFORM_COL[2] / 4,
                        255,
                    ],
                );
            }
        }
    }

    let frame_path = output_dir.join(format!("frame_{:06}.png", frame_num));
    canvas.save_png(&frame_path, transparent, rgb_scratch)
}

enum BandMap {
    Range { start: usize, end: usize },
    Interp { i0: usize, i1: usize, frac: f32 },
}

fn build_band_maps(sr: u32, n_bars: usize, fft_size: usize) -> Vec<BandMap> {
    let n_bins = fft_size / 2 + 1;
    let df = sr as f64 / fft_size as f64;
    let lo_hz = SPEC_MIN_FREQ.max(1.0);
    let hi_hz = SPEC_MAX_FREQ.min((sr as f64 * 0.48).max(lo_hz * 2.0));
    let log_lo = lo_hz.log10();
    let log_hi = hi_hz.log10();

    let mut edges = Vec::with_capacity(n_bars + 1);
    for i in 0..=n_bars {
        let exp = log_lo + (log_hi - log_lo) * (i as f64 / n_bars as f64);
        edges.push(10.0f64.powf(exp));
    }

    let mut maps = Vec::with_capacity(n_bars);
    for b in 0..n_bars {
        let e0 = edges[b];
        let e1 = edges[b + 1];
        let idx_start = ((e0 / df).ceil() as usize).min(n_bins);
        let idx_end = ((e1 / df).ceil() as usize).min(n_bins);
        if idx_end > idx_start {
            maps.push(BandMap::Range {
                start: idx_start,
                end: idx_end,
            });
        } else {
            let center_hz = 0.5 * (e0 + e1);
            let f_idx = center_hz / df;
            let i0 = (f_idx.floor() as usize).min(n_bins - 1);
            let i1 = (i0 + 1).min(n_bins - 1);
            let frac = (f_idx - i0 as f64) as f32;
            maps.push(BandMap::Interp { i0, i1, frac });
        }
    }
    maps
}

fn precompute_spectrum_timeline(
    pool: &rayon::ThreadPool,
    samples: &[f32],
    sr: u32,
    total_frames: usize,
    n_bars: usize,
) -> (Vec<Vec<f32>>, Vec<Vec<f32>>) {
    let fft_size = 4096usize;
    let maps = build_band_maps(sr, n_bars, fft_size);
    let mut planner = RealFftPlanner::<f32>::new();
    let r2c = planner.plan_fft_forward(fft_size);

    let hanning: Vec<f32> = (0..fft_size)
        .map(|i| {
            let phase = 2.0 * std::f64::consts::PI * (i as f64) / ((fft_size - 1) as f64);
            (0.5 * (1.0 - phase.cos())) as f32
        })
        .collect();

    let half_win = fft_size as f64 / sr as f64 / 2.0;
    let log_denom = (500.0f32).ln_1p();
    let fft_norm = 1.0f32; // matches numpy rfft unnormalized scale

    // Parallel FFT across all frames!
    let raw_all: Vec<Vec<f32>> = pool.install(|| {
        (0..total_frames)
            .into_par_iter()
            .map_init(
                || {
                    (
                        r2c.make_input_vec(),
                        r2c.make_output_vec(),
                        r2c.make_scratch_vec(),
                        vec![0.0f32; fft_size / 2 + 1],
                    )
                },
                |(in_buf, out_buf, scratch, mag_sq), f| {
                    let mut bars = vec![0.0f32; n_bars];
                    if samples.len() < 64 {
                        return bars;
                    }
                    let t = f as f64 / FPS;
                    let s0 = (((t - half_win) * sr as f64) as isize).max(0) as usize;
                    if s0 >= samples.len() {
                        return bars;
                    }
                    let s1 = (s0 + fft_size).min(samples.len());
                    let win_len = s1 - s0;
                    if win_len < 64 {
                        return bars;
                    }

                    in_buf.fill(0.0);
                    if win_len == fft_size {
                        for i in 0..fft_size {
                            in_buf[i] = samples[s0 + i] * hanning[i];
                        }
                    } else {
                        for i in 0..win_len {
                            let phase =
                                2.0 * std::f64::consts::PI * (i as f64) / ((win_len - 1) as f64);
                            let w = (0.5 * (1.0 - phase.cos())) as f32;
                            in_buf[i] = samples[s0 + i] * w;
                        }
                    }

                    if r2c.process_with_scratch(in_buf, out_buf, scratch).is_err() {
                        return bars;
                    }

                    for (i, c) in out_buf.iter().enumerate() {
                        mag_sq[i] = (c.re * c.re + c.im * c.im) * fft_norm;
                    }

                    for (b, m) in maps.iter().enumerate() {
                        let mean_sq = match *m {
                            BandMap::Range { start, end } => {
                                let sum: f32 = mag_sq[start..end].iter().sum();
                                sum / ((end - start) as f32)
                            }
                            BandMap::Interp { i0, i1, frac } => {
                                mag_sq[i0] * (1.0 - frac) + mag_sq[i1] * frac
                            }
                        };
                        let v = ((mean_sq.sqrt() * 500.0).ln_1p() / log_denom).clamp(0.0, 1.0);
                        bars[b] = v;
                    }
                    bars
                },
            )
            .collect()
    });

    // Sequential causal smoothing & peak hold across frames 0..total_frames
    let mut smoothed_all = Vec::with_capacity(total_frames);
    let mut peaks_all = Vec::with_capacity(total_frames);
    let mut prev = vec![0.0f32; n_bars];
    let mut peaks = vec![0.0f32; n_bars];
    let mut peak_hold = vec![0i32; n_bars];

    for raw in &raw_all {
        for b in 0..n_bars {
            prev[b] = SPEC_SMOOTHING * prev[b] + (1.0 - SPEC_SMOOTHING) * raw[b];
            if prev[b] >= peaks[b] {
                peaks[b] = prev[b];
                peak_hold[b] = SPEC_PEAK_HOLD;
            } else if peak_hold[b] > 0 {
                peak_hold[b] -= 1;
            } else {
                peaks[b] = prev[b].max(peaks[b] - 0.015);
            }
        }
        smoothed_all.push(prev.clone());
        peaks_all.push(peaks.clone());
    }

    (smoothed_all, peaks_all)
}

#[allow(clippy::too_many_arguments)]
fn render_spectrum_frame(
    canvas: &mut Canvas,
    glow_strip: &mut Canvas,
    blur_scratch: &mut Vec<u8>,
    rgb_scratch: &mut Vec<u8>,
    smoothed_row: &[f32],
    peaks_row: &[f32],
    n_bars: usize,
    base_canvas: &Canvas,
    frame_num: usize,
    transparent: bool,
    output_dir: &Path,
) -> Result<(), String> {
    canvas.copy_from(base_canvas);

    let margin = 120i32;
    let total_w = WIDTH as i32 - margin * 2;
    let gaps = SPEC_BAR_GAP * (n_bars.saturating_sub(1) as i32);
    let bar_w = ((total_w - gaps) / (n_bars as i32)).max(2);
    let max_h = VIZ_H;
    let base_y = VIZ_Y + max_h;

    let glow_blur = 8u32;
    let strip_pad = (glow_blur as i32) * 2 + 8;
    let strip_y0 = (VIZ_Y - strip_pad).max(0);
    let strip_y1 = (base_y + strip_pad).min(HEIGHT as i32);

    glow_strip.fill([0, 0, 0, 0]);
    let body_a = if transparent { 220 } else { 255 };
    let peak_a = if transparent { 200 } else { 255 };

    for b in 0..n_bars {
        let v = smoothed_row[b] as f64;
        let bh = ((v * max_h as f64) as i32).max(4);
        let x0 = margin + (b as i32) * (bar_w + SPEC_BAR_GAP);
        let x1 = x0 + bar_w;
        if x0 >= WIDTH as i32 - margin + bar_w {
            break;
        }
        let y0 = base_y - bh;
        let y1 = base_y;

        let col = lerp_color3(SPECTRUM_COLS[0], SPECTRUM_COLS[1], SPECTRUM_COLS[2], v);

        canvas.fill_rounded_rect(
            x0,
            y0,
            x1,
            y1,
            SPEC_CORNER,
            [col[0], col[1], col[2], body_a],
        );

        glow_strip.fill_rect(
            x0,
            y0 - strip_y0,
            x1,
            (y0 + 16).min(y1) - strip_y0,
            [col[0], col[1], col[2], 85],
        );

        let ph = peaks_row[b] as f64;
        let py = base_y - (ph * max_h as f64) as i32 - 2;
        if py >= strip_y0 && py < strip_y1 - 2 {
            let peak_col = lerp_color3(SPECTRUM_COLS[0], SPECTRUM_COLS[1], SPECTRUM_COLS[2], ph);
            canvas.fill_rect(
                x0,
                py,
                x1,
                py + 2,
                [peak_col[0], peak_col[1], peak_col[2], peak_a],
            );
        }
    }

    glow_strip.gaussian_blur_with_scratch(glow_blur, blur_scratch);
    canvas.alpha_composite(glow_strip, 0, strip_y0);

    let frame_path = output_dir.join(format!("frame_{:06}.png", frame_num));
    canvas.save_png(&frame_path, transparent, rgb_scratch)
}

pub fn run(opts: WaveformOptions<'_>) -> Result<(), String> {
    if opts.transparent && opts.greenscreen {
        return Err("--transparent and --greenscreen are mutually exclusive.".to_owned());
    }
    if !opts.input.is_file() {
        return Err(format!("file not found: {}", opts.input.display()));
    }

    print!("\n  Loading {} …", opts.input.display());
    let _ = std::io::stdout().flush();
    let (samples, sr) = load_audio(opts.input)?;
    println!(" done.  ({:.1}s @ {}Hz)", samples.len() as f64 / sr as f64, sr);

    let auto_dur = samples.len() as f64 / sr as f64;
    let duration = opts.duration.filter(|&d| d > 0.0).unwrap_or(auto_dur);
    if duration <= 0.0 {
        return Err("duration must be positive.".to_owned());
    }

    let default_title = opts
        .input
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("Untitled");
    let title = opts.title.unwrap_or(default_title);
    let n_bars = opts.bars.max(1);
    let workers = opts.workers.max(1);

    let mode_name = match opts.mode {
        VizMode::Waveform => "waveform",
        VizMode::Spectrum => "spectrum",
    };
    let out_str = if opts.transparent {
        "transparent RGBA"
    } else if opts.greenscreen {
        "green screen"
    } else {
        "opaque"
    };

    println!("  ♪ {}", title);
    println!(
        "  mode: {} · output: {} · duration: {:.1}s",
        mode_name, out_str, duration
    );

    fs::create_dir_all(opts.output_dir)
        .map_err(|e| format!("failed to create '{}': {}", opts.output_dir.display(), e))?;

    let total = (duration * FPS) as usize;
    let step = if opts.preview { FPS as usize } else { 1 };
    let frames: Vec<usize> = (opts.start..total).step_by(step).collect();

    if opts.preview {
        println!(
            "  PREVIEW MODE — {} frames (1/sec) · {} workers",
            frames.len(),
            workers
        );
    } else {
        println!(
            "  {} frames @ {}fps · {} workers",
            frames.len(),
            FPS as u32,
            workers
        );
    }

    if frames.is_empty() {
        println!("  Nothing to render (0 frames).");
        return Ok(());
    }

    println!("  output → {}/\n", opts.output_dir.display());

    if !confirm_render(opts.preview || opts.yes) {
        return Ok(());
    }

    let base_canvas = build_base_canvas(title, opts.transparent, opts.greenscreen);
    let pool = build_pool(workers)?;
    let pb = ProgressBar::new(frames.len());

    match opts.mode {
        VizMode::Waveform => {
            let ref_peak = compute_waveform_ref_peak(&samples, sr);
            let draw_w = WIDTH - 240;
            let band_h = (VIZ_H + (GLOW_BLUR as i32) * 2 + 6 * 2) as u32;
            let res: Result<(), String> = pool.install(|| {
                frames.par_iter().try_for_each_init(
                    || {
                        (
                            Canvas::new(WIDTH, HEIGHT, [0, 0, 0, 0]),
                            Canvas::new(draw_w, band_h, [0, 0, 0, 0]),
                            Vec::<u8>::new(),
                            Vec::<u8>::new(),
                            Vec::<u8>::new(),
                        )
                    },
                    |(canvas, glow_tile, blur_scratch, mask_scratch, rgb_scratch), &f| {
                        render_waveform_frame(
                            canvas,
                            glow_tile,
                            blur_scratch,
                            mask_scratch,
                            rgb_scratch,
                            &samples,
                            sr,
                            ref_peak,
                            &base_canvas,
                            f,
                            opts.transparent,
                            opts.output_dir,
                        )?;
                        pb.inc();
                        Ok(())
                    },
                )
            });
            res?;
        }
        VizMode::Spectrum => {
            print!("  Pre-computing FFT spectrum & peak envelopes …");
            let _ = std::io::stdout().flush();
            let (smoothed_all, peaks_all) =
                precompute_spectrum_timeline(&pool, &samples, sr, total, n_bars);
            println!(" done.");

            let strip_pad = 8 * 2 + 8;
            let strip_y0 = (VIZ_Y - strip_pad).max(0);
            let strip_y1 = (VIZ_Y + VIZ_H + strip_pad).min(HEIGHT as i32);
            let strip_h = (strip_y1 - strip_y0) as u32;

            let res: Result<(), String> = pool.install(|| {
                frames.par_iter().try_for_each_init(
                    || {
                        (
                            Canvas::new(WIDTH, HEIGHT, [0, 0, 0, 0]),
                            Canvas::new(WIDTH, strip_h, [0, 0, 0, 0]),
                            Vec::<u8>::new(),
                            Vec::<u8>::new(),
                        )
                    },
                    |(canvas, glow_strip, blur_scratch, rgb_scratch), &f| {
                        render_spectrum_frame(
                            canvas,
                            glow_strip,
                            blur_scratch,
                            rgb_scratch,
                            &smoothed_all[f],
                            &peaks_all[f],
                            n_bars,
                            &base_canvas,
                            f,
                            opts.transparent,
                            opts.output_dir,
                        )?;
                        pb.inc();
                        Ok(())
                    },
                )
            });
            res?;
        }
    }

    let input_str = opts.input.to_string_lossy();
    print_ffmpeg_hint(
        &opts.output_dir.to_string_lossy(),
        FPS as u32,
        opts.transparent,
        opts.greenscreen,
        &input_str,
    );
    Ok(())
}
