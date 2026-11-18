mod canvas;
mod lrc;
mod lyrics;
mod midimap;
mod progress;
mod text;
mod waveform;

use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process;
use std::sync::OnceLock;

use clap::{Args, Parser, Subcommand};
use regex::Regex;

use crate::lrc::decode_bytes;
use crate::lyrics::{LyricsOptions};
use crate::midimap::{MidiOptions, DEFAULT_LOOKAHEAD};
use crate::progress::default_workers;
use crate::waveform::{VizMode, WaveformOptions, DEFAULT_SPEC_BARS};

// ══════════════════════════════════════════
// FILE TYPE DETECTION
// ══════════════════════════════════════════

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum FileKind {
    Ttml,
    Lrc,
    EnhancedLrc,
    Midi,
    Audio,
    Unknown,
}

impl FileKind {
    fn wizard_label(self) -> &'static str {
        match self {
            FileKind::Ttml => "TTML lyrics",
            FileKind::Lrc => "LRC lyrics",
            FileKind::EnhancedLrc => "Enhanced LRC lyrics",
            FileKind::Midi => "MIDI file",
            FileKind::Audio => "audio file",
            FileKind::Unknown => "unknown",
        }
    }

    fn cli_label(self) -> &'static str {
        match self {
            FileKind::Ttml => "TTML lyrics   → lyrics renderer",
            FileKind::Lrc => "LRC lyrics    → lyrics renderer",
            FileKind::EnhancedLrc => "Enhanced LRC  → lyrics renderer",
            FileKind::Midi => "MIDI file     → midimap renderer",
            FileKind::Audio => "audio file    → waveform renderer",
            FileKind::Unknown => "unknown type",
        }
    }
}

fn read_head(path: &Path, max_bytes: usize) -> Vec<u8> {
    let Ok(mut f) = File::open(path) else {
        return Vec::new();
    };
    let mut buf = vec![0u8; max_bytes];
    let Ok(n) = f.read(&mut buf) else {
        return Vec::new();
    };
    buf.truncate(n);
    buf
}

fn is_mpeg_or_adts(h: &[u8]) -> bool {
    if h.len() < 4 {
        return false;
    }
    if (h.starts_with(&[0xFF, 0xFE]) || h.starts_with(&[0xFE, 0xFF]))
        && (h[2] == 0x00 || h[3] == 0x00)
    {
        return false;
    }
    if h[0] != 0xFF || (h[1] & 0xE0) != 0xE0 {
        return false;
    }
    if (h[1] & 0xF6) == 0xF0 {
        return true;
    }
    let version = (h[1] >> 3) & 0x03;
    let layer = (h[1] >> 1) & 0x03;
    let bitrate = (h[2] >> 4) & 0x0F;
    let sr = (h[2] >> 2) & 0x03;
    version != 0x01 && layer != 0x00 && bitrate != 0x00 && bitrate != 0x0F && sr != 0x03
}

