use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::sync::OnceLock;

use rayon::prelude::*;
use regex::Regex;
use roxmltree::Document;

use crate::canvas::{
    clamp, ease_in_out, ease_out, lerp, lerp_color, Canvas, Color, BG_COLOR, FPS, GS_COLOR, HEIGHT,
    WIDTH,
};
use crate::lrc::{parse_lrc, read_text_file, Syllable};
use crate::progress::{build_pool, confirm_render, print_ffmpeg_hint, ProgressBar};
use crate::text::{
    draw_text, draw_wipe, line_metrics, text_width, FontWeight,
};

const MARGIN_LEFT: f32 = 120.0;
const MARGIN_RIGHT: f32 = 120.0;
const MARGIN_TOP: f64 = 100.0;
const LINE_H_IDLE: f64 = 58.0;
const LINE_H_ACTIVE: f64 = 150.0;
const LINE_GAP: f64 = 24.0;

const SIZE_IDLE: u32 = 36;
const SIZE_IDLE_MIN: u32 = 20;
const SIZE_ACTIVE: u32 = 96;
const SIZE_ACTIVE_MIN: u32 = 48;
const SIZE_TITLE: u32 = 80;
const SIZE_TITLE_MIN: u32 = 36;
const SIZE_WATERMARK: u32 = 22;
const SIZE_ROMAN_SCALE: f64 = 0.38;
const SIZE_ROMAN_IDLE: u32 = 22;

const TITLE_COLOR: Color = [180, 180, 190];
const WATERMARK_COL: Color = [60, 60, 70];
const IDLE_COLOR: Color = [70, 70, 80];
const DONE_COLOR: Color = [200, 200, 210];
const ACTIVE_BASE: Color = [130, 130, 145];
const ACTIVE_SUNG: Color = [255, 255, 255];
const ROMAN_COLOR: Color = [160, 160, 175];

const GROW_IN_DUR: f64 = 0.25;
const GROW_OUT_DUR: f64 = 0.30;
const WORD_FADE_DUR: f64 = 0.18;

const BREAK_THRESHOLD: f64 = 2.5;
const DOT_COUNT: usize = 3;
const DOT_RADIUS: f64 = 14.0;
const DOT_SPACING: f64 = 52.0;
const DOT_PULSE_PERIOD: f64 = 1.40;
const DOT_FADE_IN: f64 = 0.50;
const DOT_FADE_OUT: f64 = 0.50;
const DOT_LINE_H: f64 = 60.0;
const DOT_COLOR: Color = [200, 200, 210];
const DOT_DIM: Color = [55, 55, 65];

const SHADOW_OFFSET: i32 = 3;
const SHADOW_BLUR: u32 = 6;

pub struct LyricsOptions<'a> {
    pub input: &'a Path,
    pub duration: Option<f64>,
    pub start: usize,
    pub workers: usize,
    pub transparent: bool,
    pub greenscreen: bool,
    pub romanise: Option<&'a Path>,
    pub title: Option<&'a str>,
    pub preview: bool,
    pub yes: bool,
    pub output_dir: &'a Path,
}

#[derive(Clone, Debug)]
struct RomanEntry {
    syllables: Vec<Syllable>,
    synced: bool,
    full: String,
    idle_fs: u32,
    active_max_fs: u32,
}

#[derive(Clone, Debug)]
struct Phrase {
    begin: f64,
    end: f64,
    active_end: f64,
    syllables: Vec<Syllable>,
    synced: bool,
    full: String,
    idle_fs: u32,
    idle_h: f64,
    active_h: f64,
    roman: Option<RomanEntry>,
}

#[derive(Clone, Debug)]
struct Break {
    start: f64,
    end: f64,
    prev_idx: Option<usize>,
}

fn offset_time_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s|m|h|f)?\s*$").unwrap())
}

pub fn parse_ttml_time(ts: &str) -> f64 {
    let ts = ts.trim();
    if ts.is_empty() {
        return 0.0;
    }
    if !ts.contains(':') {
        if let Some(caps) = offset_time_re().captures(ts) {
            let val: f64 = caps[1].parse().unwrap_or(0.0);
            let unit = caps.get(2).map(|m| m.as_str()).unwrap_or("s");
            return match unit {
                "ms" => val / 1000.0,
                "s" => val,
                "m" => val * 60.0,
                "h" => val * 3600.0,
                "f" => val / FPS,
                _ => val,
            };
        }
    }

    let parts: Vec<&str> = ts.split(':').collect();
    match parts.len() {
        2 => {
            let m = parts[0].parse::<f64>().unwrap_or(0.0);
            let s = parts[1].parse::<f64>().unwrap_or(0.0);
            m * 60.0 + s
        }
        3 => {
            let h = parts[0].parse::<f64>().unwrap_or(0.0);
            let m = parts[1].parse::<f64>().unwrap_or(0.0);
            let s = parts[2].parse::<f64>().unwrap_or(0.0);
            h * 3600.0 + m * 60.0 + s
        }
        4 => {
            let h = parts[0].parse::<f64>().unwrap_or(0.0);
            let m = parts[1].parse::<f64>().unwrap_or(0.0);
            let s = parts[2].parse::<f64>().unwrap_or(0.0);
            let f = parts[3].parse::<f64>().unwrap_or(0.0);
            h * 3600.0 + m * 60.0 + s + f / FPS
        }
        _ => ts.parse::<f64>().unwrap_or(0.0),
    }
}

fn is_xml_text(text: &str, path: &Path) -> bool {
    let s = text.trim_start_matches('\u{feff}').trim_start();
    let slow = s.to_ascii_lowercase();
    if slow.starts_with("<?xml") || slow.starts_with("<tt") {
        return true;
    }
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    matches!(ext.as_str(), "ttml" | "xml") && s.starts_with('<')
}

