use std::collections::HashMap;
use std::fs;
use std::io::{self, Write};
use std::path::Path;
use std::sync::OnceLock;

use encoding_rs::{GB18030, SHIFT_JIS, UTF_16BE, UTF_16LE, UTF_8};
use regex::Regex;

use crate::progress::is_stdin_tty;

#[derive(Clone, Debug)]
pub struct Syllable {
    pub text: String,
    pub begin: f64,
    pub end: f64,
}

#[derive(Clone, Debug)]
pub struct LrcLine {
    pub begin: f64,
    pub end: f64,
    pub words: Vec<Syllable>,
    pub synced: bool,
    pub sub_words: Vec<Syllable>,
    pub sub_synced: bool,
}

#[derive(Clone, Debug, Default)]
pub struct LrcDocument {
    pub meta: HashMap<String, String>,
    pub duration: Option<f64>,
    pub lines: Vec<LrcLine>,
}

/// Read text file supporting UTF-8 (with/without BOM), UTF-16LE/BE, Shift-JIS, and GB18030.
pub fn read_text_file(path: &Path) -> Result<String, String> {
    let raw = fs::read(path)
        .map_err(|e| format!("failed to read '{}': {}", path.display(), e))?;
    Ok(decode_bytes(&raw))
}

pub fn decode_bytes(raw: &[u8]) -> String {
    if raw.starts_with(&[0xFF, 0xFE]) {
        let (cow, _, _) = UTF_16LE.decode(&raw[2..]);
        return cow.into_owned();
    }
    if raw.starts_with(&[0xFE, 0xFF]) {
        let (cow, _, _) = UTF_16BE.decode(&raw[2..]);
        return cow.into_owned();
    }
    let slice = raw.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(raw);
    if let Ok(s) = std::str::from_utf8(slice) {
        return s.to_owned();
    }
    for enc in [SHIFT_JIS, GB18030] {
        let (cow, _, had_errors) = enc.decode(slice);
        if !had_errors {
            return cow.into_owned();
        }
    }
    let (cow, _, _) = UTF_8.decode(slice);
    cow.into_owned()
}

fn ts_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"^\s*(?:(\d+):)?(\d+):(\d+)(?:([.:])(\d+))?\s*$").unwrap()
    })
}

pub fn line_tag_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\[(\d+:\d+(?:[.:]\d+){0,2})\]").unwrap())
}

pub fn word_tag_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"<(\d+:\d+(?:[.:]\d+){0,2})>").unwrap())
}

pub fn meta_tag_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\[([a-zA-Z][a-zA-Z0-9_-]*):(.*)\]$").unwrap())
}

pub fn parse_lrc_ts(ts: &str) -> Option<f64> {
    let caps = ts_re().captures(ts)?;
    let g1 = caps.get(1).map(|m| m.as_str());
    let g2 = caps.get(2)?.as_str();
    let g3 = caps.get(3)?.as_str();
    let sep = caps.get(4).map(|m| m.as_str());
    let frac = caps.get(5).map(|m| m.as_str());

    if let (Some(s1), None) = (g1, sep) {
        let a: u64 = s1.parse().ok()?;
        let b: u64 = g2.parse().ok()?;
        let c: u64 = g3.parse().ok()?;
        if g3.len() == 2 && a == 0 && b < 60 {
            let sub: f64 = format!("0.{}", g3).parse().ok()?;
            return Some(a as f64 * 60.0 + b as f64 + sub);
        }
        return Some(a as f64 * 3600.0 + b as f64 * 60.0 + c as f64);
    }

    let hours: u64 = match g1 {
        Some(h) => h.parse().ok()?,
        None => 0,
    };
    let minutes: u64 = g2.parse().ok()?;
    let seconds: u64 = g3.parse().ok()?;
    let sub: f64 = match frac {
        Some(f) => format!("0.{}", f).parse().ok()?,
        None => 0.0,
    };
    Some(hours as f64 * 3600.0 + minutes as f64 * 60.0 + seconds as f64 + sub)
}

pub fn fmt_ttml(sec: f64) -> String {
    let total_ms = (sec.max(0.0) * 1000.0).round() as u64;
    let h = total_ms / 3_600_000;
    let rem = total_ms % 3_600_000;
    let m = rem / 60_000;
    let s = (rem % 60_000) / 1000;
    let ms = rem % 1000;
    format!("{:02}:{:02}:{:02}.{:03}", h, m, s, ms)
}

