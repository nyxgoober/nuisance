# Nuisance

**Nuisance** is a general-purpose music renderer that generates beautifully animated video frames as PNG sequences, ready to encode with ffmpeg. It currently ships two renderers:

- **`nuisance.py`** — time-synced lyric videos from TTML files
- **`midimap.py`** — Synthesia-style falling-note visualisations from MIDI files

All renderers share the same output pipeline: PNG frame sequences → ffmpeg.

---

## Installation

```bash
pip install pillow mido
```

`mido` is only required for `midimap.py`. `nuisance.py` only needs Pillow.

### Font Requirements

Both renderers look for these fonts in the working directory:

- **`ZenKakuGothicNew-Bold.ttf`** — active lines, titles, highlights
- **`ZenKakuGothicNew-Regular.ttf`** — idle lines, romanization, watermark

If the fonts are missing, the renderer falls back to the system default bitmap font without crashing.

---

## nuisance.py — Lyric Video Renderer

### Features

- **Syllable-level wipe** — a left-to-right pixel wipe sweeps across each word as it's sung (requires word-synced TTML)
- **Line-level fade** — for line-synced TTML, the whole line cross-fades as it becomes active
- **Kinetic scroll** — the active line is anchored at 40% of screen height; lines above snap to position, lines below use a spring for a lazy catch-up feel; recently-finished lines ease upward in sync with their shrink animation
- **Proximity fading** — lines far from the active line fade out proportionally
- **Intermission dots** — gaps ≥ 2.5 seconds between lines show three pulsing dots that slide into the layout and push lines apart rather than overlaying them
- **Romanization** — a second TTML file can be overlaid below each line simultaneously
- **Background modes** — opaque dark, transparent RGBA (blurred shadows), or green screen

### Step 1: Get a TTML lyrics file

If you have an `.lrc` file, convert it first:

```bash
python lrc2ttml.py lyrics.lrc --output lyrics.ttml
```

Options:
- `--output`, `-o` — output path (defaults to input filename with `.ttml`)
- `--title`, `-t` — set track title without interactive prompt

For word-by-word timing, use the web stamping tool at **https://nuisance.patchednexus.win/** or the included HTML file — upload the line-synced TTML and an audio file, stamp each word, then export.

You can also source `.lrc` files from `syncedlyrics`, LRCLIB, or similar services.

### Step 2: Render

```bash
python nuisance.py --input lyrics.ttml --duration 217
```

Options:

| Flag | Required | Description |
|---|---|---|
| `--input` | ✓ | Path to TTML file |
| `--duration` | ✓ | Track length in seconds |
| `--start` | | Resume from this frame index |
| `--workers` | | Thread count (default: CPU count) |
| `--transparent` | | RGBA output with soft blurred shadows |
| `--greenscreen` | | Solid `#00ff00` background for chroma key |
| `--romanise` | | Path to a second TTML with romanized lyrics |

`--transparent` and `--greenscreen` are mutually exclusive. `--transparent` is slower; use `--greenscreen` when you just need to composite over a video.

### Step 3: Encode

**Opaque (standard):**
```bash
ffmpeg -framerate 60 -i frames/frame_%06d.png -i audio.mp3 \
       -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4
```

**Transparent (ProRes 4444 with alpha):**
```bash
ffmpeg -framerate 60 -i frames/frame_%06d.png \
       -c:v prores_ks -pix_fmt yuva444p10le -profile:v 4444 out.mov
```

**Transparent composited over a background video:**
```bash
ffmpeg -i background.mp4 -framerate 60 -i frames/frame_%06d.png \
       -i audio.mp3 -filter_complex "[0:v][1:v]overlay=0:0" \
       -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4
```

**Green screen encode + composite:**
```bash
# Encode
ffmpeg -framerate 60 -i frames/frame_%06d.png \
       -c:v libx264 -pix_fmt yuv420p -crf 0 out_gs.mp4

# Composite over background
ffmpeg -i background.mp4 -i out_gs.mp4 \
       -filter_complex "[1:v]colorkey=0x00ff00:0.3:0.1[ov];[0:v][ov]overlay" \
       -i audio.mp3 -c:a aac -b:a 192k -shortest out.mp4
```

