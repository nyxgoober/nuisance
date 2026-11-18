use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Write;
use std::path::Path;

use midly::{Format, MetaMessage, MidiMessage, Smf, Timing, TrackEventKind};
use rayon::prelude::*;

use crate::canvas::{
    clamp, ease_out, lerp, lerp_color, Canvas, Color, BG_COLOR, FPS, GS_COLOR, HEIGHT, WIDTH,
};
use crate::lrc::decode_bytes;
use crate::progress::{build_pool, confirm_render, print_ffmpeg_hint, ProgressBar};
use crate::text::{blit_glyphs_solid, draw_text, line_metrics, text_width, FontWeight};

pub const DEFAULT_LOOKAHEAD: f64 = 3.0;

const KEY_AREA_H: i32 = 180;
const WHITE_KEY_H: i32 = KEY_AREA_H;
const BLACK_KEY_H: i32 = (KEY_AREA_H as f64 * 0.60) as i32;
const NOTE_AREA_H: i32 = HEIGHT as i32 - KEY_AREA_H;

const MIDI_LOW: u8 = 21;
const MIDI_HIGH: u8 = 108;

const NOTE_RADIUS: i32 = 6;
const NOTE_MIN_H: f64 = 8.0;
const NOTE_GAP: f64 = 4.0;
const GLOW_BLUR: u32 = 18;
const GLOW_ALPHA: u8 = 160;

const WK_COLOR: Color = [220, 220, 228];
const BK_COLOR: Color = [18, 18, 24];
const DIVIDER_COL: Color = [30, 30, 40];
const LABEL_COL: Color = [100, 100, 115];
const WATERMARK_COL: Color = [60, 60, 70];
const TITLE_COL: Color = [180, 180, 190];

const SIZE_TITLE: u32 = 52;
const SIZE_TITLE_MIN: u32 = 28;
const SIZE_WATERMARK: u32 = 22;
const SIZE_LABEL: u32 = 18;

const SHADOW_OFFSET: i32 = 3;
const SHADOW_BLUR: u32 = 6;

const TRACK_PALETTE: [Color; 8] = [
    [100, 180, 255], // soft blue
    [255, 130, 90],  // coral
    [120, 220, 140], // mint green
    [220, 140, 255], // lavender
    [255, 210, 70],  // amber
    [80, 210, 210],  // teal
    [255, 120, 160], // pink
    [160, 200, 80],  // lime
];

pub struct MidiOptions<'a> {
    pub input: &'a Path,
    pub duration: Option<f64>,
    pub start: usize,
    pub workers: usize,
    pub lookahead: f64,
    pub transparent: bool,
    pub greenscreen: bool,
    pub title: Option<&'a str>,
    pub preview: bool,
    pub yes: bool,
    pub output_dir: &'a Path,
}

#[derive(Clone, Debug)]
struct Note {
    pitch: u8,
    start: f64,
    end: f64,
    velocity: u8,
}

struct TempoConverter {
    ticks: Vec<u64>,
    tempos_us: Vec<u32>,
    cum_sec: Vec<f64>,
    tpb: f64,
    smpte_tps: Option<f64>,
}