fn collect_node_text(node: roxmltree::Node) -> String {
    let mut out = String::new();
    for desc in node.descendants() {
        if desc.is_text() {
            if let Some(t) = desc.text() {
                out.push_str(t);
            }
        }
    }
    out
}

fn load_ttml_text(text: &str) -> Result<(String, Vec<Phrase>, f64), String> {
    let clean = text.trim_start_matches('\u{feff}');
    let doc = Document::parse(clean).map_err(|e| format!("TTML XML parse error: {}", e))?;

    let mut title = String::new();
    let mut auto_dur: Option<f64> = None;

    for node in doc.descendants() {
        if !node.is_element() {
            continue;
        }
        let lname = node.tag_name().name();
        if lname == "title" && title.is_empty() {
            let t = collect_node_text(node);
            let trimmed = t.trim();
            if !trimmed.is_empty() {
                title = trimmed.to_owned();
            }
        }
        if matches!(lname, "tt" | "body" | "div") && auto_dur.is_none() {
            if let Some(d) = node.attribute("dur") {
                auto_dur = Some(parse_ttml_time(d));
            } else if let Some(e) = node.attribute("end") {
                auto_dur = Some(parse_ttml_time(e));
            }
        }
    }

    let p_nodes: Vec<_> = doc
        .descendants()
        .filter(|n| n.is_element() && n.tag_name().name() == "p" && n.attribute("begin").is_some())
        .collect();

    let mut raw_p = Vec::with_capacity(p_nodes.len());
    for p in &p_nodes {
        let b = parse_ttml_time(p.attribute("begin").unwrap());
        let e = if let Some(ea) = p.attribute("end") {
            Some(parse_ttml_time(ea))
        } else {
            p.attribute("dur").map(|da| b + parse_ttml_time(da))
        };
        raw_p.push((b, e, *p));
    }

    let mut phrases = Vec::new();
    for (idx, &(begin, end_opt, p)) in raw_p.iter().enumerate() {
        let end = end_opt
            .unwrap_or_else(|| {
                if idx + 1 < raw_p.len() {
                    raw_p[idx + 1].0
                } else {
                    begin + 5.0
                }
            })
            .max(begin + 0.05);

        let span_els: Vec<_> = p
            .children()
            .filter(|ch| ch.is_element() && ch.tag_name().name() == "span")
            .collect();

        let (syls, synced) = if span_els.is_empty() {
            let full_txt = collect_node_text(p);
            let trimmed = full_txt.trim();
            if trimmed.is_empty() {
                (Vec::new(), false)
            } else {
                (
                    vec![Syllable {
                        text: trimmed.to_owned(),
                        begin,
                        end,
                    }],
                    false,
                )
            }
        } else {
            let first_sb = span_els[0]
                .attribute("begin")
                .map(parse_ttml_time)
                .unwrap_or(begin);
            let mut syls: Vec<Syllable> = Vec::new();
            let mut span_idx = 0usize;

            for child in p.children() {
                if child.is_text() {
                    let t = child.text().unwrap_or("");
                    if syls.is_empty() {
                        // Leading text before the first <span>
                        if !t.trim().is_empty() {
                            let lead = t.trim_start_matches(['\r', '\n', '\t', ' ']);
                            if !lead.is_empty() {
                                syls.push(Syllable {
                                    text: lead.to_owned(),
                                    begin,
                                    end: begin.max(first_sb),
                                });
                            }
                        }
                    } else if !t.contains('\n') && !t.contains('\r') && !t.is_empty() {
                        // Inline space between </span> and <span>
                        if let Some(last) = syls.last_mut() {
                            last.text.push_str(t);
                        }
                    }
                } else if child.is_element() && child.tag_name().name() == "span" {
                    let sb = child
                        .attribute("begin")
                        .map(parse_ttml_time)
                        .unwrap_or(begin);
                    let se = if let Some(ea) = child.attribute("end") {
                        parse_ttml_time(ea)
                    } else if let Some(da) = child.attribute("dur") {
                        sb + parse_ttml_time(da)
                    } else if span_idx + 1 < span_els.len() {
                        span_els[span_idx + 1]
                            .attribute("begin")
                            .map(parse_ttml_time)
                            .unwrap_or(end)
                    } else {
                        end
                    }
                    .max(sb);

                    let raw_txt = collect_node_text(child);
                    if !raw_txt.trim().is_empty() {
                        let clean_txt = raw_txt.replace(['\r', '\n'], "");
                        syls.push(Syllable {
                            text: clean_txt,
                            begin: sb,
                            end: se,
                        });
                    } else if !raw_txt.is_empty() && !raw_txt.contains('\n') && !syls.is_empty() {
                        if let Some(last) = syls.last_mut() {
                            last.text.push_str(&raw_txt);
                        }
                    }
                    span_idx += 1;
                }
            }

            if syls.is_empty() {
                let fallback = collect_node_text(p);
                let trimmed = fallback.trim();
                if trimmed.is_empty() {
                    (Vec::new(), false)
                } else {
                    (
                        vec![Syllable {
                            text: trimmed.to_owned(),
                            begin,
                            end,
                        }],
                        false,
                    )
                }
            } else {
                (syls, true)
            }
        };

        if !syls.is_empty() {
            phrases.push(Phrase {
                begin,
                end,
                active_end: end,
                syllables: syls,
                synced,
                full: String::new(),
                idle_fs: SIZE_IDLE,
                idle_h: LINE_H_IDLE,
                active_h: LINE_H_ACTIVE,
                roman: None,
            });
        }
    }

    phrases.sort_by(|a, b| a.begin.partial_cmp(&b.begin).unwrap_or(std::cmp::Ordering::Equal));
    let dur = if let Some(last) = phrases.last() {
        let inferred = last.end + 2.0;
        auto_dur.map(|d| d.max(inferred)).unwrap_or(inferred)
    } else {
        auto_dur.unwrap_or(0.0)
    };

    Ok((title, phrases, dur))
}