pub fn detect_type(path: &Path) -> FileKind {
    let raw = read_head(path, 8192);
    if raw.is_empty() {
        return FileKind::Unknown;
    }

    if raw.starts_with(b"MThd") {
        return FileKind::Midi;
    }
    if raw.len() >= 12
        && (raw.starts_with(b"RIFF") || raw.starts_with(b"RF64"))
        && &raw[8..12] == b"WAVE"
    {
        return FileKind::Audio;
    }
    if raw.starts_with(b"fLaC") || raw.starts_with(b"OggS") || raw.starts_with(b"ID3") {
        return FileKind::Audio;
    }
    if raw.len() >= 12
        && raw.starts_with(b"FORM")
        && (&raw[8..12] == b"AIFF" || &raw[8..12] == b"AIFC")
    {
        return FileKind::Audio;
    }
    if raw.len() >= 8 && (&raw[4..8] == b"ftyp" || &raw[4..8] == b"moov") {
        return FileKind::Audio;
    }
    if is_mpeg_or_adts(&raw) {
        return FileKind::Audio;
    }

    let decoded = decode_bytes(&raw);
    let clean = decoded.trim_start_matches('\u{feff}').trim_start();
    let lower = clean.to_ascii_lowercase();

    if (lower.starts_with("<?xml") || lower.starts_with("<tt"))
        && [
            "http://www.w3.org/ns/ttml",
            "<tt ",
            "<tt\n",
            "<tt\r",
            "<tt\t",
            "<tt>",
        ]
        .iter()
        .any(|tok| lower.contains(tok))
    {
        return FileKind::Ttml;
    }

    if ["<tt ", "<tt>", "http://www.w3.org/ns/ttml"]
        .iter()
        .any(|tok| lower.contains(tok))
    {
        return FileKind::Ttml;
    }

    static LRC_TS_RE: OnceLock<Regex> = OnceLock::new();
    static LRC_META_RE: OnceLock<Regex> = OnceLock::new();
    static ELRC_WORD_RE: OnceLock<Regex> = OnceLock::new();

    let ts_re = LRC_TS_RE.get_or_init(|| Regex::new(r"^\s*\[\d+:\d+(?:[.:]\d+){0,2}\]").unwrap());
    let meta_re = LRC_META_RE
        .get_or_init(|| Regex::new(r"(?i)^\s*\[(?:ti|ar|al|au|length|by|offset|re|ve):").unwrap());
    let word_re = ELRC_WORD_RE.get_or_init(|| Regex::new(r"<\d+:\d+(?:[.:]\d+){0,2}>").unwrap());

    let mut ts_hits = 0usize;
    let mut meta_hits = 0usize;
    for ln in clean.lines().take(50) {
        if ts_re.is_match(ln) {
            ts_hits += 1;
        } else if meta_re.is_match(ln) {
            meta_hits += 1;
        }
    }
    if ts_hits >= 1 || meta_hits >= 2 {
        if word_re.is_match(clean) {
            return FileKind::EnhancedLrc;
        }
        return FileKind::Lrc;
    }

    FileKind::Unknown
}

fn ext_fallback(path: &Path) -> FileKind {
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    match ext.as_str() {
        "mid" | "midi" => FileKind::Midi,
        "ttml" | "xml" => FileKind::Ttml,
        "elrc" => FileKind::EnhancedLrc,
        "lrc" => FileKind::Lrc,
        "wav" | "flac" | "ogg" | "mp3" | "aiff" | "aif" | "m4a" | "aac" | "opus" => FileKind::Audio,
        _ => FileKind::Unknown,
    }
}

pub fn identify(path: &Path) -> FileKind {
    let k = detect_type(path);
    if k == FileKind::Unknown {
        ext_fallback(path)
    } else {
        k
    }
}

// ══════════════════════════════════════════
// CLI DEFINITIONS
// ══════════════════════════════════════════

#[derive(Args, Clone, Debug)]
struct CommonFlags {
    /// Override auto-detected duration in seconds
    #[arg(long)]
    duration: Option<f64>,

    /// Resume from this frame index
    #[arg(long, default_value_t = 0)]
    start: usize,

    /// Thread count for parallel rendering (default: CPU count)
    #[arg(long)]
    workers: Option<usize>,

    /// Export RGBA PNGs with blurred shadows for alpha compositing
    #[arg(long)]
    transparent: bool,

    /// Solid green (#00ff00) background for chroma key
    #[arg(long)]
    greenscreen: bool,

    /// Override title shown on screen
    #[arg(long)]
    title: Option<String>,

    /// Render 1 frame per second only (skips confirmation prompt)
    #[arg(long)]
    preview: bool,

    /// Skip interactive confirmation prompt
    #[arg(short = 'y', long)]
    yes: bool,

    /// Output directory for PNG frame sequence
    #[arg(long, default_value = "frames")]
    output_dir: PathBuf,
}