pub fn esc_xml(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for ch in s.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            _ => out.push(ch),
        }
    }
    out
}

#[inline]
fn shift_ts(sec: f64, offset_sec: f64, delta: f64) -> f64 {
    (sec + delta - offset_sec).max(0.0)
}

fn parse_line_words(
    content: &str,
    begin: f64,
    default_end: f64,
    offset_sec: f64,
    delta: f64,
) -> (Vec<Syllable>, f64, bool) {
    let wtags: Vec<_> = word_tag_re().captures_iter(content).collect();
    let wmatches: Vec<_> = word_tag_re().find_iter(content).collect();

    if wmatches.is_empty() {
        let trimmed = content.trim();
        if trimmed.is_empty() {
            return (Vec::new(), default_end, false);
        }
        return (
            vec![Syllable {
                text: trimmed.to_owned(),
                begin,
                end: default_end,
            }],
            default_end,
            false,
        );
    }

    let mut words: Vec<Syllable> = Vec::new();
    let lead_text = &content[..wmatches[0].start()];
    let first_w_begin = parse_lrc_ts(&wtags[0][1])
        .map(|t| shift_ts(t, offset_sec, delta))
        .unwrap_or(begin);

    if !lead_text.trim().is_empty() {
        words.push(Syllable {
            text: lead_text.to_owned(),
            begin,
            end: begin.max(first_w_begin),
        });
    }

    let mut effective_end = default_end;
    let count = wmatches.len();

    for wi in 0..count {
        let w_begin = parse_lrc_ts(&wtags[wi][1])
            .map(|t| shift_ts(t, offset_sec, delta))
            .unwrap_or(begin);
        let text_start = wmatches[wi].end();
        let is_last = wi + 1 >= count;
        let text_end = if !is_last {
            wmatches[wi + 1].start()
        } else {
            content.len()
        };
        let seg_text = &content[text_start..text_end];

        let mut w_end = if !is_last {
            parse_lrc_ts(&wtags[wi + 1][1])
                .map(|t| shift_ts(t, offset_sec, delta))
                .unwrap_or(default_end)
        } else {
            default_end
        };
        if w_end < w_begin {
            w_end = w_begin;
        }

        if !seg_text.trim().is_empty() {
            words.push(Syllable {
                text: seg_text.to_owned(),
                begin: w_begin,
                end: w_end,
            });
        } else if !seg_text.is_empty() && !words.is_empty() && !is_last {
            // Timed space between words: preserve spacing on previous word
            if let Some(last) = words.last_mut() {
                last.text.push_str(seg_text);
            }
        } else if is_last && seg_text.trim().is_empty() && w_begin > begin {
            effective_end = if default_end > begin {
                default_end.min(w_begin)
            } else {
                w_begin
            };
        }
    }

    if let Some(last) = words.last() {
        if last.end > effective_end {
            effective_end = last.end;
        }
    }

    (words, effective_end, true)
}