fn load_lrc_phrases(text: &str) -> (String, Vec<Phrase>, f64, bool, String) {
    let doc = parse_lrc(text);
    let title = doc.meta.get("ti").cloned().unwrap_or_default();
    let mut phrases = Vec::with_capacity(doc.lines.len());
    let mut has_inline_sub = false;

    for line in doc.lines {
        let roman = if !line.sub_words.is_empty() {
            has_inline_sub = true;
            let full: String = line.sub_words.iter().map(|w| w.text.as_str()).collect();
            Some(RomanEntry {
                syllables: line.sub_words,
                synced: line.sub_synced,
                full,
                idle_fs: SIZE_ROMAN_IDLE,
                active_max_fs: 24,
            })
        } else {
            None
        };

        phrases.push(Phrase {
            begin: line.begin,
            end: line.end,
            active_end: line.end,
            syllables: line.words,
            synced: line.synced,
            full: String::new(),
            idle_fs: SIZE_IDLE,
            idle_h: LINE_H_IDLE,
            active_h: LINE_H_ACTIVE,
            roman,
        });
    }

    let fmt = if phrases.iter().any(|p| p.synced) {
        "Enhanced LRC".to_owned()
    } else {
        "LRC".to_owned()
    };

    let auto_dur = if let Some(last) = phrases.last() {
        let inferred = last.end + 2.0;
        doc.duration.map(|d| d.max(inferred)).unwrap_or(inferred)
    } else {
        doc.duration.unwrap_or(0.0)
    };

    (title, phrases, auto_dur, has_inline_sub, fmt)
}

fn load_lyrics_file(path: &Path) -> Result<(String, Vec<Phrase>, f64, bool, String), String> {
    let text = read_text_file(path)?;
    if is_xml_text(&text, path) {
        let (title, phrases, dur) = load_ttml_text(&text)?;
        Ok((title, phrases, dur, false, "TTML".to_owned()))
    } else {
        Ok(load_lrc_phrases(&text))
    }
}

fn apply_external_roman(phrases: &mut [Phrase], roman_path: &Path) -> Result<usize, String> {
    let (_, r_phrases, _, _, _) = load_lyrics_file(roman_path)?;
    let mut keys: Vec<(f64, Vec<Syllable>, bool)> = r_phrases
        .into_iter()
        .map(|rp| (rp.begin, rp.syllables, rp.synced))
        .collect();
    keys.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));

    let mut matched = 0usize;
    for p in phrases.iter_mut() {
        let pb = p.begin;
        let idx = keys.partition_point(|k| k.0 < pb);
        let mut best: Option<usize> = None;
        let mut best_diff = 0.15f64;
        for cand in [idx.wrapping_sub(1), idx] {
            if cand < keys.len() {
                let diff = (keys[cand].0 - pb).abs();
                if diff <= best_diff {
                    best_diff = diff;
                    best = Some(cand);
                }
            }
        }
        if let Some(bi) = best {
            let syls = keys[bi].1.clone();
            let synced = keys[bi].2;
            let full: String = syls.iter().map(|s| s.text.as_str()).collect();
            p.roman = Some(RomanEntry {
                syllables: syls,
                synced,
                full,
                idle_fs: SIZE_ROMAN_IDLE,
                active_max_fs: 24,
            });
            matched += 1;
        } else {
            p.roman = None;
        }
    }
    Ok(matched)
}

fn split_oversized_syllable(syl: &Syllable, size_px: u32, max_w: f32, synced: bool) -> Vec<Syllable> {
    let txt = &syl.text;
    let total_chars = txt.chars().count().max(1);

    let tokens: Vec<String> = if txt.contains(' ') || txt.contains('\u{3000}') {
        let re = Regex::new(r"\S+\s*|\s+").unwrap();
        re.find_iter(txt).map(|m| m.as_str().to_owned()).collect()
    } else {
        txt.chars().map(|c| c.to_string()).collect()
    };

    let mut pieces: Vec<String> = Vec::new();
    let mut cur = String::new();
    for tok in tokens {
        let cand = format!("{}{}", cur, tok);
        if !cur.is_empty() && text_width(&cand, FontWeight::Bold, size_px) > max_w {
            pieces.push(cur);
            cur = String::new();
            if text_width(&tok, FontWeight::Bold, size_px) > max_w {
                for ch in tok.chars() {
                    let c2 = format!("{}{}", cur, ch);
                    if !cur.is_empty() && text_width(&c2, FontWeight::Bold, size_px) > max_w {
                        pieces.push(cur);
                        cur = ch.to_string();
                    } else {
                        cur.push(ch);
                    }
                }
            } else {
                cur = tok;
            }
        } else {
            cur = cand;
        }
    }
    if !cur.is_empty() {
        pieces.push(cur);
    }
    if pieces.len() <= 1 {
        return vec![syl.clone()];
    }

    let mut out = Vec::with_capacity(pieces.len());
    let mut pos = 0usize;
    let dur = syl.end - syl.begin;
    for piece in pieces {
        let plen = piece.chars().count();
        let (pb, pe) = if synced {
            (
                syl.begin + dur * (pos as f64 / total_chars as f64),
                syl.begin + dur * ((pos + plen) as f64 / total_chars as f64),
            )
        } else {
            (syl.begin, syl.end)
        };
        out.push(Syllable {
            text: piece,
            begin: pb,
            end: pe,
        });
        pos += plen;
    }
    out
}