impl TempoConverter {
    fn from_events(mut events: Vec<(u64, u32)>, timing: Timing) -> Self {
        match timing {
            Timing::Timecode(fps, sub) => Self {
                ticks: vec![0],
                tempos_us: vec![500_000],
                cum_sec: vec![0.0],
                tpb: 480.0,
                smpte_tps: Some((fps.as_f32() as f64 * sub as f64).max(1.0)),
            },
            Timing::Metrical(tpb_raw) => {
                let tpb = (tpb_raw.as_int() as f64).max(1.0);
                events.sort_by_key(|e| e.0);
                let mut dedup: Vec<(u64, u32)> = vec![(0, 500_000)];
                for (tk, tempo) in events {
                    if let Some(last) = dedup.last_mut() {
                        if last.0 == tk {
                            last.1 = tempo;
                            continue;
                        }
                    }
                    dedup.push((tk, tempo));
                }
                let mut ticks = Vec::with_capacity(dedup.len());
                let mut tempos_us = Vec::with_capacity(dedup.len());
                let mut cum_sec = Vec::with_capacity(dedup.len());
                cum_sec.push(0.0);
                for (i, &(tk, tm)) in dedup.iter().enumerate() {
                    ticks.push(tk);
                    tempos_us.push(tm);
                    if i > 0 {
                        let dt = (tk - dedup[i - 1].0) as f64;
                        let prev_t = dedup[i - 1].1 as f64;
                        let prev_s = *cum_sec.last().unwrap();
                        cum_sec.push(prev_s + (dt / tpb) * (prev_t / 1_000_000.0));
                    }
                }
                Self {
                    ticks,
                    tempos_us,
                    cum_sec,
                    tpb,
                    smpte_tps: None,
                }
            }
        }
    }

    #[inline]
    fn ticks_to_sec(&self, tick: u64) -> f64 {
        if let Some(tps) = self.smpte_tps {
            return tick as f64 / tps;
        }
        let idx = self.ticks.partition_point(|&tk| tk <= tick).saturating_sub(1);
        let dt = (tick - self.ticks[idx]) as f64;
        self.cum_sec[idx] + (dt / self.tpb) * (self.tempos_us[idx] as f64 / 1_000_000.0)
    }
}

fn load_midi(path: &Path) -> Result<(Vec<Vec<Note>>, Vec<String>, f64), String> {
    let raw = fs::read(path)
        .map_err(|e| format!("failed to read '{}': {}", path.display(), e))?;
    let smf = Smf::parse(&raw).map_err(|e| format!("MIDI parse error: {}", e))?;

    let is_seq = matches!(smf.header.format, Format::Sequential);
    let mut global_tempos = Vec::new();
    if !is_seq {
        for tr in &smf.tracks {
            let mut abs_tick = 0u64;
            for ev in tr {
                abs_tick += ev.delta.as_int() as u64;
                if let TrackEventKind::Meta(MetaMessage::Tempo(t)) = ev.kind {
                    global_tempos.push((abs_tick, t.as_int()));
                }
            }
        }
    }
    let global_conv = TempoConverter::from_events(global_tempos, smf.header.timing);

    let mut tracks = Vec::new();
    let mut names = Vec::new();
    let mut duration = 0.0f64;

    for tr in &smf.tracks {
        let local_conv = if is_seq {
            let mut tr_tempos = Vec::new();
            let mut tk = 0u64;
            for ev in tr {
                tk += ev.delta.as_int() as u64;
                if let TrackEventKind::Meta(MetaMessage::Tempo(t)) = ev.kind {
                    tr_tempos.push((tk, t.as_int()));
                }
            }
            Some(TempoConverter::from_events(tr_tempos, smf.header.timing))
        } else {
            None
        };
        let conv = local_conv.as_ref().unwrap_or(&global_conv);

        let mut track_name = String::new();
        let mut abs_tick = 0u64;
        let mut open_notes: HashMap<(u8, u8), Vec<(f64, u8)>> = HashMap::new();
        let mut notes: Vec<Note> = Vec::new();

        for ev in tr {
            abs_tick += ev.delta.as_int() as u64;
            match ev.kind {
                TrackEventKind::Meta(MetaMessage::TrackName(b)) if track_name.is_empty() => {
                    let s = decode_bytes(b);
                    let trimmed = s.trim();
                    if !trimmed.is_empty() {
                        track_name = trimmed.to_owned();
                    }
                }
                TrackEventKind::Midi { channel, message } => {
                    let ch = channel.as_int();
                    let t_sec = conv.ticks_to_sec(abs_tick);
                    match message {
                        MidiMessage::NoteOn { key, vel } if vel.as_int() > 0 => {
                            open_notes
                                .entry((ch, key.as_int()))
                                .or_default()
                                .push((t_sec, vel.as_int()));
                        }
                        MidiMessage::NoteOff { key, .. } | MidiMessage::NoteOn { key, .. } => {
                            if let Some(stack) = open_notes.get_mut(&(ch, key.as_int())) {
                                if !stack.is_empty() {
                                    let (start, vel) = stack.remove(0);
                                    let end = t_sec.max(start + 0.02);
                                    notes.push(Note {
                                        pitch: key.as_int(),
                                        start,
                                        end,
                                        velocity: vel,
                                    });
                                    if end > duration {
                                        duration = end;
                                    }
                                }
                            }
                        }
                        _ => {}
                    }
                }
                _ => {}
            }
        }

        for ((_, pitch), stack) in open_notes {
            for (start, vel) in stack {
                let end = start + 0.1;
                notes.push(Note {
                    pitch,
                    start,
                    end,
                    velocity: vel,
                });
                if end > duration {
                    duration = end;
                }
            }
        }

        if !notes.is_empty() {
            let final_name = if track_name.is_empty() {
                format!("Track {}", tracks.len() + 1)
            } else {
                track_name
            };
            tracks.push(notes);
            names.push(final_name);
        }
    }

    let final_dur = if duration > 0.0 { duration + 1.0 } else { 0.0 };
    Ok((tracks, names, final_dur))
}