#[derive(Parser, Debug)]
#[command(
    name = "nuisance",
    version,
    about = "Nuisance — native music-to-video rendering suite (TTML/LRC/Enhanced LRC, MIDI, Audio).\nRun without arguments to launch the interactive wizard."
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,

    /// Input file (TTML, LRC, Enhanced LRC, MIDI, or audio) — type is auto-detected
    #[arg(short = 'i', long)]
    input: Option<PathBuf>,

    #[command(flatten)]
    common: CommonFlags,

    /// [Lyrics] Secondary TTML or LRC file with romanized/translated lyrics
    #[arg(long)]
    romanise: Option<PathBuf>,

    /// [MIDI] Seconds of notes visible above keyboard
    #[arg(long, default_value_t = DEFAULT_LOOKAHEAD)]
    lookahead: f64,

    /// [Audio] Visualiser mode: waveform or spectrum
    #[arg(long, value_enum, default_value_t = VizMode::Waveform)]
    mode: VizMode,

    /// [Audio] Number of frequency bars in spectrum mode
    #[arg(long, default_value_t = DEFAULT_SPEC_BARS)]
    bars: usize,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// Render time-synced lyrics (TTML, LRC, or Enhanced LRC)
    Lyrics {
        /// Path to lyrics file (TTML, LRC, or Enhanced LRC)
        #[arg(short = 'i', long)]
        input: PathBuf,
        /// Secondary TTML or LRC file with romanized/translated lyrics
        #[arg(long)]
        romanise: Option<PathBuf>,
        #[command(flatten)]
        common: CommonFlags,
    },
    /// Render Synthesia-style falling-note MIDI visualiser
    Midimap {
        /// Path to .mid / .midi file
        #[arg(short = 'i', long)]
        input: PathBuf,
        /// Seconds of notes visible above keyboard
        #[arg(long, default_value_t = DEFAULT_LOOKAHEAD)]
        lookahead: f64,
        #[command(flatten)]
        common: CommonFlags,
    },
    /// Render audio waveform or FFT frequency spectrum visualiser
    Waveform {
        /// Path to audio file (WAV, FLAC, OGG, MP3, M4A, AAC, OPUS, AIFF)
        #[arg(short = 'i', long)]
        input: PathBuf,
        /// Visualiser mode: waveform or spectrum
        #[arg(long, value_enum, default_value_t = VizMode::Waveform)]
        mode: VizMode,
        /// Number of frequency bars in spectrum mode
        #[arg(long, default_value_t = DEFAULT_SPEC_BARS)]
        bars: usize,
        #[command(flatten)]
        common: CommonFlags,
    },
    /// Convert .lrc (standard or Enhanced LRC) to TTML XML
    Lrc2ttml {
        /// Input .lrc file
        input: PathBuf,
        /// Output .ttml file (default: <input_stem>.ttml)
        #[arg(short = 'o', long)]
        output: Option<PathBuf>,
        /// Song title override
        #[arg(short = 't', long)]
        title: Option<String>,
        /// Non-interactive mode
        #[arg(short = 'y', long)]
        yes: bool,
    },
}

// ══════════════════════════════════════════
// INTERACTIVE WIZARD
// ══════════════════════════════════════════

fn prompt_line(label: &str, default: Option<&str>, required: bool) -> String {
    let hint = match default {
        Some(d) if !d.is_empty() => format!("  [{}]", d),
        _ => String::new(),
    };
    loop {
        print!("  {}{}: ", label, hint);
        let _ = io::stdout().flush();
        let mut buf = String::new();
        match io::stdin().read_line(&mut buf) {
            Ok(0) | Err(_) => {
                println!();
                process::exit(0);
            }
            Ok(_) => {}
        }
        let val = buf.trim();
        if !val.is_empty() {
            return val.to_owned();
        }
        if let Some(d) = default {
            return d.to_owned();
        }
        if !required {
            return String::new();
        }
        println!("    (required)");
    }
}

fn prompt_opt_f64(label: &str, default: Option<&str>, required: bool) -> Option<f64> {
    loop {
        let raw = prompt_line(label, default, required);
        if raw.is_empty() {
            return None;
        }
        match raw.parse::<f64>() {
            Ok(v) if v > 0.0 => return Some(v),
            _ => println!("    (enter a number > 0)"),
        }
    }
}

fn prompt_usize(label: &str, default_val: usize, min_val: usize) -> usize {
    let def_str = default_val.to_string();
    loop {
        let raw = prompt_line(label, Some(&def_str), true);
        match raw.parse::<usize>() {
            Ok(v) if v >= min_val => return v,
            _ => println!("    (enter an integer >= {})", min_val),
        }
    }
}

