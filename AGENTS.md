# AGENTS.md

## Quickstart for an agent

* **Install dependencies** – the project has a tiny `requirements.txt` that lists only the libraries the renderers need. An agent should run:
  ```bash
  pip install -r requirements.txt
  ```

* **Launch the interactive wizard** – the central dispatcher is the `nuisance` script. Running it without arguments opens the wizard which will auto‑detect the file type and ask for any renderer‑specific options:
  ```bash
  python nuisance
  ```

* **Use the CLI** – the same dispatcher accepts a `--input` (or `-i`) flag and forwards all subsequent arguments to the detected renderer. For example:
  ```bash
  python nuisance --input song.mid --lookahead 4.0
  python nuisance --input song.lrc --preview
  ```

* **Render a single renderer directly** – each renderer (`lyrics.py`, `midimap.py`, `waveform.py`) has its own `argparse` help text. To view it run:
  ```bash
  python <renderer> --help
  ```

* **LRC & Enhanced LRC** – both standard LRC and Enhanced LRC (`<mm:ss.xx>` word/syllable tags, `[offset:]`, `[length:]`, and co-timed bilingual/romaji lines) are accepted directly by `nuisance` and `lyrics.py` (including `--romanise`). You can also optionally convert `.lrc` to `.ttml` with:
  ```bash
  python lrc2ttml.py path/to/lyrics.lrc --output lyrics.ttml
  ```

* **Output frames** – all renderers write PNG frame sequences to a `frames/` directory in the current working directory. After rendering you can delete them with:
  ```bash
  rm -rf frames
  ```

* **Encode video with ffmpeg** – the frames are ready for ffmpeg. Example command for a full‑resolution video with an accompanying audio track:
  ```bash
  ffmpeg -framerate 60 -i frames/frame_%06d.png -i audio.mp3 \
         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4
  ```

* **Common flags** – each renderer accepts the following shared options:
  * `--duration <seconds>` – override auto‑detected duration.
  * `--title <text>` – override title shown on screen.
  * `--transparent` – output RGBA frames (slower, suitable for compositing).
  * `--greenscreen` – output a solid green background (fast, suitable for chroma‑key).
  * `--preview` – render only one frame per second for a quick visual check.
  * `--yes` / `-y` – skip interactive render confirmation prompt.
  * `--workers <n>` – number of parallel threads (default is the CPU count).

## Things to watch out for

* The dispatcher (`nuisance`) automatically detects the file type by inspecting magic bytes, XML structure, and LRC timestamps/metadata before falling back to the file extension. Relying on the extension alone can misidentify files.
* The dispatcher does **not** install the renderers as console‑scripts. They are regular Python scripts and must be invoked with `python <script>`.
* The output directory `frames/` is created automatically; it does **not** clean itself after rendering.

---

This file is intentionally short – it only contains facts an agent would otherwise have to discover by trial and error. All other information is either documented in the README or can be inferred from the code itself.