fn wrap_syllables(syls: &[Syllable], size_px: u32, synced: bool) -> Vec<Vec<Syllable>> {
    let max_w = WIDTH as f32 - MARGIN_LEFT - MARGIN_RIGHT;
    let mut rows: Vec<Vec<Syllable>> = Vec::new();
    let mut row: Vec<Syllable> = Vec::new();
    let mut w = 0.0f32;

    for syl in syls {
        let sw = text_width(&syl.text, FontWeight::Bold, size_px);
        if sw > max_w {
            for ss in split_oversized_syllable(syl, size_px, max_w, synced) {
                let ssw = text_width(&ss.text, FontWeight::Bold, size_px);
                if !row.is_empty() && w + ssw > max_w {
                    rows.push(std::mem::take(&mut row));
                    w = 0.0;
                }
                row.push(ss);
                w += ssw;
            }
            continue;
        }
        if !row.is_empty() && w + sw > max_w {
            rows.push(std::mem::take(&mut row));
            w = 0.0;
        }
        row.push(syl.clone());
        w += sw;
    }
    if !row.is_empty() {
        rows.push(row);
    }
    rows
}

fn fit_active_layout(p: &Phrase, target_fs: u32) -> (u32, Vec<Vec<Syllable>>, f32) {
    let max_w = WIDTH as f32 - MARGIN_LEFT - MARGIN_RIGHT;
    let mut fs = target_fs;
    let mut needs_wrap = true;
    while fs >= SIZE_ACTIVE_MIN {
        let w: f32 = p
            .syllables
            .iter()
            .map(|s| text_width(&s.text, FontWeight::Bold, fs))
            .sum();
        if w <= max_w {
            needs_wrap = false;
            break;
        }
        fs = fs.saturating_sub(2);
    }
    let fs_actual = if needs_wrap { SIZE_ACTIVE_MIN } else { fs };
    let lm = line_metrics(FontWeight::Bold, fs_actual);
    let rows = if needs_wrap {
        wrap_syllables(&p.syllables, fs_actual, p.synced)
    } else {
        vec![p.syllables.clone()]
    };
    (fs_actual, rows, lm.line_height)
}

fn prepare_phrases(phrases: &mut [Phrase]) {
    let max_w = WIDTH as f32 - MARGIN_LEFT - MARGIN_RIGHT;
    let n = phrases.len();
    let next_begins: Vec<Option<f64>> = (0..n)
        .map(|i| phrases.get(i + 1).map(|np| np.begin))
        .collect();

    for (i, p) in phrases.iter_mut().enumerate() {
        p.full = p.syllables.iter().map(|s| s.text.as_str()).collect();
        if let Some(nb) = next_begins[i] {
            if nb - p.end < BREAK_THRESHOLD {
                p.active_end = p.end.max(nb);
            } else {
                p.active_end = p.end;
            }
        } else {
            p.active_end = p.end;
        }

        let mut idle_fs = SIZE_IDLE;
        while idle_fs > SIZE_IDLE_MIN {
            if text_width(&p.full, FontWeight::Regular, idle_fs) <= max_w {
                break;
            }
            idle_fs -= 2;
        }
        p.idle_fs = idle_fs;

        if let Some(ref mut rent) = p.roman {
            let mut r_idle = SIZE_ROMAN_IDLE;
            while r_idle > 14 {
                if text_width(&rent.full, FontWeight::Regular, r_idle) <= max_w {
                    break;
                }
                r_idle -= 1;
            }
            rent.idle_fs = r_idle;
        }

        let (fs_act, rows_act, lh_act) = fit_active_layout(p, SIZE_ACTIVE);
        let n_rows = rows_act.len();
        let mut needed_active_h =
            (n_rows as f64) * (lh_act as f64) + (n_rows.saturating_sub(1) as f64) * 8.0 + 30.0;
        let mut needed_idle_h = LINE_H_IDLE;

        if let Some(ref mut rent) = p.roman {
            let mut r_target = ((fs_act as f64 * SIZE_ROMAN_SCALE) as u32).max(18);
            while r_target > 16 {
                if text_width(&rent.full, FontWeight::Bold, r_target) <= max_w {
                    break;
                }
                r_target -= 2;
            }
            rent.active_max_fs = r_target;
            needed_active_h += r_target as f64 + 22.0;
            needed_idle_h += rent.idle_fs as f64 + 10.0;
        }

        p.idle_h = LINE_H_IDLE.max(needed_idle_h);
        p.active_h = LINE_H_ACTIVE.max(needed_active_h);
    }
}

fn detect_breaks(phrases: &[Phrase], duration: f64) -> Vec<Break> {
    let mut breaks = Vec::new();
    if let Some(first) = phrases.first() {
        if first.begin >= BREAK_THRESHOLD {
            breaks.push(Break {
                start: 0.0,
                end: first.begin,
                prev_idx: None,
            });
        }
    }
    for i in 0..phrases.len().saturating_sub(1) {
        let gs = phrases[i].end;
        let ge = phrases[i + 1].begin;
        if ge - gs >= BREAK_THRESHOLD {
            breaks.push(Break {
                start: gs,
                end: ge,
                prev_idx: Some(i),
            });
        }
    }
    if let Some(last) = phrases.last() {
        if duration - last.end >= BREAK_THRESHOLD {
            breaks.push(Break {
                start: last.end,
                end: duration,
                prev_idx: Some(phrases.len() - 1),
            });
        }
    }
    breaks
}

fn in_break(breaks: &[Break], t: f64) -> Option<&Break> {
    if breaks.is_empty() {
        return None;
    }
    let idx = breaks.partition_point(|b| b.start <= t);
    if idx > 0 {
        let b = &breaks[idx - 1];
        if t >= b.start && t < b.end {
            return Some(b);
        }
    }
    None
}

