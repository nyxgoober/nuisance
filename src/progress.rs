use std::io::{self, IsTerminal, Write};
use std::sync::atomic::{AtomicUsize, Ordering};

use rayon::ThreadPool;

pub fn default_workers() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
        .max(1)
}

/// Build a Rayon worker pool with a 4 MiB stack per thread so musl (128 KiB default)
/// and Android Bionic (1 MiB default) never hit thread stack overflows.
pub fn build_pool(workers: usize) -> Result<ThreadPool, String> {
    rayon::ThreadPoolBuilder::new()
        .num_threads(workers.max(1))
        .stack_size(4 * 1024 * 1024)
        .build()
        .map_err(|e| format!("failed to build thread pool: {}", e))
}

pub fn confirm_render(skip_prompt: bool) -> bool {
    if skip_prompt {
        return true;
    }
    print!("  Render? (y/n): ");
    let _ = io::stdout().flush();
    let mut line = String::new();
    if io::stdin().read_line(&mut line).is_err() {
        println!();
        return false;
    }
    line.trim().eq_ignore_ascii_case("y")
}

pub fn is_stdin_tty() -> bool {
    io::stdin().is_terminal()
}

pub struct ProgressBar {
    done: AtomicUsize,
    total: usize,
    step: usize,
}

impl ProgressBar {
    pub fn new(total: usize) -> Self {
        let step = (total / 200).max(1);
        Self {
            done: AtomicUsize::new(0),
            total,
            step,
        }
    }

    pub fn inc(&self) {
        if self.total == 0 {
            return;
        }
        let i = self.done.fetch_add(1, Ordering::Relaxed) + 1;
        if i < self.total && (i % self.step) != 0 {
            return;
        }
        let bar = 40usize;
        let filled = (bar * i) / self.total;
        let empty = bar.saturating_sub(filled);
        let pct = (i as f64 / self.total as f64) * 100.0;
        let mut out = io::stdout().lock();
        let _ = write!(
            out,
            "\r[{}{}] {}/{}  {:.1}%",
            "█".repeat(filled),
            "░".repeat(empty),
            i,
            self.total,
            pct
        );
        let _ = out.flush();
    }
}

pub fn print_ffmpeg_hint(
    output_dir: &str,
    fps: u32,
    transparent: bool,
    greenscreen: bool,
    audio_hint: &str,
) {
    println!("\n\n  Done. Encode with:");
    if transparent {
        println!("  # lossless with alpha (ProRes 4444):");
        println!("  ffmpeg -framerate {} -i {}/frame_%06d.png \\", fps, output_dir);
        println!("         -c:v prores_ks -pix_fmt yuva444p10le -profile:v 4444 out.mov\n");
        println!("  # composite over background video:");
        println!(
            "  ffmpeg -i background.mp4 -framerate {} -i {}/frame_%06d.png \\",
            fps, output_dir
        );
        println!(
            "         -i {} -filter_complex \"[0:v][1:v]overlay=0:0\" \\",
            audio_hint
        );
        println!("         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4");
    } else if greenscreen {
        println!("  ffmpeg -framerate {} -i {}/frame_%06d.png \\", fps, output_dir);
        println!("         -c:v libx264 -pix_fmt yuv420p -crf 0 out_gs.mp4\n");
        println!("  # chroma-key composite:");
        println!("  ffmpeg -i background.mp4 -i out_gs.mp4 \\");
        println!("         -filter_complex \"[1:v]colorkey=0x00ff00:0.3:0.1[ov];[0:v][ov]overlay\" \\");
        println!("         -i {} -c:a aac -b:a 192k -shortest out.mp4", audio_hint);
    } else {
        println!(
            "  ffmpeg -framerate {} -i {}/frame_%06d.png -i {} \\",
            fps, output_dir, audio_hint
        );
        println!("         -c:v libx264 -c:a aac -b:a 192k -shortest out.mp4");
    }
    println!();
}
