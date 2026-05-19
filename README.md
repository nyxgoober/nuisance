# Nuisance Lyrics Renderer
**Nuisance** is a minimalist, precise command-line utility for generating beautifully animated, time-synced lyric video frames from standard subtitle and lyric formats. It features smooth "word-by-word" highlight animations, kinetic vertical scrolling, and subtle focal fading.
## Features
 * **Word-by-Word Highlight Tracking:** Smoothly transitions individual text segments from inactive to active, and finally to completed states using custom easing functions (ease_out).
 * **Dynamic Vertical Centering:** Automatically computes active line heights and smoothly shifts the canvas coordinate space to keep the active line centered at 40\% of the screen height.
 * **Proximity Fading:** Real-time distance-based alpha scaling dynamically fades lines that are too far above or below the active viewpoint.
 * **LRC to TTML Conversion:** Includes a pre-processor utility (lrc2ttml.py) to flawlessly translate standard Enhanced LRC lyric formats into structurally sound TTML files.
## Installation & Requirements
Ensure you have Python 3 installed alongside the required imaging dependency:
```bash
pip install pillow

```
### Font Requirements
The renderer is pre-configured to look for specific fonts in the local directory to maintain typographic visual hierarchy:
 * **ZenKakuGothicNew-Bold.ttf** (Used for active, scaling, and bold titles)
 * **ZenKakuGothicNew-Regular.ttf** (Used for idle text and the system watermark)
> **Note:** If these specific .ttf files are missing, the utility will automatically fall back to standard system default bitmaps without crashing.
> 
## Step 1: Convert Lyrics (.lrc to .ttml)
If your lyrics are in standard timestamped .lrc formatting, convert them first using the parser pipeline.
```bash
python lrc2ttml.py lyrics.lrc --output lyrics.ttml

```
### Options:
 * --output, -o: Specify a explicit target output file path (defaults to matching the input filename).
 * --title, -t: Set the internal track meta-title directly via CLI to bypass the interactive confirmation script.
## Step 2: Render Frames
The primary script (nuisance.py) parses the layout metrics and dumps individual frame sequences as static .png files.
```bash
python nuisance.py --input lyrics.ttml --duration 180.5

```
### Options:
 * --input: (Required) Path to the target source XML/TTML file structure.
 * --duration: (Required) Explicit timeframe length of the track loop in total float seconds.
 * --start: (Optional) Specifies an arbitrary frame index offset to resume interrupted rendering workflows.
## Step 3: Encode into Video with FFmpeg
Once individual frame sequences are fully written out to disk within the local /frames directory environment, compile them alongside your original audio mix file using standard ffmpeg stream copy pipelines:
```bash
ffmpeg -framerate 60 -i frames/frame_%06d.png -i audio.mp3 \
       -c:v h264_mediacodec -c:a aac -b:a 192k -shortest out.mp4

```
### Command Flags Breakdown:
 * -framerate 60: Dictates matching timeline frequency interpolation corresponding to the source canvas capture variables.
 * -i frames/frame_%06d.png: Feeds sequentially indexed source file matrices into the visual layout buffer engine.
 * -i audio.mp3: Merges the master audio track accompaniment file stream.
 * -c:v h264_mediacodec: Instructs hardware-accelerated video processing options (swap for -c:v libx264 if rendering on traditional CPU architectures).
 * -shortest: Limits final video timeline bounds strictly to whichever media asset terminates first to ensure structural synchronization.
## Design Constants Reference
For custom adjustments, the following layout constants can be tuned directly in the header of nuisance.py:
| Constant Name | Value | Purpose |
|---|---|---|
| WIDTH / HEIGHT | 1920 / 1080 | Canvas dimension metrics |
| FPS | 60 | Targeting baseline timeline processing targets |
| SIZE_IDLE | 36 | Text scaling size during dormant cycles |
| SIZE_ACTIVE | 96 | Dynamic text footprint when element receives active tracking focus |
| WORD_FADE_DUR | 0.18 | Interpolation timeframe curve length for alpha color transitions |
### Word-for-Word Lyrics
To get word-for-word lyrics syncing, first get a line-by-line TTML.
After that, use the included HTML, upload the TTML, the same music file you used, and start stamping words from what you hear.
After finishing stamping, press the export button, download/copy the TTML and pass it inside the input flag. Nuisance will automatically detect that it is a line-by-line TTML.

If you do not wish to use the HTML, you can also use https://nuisance.patchednexus.win/ .
### Lyric File Sources
You can obtain lyric files from other sources if you prefer to avoid manually creating one from sources such as Python syncedlyrics, LRCLIB or any other source, then use the provided lrc2ttml.py to convert it to a TTML.