#[inline]
fn is_black(pitch: u8) -> bool {
    matches!(pitch % 12, 1 | 3 | 6 | 8 | 10)
}

#[derive(Copy, Clone, Debug)]
struct KeyGeom {
    x: f64,
    w: f64,
    is_black: bool,
}

fn get_actual_range(tracks: &[Vec<Note>]) -> (u8, u8) {
    let mut lo = 127u8;
    let mut hi = 0u8;
    for tr in tracks {
        for n in tr {
            lo = lo.min(n.pitch);
            hi = hi.max(n.pitch);
        }
    }
    if lo > hi {
        return (MIDI_LOW, MIDI_HIGH);
    }
    if hi >= MIDI_LOW && lo <= MIDI_HIGH {
        lo = lo.max(MIDI_LOW);
        hi = hi.min(MIDI_HIGH);
        if lo > hi {
            lo = MIDI_LOW;
            hi = MIDI_HIGH;
        }
    }
    if hi - lo < 24 {
        let mid_p = ((lo as u16 + hi as u16) / 2) as i32;
        let new_lo = (mid_p - 12).max(0);
        let new_hi = (new_lo + 24).min(127);
        lo = (new_hi - 24).max(0) as u8;
        hi = new_hi as u8;
    }
    if is_black(lo) && lo > 0 {
        lo -= 1;
    }
    if is_black(hi) && hi < 127 {
        hi += 1;
    }
    (lo, hi)
}

fn build_key_geometry(lo: u8, hi: u8) -> HashMap<u8, KeyGeom> {
    let whites: Vec<u8> = (lo..=hi).filter(|&p| !is_black(p)).collect();
    if whites.is_empty() {
        return HashMap::new();
    }
    let margin = 40.0;
    let total_w = WIDTH as f64 - 2.0 * margin;
    let wk_w = total_w / whites.len() as f64;

    let mut white_x = HashMap::with_capacity(whites.len());
    for (idx, &p) in whites.iter().enumerate() {
        white_x.insert(p, margin + idx as f64 * wk_w);
    }

    let mut geom = HashMap::with_capacity((hi - lo + 1) as usize);
    for p in lo..=hi {
        if !is_black(p) {
            geom.insert(
                p,
                KeyGeom {
                    x: white_x[&p],
                    w: (wk_w - 1.0).max(2.0),
                    is_black: false,
                },
            );
        } else {
            let bw = (wk_w * 0.55).max(2.0);
            let bx = if let Some(&lx) = p.checked_sub(1).and_then(|lp| white_x.get(&lp)) {
                lx + wk_w - bw * 0.5
            } else if let Some(&rx) = p.checked_add(1).and_then(|rp| white_x.get(&rp)) {
                rx - bw * 0.5
            } else {
                continue;
            };
            geom.insert(
                p,
                KeyGeom {
                    x: bx,
                    w: bw,
                    is_black: true,
                },
            );
        }
    }
    geom
}

