# Nuisance (Rust Edition)

Nuisance is a standalone, zero-dependency music-to-video rendering suite written in pure Rust. It converts lyrics, MIDI, and audio files into animated 1080p PNG frame sequences ready for `ffmpeg` encoding.

Bundled fonts (`ZenKakuGothicNew-Bold` & `ZenKakuGothicNew-Regular`), audio decoders, MIDI parsers, and 2D anti-aliased rendering are compiled into a single portable binary that runs natively across Linux (`musl` & `gnu`), **Termux / Android Bionic (`aarch64`, `armv7`, `x86_64`)**, macOS, and Windows.

---

## Quick Start

Download a prebuilt binary from Releases, or build from source:

```bash
cargo build --release
```

The standalone executable is generated at `./target/release/nuisance` (or `nuisance.exe` on Windows).

---

## Interactive Wizard Mode (Recommended)

Run without arguments to launch the interactive wizard:

```bash
./nuisance
```

The wizard automatically inspects file content, asks only relevant options, and renders in-process using parallel worker threads.

---

## CLI Mode (Auto-Detecting)

Pass `--input` / `-i` and Nuisance detects the input type automatically from magic bytes, XML structure, or LRC tags:

```bash
nuisance --input lyrics.ttml --duration 217
nuisance --input lyrics.lrc --preview
nuisance --input song.mid --transparent --lookahead 4.0
nuisance --input audio.mp3 --mode spectrum --bars 80
```

---

## Explicit Subcommands

Each renderer and converter can also be invoked directly as a subcommand:

```bash
nuisance lyrics   --input song.lrc --romanise romaji.lrc
nuisance midimap  --input song.mid --transparent
nuisance waveform --input audio.flac --mode spectrum --bars 80
nuisance lrc2ttml song.lrc --output song.ttml
```

Run `nuisance --help` or `nuisance <subcommand> --help` for full flag details.

---

## Supported Formats

1. **Lyrics (`lyrics` renderer)**:
   - **TTML / XML** (`.ttml`, `.xml`) — syllable/word `<span>` wipes or line-synced `<p>` blocks
   - **LRC & Enhanced LRC** (`.lrc`, `.elrc`) — `<mm:ss.xx>` word/syllable tags, `[offset:]`, `[length:]`, chorus repeat tags, empty-line break markers, co-timed bilingual/romaji sub-lines, and UTF-8/UTF-16/Shift-JIS/GB18030 encodings
2. **MIDI (`midimap` renderer)**:
   - Standard MIDI Files (`.mid`, `.midi` — Type 0, 1, and 2) → Synthesia-style falling notes with 88-key active hit glow
3. **Audio (`waveform` renderer)**:
   - Pure-Rust decoding (`symphonia`) for `.wav`, `.flac`, `.ogg`, `.mp3`, `.aiff`, `.m4a`, `.aac`, plus automatic `ffmpeg` fallback if installed (e.g. `.opus`)

---

## Output Modes & Encoding

All renderers support three compositing modes:
- **Opaque** (default): dark RGB background
- **Transparent** (`--transparent`): straight-alpha RGBA with soft Gaussian drop-shadows
- **Green screen** (`--greenscreen`): `#00ff00` chroma-key RGB background

Encode generated frames (`frames/frame_%06d.png`) with `ffmpeg`:

```bash
ffmpeg -framerate 60 -i frames/frame_%06d.png -i audio.mp3 \
       -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4
```

---

## Multi-Platform CI / Releases

[`.github/workflows/build.yml`](.github/workflows/build.yml) automatically tests and builds standalone archives for:
- **Linux**: `x86_64-unknown-linux-musl` (static), `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`
- **Termux / Android Bionic (API 24+)**: `aarch64-linux-android`, `armv7-linux-androideabi`, `x86_64-linux-android`
- **macOS**: `aarch64-apple-darwin` (Apple Silicon), `x86_64-apple-darwin` (Intel)
- **Windows**: `x86_64-pc-windows-msvc`, `aarch64-pc-windows-msvc`

Pushing a version tag (`v*` or `V*`) automatically publishes all binaries and `SHA256SUMS.txt` to GitHub Releases.