fn prompt_choice<'a>(label: &str, choices: &[&'a str], default: &'a str) -> &'a str {
    println!("  {}", label);
    for (i, &c) in choices.iter().enumerate() {
        let marker = if c == default { " *" } else { "" };
        println!("    {}) {}{}", i + 1, c, marker);
    }
    loop {
        let raw = prompt_line("choice", Some(default), true);
        if let Ok(idx) = raw.parse::<usize>() {
            if idx >= 1 && idx <= choices.len() {
                return choices[idx - 1];
            }
        }
        if let Some(&matched) = choices.iter().find(|&&c| c.eq_ignore_ascii_case(&raw)) {
            return matched;
        }
        println!("    enter a number 1–{}", choices.len());
    }
}

fn prompt_bool(label: &str, default: bool) -> bool {
    let hint = if default { "Y/n" } else { "y/N" };
    let full = format!("{} ({})", label, hint);
    let raw = prompt_line(&full, Some(""), false).to_ascii_lowercase();
    if raw.is_empty() {
        default
    } else {
        raw.starts_with('y')
    }
}

fn run_wizard() -> Result<(), String> {
    println!();
    println!("  ╔══════════════════════════════╗");
    println!("  ║       Nuisance Wizard        ║");
    println!("  ╚══════════════════════════════╝");
    println!();

    let input_path = loop {
        let raw = prompt_line("input file", None, true);
        let p = PathBuf::from(&raw);
        if p.is_file() {
            break p;
        }
        println!("    file not found: {}", raw);
    };

    let kind = identify(&input_path);
    println!("    detected: {}\n", kind.wizard_label());

    if kind == FileKind::Unknown {
        return Err(format!(
            "Could not identify '{}' from content or extension.\n  \
             Supported: TTML (.ttml/.xml), LRC / Enhanced LRC (.lrc), \
             MIDI (.mid/.midi), audio (wav/flac/ogg/mp3/aiff/m4a/aac/opus).",
            input_path.display()
        ));
    }

    let duration: Option<f64>;
    let mut title_ov: Option<String> = None;
    let mut romanise_path: Option<PathBuf> = None;
    let mut lookahead = DEFAULT_LOOKAHEAD;
    let mut mode = VizMode::Waveform;
    let mut bars = DEFAULT_SPEC_BARS;

    match kind {
        FileKind::Ttml | FileKind::Lrc | FileKind::EnhancedLrc => {
            duration = prompt_opt_f64("duration (seconds, blank = auto-detect)", Some(""), false);
            let t = prompt_line("title override (blank = from file)", Some(""), false);
            if !t.is_empty() {
                title_ov = Some(t);
            }
            loop {
                let r = prompt_line(
                    "romanisation TTML/LRC (leave blank to skip)",
                    Some(""),
                    false,
                );
                if r.is_empty() {
                    break;
                }
                let rp = PathBuf::from(&r);
                if rp.is_file() {
                    romanise_path = Some(rp);
                    break;
                }
                println!("    file not found: {}", r);
            }
        }
        FileKind::Midi => {
            duration = prompt_opt_f64("duration (seconds, blank = auto-detect)", Some(""), false);
            let t = prompt_line("title override (blank = filename)", Some(""), false);
            if !t.is_empty() {
                title_ov = Some(t);
            }
            lookahead = prompt_opt_f64(
                "lookahead seconds (notes visible above keyboard)",
                Some("3.0"),
                true,
            )
            .unwrap_or(DEFAULT_LOOKAHEAD);
        }
        FileKind::Audio => {
            let m_choice = prompt_choice("visualiser mode", &["waveform", "spectrum"], "waveform");
            mode = if m_choice == "spectrum" {
                bars = prompt_usize("number of frequency bars", DEFAULT_SPEC_BARS, 1);
                VizMode::Spectrum
            } else {
                VizMode::Waveform
            };
            duration = prompt_opt_f64("duration (seconds, blank = auto-detect)", Some(""), false);
            let t = prompt_line("title override (blank = filename)", Some(""), false);
            if !t.is_empty() {
                title_ov = Some(t);
            }
        }
        FileKind::Unknown => unreachable!(),
    }

    println!();
    let out_choice = prompt_choice(
        "output mode",
        &["opaque", "transparent (RGBA)", "green screen"],
        "opaque",
    );
    let transparent = out_choice.contains("transparent");
    let greenscreen = out_choice.contains("green");
    let preview = prompt_bool("preview only (1 frame/sec, skips confirmation)", false);
    let workers = prompt_usize("worker threads", default_workers(), 1);
    let output_dir = PathBuf::from("frames");

    match kind {
        FileKind::Ttml | FileKind::Lrc | FileKind::EnhancedLrc => lyrics::run(LyricsOptions {
            input: &input_path,
            duration,
            start: 0,
            workers,
            transparent,
            greenscreen,
            romanise: romanise_path.as_deref(),
            title: title_ov.as_deref(),
            preview,
            yes: false,
            output_dir: &output_dir,
        }),
        FileKind::Midi => midimap::run(MidiOptions {
            input: &input_path,
            duration,
            start: 0,
            workers,
            lookahead,
            transparent,
            greenscreen,
            title: title_ov.as_deref(),
            preview,
            yes: false,
            output_dir: &output_dir,
        }),
        FileKind::Audio => waveform::run(WaveformOptions {
            input: &input_path,
            duration,
            mode,
            bars,
            start: 0,
            workers,
            transparent,
            greenscreen,
            title: title_ov.as_deref(),
            preview,
            yes: false,
            output_dir: &output_dir,
        }),
        FileKind::Unknown => unreachable!(),
    }
}