struct IndexedTrack {
    notes: Vec<Note>,
    prefix_max_end: Vec<f64>,
}

fn build_index(tracks: Vec<Vec<Note>>) -> Vec<IndexedTrack> {
    tracks
        .into_iter()
        .map(|mut notes| {
            notes.sort_by(|a, b| {
                a.start
                    .partial_cmp(&b.start)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then_with(|| a.end.partial_cmp(&b.end).unwrap_or(std::cmp::Ordering::Equal))
            });
            let mut pmax = Vec::with_capacity(notes.len());
            let mut cur = -1.0f64;
            for n in &notes {
                if n.end > cur {
                    cur = n.end;
                }
                pmax.push(cur);
            }
            IndexedTrack {
                notes,
                prefix_max_end: pmax,
            }
        })
        .collect()
}

fn notes_visible<'a>(track: &'a IndexedTrack, t_now: f64, lookahead: f64) -> impl Iterator<Item = &'a Note> {
    let hi = track.notes.partition_point(|n| n.start <= t_now + lookahead);
    let cutoff = t_now - 0.1;
    let lo = track.prefix_max_end[..hi].partition_point(|&m| m < cutoff);
    track.notes[lo..hi].iter().filter(move |n| n.end >= cutoff)
}

fn draw_white_key(canvas: &mut Canvas, pitch: u8, g: &KeyGeom, fill: Color) {
    let x0 = g.x as i32;
    let x1 = (g.x + g.w) as i32;
    let y0 = NOTE_AREA_H;
    let y1 = NOTE_AREA_H + WHITE_KEY_H - 1;
    canvas.fill_rect(x0, y0, x1, y1, [fill[0], fill[1], fill[2], 255]);
    canvas.draw_vline(x1, y0, y1, [160, 160, 170, 255]);
    if pitch % 12 == 0 {
        let octave = (pitch as i32 / 12) - 1;
        let lbl = format!("C{}", octave);
        let lx = (g.x + 3.0) as f32;
        let ly = (NOTE_AREA_H + WHITE_KEY_H - SIZE_LABEL as i32 - 6) as f32;
        blit_glyphs_solid(
            canvas,
            lx,
            ly,
            &lbl,
            FontWeight::Regular,
            SIZE_LABEL,
            [LABEL_COL[0], LABEL_COL[1], LABEL_COL[2], 255],
        );
    }
}

fn draw_black_key(canvas: &mut Canvas, g: &KeyGeom, fill: Color) {
    let x0 = g.x as i32;
    let x1 = (g.x + g.w) as i32;
    let y0 = NOTE_AREA_H;
    let y1 = NOTE_AREA_H + BLACK_KEY_H;
    canvas.fill_rect(x0, y0, x1, y1, [fill[0], fill[1], fill[2], 255]);
    canvas.draw_hline(x0, x1, y0, [50, 50, 60, 255]);
}

struct StaticScene {
    base: Canvas,
    title: String,
    title_fs: u32,
    legend_items: Vec<(i32, i32, String, Color)>,
    header_strip: Option<Canvas>,
}