fn find_active(phrases: &[Phrase], t: f64) -> (isize, bool) {
    if phrases.is_empty() {
        return (-1, false);
    }
    let idx = phrases.partition_point(|p| p.begin <= t);
    if idx == 0 {
        return (-1, false);
    }
    let i = idx - 1;
    (i as isize, t < phrases[i].active_end)
}

#[derive(Copy, Clone, PartialEq, Eq)]
enum PhraseState {
    Active,
    Past,
    Idle,
}

enum Slot {
    Dots,
    PhraseSlot {
        idx: usize,
        state: PhraseState,
        fs: u32,
        shrinking: bool,
    },
}

fn phrase_visual_state(
    p: &Phrase,
    i: usize,
    last_started_idx: isize,
    is_currently_active: bool,
    t: f64,
) -> (PhraseState, f64, u32, bool) {
    if is_currently_active && (i as isize) == last_started_idx {
        let grow_t = ease_out(clamp((t - p.begin) / GROW_IN_DUR, 0.0, 1.0));
        let h = lerp(p.idle_h, p.active_h, grow_t);
        let fs = lerp(p.idle_fs as f64, SIZE_ACTIVE as f64, grow_t) as u32;
        return (PhraseState::Active, h, fs, false);
    }
    if (i as isize) <= last_started_idx {
        let elapsed = t - p.active_end;
        if (0.0..GROW_OUT_DUR).contains(&elapsed) {
            let shrink_t = ease_in_out(clamp(elapsed / GROW_OUT_DUR, 0.0, 1.0));
            let h = lerp(p.active_h, p.idle_h, shrink_t);
            let fs = lerp(SIZE_ACTIVE as f64, p.idle_fs as f64, shrink_t) as u32;
            return (PhraseState::Past, h, fs, true);
        }
        return (PhraseState::Past, p.idle_h, p.idle_fs, false);
    }
    (PhraseState::Idle, p.idle_h, p.idle_fs, false)
}

fn dot_slot_height(brk: Option<&Break>, t: f64) -> f64 {
    let Some(b) = brk else { return 0.0 };
    let elapsed = t - b.start;
    let remain = b.end - t;
    let fade_in = ease_out(clamp(elapsed / DOT_FADE_IN, 0.0, 1.0));
    let fade_out = ease_out(clamp(remain / DOT_FADE_OUT, 0.0, 1.0));
    DOT_LINE_H * fade_in * fade_out
}

fn build_layout(
    phrases: &[Phrase],
    last_started_idx: isize,
    is_currently_active: bool,
    brk: Option<&Break>,
    t: f64,
) -> (Vec<Slot>, Vec<f64>, Vec<f64>) {
    let prev_phrase_idx = brk.and_then(|b| b.prev_idx);
    let dot_h = dot_slot_height(brk, t);

    let mut slots = Vec::with_capacity(phrases.len() + 1);
    let mut heights = Vec::with_capacity(phrases.len() + 1);

    if brk.is_some() && prev_phrase_idx.is_none() && dot_h > 0.0 {
        slots.push(Slot::Dots);
        heights.push(dot_h + LINE_GAP);
    }

    for (i, p) in phrases.iter().enumerate() {
        let (state, lh, fs, shrinking) =
            phrase_visual_state(p, i, last_started_idx, is_currently_active, t);
        slots.push(Slot::PhraseSlot {
            idx: i,
            state,
            fs,
            shrinking,
        });
        heights.push(lh + LINE_GAP);

        if brk.is_some() && prev_phrase_idx == Some(i) && dot_h > 0.0 {
            slots.push(Slot::Dots);
            heights.push(dot_h + LINE_GAP);
        }
    }

    let mut cumulative = Vec::with_capacity(heights.len().max(1));
    cumulative.push(MARGIN_TOP);
    for h in heights.iter().take(heights.len().saturating_sub(1)) {
        let prev = *cumulative.last().unwrap();
        cumulative.push(prev + h);
    }

    (slots, heights, cumulative)
}

#[derive(Clone)]
struct FrameOffsets {
    lines: Vec<f64>,
    dots: f64,
}

fn precompute_offsets(phrases: &[Phrase], breaks: &[Break], total_frames: usize) -> Vec<FrameOffsets> {
    let anchor_y = HEIGHT as f64 * 0.40;
    let n = phrases.len();
    if n == 0 {
        return vec![
            FrameOffsets {
                lines: Vec::new(),
                dots: 0.0
            };
            total_frames
        ];
    }

    let tau_below = 0.10;
    let tau_past = GROW_OUT_DUR * 0.9;
    let k_below = 1.0 - (-1.0 / (FPS * tau_below)).exp();
    let k_past = 1.0 - (-1.0 / (FPS * tau_past)).exp();
    let k_settled = 1.0 - (-1.0 / (FPS * (tau_below * 0.5))).exp();

    let mut springs: Option<Vec<f64>> = None;
    let mut dot_spring = 0.0f64;
    let mut offsets = Vec::with_capacity(total_frames);

    for frame_num in 0..total_frames {
        let t = frame_num as f64 / FPS;
        let (last_idx, is_act) = find_active(phrases, t);
        let brk = in_break(breaks, t);
        let (slots, heights, cumulative) = build_layout(phrases, last_idx, is_act, brk, t);

        let mut target = 0.0;
        let mut found = false;

        if is_act {
            for (si, slot) in slots.iter().enumerate() {
                if let Slot::PhraseSlot { idx, .. } = *slot {
                    if idx as isize == last_idx {
                        target = anchor_y - (cumulative[si] + heights[si] * 0.5);
                        found = true;
                        break;
                    }
                }
            }
        } else if brk.is_some() {
            for (si, slot) in slots.iter().enumerate() {
                if matches!(slot, Slot::Dots) {
                    target = anchor_y - (cumulative[si] + heights[si] * 0.5);
                    found = true;
                    break;
                }
            }
        }

        if !found {
            let focus_idx = last_idx.max(0) as usize;
            for (si, slot) in slots.iter().enumerate() {
                if let Slot::PhraseSlot { idx, .. } = *slot {
                    if idx == focus_idx {
                        target = anchor_y - (cumulative[si] + heights[si] * 0.5);
                        break;
                    }
                }
            }
        }

        let sp = springs.get_or_insert_with(|| {
            dot_spring = target;
            vec![target; n]
        });

        dot_spring += (target - dot_spring) * k_settled;
        for (i, p) in phrases.iter().enumerate() {
            if is_act && (i as isize) == last_idx {
                sp[i] = target;
            } else if (i as isize) > last_idx {
                sp[i] += (target - sp[i]) * k_below;
            } else {
                let elapsed = t - p.active_end;
                let k = if (0.0..GROW_OUT_DUR).contains(&elapsed) {
                    k_past
                } else {
                    k_settled
                };
                sp[i] += (target - sp[i]) * k;
            }
        }

        offsets.push(FrameOffsets {
            lines: sp.clone(),
            dots: dot_spring,
        });
    }

    offsets
}