// ══════════════════════════════════════════
// ENTRY POINT
// ══════════════════════════════════════════

fn run_app() -> Result<(), String> {
    if std::env::args_os().len() == 1 {
        return run_wizard();
    }

    let cli = Cli::parse();

    if let Some(cmd) = cli.command {
        return match cmd {
            Commands::Lyrics {
                input,
                romanise,
                common,
            } => lyrics::run(LyricsOptions {
                input: &input,
                duration: common.duration,
                start: common.start,
                workers: common.workers.unwrap_or_else(default_workers),
                transparent: common.transparent,
                greenscreen: common.greenscreen,
                romanise: romanise.as_deref(),
                title: common.title.as_deref(),
                preview: common.preview,
                yes: common.yes,
                output_dir: &common.output_dir,
            }),
            Commands::Midimap {
                input,
                lookahead,
                common,
            } => midimap::run(MidiOptions {
                input: &input,
                duration: common.duration,
                start: common.start,
                workers: common.workers.unwrap_or_else(default_workers),
                lookahead,
                transparent: common.transparent,
                greenscreen: common.greenscreen,
                title: common.title.as_deref(),
                preview: common.preview,
                yes: common.yes,
                output_dir: &common.output_dir,
            }),
            Commands::Waveform {
                input,
                mode,
                bars,
                common,
            } => waveform::run(WaveformOptions {
                input: &input,
                duration: common.duration,
                mode,
                bars,
                start: common.start,
                workers: common.workers.unwrap_or_else(default_workers),
                transparent: common.transparent,
                greenscreen: common.greenscreen,
                title: common.title.as_deref(),
                preview: common.preview,
                yes: common.yes,
                output_dir: &common.output_dir,
            }),
            Commands::Lrc2ttml {
                input,
                output,
                title,
                yes,
            } => lrc::run_lrc2ttml(&input, output.as_deref(), title.as_deref(), yes),
        };
    }

    let Some(input) = cli.input else {
        return Err("missing --input <FILE> (or run `nuisance` without flags for the wizard)".to_owned());
    };
    if !input.is_file() {
        return Err(format!("file not found: {}", input.display()));
    }

    let kind = identify(&input);
    println!("\n  detected: {}", kind.cli_label());

    let workers = cli.common.workers.unwrap_or_else(default_workers);

    match kind {
        FileKind::Ttml | FileKind::Lrc | FileKind::EnhancedLrc => lyrics::run(LyricsOptions {
            input: &input,
            duration: cli.common.duration,
            start: cli.common.start,
            workers,
            transparent: cli.common.transparent,
            greenscreen: cli.common.greenscreen,
            romanise: cli.romanise.as_deref(),
            title: cli.common.title.as_deref(),
            preview: cli.common.preview,
            yes: cli.common.yes,
            output_dir: &cli.common.output_dir,
        }),
        FileKind::Midi => midimap::run(MidiOptions {
            input: &input,
            duration: cli.common.duration,
            start: cli.common.start,
            workers,
            lookahead: cli.lookahead,
            transparent: cli.common.transparent,
            greenscreen: cli.common.greenscreen,
            title: cli.common.title.as_deref(),
            preview: cli.common.preview,
            yes: cli.common.yes,
            output_dir: &cli.common.output_dir,
        }),
        FileKind::Audio => waveform::run(WaveformOptions {
            input: &input,
            duration: cli.common.duration,
            mode: cli.mode,
            bars: cli.bars,
            start: cli.common.start,
            workers,
            transparent: cli.common.transparent,
            greenscreen: cli.common.greenscreen,
            title: cli.common.title.as_deref(),
            preview: cli.common.preview,
            yes: cli.common.yes,
            output_dir: &cli.common.output_dir,
        }),
        FileKind::Unknown => Err(format!(
            "Could not identify '{}' from content or extension.\n  \
             Supported types: TTML (.ttml/.xml), LRC / Enhanced LRC (.lrc), \
             MIDI (.mid/.midi), audio (wav/flac/ogg/mp3/aiff/m4a/aac/opus).",
            input.display()
        )),
    }
}