fn build_static_scene(
    geom: &HashMap<u8, KeyGeom>,
    lo: u8,
    hi: u8,
    title: &str,
    track_names: &[String],
    track_colors: &[Color],
    transparent: bool,
    greenscreen: bool,
) -> StaticScene {
    let bg = if transparent {
        [0, 0, 0, 0]
    } else if greenscreen {
        [GS_COLOR[0], GS_COLOR[1], GS_COLOR[2], 255]
    } else {
        [BG_COLOR[0], BG_COLOR[1], BG_COLOR[2], 255]
    };
    let mut base = Canvas::new(WIDTH, HEIGHT, bg);

    if !transparent && !greenscreen {
        for y in (0..NOTE_AREA_H).step_by(2) {
            let frac = y as f64 / NOTE_AREA_H as f64;
            let r = lerp(BG_COLOR[0] as f64, BG_COLOR[0] as f64 + 6.0, frac) as u8;
            let g = lerp(BG_COLOR[1] as f64, BG_COLOR[1] as f64 + 6.0, frac) as u8;
            let b = lerp(BG_COLOR[2] as f64, BG_COLOR[2] as f64 + 8.0, frac) as u8;
            base.draw_hline(0, WIDTH as i32 - 1, y, [r, g, b, 255]);
            base.draw_hline(0, WIDTH as i32 - 1, y + 1, [r, g, b, 255]);
        }
        for y in (0..NOTE_AREA_H).step_by(60) {
            base.draw_hline(0, WIDTH as i32 - 1, y, [20, 20, 28, 255]);
        }
    }

    base.fill_rect(
        0,
        NOTE_AREA_H - 1,
        WIDTH as i32 - 1,
        NOTE_AREA_H + 1,
        [DIVIDER_COL[0], DIVIDER_COL[1], DIVIDER_COL[2], 255],
    );

    for p in lo..=hi {
        if let Some(g) = geom.get(&p) {
            if !g.is_black {
                draw_white_key(&mut base, p, g, WK_COLOR);
            }
        }
    }
    for p in lo..=hi {
        if let Some(g) = geom.get(&p) {
            if g.is_black {
                draw_black_key(&mut base, g, BK_COLOR);
            }
        }
    }

    let wm = "made with Nuisance";
    let wm_w = text_width(wm, FontWeight::Regular, SIZE_WATERMARK);
    let wm_lm = line_metrics(FontWeight::Regular, SIZE_WATERMARK);
    let wx = WIDTH as f32 - wm_w - 40.0;
    let wy = HEIGHT as f32 - wm_lm.line_height - 30.0;
    if transparent {
        draw_text(
            &mut base,
            wx,
            wy,
            wm,
            FontWeight::Regular,
            SIZE_WATERMARK,
            WATERMARK_COL,
            0.6,
            true,
            false,
            SHADOW_BLUR,
            SHADOW_OFFSET,
        );
    } else {
        blit_glyphs_solid(
            &mut base,
            wx + SHADOW_OFFSET as f32,
            wy + SHADOW_OFFSET as f32,
            wm,
            FontWeight::Regular,
            SIZE_WATERMARK,
            [0, 0, 0, 255],
        );
        blit_glyphs_solid(
            &mut base,
            wx,
            wy,
            wm,
            FontWeight::Regular,
            SIZE_WATERMARK,
            [WATERMARK_COL[0], WATERMARK_COL[1], WATERMARK_COL[2], 255],
        );
    }

    let mut title_fs = SIZE_TITLE;
    if !title.is_empty() {
        while title_fs > SIZE_TITLE_MIN {
            if text_width(title, FontWeight::Bold, title_fs) <= WIDTH as f32 - 100.0 {
                break;
            }
            title_fs -= 2;
        }
    }

    let mut legend_items = Vec::with_capacity(track_names.len());
    let mut lx = 50i32;
    let mut ly = if !title.is_empty() {
        28 + title_fs as i32 + 12
    } else {
        28
    };
    for (ti, name) in track_names.iter().enumerate() {
        let tw = text_width(name, FontWeight::Regular, 20).ceil() as i32;
        let item_w = tw + 40;
        if lx > 50 && lx + item_w > WIDTH as i32 - 50 {
            lx = 50;
            ly += 28;
        }
        legend_items.push((lx, ly, name.clone(), track_colors[ti]));
        lx += item_w;
    }

    let header_strip = if transparent && (!title.is_empty() || !legend_items.is_empty()) {
        let hdr_h = (ly + 48).max(140).min(NOTE_AREA_H) as u32;
        let mut hdr = Canvas::new(WIDTH, hdr_h, [0, 0, 0, 0]);
        if !title.is_empty() {
            draw_text(
                &mut hdr,
                50.0,
                28.0,
                title,
                FontWeight::Bold,
                title_fs,
                TITLE_COL,
                1.0,
                true,
                false,
                SHADOW_BLUR,
                SHADOW_OFFSET,
            );
        }
        for &(ix, iy, ref name, col) in &legend_items {
            hdr.fill_ellipse(ix, iy + 4, ix + 12, iy + 16, [col[0], col[1], col[2], 255]);
            draw_text(
                &mut hdr,
                (ix + 18) as f32,
                iy as f32,
                name,
                FontWeight::Regular,
                20,
                TITLE_COL,
                0.75,
                true,
                false,
                SHADOW_BLUR,
                SHADOW_OFFSET,
            );
        }
        Some(hdr)
    } else {
        None
    };

    StaticScene {
        base,
        title: title.to_owned(),
        title_fs,
        legend_items,
        header_strip,
    }
}