struct ChromeOverlay {
    transparent: bool,
    greenscreen: bool,
    title: String,
    title_fs: u32,
    wm_x: f32,
    wm_y: f32,
    strips: Vec<(Canvas, i32, i32)>,
}

fn build_chrome(title: &str, transparent: bool, greenscreen: bool) -> ChromeOverlay {
    let max_title_w = WIDTH as f32 - MARGIN_LEFT - MARGIN_RIGHT;
    let mut title_fs = SIZE_TITLE;
    if !title.is_empty() {
        while title_fs >= SIZE_TITLE_MIN {
            if text_width(title, FontWeight::Bold, title_fs) <= max_title_w {
                break;
            }
            title_fs -= 4;
        }
    }

    let wm = "made with Nuisance";
    let wm_w = text_width(wm, FontWeight::Regular, SIZE_WATERMARK);
    let wm_lm = line_metrics(FontWeight::Regular, SIZE_WATERMARK);
    let wm_x = WIDTH as f32 - wm_w - 40.0;
    let wm_y = HEIGHT as f32 - wm_lm.line_height - 30.0;

    let mut strips = Vec::new();
    if transparent {
        if !title.is_empty() {
            let mut t_strip = Canvas::new(WIDTH, 160, [0, 0, 0, 0]);
            draw_text(
                &mut t_strip,
                MARGIN_LEFT,
                28.0,
                title,
                FontWeight::Bold,
                title_fs,
                TITLE_COLOR,
                1.0,
                true,
                false,
                10,
                4,
            );
            strips.push((t_strip, 0, 0));
        }
        let sx = (wm_x as i32 - 20).max(0);
        let sy = (wm_y as i32 - 20).max(0);
        let sw = (WIDTH as i32 - sx).max(1) as u32;
        let sh = (HEIGHT as i32 - sy).max(1) as u32;
        let mut b_strip = Canvas::new(sw, sh, [0, 0, 0, 0]);
        draw_text(
            &mut b_strip,
            wm_x - sx as f32,
            wm_y - sy as f32,
            wm,
            FontWeight::Regular,
            SIZE_WATERMARK,
            WATERMARK_COL,
            0.6,
            true,
            false,
            3,
            2,
        );
        strips.push((b_strip, sx, sy));
    }

    ChromeOverlay {
        transparent,
        greenscreen,
        title: title.to_owned(),
        title_fs,
        wm_x,
        wm_y,
        strips,
    }
}

fn apply_chrome(canvas: &mut Canvas, chrome: &ChromeOverlay) {
    if chrome.transparent {
        for (strip, sx, sy) in &chrome.strips {
            canvas.alpha_composite(strip, *sx, *sy);
        }
    } else {
        if !chrome.title.is_empty() {
            draw_text(
                canvas,
                MARGIN_LEFT,
                28.0,
                &chrome.title,
                FontWeight::Bold,
                chrome.title_fs,
                TITLE_COLOR,
                1.0,
                false,
                chrome.greenscreen,
                10,
                4,
            );
        }
        draw_text(
            canvas,
            chrome.wm_x,
            chrome.wm_y,
            "made with Nuisance",
            FontWeight::Regular,
            SIZE_WATERMARK,
            WATERMARK_COL,
            0.6,
            false,
            chrome.greenscreen,
            3,
            2,
        );
    }
}

fn draw_dots(canvas: &mut Canvas, cy: f64, brk: &Break, t: f64, transparent: bool, greenscreen: bool) {
    let elapsed = t - brk.start;
    let remain = brk.end - t;
    let fade_in = ease_out(clamp(elapsed / DOT_FADE_IN, 0.0, 1.0));
    let fade_out = ease_out(clamp(remain / DOT_FADE_OUT, 0.0, 1.0));
    let group_alpha = fade_in * fade_out;
    if group_alpha <= 0.0 {
        return;
    }

    let peak_pos = (elapsed.rem_euclid(DOT_PULSE_PERIOD)) / DOT_PULSE_PERIOD * (DOT_COUNT as f64);
    let cx_start = MARGIN_LEFT as f64 + 18.0;

    for d in 0..DOT_COUNT {
        let mut dist = (peak_pos - d as f64).abs();
        dist = dist.min(DOT_COUNT as f64 - dist);
        let pulse = ease_in_out(clamp(1.0 - dist / 0.9, 0.0, 1.0));

        let dot_alpha = group_alpha * lerp(0.15, 1.0, pulse);
        let cx = (cx_start + d as f64 * DOT_SPACING).round() as i32;
        let cy_i = cy.round() as i32;
        let r = ((DOT_RADIUS * lerp(0.55, 1.0, pulse)) as i32).max(1);
        let col = lerp_color(DOT_DIM, DOT_COLOR, pulse);

        let rgba = if transparent {
            [col[0], col[1], col[2], (255.0 * dot_alpha) as u8]
        } else if greenscreen {
            [
                (col[0] as f64 * dot_alpha) as u8,
                (col[1] as f64 * dot_alpha) as u8,
                (col[2] as f64 * dot_alpha) as u8,
                255,
            ]
        } else {
            let c = lerp_color(BG_COLOR, col, dot_alpha);
            [c[0], c[1], c[2], 255]
        };

        canvas.fill_ellipse(cx - r, cy_i - r, cx + r, cy_i + r, rgba);
    }
}