pub fn parse_lrc(text: &str) -> LrcDocument {
    let clean = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut meta: HashMap<String, String> = HashMap::new();
    let mut raw_entries: Vec<(f64, f64, String, usize)> = Vec::new();
    let mut order_idx = 0usize;

    for raw in clean.lines() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }

        let lmatches: Vec<_> = line_tag_re().find_iter(line).collect();
        if !lmatches.is_empty() && lmatches[0].start() == 0 {
            let content = line[lmatches.last().unwrap().end()..].to_owned();
            let mut parsed_times = Vec::new();
            for caps in line_tag_re().captures_iter(line) {
                if let Some(t) = parse_lrc_ts(&caps[1]) {
                    parsed_times.push(t);
                }
            }
            if !parsed_times.is_empty() {
                let base_t = parsed_times[0];
                for t in parsed_times {
                    raw_entries.push((t, t - base_t, content.clone(), order_idx));
                    order_idx += 1;
                }
                continue;
            }
        }

        if let Some(caps) = meta_tag_re().captures(line) {
            let key = caps[1].trim().to_ascii_lowercase();
            let val = caps[2].trim().to_owned();
            meta.insert(key, val);
        }
    }

    let offset_sec = meta
        .get("offset")
        .and_then(|v| v.parse::<f64>().ok())
        .map(|ms| ms / 1000.0)
        .unwrap_or(0.0);

    let duration = meta.get("length").and_then(|len_str| {
        if let Some(d) = parse_lrc_ts(len_str) {
            if d > 0.0 {
                return Some(d);
            }
        }
        let parts: Vec<&str> = len_str.trim().split(':').collect();
        let val = match parts.len() {
            3 => {
                let h = parts[0].parse::<f64>().ok()?;
                let m = parts[1].parse::<f64>().ok()?;
                let s = parts[2].parse::<f64>().ok()?;
                h * 3600.0 + m * 60.0 + s
            }
            2 => {
                let m = parts[0].parse::<f64>().ok()?;
                let s = parts[1].parse::<f64>().ok()?;
                m * 60.0 + s
            }
            _ => return None,
        };
        (val > 0.0).then_some(val)
    });

    let mut entries: Vec<(f64, f64, String, usize)> = raw_entries
        .into_iter()
        .map(|(t, delta, content, idx)| (shift_ts(t, offset_sec, 0.0), delta, content, idx))
        .collect();

    entries.sort_by(|a, b| {
        let ka = (a.0 * 1000.0).round() as i64;
        let kb = (b.0 * 1000.0).round() as i64;
        ka.cmp(&kb).then_with(|| a.3.cmp(&b.3))
    });

    struct Group {
        begin: f64,
        items: Vec<(f64, f64, String)>,
    }
    let mut groups: Vec<Group> = Vec::new();
    for (t, delta, content, _) in entries {
        if let Some(last) = groups.last_mut() {
            if (t - last.begin).abs() <= 0.002 {
                last.items.push((t, delta, content));
                continue;
            }
        }
        groups.push(Group {
            begin: t,
            items: vec![(t, delta, content)],
        });
    }

    let mut lines = Vec::new();
    for (gi, grp) in groups.iter().enumerate() {
        let begin = grp.begin;
        let default_end = if gi + 1 < groups.len() {
            groups[gi + 1].begin
        } else if let Some(td) = duration {
            if td > begin {
                td.min(begin + 8.0)
            } else {
                begin + 5.0
            }
        } else {
            begin + 5.0
        };

        let mut parsed_lines = Vec::new();
        for (item_t, delta, content) in &grp.items {
            let (words, eff_end, synced) =
                parse_line_words(content, *item_t, default_end, offset_sec, *delta);
            if !words.is_empty() {
                parsed_lines.push((words, eff_end, synced));
            }
        }

        if parsed_lines.is_empty() {
            continue;
        }

        let mut iter = parsed_lines.into_iter();
        let (main_words, mut main_end, main_synced) = iter.next().unwrap();
        let mut sub_words = Vec::new();
        let mut sub_synced = false;

        if let Some((sw, se, ss)) = iter.next() {
            sub_words = sw;
            sub_synced = ss;
            if se > main_end {
                main_end = se;
            }
            for (extra_words, _, _) in iter {
                let extra_txt: String = extra_words.iter().map(|w| w.text.as_str()).collect();
                let trimmed = extra_txt.trim();
                if !trimmed.is_empty() {
                    if let Some(last) = sub_words.last_mut() {
                        last.text.push_str(" / ");
                        last.text.push_str(trimmed);
                    }
                }
            }
        }

        if main_end <= begin {
            main_end = begin + 0.1;
        }

        lines.push(LrcLine {
            begin,
            end: main_end,
            words: main_words,
            synced: main_synced,
            sub_words,
            sub_synced,
        });
    }

    LrcDocument {
        meta,
        duration,
        lines,
    }
}

