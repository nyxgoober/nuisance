# Nuisance

Nuisance is a modular music-to-video rendering suite that converts musical data into animated PNG frame sequences for encoding via ffmpeg.

It provides a unified CLI dispatcher ("wizard") that automatically detects input types and routes them to the appropriate renderer.

## Components

- `lyrics.py`   → Lyric video renderer supporting TTML, LRC, and Enhanced LRC (syllable wipe, kinetic scrolling)
- `midimap.py`  → MIDI visualiser (Synthesia-style falling notes)
- `waveform.py` → Audio visualiser (waveform and spectrum modes)
- `nuisance`    → Central dispatcher and interactive wizard
- `lrc2ttml.py` → Optional LRC / Enhanced LRC → TTML conversion utility
- `romini.py`   → Japanese romanizer for TTML, LRC, and Enhanced LRC

All renderers output PNG frame sequences intended for ffmpeg encoding.

---

## Installation

```bash
pip install -r requirements.txt
```

- `pillow` is required for all renderers
- `mido` is required for MIDI processing (`midimap.py`)
- `numpy` and `soundfile` (with `pydub` / `ffmpeg` fallback) are required for waveform/spectrum visualisation (`waveform.py`)
- `pykakasi` (optional) is used by `romini.py` for Japanese romanization

---

## Wizard Mode (Recommended)

Run without arguments to launch the interactive wizard:

```bash
python nuisance
```

The wizard will:
- Detect file type automatically
- Prompt only for relevant parameters
- Dispatch execution to the appropriate renderer

Supported inputs:
- TTML/XML (`.ttml`, `.xml`) → `lyrics.py`
- LRC & Enhanced LRC (`.lrc`, `.elrc`) → `lyrics.py`
- MIDI (`.mid`, `.midi`) → `midimap.py`
- Audio files → `waveform.py`

---

## CLI Mode

Direct invocation with automatic routing:

```bash
python nuisance --input song.mid
```

All arguments after `--input` are forwarded to the detected renderer.

Examples:

```bash
python nuisance --input lyrics.ttml --duration 217
python nuisance --input lyrics.lrc --preview
python nuisance --input song.mid --transparent
python nuisance --input audio.mp3 --mode spectrum --bars 80
```

---

## File Detection

Input type is determined in the following order:

1. Magic byte inspection (MIDI, audio formats)
2. XML structure detection (TTML)
3. Text heuristics (LRC and Enhanced LRC detection)
4. File extension fallback

Supported formats:

- Lyrics: `.ttml`, `.xml`, `.lrc`, `.elrc` (both standard line-synced LRC and word/syllable-synced Enhanced LRC, including co-timed bilingual/romaji lines and `[offset:]` / `[length:]` tags)
- MIDI: `.mid`, `.midi`
- Audio: `.wav`, `.flac`, `.ogg`, `.mp3`, `.aiff`, `.aif`, `.m4a`, `.aac`, `.opus`

---

## Output Modes

All renderers support the following output modes:

- opaque: standard RGB video
- transparent: RGBA output for compositing
- green screen: solid chroma key background

---

## Rendering Pipeline

All inputs follow the same pipeline:

input file → renderer → PNG frame sequence → ffmpeg → final video

---

## File Structure

```
nuisance/
├── nuisance                     # dispatcher + wizard
├── lyrics.py                    # TTML / LRC / Enhanced LRC renderer
├── midimap.py                   # MIDI renderer
├── waveform.py                  # audio renderer
├── lrc2ttml.py                  # LRC / Enhanced LRC → TTML converter
├── romini.py                    # Japanese romanizer (TTML & LRC)
├── nuisancestamper.html         # browser-based syllable timing tool
├── frames/                      # generated output frames
├── ZenKakuGothicNew-Bold.ttf
├── ZenKakuGothicNew-Regular.ttf
└── README.md
```

---

## Notes

- Worker threading is supported across all renderers
- Output frame rate defaults to 60 FPS
- Missing fonts fall back to system defaults
- The wizard is optional; CLI mode remains fully functional