#[inline]
fn note_y(note_time: f64, t_now: f64, lookahead: f64) -> f64 {
    let dt = note_time - t_now;
    let frac = dt / lookahead;
    NOTE_AREA_H as f64 * (1.0 - frac)
}

#[allow(clippy::too_many_arguments)]
fn render_frame(
    canvas: &mut Canvas,
    rgb_scratch: &mut Vec<u8>,
    tracks_indexed: &[IndexedTrack],
    track_colors: &[Color],
    geom: &HashMap<u8, KeyGeom>,
    scene: &StaticScene,
    lookahead: f64,
    frame_num: usize,
    transparent: bool,
    output_dir: &Path,
) -> Result<(), String> {
    let t = frame_num as f64 / FPS;
    canvas.copy_from(&scene.base);

    let mut active: HashMap<u8, (Color, u8, f64)> = HashMap::new();
    for (ti, tr) in tracks_indexed.iter().enumerate() {
        let col = track_colors[ti];
        for n in notes_visible(tr, t, lookahead) {
            if t >= n.start && t < n.end {
                let held = t - n.start;
                let replace = match active.get(&n.pitch) {
                    None => true,
                    Some(&(_, prev_vel, _)) => n.velocity > prev_vel,
                };
                if replace {
                    active.insert(n.pitch, (col, n.velocity, held));
                }
            }
        }
    }

    if !active.is_empty() {
        let mut black_to_redraw: HashSet<u8> = HashSet::new();
        for (&pitch, &(col, _, held)) in &active {
            let Some(g) = geom.get(&pitch) else { continue };
            let flash = ease_out(clamp(1.0 - held * 8.0, 0.0, 1.0));
            if !g.is_black {
                let key_fill = lerp_color(WK_COLOR, col, lerp(0.35, 0.70, flash));
                draw_white_key(canvas, pitch, g, key_fill);
                if let Some(lp) = pitch.checked_sub(1) {
                    if geom.get(&lp).is_some_and(|lg| lg.is_black) {
                        black_to_redraw.insert(lp);
                    }
                }
                if let Some(rp) = pitch.checked_add(1) {
                    if geom.get(&rp).is_some_and(|rg| rg.is_black) {
                        black_to_redraw.insert(rp);
                    }
                }
            } else {
                black_to_redraw.insert(pitch);
            }
        }

        let mut sorted_blacks: Vec<u8> = black_to_redraw.into_iter().collect();
        sorted_blacks.sort_unstable();
        for bp in sorted_blacks {
            let bg = &geom[&bp];
            let key_fill = if let Some(&(col, _, held)) = active.get(&bp) {
                let flash = ease_out(clamp(1.0 - held * 8.0, 0.0, 1.0));
                lerp_color(BK_COLOR, col, lerp(0.50, 0.90, flash))
            } else {
                BK_COLOR
            };
            draw_black_key(canvas, bg, key_fill);
        }
    }

    let mut glow_ellipses: Vec<(i32, i32, i32, Color)> = Vec::new();
    let mut max_glow_r = 0i32;

    for (ti, tr) in tracks_indexed.iter().enumerate() {
        let col = track_colors[ti];
        for n in notes_visible(tr, t, lookahead) {
            let Some(gm) = geom.get(&n.pitch) else { continue };
            let cx = gm.x + gm.w * 0.5;
            let bw = (gm.w - NOTE_GAP * 2.0).max(2.0);

            let y_bottom = note_y(n.start, t, lookahead).min((NOTE_AREA_H - 1) as f64);
            let y_top = note_y(n.end, t, lookahead).max(0.0);
            if y_bottom <= y_top {
                continue;
            }

            let block_h = (y_bottom - y_top).max(NOTE_MIN_H);
            let x0 = (cx - bw * 0.5) as i32;
            let x1 = ((cx + bw * 0.5) as i32).max(x0 + 1);
            let y0 = y_top as i32;
            let y1 = ((y_top + block_h) as i32).min(NOTE_AREA_H - 1);
            if y1 <= y0 {
                continue;
            }

            let is_active = t >= n.start && t < n.end;
            let v_bright = lerp(0.55, 1.0, n.velocity as f64 / 127.0);

            let (fill_col, hi_col) = if is_active {
                (
                    [
                        (col[0] as f64 * v_bright) as u8,
                        (col[1] as f64 * v_bright) as u8,
                        (col[2] as f64 * v_bright) as u8,
                    ],
                    lerp_color(col, [255, 255, 255], 0.5),
                )
            } else {
                let fc = [
                    (lerp(col[0] as f64, 180.0, 0.35) * v_bright) as u8,
                    (lerp(col[1] as f64, 180.0, 0.35) * v_bright) as u8,
                    (lerp(col[2] as f64, 180.0, 0.35) * v_bright) as u8,
                ];
                (fc, fc)
            };

            let body_a = if transparent { 220 } else { 255 };
            let hi_a = if transparent { 180 } else { 255 };
            canvas.fill_rounded_rect(
                x0,
                y0,
                x1,
                y1,
                NOTE_RADIUS,
                [fill_col[0], fill_col[1], fill_col[2], body_a],
            );
            if x1 - x0 > 2 && y1 - y0 > 2 {
                canvas.fill_rounded_rect(
                    x0 + 1,
                    y0 + 1,
                    x1 - 1,
                    (y0 + 6).min(y1 - 1),
                    NOTE_RADIUS,
                    [hi_col[0], hi_col[1], hi_col[2], hi_a],
                );
            }

            if is_active {
                let gr = ((bw * if transparent { 0.8 } else { 1.0 }) as i32).max(4);
                max_glow_r = max_glow_r.max(gr);
                glow_ellipses.push((cx as i32, y1, gr, col));
            }
        }
    }

    if !glow_ellipses.is_empty() {
        let pad = max_glow_r + (GLOW_BLUR as i32) * 2 + 4;
        let sy0 = (NOTE_AREA_H - pad).max(0);
        let sy1 = (NOTE_AREA_H + pad).min(HEIGHT as i32);
        let sh = sy1 - sy0;
        if sh > 0 {
            let mut glow_strip = Canvas::new(WIDTH, sh as u32, [0, 0, 0, 0]);
            for (gcx, gy, gr, gcol) in glow_ellipses {
                let ly = gy - sy0;
                glow_strip.fill_ellipse(
                    gcx - gr,
                    ly - gr,
                    gcx + gr,
                    ly + gr,
                    [gcol[0], gcol[1], gcol[2], GLOW_ALPHA],
                );
            }
            glow_strip.gaussian_blur(GLOW_BLUR);
            canvas.alpha_composite(&glow_strip, 0, sy0);
        }
    }

    if transparent {
        if let Some(ref hdr) = scene.header_strip {
            canvas.alpha_composite(hdr, 0, 0);
        }
    } else {
        if !scene.title.is_empty() {
            blit_glyphs_solid(
                canvas,
                50.0 + SHADOW_OFFSET as f32,
                28.0 + SHADOW_OFFSET as f32,
                &scene.title,
                FontWeight::Bold,
                scene.title_fs,
                [0, 0, 0, 255],
            );
            blit_glyphs_solid(
                canvas,
                50.0,
                28.0,
                &scene.title,
                FontWeight::Bold,
                scene.title_fs,
                [TITLE_COL[0], TITLE_COL[1], TITLE_COL[2], 255],
            );
        }
        for &(ix, iy, ref name, col) in &scene.legend_items {
            canvas.fill_ellipse(ix, iy + 4, ix + 12, iy + 16, [col[0], col[1], col[2], 255]);
            let dim_col = [
                (col[0] as f64 * 0.8) as u8,
                (col[1] as f64 * 0.8) as u8,
                (col[2] as f64 * 0.8) as u8,
                255,
            ];
            blit_glyphs_solid(
                canvas,
                (ix + 18) as f32,
                iy as f32,
                name,
                FontWeight::Regular,
                20,
                dim_col,
            );
        }
    }

    let frame_path = output_dir.join(format!("frame_{:06}.png", frame_num));
    canvas.save_png(&frame_path, transparent, rgb_scratch)
}