#[allow(clippy::too_many_arguments)]
fn render_frame(
    canvas: &mut Canvas,
    rgb_scratch: &mut Vec<u8>,
    layout_cache: &mut HashMap<(usize, u32), (u32, Vec<Vec<Syllable>>, f32)>,
    phrases: &[Phrase],
    breaks: &[Break],
    chrome: &ChromeOverlay,
    frame_num: usize,
    frame_offsets: &FrameOffsets,
    transparent: bool,
    greenscreen: bool,
    output_dir: &Path,
) -> Result<(), String> {
    let t = frame_num as f64 / FPS;

    let bg = if transparent {
        [0, 0, 0, 0]
    } else if greenscreen {
        [GS_COLOR[0], GS_COLOR[1], GS_COLOR[2], 255]
    } else {
        [BG_COLOR[0], BG_COLOR[1], BG_COLOR[2], 255]
    };
    canvas.fill(bg);

    let (last_idx, is_act) = find_active(phrases, t);
    let brk = in_break(breaks, t);
    let (slots, heights, cumulative) = build_layout(phrases, last_idx, is_act, brk, t);
    let focus_idx = last_idx.max(0) as usize;

    for (si, slot) in slots.iter().enumerate() {
        let offset = match *slot {
            Slot::Dots => frame_offsets.dots,
            Slot::PhraseSlot { idx, .. } => frame_offsets.lines.get(idx).copied().unwrap_or(0.0),
        };
        let y = cumulative[si] + offset;
        let h = heights[si];

        if y + h < -30.0 || y > HEIGHT as f64 + 30.0 {
            continue;
        }

        match *slot {
            Slot::Dots => {
                if let Some(b) = brk {
                    draw_dots(canvas, y + h * 0.18, b, t, transparent, greenscreen);
                }
            }
            Slot::PhraseSlot {
                idx: i,
                state,
                fs,
                shrinking,
            } => {
                let p = &phrases[i];
                let dist = (i as isize - focus_idx as isize).unsigned_abs() as f64;
                let alpha = match state {
                    PhraseState::Past => lerp(1.0, 0.15, clamp((dist - 1.0) / 5.0, 0.0, 1.0)),
                    PhraseState::Idle => lerp(0.85, 0.10, clamp((dist - 1.0) / 6.0, 0.0, 1.0)),
                    PhraseState::Active => 1.0,
                };

                if state == PhraseState::Active {
                    let (fs_actual, rows, line_h) = layout_cache
                        .entry((i, fs))
                        .or_insert_with(|| fit_active_layout(p, fs))
                        .clone();

                    let mut row_y = y as f32;
                    for row in &rows {
                        let mut x = MARGIN_LEFT;
                        for syl in row {
                            let sw = text_width(&syl.text, FontWeight::Bold, fs_actual);
                            if p.synced {
                                let wipe_frac = if t < syl.begin {
                                    0.0
                                } else if t <= syl.end {
                                    (t - syl.begin) / (syl.end - syl.begin).max(0.01)
                                } else {
                                    1.0
                                };
                                draw_wipe(
                                    canvas,
                                    x,
                                    row_y,
                                    &syl.text,
                                    FontWeight::Bold,
                                    fs_actual,
                                    ACTIVE_BASE,
                                    ACTIVE_SUNG,
                                    wipe_frac,
                                    alpha,
                                    transparent,
                                    greenscreen,
                                    SHADOW_BLUR,
                                    SHADOW_OFFSET,
                                );
                            } else {
                                let wf = ease_out(clamp((t - syl.begin) / WORD_FADE_DUR, 0.0, 1.0));
                                let col = if t < syl.begin {
                                    ACTIVE_BASE
                                } else if t <= syl.end {
                                    lerp_color(ACTIVE_BASE, ACTIVE_SUNG, wf)
                                } else {
                                    DONE_COLOR
                                };
                                draw_text(
                                    canvas,
                                    x,
                                    row_y,
                                    &syl.text,
                                    FontWeight::Bold,
                                    fs_actual,
                                    col,
                                    alpha,
                                    transparent,
                                    greenscreen,
                                    SHADOW_BLUR,
                                    SHADOW_OFFSET,
                                );
                            }
                            x += sw;
                        }
                        row_y += line_h + 8.0;
                    }

                    if let Some(ref rent) = p.roman {
                        let scaled = ((fs_actual as f64 * SIZE_ROMAN_SCALE) as u32).max(16);
                        let roman_fs = scaled.min(rent.active_max_fs);
                        let roman_y = row_y + 6.0;
                        let mut rx = MARGIN_LEFT + 6.0;

                        if rent.synced {
                            for rsyl in &rent.syllables {
                                let rsw = text_width(&rsyl.text, FontWeight::Bold, roman_fs);
                                let wipe_frac = if t < rsyl.begin {
                                    0.0
                                } else if t <= rsyl.end {
                                    (t - rsyl.begin) / (rsyl.end - rsyl.begin).max(0.01)
                                } else {
                                    1.0
                                };
                                draw_wipe(
                                    canvas,
                                    rx,
                                    roman_y,
                                    &rsyl.text,
                                    FontWeight::Bold,
                                    roman_fs,
                                    ROMAN_COLOR,
                                    ACTIVE_SUNG,
                                    wipe_frac,
                                    alpha,
                                    transparent,
                                    greenscreen,
                                    SHADOW_BLUR,
                                    SHADOW_OFFSET,
                                );
                                rx += rsw;
                            }
                        } else {
                            draw_text(
                                canvas,
                                rx,
                                roman_y,
                                &rent.full,
                                FontWeight::Bold,
                                roman_fs,
                                ACTIVE_SUNG,
                                alpha,
                                transparent,
                                greenscreen,
                                4,
                                2,
                            );
                        }
                    }
                } else {
                    let weight = if shrinking {
                        FontWeight::Bold
                    } else {
                        FontWeight::Regular
                    };
                    let lm = line_metrics(weight, fs);
                    let glyph_h = lm.line_height as f64;
                    let slot_inner = h - LINE_GAP;
                    let draw_y = if let Some(ref rent) = p.roman {
                        let group_h = glyph_h + 8.0 + rent.idle_fs as f64;
                        y + (slot_inner - group_h) * 0.5
                    } else {
                        y + (slot_inner - glyph_h) * 0.5
                    };

                    let base_col = if state == PhraseState::Past {
                        DONE_COLOR
                    } else {
                        IDLE_COLOR
                    };

                    draw_text(
                        canvas,
                        MARGIN_LEFT,
                        draw_y as f32,
                        &p.full,
                        weight,
                        fs,
                        base_col,
                        alpha,
                        transparent,
                        greenscreen,
                        SHADOW_BLUR,
                        SHADOW_OFFSET,
                    );

                    if let Some(ref rent) = p.roman {
                        let roman_y = (draw_y + glyph_h + 8.0) as f32;
                        let roman_col = if state == PhraseState::Idle {
                            IDLE_COLOR
                        } else {
                            DONE_COLOR
                        };
                        draw_text(
                            canvas,
                            MARGIN_LEFT + 6.0,
                            roman_y,
                            &rent.full,
                            FontWeight::Regular,
                            rent.idle_fs,
                            roman_col,
                            alpha * 0.75,
                            transparent,
                            greenscreen,
                            3,
                            2,
                        );
                    }
                }
            }
        }
    }

    apply_chrome(canvas, chrome);

    let frame_path = output_dir.join(format!("frame_{:06}.png", frame_num));
    canvas.save_png(&frame_path, transparent, rgb_scratch)
}

