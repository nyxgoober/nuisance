# Nuisance

Nuisance is a modular music-to-video rendering suite that converts musical data into animated PNG frame sequences for encoding via ffmpeg.

It provides a unified CLI dispatcher ("wizard") that automatically detects input types and routes them to the appropriate renderer.

## Components

- lyrics.py   → TTML-based lyric video renderer (kinetic scrolling, timing-based animation)
- midimap.py  → MIDI visualiser (Synthesia-style falling notes)
- waveform.py → Audio visualiser (waveform and spectrum modes)
- nuisance → Central dispatcher and interactive wizard

All renderers output PNG frame sequences intended for ffmpeg encoding.

---

## Installation

```bash
pip install -r requirements
```

- Pillow is required for all renderers
- mido is required only for MIDI processing (midimap.py)
- soundfile and pydub is required for waveform/spectrum visualisation (waveform.py)

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
- TTML/XML → lyrics.py
- MIDI (.mid, .midi) → midimap.py
- Audio files → waveform.py

LRC files are not processed directly and must be converted to TTML first using lrc2ttml.py.

---

## CLI Mode

Direct invocation with automatic routing:

```bash
python nuisance --input song.mid
```

All arguments after --input are forwarded to the detected renderer.

Examples:

```bash
python nuisance --input lyrics.ttml --duration 217
python nuisance --input song.mid --transparent
python nuisance --input audio.mp3 --mode spectrum --bars 80
```

---

## File Detection

Input type is determined in the following order:

1. Magic byte inspection (MIDI, audio formats)
2. XML structure detection (TTML)
3. Text heuristics (LRC detection)
4. File extension fallback

Supported formats:

- TTML: .ttml, .xml
- MIDI: .mid, .midi
- Audio: .wav, .flac, .ogg, .mp3, .aiff, .m4a, .aac, .opus

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
├── nuisance      # dispatcher + wizard
├── lyrics.py        # TTML renderer
├── midimap.py       # MIDI renderer
├── waveform.py      # audio renderer
├── lrc2ttml.py      # LRC conversion utility
├── frames/          # generated output frames
├── ZenKakuGothicNew-Bold.ttf
├── ZenKakuGothicNew-Regular.ttf
└── README.md
```

---

## Notes

- Worker threading is supported where applicable
- Output frame rate defaults to 60 FPS
- Missing fonts fall back to system defaults
- The wizard is optional; CLI mode remains fully functional