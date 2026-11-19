# AGENTS.md

## Quickstart for an agent (Rust branch)

* **Build & test** – the entire suite is a pure-Rust Cargo project with zero C/system library dependencies (safe on Termux/Android Bionic, musl, glibc, macOS, and Windows):
  ```bash
  cargo build --release
  cargo test
  ```
  The compiled binary is `./target/release/nuisance` (fonts are embedded via `include_bytes!`).

* **Launch the interactive wizard** – running `nuisance` with no arguments opens the interactive wizard:
  ```bash
  ./target/release/nuisance
  ```

* **Use auto-detected CLI or explicit subcommands**:
  ```bash
  ./target/release/nuisance --input song.mid --lookahead 4.0
  ./target/release/nuisance --input song.lrc --preview
  ./target/release/nuisance lyrics --help
  ./target/release/nuisance midimap --help
  ./target/release/nuisance waveform --help
  ./target/release/nuisance lrc2ttml path/to/lyrics.lrc --output lyrics.ttml
  ```

* **Output frames** – all renderers write PNG frame sequences to `frames/` (configurable via `--output-dir`). Clean up after testing with:
  ```bash
  rm -rf frames
  ```

* **Common flags** – shared across all renderers:
  * `--duration <seconds>` – override auto-detected duration.
  * `--title <text>` – override title shown on screen.
  * `--transparent` – output RGBA frames with blurred shadows.
  * `--greenscreen` – output `#00ff00` background for chroma-key.
  * `--preview` – render 1 frame/sec only (skips confirmation).
  * `--yes` / `-y` – skip interactive confirmation prompt.
  * `--workers <n>` – number of Rayon worker threads (defaults to online CPU count, 4 MiB stack per thread for musl/Bionic safety).
  * `--output-dir <dir>` – output directory (default: `frames`).
  * `--font <path>` – custom regular-weight font (.ttf/.otf), overrides built-in. A variable font with a `wght` axis animates lyric weight (400 → 700) when no `--font-bold` is given.
  * `--font-bold <path>` – custom bold-weight font (.ttf/.otf), overrides built-in.

## Things to watch out for

* Keep dependencies 100% pure Rust (avoid `*-sys` C wrappers like `alsa-sys`, `openssl-sys`, `freetype-sys`, or `cmake`) so cross-compilation to `*-linux-android` (Termux/Bionic) and `*-unknown-linux-musl` works cleanly without sysroot headaches.
* `.github/workflows/build.yml` builds all 10 Linux, Android Bionic, macOS, and Windows targets and publishes GitHub Releases on `v*` / `V*` tags.