fn main() {
    if let Err(err) = run_app() {
        eprintln!("  error: {}", err);
        process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lrc::{fmt_ttml, parse_lrc, parse_lrc_ts};
    use crate::lyrics::parse_ttml_time;

    #[test]
    fn test_time_formatting_and_parsing() {
        assert_eq!(fmt_ttml(59.9999), "00:01:00.000");
        assert_eq!(fmt_ttml(3599.9999), "01:00:00.000");
        assert!((parse_lrc_ts("01:02.5").unwrap() - 62.5).abs() < 1e-6);
        assert!((parse_lrc_ts("01:02.05").unwrap() - 62.05).abs() < 1e-6);
        assert!((parse_lrc_ts("01:02").unwrap() - 62.0).abs() < 1e-6);
        assert!((parse_ttml_time("12.5s") - 12.5).abs() < 1e-6);
        assert!((parse_ttml_time("500ms") - 0.5).abs() < 1e-6);
        assert!((parse_ttml_time("01:23.5") - 83.5).abs() < 1e-6);
    }

    #[test]
    fn test_enhanced_lrc_edge_cases() {
        let sample = "\u{feff}[ti:Rust Port]\n\
                      [length: 00:25.00]\n\
                      [offset: +500]\n\
                      [00:02.50]Lead <00:03.00>word<00:03.40> <00:03.60>two<00:04.50>\n\
                      [00:02.50]<00:02.50>riido <00:03.00>waado <00:03.60>tsuu<00:04.50>\n\
                      [00:10.50][00:18.50]<00:10.50>Chorus <00:11.00>line<00:12.50>\n\
                      [00:14.50]Plain line\n\
                      [00:16.00]\n";
        let doc = parse_lrc(sample);
        assert_eq!(doc.meta.get("ti").map(|s| s.as_str()), Some("Rust Port"));
        assert_eq!(doc.duration, Some(25.0));
        assert_eq!(doc.lines.len(), 4);

        let p0 = &doc.lines[0];
        assert!((p0.begin - 2.0).abs() < 1e-6);
        assert!((p0.end - 4.0).abs() < 1e-6);
        let joined: String = p0.words.iter().map(|w| w.text.as_str()).collect();
        assert_eq!(joined, "Lead word two");
        assert_eq!(p0.sub_words.len(), 3);

        let p3 = &doc.lines[3];
        assert!((p3.begin - 18.0).abs() < 1e-6);
        assert!((p3.words[1].begin - 18.5).abs() < 1e-6);
    }

    #[test]
    fn test_utf16_bom_not_misdetected_as_mpeg() {
        let mut utf16le = vec![0xFF, 0xFE];
        for cu in "[00:01.00]Hello\n".encode_utf16() {
            utf16le.extend_from_slice(&cu.to_le_bytes());
        }
        assert!(!is_mpeg_or_adts(&utf16le));
    }
}