pub fn run(opts: MidiOptions<'_>) -> Result<(), String> {
    if opts.transparent && opts.greenscreen {
        return Err("--transparent and --greenscreen are mutually exclusive.".to_owned());
    }
    if opts.lookahead <= 0.0 {
        return Err("--lookahead must be positive.".to_owned());
    }
    if !opts.input.is_file() {
        return Err(format!("file not found: {}", opts.input.display()));
    }

    print!("\n  Loading {} …", opts.input.display());
    let _ = std::io::stdout().flush();
    let (tracks, names, auto_dur) = load_midi(opts.input)?;
    println!(" done.");

    if tracks.is_empty() {
        println!("  No note tracks found in MIDI file.");
        return Ok(());
    }

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

    let (pitch_lo, pitch_hi) = get_actual_range(&tracks);
    let geom = build_key_geometry(pitch_lo, pitch_hi);
    if geom.is_empty() {
        return Err("could not construct keyboard geometry.".to_owned());
    }

    let track_colors: Vec<Color> = (0..tracks.len())
        .map(|i| TRACK_PALETTE[i % TRACK_PALETTE.len()])
        .collect();
    let tracks_indexed = build_index(tracks);
    let workers = opts.workers.max(1);

    let mode_str = if opts.transparent {
        "transparent"
    } else if opts.greenscreen {
        "green screen"
    } else {
        "opaque"
    };

    println!("  ♪ {}", title);
    println!("  {} track(s): {}", names.len(), names.join(", "));
    println!("  pitch range: MIDI {}–{}", pitch_lo, pitch_hi);
    println!("  duration: {:.1}s · mode: {}", duration, mode_str);

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

    let scene = build_static_scene(
        &geom,
        pitch_lo,
        pitch_hi,
        title,
        &names,
        &track_colors,
        opts.transparent,
        opts.greenscreen,
    );

    let pool = build_pool(workers)?;
    let pb = ProgressBar::new(frames.len());

    let res: Result<(), String> = pool.install(|| {
        frames.par_iter().try_for_each_init(
            || (Canvas::new(WIDTH, HEIGHT, [0, 0, 0, 0]), Vec::<u8>::new()),
            |(canvas, rgb_scratch), &f| {
                render_frame(
                    canvas,
                    rgb_scratch,
                    &tracks_indexed,
                    &track_colors,
                    &geom,
                    &scene,
                    opts.lookahead,
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

    print_ffmpeg_hint(
        &opts.output_dir.to_string_lossy(),
        FPS as u32,
        opts.transparent,
        opts.greenscreen,
        "audio.mp3",
    );
    Ok(())
}