pub fn run(opts: LyricsOptions<'_>) -> Result<(), String> {
    if opts.transparent && opts.greenscreen {
        return Err("--transparent and --greenscreen are mutually exclusive.".to_owned());
    }
    if !opts.input.is_file() {
        return Err(format!("file not found: {}", opts.input.display()));
    }

    let (file_title, mut phrases, auto_dur, has_inline_sub, fmt_label) =
        load_lyrics_file(opts.input)?;
    let title = opts
        .title
        .map(|s| s.to_owned())
        .unwrap_or(file_title);

    if phrases.is_empty() {
        println!("  No lyric lines found in {}.", opts.input.display());
        return Ok(());
    }

    let duration = opts.duration.filter(|&d| d > 0.0).unwrap_or(auto_dur);
    if duration <= 0.0 {
        return Err("duration must be positive.".to_owned());
    }

    if let Some(rpath) = opts.romanise {
        if !rpath.is_file() {
            return Err(format!("romanization file not found: {}", rpath.display()));
        }
        let matched = apply_external_roman(&mut phrases, rpath)?;
        println!("  romanization: {} / {} lines matched", matched, phrases.len());
    } else if has_inline_sub {
        let count = phrases.iter().filter(|p| p.roman.is_some()).count();
        println!("  co-timed sub-lines: {} / {} lines", count, phrases.len());
    }

    prepare_phrases(&mut phrases);
    let breaks = detect_breaks(&phrases, duration);

    let workers = opts.workers.max(1);
    let mode_str = if opts.transparent {
        "transparent RGBA"
    } else if opts.greenscreen {
        "green screen"
    } else {
        "opaque"
    };

    println!(
        "\n  ♪ {}  [{}]",
        if title.is_empty() { "(no title)" } else { &title },
        fmt_label
    );
    println!(
        "  {} lines · {:.1}s · mode: {}",
        phrases.len(),
        duration,
        mode_str
    );
    if !breaks.is_empty() {
        println!("  {} intermission break(s) detected", breaks.len());
    }

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

    print!("  Pre-computing scroll offsets …");
    let _ = std::io::Write::flush(&mut std::io::stdout());
    let offsets = precompute_offsets(&phrases, &breaks, total);
    let chrome = build_chrome(&title, opts.transparent, opts.greenscreen);
    println!(" done.");

    let pool = build_pool(workers)?;
    let pb = ProgressBar::new(frames.len());

    let res: Result<(), String> = pool.install(|| {
        frames.par_iter().try_for_each_init(
            || {
                (
                    Canvas::new(WIDTH, HEIGHT, [0, 0, 0, 0]),
                    Vec::<u8>::new(),
                    HashMap::<(usize, u32), (u32, Vec<Vec<Syllable>>, f32)>::new(),
                )
            },
            |(canvas, rgb_scratch, lcache), &f| {
                render_frame(
                    canvas,
                    rgb_scratch,
                    lcache,
                    &phrases,
                    &breaks,
                    &chrome,
                    f,
                    &offsets[f],
                    opts.transparent,
                    opts.greenscreen,
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