> On Android/Termux or hardware-accelerated systems, swap `-c:v libx264` for `-c:v h264_mediacodec`.

### Tunable Constants

Edit these at the top of `nuisance.py`:

| Constant | Default | Purpose |
|---|---|---|
| `WIDTH` / `HEIGHT` | 1920 / 1080 | Canvas size |
| `FPS` | 60 | Frame rate |
| `SIZE_IDLE` | 36 | Font size for inactive lines |
| `SIZE_ACTIVE` | 96 | Font size for the active line |
| `WORD_FADE_DUR` | 0.18 | Duration of line-level fade (seconds) |
| `LOOKAHEAD` (scroll) | — | Controlled by spring constants `TAU_BELOW`, `TAU_PAST` |
| `BREAK_THRESHOLD` | 2.5 | Minimum silence gap (seconds) to show intermission dots |
| `DOT_PULSE_PERIOD` | 1.40 | Seconds for the dot pulse to travel across all three dots |

---

## midimap.py — MIDI Visualiser

Renders a Synthesia-style falling-note view with a piano keyboard at the bottom. Each MIDI track gets its own colour from a built-in palette.

### Features

- **Falling note blocks** — notes scroll down and hit the keyboard exactly on time; block height reflects note duration
- **Per-track colours** — up to 8 tracks with distinct colours; track names and coloured dots shown in the top-left
- **Key flash** — white and black keys light up to their track colour on hit with a fast ease-out flash
- **Glow** — active notes emit a blurred glow at the keyboard surface
- **Auto pitch range** — the keyboard is sized to fit only the notes actually used in the file
- **Tempo map support** — handles mid-song tempo changes correctly
- **Same output modes** as nuisance.py: opaque, transparent, green screen

### Render

```bash
python midimap.py --input song.mid
```

Options:

| Flag | Required | Description |
|---|---|---|
| `--input` | ✓ | Path to `.mid` or `.midi` file |
| `--duration` | | Override duration (auto-detected if omitted) |
| `--start` | | Resume from this frame index |
| `--workers` | | Thread count (default: CPU count) |
| `--lookahead` | | Seconds of notes visible above keyboard (default: 3.0) |
| `--transparent` | | RGBA output with blurred shadows |
| `--greenscreen` | | Solid `#00ff00` background for chroma key |
| `--title` | | Override the title shown on screen |

### Encode

Same ffmpeg commands as nuisance.py above. Since midimap has no audio, always provide `-i audio.mp3` (or your source audio) separately.

```bash
ffmpeg -framerate 60 -i frames/frame_%06d.png -i audio.mp3 \
       -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4
```

### Tunable Constants

Edit these at the top of `midimap.py`:

| Constant | Default | Purpose |
|---|---|---|
| `WIDTH` / `HEIGHT` | 1920 / 1080 | Canvas size |
| `FPS` | 60 | Frame rate |
| `LOOKAHEAD` | 3.0 | Seconds of notes visible above keyboard |
| `KEY_AREA_H` | 180 | Pixel height of the keyboard strip |
| `NOTE_RADIUS` | 6 | Rounded corner radius on note blocks |
| `GLOW_BLUR` | 18 | Gaussian blur radius for hit glow |
| `TRACK_PALETTE` | 8 colours | Per-track colour list — edit to taste |

---

## File Layout

```
nuisance/
├── nuisance.py       # lyric video renderer
├── midimap.py        # MIDI falling-note renderer
├── lrc2ttml.py       # LRC → TTML converter
├── README.md
├── ZenKakuGothicNew-Bold.ttf
├── ZenKakuGothicNew-Regular.ttf
└── frames/           # rendered PNGs go here (auto-created)
```

---

***Thank you for using Nuisance!***