pub fn build_ttml(title: &str, lines: &[LrcLine], lang: &str) -> String {
    let mut ps = Vec::with_capacity(lines.len());
    for line in lines {
        let begin = fmt_ttml(line.begin);
        let end = fmt_ttml(line.end);
        if !line.synced && line.words.len() == 1 {
            ps.push(format!(
                "    <p begin=\"{}\" end=\"{}\">{}</p>",
                begin,
                end,
                esc_xml(&line.words[0].text)
            ));
        } else {
            let mut spans = Vec::with_capacity(line.words.len());
            for w in &line.words {
                spans.push(format!(
                    "      <span begin=\"{}\" end=\"{}\" xml:space=\"preserve\">{}</span>",
                    fmt_ttml(w.begin),
                    fmt_ttml(w.end),
                    esc_xml(&w.text)
                ));
            }
            ps.push(format!(
                "    <p begin=\"{}\" end=\"{}\">\n{}\n    </p>",
                begin,
                end,
                spans.join("\n")
            ));
        }
    }

    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
<tt xmlns=\"http://www.w3.org/ns/ttml\"\n\
    xmlns:tts=\"http://www.w3.org/ns/ttml#styling\"\n\
    xmlns:ttm=\"http://www.w3.org/ns/ttml#metadata\"\n\
    xml:lang=\"{}\">\n\
  <head>\n\
    <ttm:title>{}</ttm:title>\n\
  </head>\n\
  <body>\n\
    <div>\n\
{}\n\
    </div>\n\
  </body>\n\
</tt>",
        esc_xml(lang),
        esc_xml(title),
        ps.join("\n")
    )
}

pub fn run_lrc2ttml(
    input: &Path,
    output: Option<&Path>,
    title_override: Option<&str>,
    yes: bool,
) -> Result<(), String> {
    if !input.is_file() {
        return Err(format!("file not found: {}", input.display()));
    }
    let text = read_text_file(input)?;
    let doc = parse_lrc(&text);
    let interactive = is_stdin_tty() && !yes;

    let default_stem = input
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("Untitled");

    let title = if let Some(t) = title_override {
        t.to_owned()
    } else if let Some(ti) = doc.meta.get("ti").filter(|s| !s.is_empty()) {
        let mut chosen = ti.clone();
        if interactive {
            println!("  Title from LRC: \"{}\"", chosen);
            print!("  Press enter to keep, or type a new title: ");
            let _ = io::stdout().flush();
            let mut buf = String::new();
            if io::stdin().read_line(&mut buf).is_ok() {
                let ov = buf.trim();
                if !ov.is_empty() {
                    chosen = ov.to_owned();
                }
            }
        }
        chosen
    } else if interactive {
        print!("  Song title: ");
        let _ = io::stdout().flush();
        let mut buf = String::new();
        match io::stdin().read_line(&mut buf) {
            Ok(0) | Err(_) => {
                // EOF or error — fall back to filename stem (matches Python behaviour)
                println!();
                default_stem.to_owned()
            }
            Ok(_) => {
                let ov = buf.trim();
                if ov.is_empty() {
                    "Untitled".to_owned()
                } else {
                    ov.to_owned()
                }
            }
        }
    } else {
        default_stem.to_owned()
    };

    let out_path = match output {
        Some(p) => p.to_path_buf(),
        None => input.with_extension("ttml"),
    };

    let xml = build_ttml(&title, &doc.lines, "ja");
    fs::write(&out_path, xml)
        .map_err(|e| format!("failed to write '{}': {}", out_path.display(), e))?;

    let kind_str = if doc.lines.iter().any(|l| l.synced) {
        "Enhanced LRC"
    } else {
        "LRC"
    };
    println!("  {} lines ({}) → {}", doc.lines.len(), kind_str, out_path.display());

    let sub_lines: Vec<LrcLine> = doc
        .lines
        .iter()
        .filter(|l| !l.sub_words.is_empty())
        .map(|l| LrcLine {
            begin: l.begin,
            end: l.end,
            words: l.sub_words.clone(),
            synced: l.sub_synced,
            sub_words: Vec::new(),
            sub_synced: false,
        })
        .collect();

    if !sub_lines.is_empty() {
        let stem = out_path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("lyrics");
        let sub_out = out_path.with_file_name(format!("{}_sub.ttml", stem));
        let sub_xml = build_ttml(&format!("{} (Sub)", title), &sub_lines, "ja-Latn");
        fs::write(&sub_out, sub_xml)
            .map_err(|e| format!("failed to write '{}': {}", sub_out.display(), e))?;
        println!(
            "  {} co-timed subtitle/romaji lines → {}",
            sub_lines.len(),
            sub_out.display()
        );
    }

    Ok(())
}
