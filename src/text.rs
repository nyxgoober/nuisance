use std::cell::RefCell;
use std::collections::HashMap;
use std::path::Path;
use std::sync::OnceLock;

use fontdue::{Font, FontSettings, Metrics};

use crate::canvas::{
    blend_pixel, clamp, div255, lerp_color, Canvas, Color, Rgba, BG_COLOR,
};

static BOLD_TTF: &[u8] = include_bytes!("../ZenKakuGothicNew-Bold.ttf");
static REGULAR_TTF: &[u8] = include_bytes!("../ZenKakuGothicNew-Regular.ttf");

#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum FontWeight {
    Regular,
    Bold,
}

struct FontPair {
    regular: Font,
    bold: Font,
}

static FONTS: OnceLock<FontPair> = OnceLock::new();

/// Initialise fonts from external .ttf/.otf files. Call once before rendering.
/// If `regular` or `bold` is `None`, the embedded default is used for that weight.
/// Returns `Err` if a file can't be read or parsed.
pub fn init_fonts(
    regular: Option<&Path>,
    bold: Option<&Path>,
) -> Result<(), String> {
    let load = |path: &Path, label: &str| -> Result<Font, String> {
        let bytes = std::fs::read(path).map_err(|e| format!("cannot read {} font '{}': {}", label, path.display(), e))?;
        Font::from_bytes(bytes, FontSettings::default())
            .map_err(|e| format!("cannot parse {} font '{}': {}", label, path.display(), e))
    };

    let reg = match regular {
        Some(p) => load(p, "regular")?,
        None => Font::from_bytes(REGULAR_TTF, FontSettings::default())
            .expect("Failed to parse embedded ZenKakuGothicNew-Regular.ttf"),
    };
    let bld = match bold {
        Some(p) => load(p, "bold")?,
        None => Font::from_bytes(BOLD_TTF, FontSettings::default())
            .expect("Failed to parse embedded ZenKakuGothicNew-Bold.ttf"),
    };

    FONTS.set(FontPair { regular: reg, bold: bld })
        .map_err(|_| "fonts already initialised".to_owned())
}

fn fonts() -> &'static FontPair {
    FONTS.get_or_init(|| {
        let regular = Font::from_bytes(REGULAR_TTF, FontSettings::default())
            .expect("Failed to parse embedded ZenKakuGothicNew-Regular.ttf");
        let bold = Font::from_bytes(BOLD_TTF, FontSettings::default())
            .expect("Failed to parse embedded ZenKakuGothicNew-Bold.ttf");
        FontPair { regular, bold }
    })
}

fn get_font(weight: FontWeight) -> &'static Font {
    let pair = fonts();
    match weight {
        FontWeight::Regular => &pair.regular,
        FontWeight::Bold => &pair.bold,
    }
}

struct CachedGlyph {
    metrics: Metrics,
    bitmap: Vec<u8>,
}

thread_local! {
    static GLYPH_CACHE: RefCell<HashMap<(FontWeight, u32, char), CachedGlyph>> =
        RefCell::new(HashMap::with_capacity(512));
}

fn with_glyph<R>(weight: FontWeight, size_px: u32, ch: char, f: impl FnOnce(&CachedGlyph) -> R) -> R {
    let sz = size_px.max(8);
    GLYPH_CACHE.with(|cell| {
        let mut map = cell.borrow_mut();
        let entry = map.entry((weight, sz, ch)).or_insert_with(|| {
            let font = get_font(weight);
            let (metrics, bitmap) = font.rasterize(ch, sz as f32);
            CachedGlyph { metrics, bitmap }
        });
        f(entry)
    })
}

#[derive(Copy, Clone, Debug)]
pub struct LineBox {
    pub ascent: f32,
    pub line_height: f32,
}

pub fn line_metrics(weight: FontWeight, size_px: u32) -> LineBox {
    let sz = size_px.max(8) as f32;
    let font = get_font(weight);
    if let Some(lm) = font.horizontal_line_metrics(sz) {
        let h = (lm.ascent - lm.descent).max(sz * 0.9);
        LineBox {
            ascent: lm.ascent,
            line_height: h,
        }
    } else {
        LineBox {
            ascent: sz * 0.85,
            line_height: sz * 1.15,
        }
    }
}

/// Measure horizontal advance width of `text` in pixels (includes spaces and kerning).
pub fn text_width(text: &str, weight: FontWeight, size_px: u32) -> f32 {
    if text.is_empty() {
        return 0.0;
    }
    let sz = size_px.max(8) as f32;
    let font = get_font(weight);
    let mut w = 0.0f32;
    let mut prev: Option<char> = None;
    for ch in text.chars() {
        if ch == '\r' || ch == '\n' {
            continue;
        }
        if let Some(p) = prev {
            if let Some(k) = font.horizontal_kern(p, ch, sz) {
                w += k;
            }
        }
        w += with_glyph(weight, size_px, ch, |g| g.metrics.advance_width);
        prev = Some(ch);
    }
    w
}

/// Tight ink bounding box `(min_x, min_y, max_x, max_y)` relative to top-left `(0, 0)`.
pub fn text_ink_bounds(text: &str, weight: FontWeight, size_px: u32) -> Option<(i32, i32, i32, i32)> {
    if text.is_empty() {
        return None;
    }
    let sz = size_px.max(8) as f32;
    let font = get_font(weight);
    let lm = line_metrics(weight, size_px);
    let mut pen_x = 0.0f32;
    let mut min_x = i32::MAX;
    let mut min_y = i32::MAX;
    let mut max_x = i32::MIN;
    let mut max_y = i32::MIN;
    let mut prev: Option<char> = None;

    for ch in text.chars() {
        if ch == '\r' || ch == '\n' {
            continue;
        }
        if let Some(p) = prev {
            if let Some(k) = font.horizontal_kern(p, ch, sz) {
                pen_x += k;
            }
        }
        with_glyph(weight, size_px, ch, |g| {
            if g.metrics.width > 0 && g.metrics.height > 0 {
                let gx0 = (pen_x + g.metrics.xmin as f32).round() as i32;
                let gy0 = (lm.ascent - (g.metrics.ymin + g.metrics.height as i32) as f32).round() as i32;
                let gx1 = gx0 + g.metrics.width as i32;
                let gy1 = gy0 + g.metrics.height as i32;
                min_x = min_x.min(gx0);
                min_y = min_y.min(gy0);
                max_x = max_x.max(gx1);
                max_y = max_y.max(gy1);
            }
            pen_x += g.metrics.advance_width;
        });
        prev = Some(ch);
    }

    if min_x <= max_x && min_y <= max_y {
        Some((min_x, min_y, max_x, max_y))
    } else if pen_x > 0.0 {
        Some((0, 0, pen_x.ceil() as i32, lm.line_height.ceil() as i32))
    } else {
        None
    }
}

/// Blit glyphs directly onto `canvas` at top-left `(x, y)` with a flat colour.
pub fn blit_glyphs_solid(
    canvas: &mut Canvas,
    x: f32,
    y: f32,
    text: &str,
    weight: FontWeight,
    size_px: u32,
    rgba: Rgba,
) {
    if text.is_empty() || rgba[3] == 0 {
        return;
    }
    let sz = size_px.max(8) as f32;
    let font = get_font(weight);
    let lm = line_metrics(weight, size_px);
    let baseline_y = y + lm.ascent;
    let mut pen_x = x;
    let cw = canvas.width as i32;
    let ch_h = canvas.height as i32;
    let stride = canvas.width as usize;
    let base_a = rgba[3] as u32;
    let mut prev: Option<char> = None;

    for ch in text.chars() {
        if ch == '\r' || ch == '\n' {
            continue;
        }
        if let Some(p) = prev {
            if let Some(k) = font.horizontal_kern(p, ch, sz) {
                pen_x += k;
            }
        }
        with_glyph(weight, size_px, ch, |g| {
            let gw = g.metrics.width as i32;
            let gh = g.metrics.height as i32;
            if gw > 0 && gh > 0 {
                let gx0 = (pen_x + g.metrics.xmin as f32).round() as i32;
                let gy0 = (baseline_y - (g.metrics.ymin + gh) as f32).round() as i32;

                let x_start = 0.max(-gx0);
                let y_start = 0.max(-gy0);
                let x_end = gw.min(cw - gx0);
                let y_end = gh.min(ch_h - gy0);

                if x_start < x_end && y_start < y_end {
                    for ly in y_start..y_end {
                        let dy = (gy0 + ly) as usize;
                        let brow = (ly as usize) * (gw as usize);
                        for lx in x_start..x_end {
                            let cov = g.bitmap[brow + lx as usize] as u32;
                            if cov > 0 {
                                let a = div255(base_a * cov) as u8;
                                if a > 0 {
                                    let dx = (gx0 + lx) as usize;
                                    let idx = (dy * stride + dx) * 4;
                                    blend_pixel(
                                        &mut canvas.data[idx..idx + 4],
                                        [rgba[0], rgba[1], rgba[2], a],
                                    );
                                }
                            }
                        }
                    }
                }
            }
            pen_x += g.metrics.advance_width;
        });
        prev = Some(ch);
    }
}

/// Blit glyphs with a sub-pixel left-to-right colour wipe (`split_screen_x`).
fn blit_glyphs_split(
    canvas: &mut Canvas,
    x: f32,
    y: f32,
    text: &str,
    weight: FontWeight,
    size_px: u32,
    col_sung: Rgba,
    col_base: Rgba,
    split_screen_x: f32,
) {
    if text.is_empty() {
        return;
    }
    let sz = size_px.max(8) as f32;
    let font = get_font(weight);
    let lm = line_metrics(weight, size_px);
    let baseline_y = y + lm.ascent;
    let mut pen_x = x;
    let cw = canvas.width as i32;
    let ch_h = canvas.height as i32;
    let stride = canvas.width as usize;
    let mut prev: Option<char> = None;

    for ch in text.chars() {
        if ch == '\r' || ch == '\n' {
            continue;
        }
        if let Some(p) = prev {
            if let Some(k) = font.horizontal_kern(p, ch, sz) {
                pen_x += k;
            }
        }
        with_glyph(weight, size_px, ch, |g| {
            let gw = g.metrics.width as i32;
            let gh = g.metrics.height as i32;
            if gw > 0 && gh > 0 {
                let gx0 = (pen_x + g.metrics.xmin as f32).round() as i32;
                let gy0 = (baseline_y - (g.metrics.ymin + gh) as f32).round() as i32;

                let x_start = 0.max(-gx0);
                let y_start = 0.max(-gy0);
                let x_end = gw.min(cw - gx0);
                let y_end = gh.min(ch_h - gy0);

                if x_start < x_end && y_start < y_end {
                    for ly in y_start..y_end {
                        let dy = (gy0 + ly) as usize;
                        let brow = (ly as usize) * (gw as usize);
                        for lx in x_start..x_end {
                            let cov = g.bitmap[brow + lx as usize] as u32;
                            if cov > 0 {
                                let sx = (gx0 + lx) as f32 + 0.5;
                                // Smooth 1px anti-aliased wipe edge
                                let sung_mix = (split_screen_x - (sx - 0.5)).clamp(0.0, 1.0) as f64;
                                let rgb = if sung_mix >= 1.0 {
                                    [col_sung[0], col_sung[1], col_sung[2]]
                                } else if sung_mix <= 0.0 {
                                    [col_base[0], col_base[1], col_base[2]]
                                } else {
                                    lerp_color(
                                        [col_base[0], col_base[1], col_base[2]],
                                        [col_sung[0], col_sung[1], col_sung[2]],
                                        sung_mix,
                                    )
                                };
                                let a_src = lerp(col_base[3] as f64, col_sung[3] as f64, sung_mix) as u32;
                                let a = div255(a_src * cov) as u8;
                                if a > 0 {
                                    let dx = (gx0 + lx) as usize;
                                    let idx = (dy * stride + dx) * 4;
                                    blend_pixel(&mut canvas.data[idx..idx + 4], [rgb[0], rgb[1], rgb[2], a]);
                                }
                            }
                        }
                    }
                }
            }
            pen_x += g.metrics.advance_width;
        });
        prev = Some(ch);
    }
}

#[inline]
fn lerp(a: f64, b: f64, t: f64) -> f64 {
    a + (b - a) * clamp(t, 0.0, 1.0)
}

/// Unified text drawing with drop-shadow (hard in opaque/greenscreen, blurred in transparent).
#[allow(clippy::too_many_arguments)]
pub fn draw_text(
    canvas: &mut Canvas,
    x: f32,
    y: f32,
    text: &str,
    weight: FontWeight,
    size_px: u32,
    fill: Color,
    alpha_mul: f64,
    transparent: bool,
    greenscreen: bool,
    shadow_blur: u32,
    shadow_offset: i32,
) {
    let alpha_mul = clamp(alpha_mul, 0.0, 1.0);
    if text.is_empty() || alpha_mul <= 0.0 {
        return;
    }

    if !transparent {
        if shadow_offset != 0 {
            let sh_rgb = if greenscreen {
                [0, 0, 0]
            } else {
                lerp_color(BG_COLOR, [0, 0, 0], alpha_mul)
            };
            blit_glyphs_solid(
                canvas,
                x + shadow_offset as f32,
                y + shadow_offset as f32,
                text,
                weight,
                size_px,
                [sh_rgb[0], sh_rgb[1], sh_rgb[2], 255],
            );
        }
        let col = if greenscreen {
            [
                (fill[0] as f64 * alpha_mul) as u8,
                (fill[1] as f64 * alpha_mul) as u8,
                (fill[2] as f64 * alpha_mul) as u8,
            ]
        } else {
            lerp_color(BG_COLOR, fill, alpha_mul)
        };
        blit_glyphs_solid(canvas, x, y, text, weight, size_px, [col[0], col[1], col[2], 255]);
        return;
    }

    // Transparent RGBA mode: blurred shadow on a tight tile
    let Some((ibx0, iby0, ibx1, iby1)) = text_ink_bounds(text, weight, size_px) else {
        return;
    };
    let pad = (shadow_blur as i32) * 2 + shadow_offset.abs() + 4;
    let ix = x.round() as i32;
    let iy = y.round() as i32;
    let bx0 = (ix + ibx0 - pad).max(0);
    let by0 = (iy + iby0 - pad).max(0);
    let bx1 = (ix + ibx1 + pad).min(canvas.width as i32);
    let by1 = (iy + iby1 + pad).min(canvas.height as i32);
    let bw = bx1 - bx0;
    let bh = by1 - by0;
    if bw <= 0 || bh <= 0 {
        return;
    }

    let lx = x - bx0 as f32;
    let ly = y - by0 as f32;
    let mut tile = Canvas::new(bw as u32, bh as u32, [0, 0, 0, 0]);
    blit_glyphs_solid(
        &mut tile,
        lx + shadow_offset as f32,
        ly + shadow_offset as f32,
        text,
        weight,
        size_px,
        [0, 0, 0, (200.0 * alpha_mul) as u8],
    );
    if shadow_blur > 0 {
        tile.gaussian_blur(shadow_blur);
    }
    blit_glyphs_solid(
        &mut tile,
        lx,
        ly,
        text,
        weight,
        size_px,
        [fill[0], fill[1], fill[2], (255.0 * alpha_mul) as u8],
    );
    canvas.alpha_composite(&tile, bx0, by0);
}

/// Single-pass left-to-right karaoke wipe (zero full-frame buffers!).
#[allow(clippy::too_many_arguments)]
pub fn draw_wipe(
    canvas: &mut Canvas,
    x: f32,
    y: f32,
    text: &str,
    weight: FontWeight,
    size_px: u32,
    fill_base: Color,
    fill_sung: Color,
    wipe_frac: f64,
    alpha_mul: f64,
    transparent: bool,
    greenscreen: bool,
    shadow_blur: u32,
    shadow_offset: i32,
) {
    let alpha_mul = clamp(alpha_mul, 0.0, 1.0);
    if text.is_empty() || alpha_mul <= 0.0 {
        return;
    }
    if wipe_frac <= 0.0 {
        draw_text(
            canvas, x, y, text, weight, size_px, fill_base, alpha_mul,
            transparent, greenscreen, shadow_blur, shadow_offset,
        );
        return;
    }
    if wipe_frac >= 1.0 {
        draw_text(
            canvas, x, y, text, weight, size_px, fill_sung, alpha_mul,
            transparent, greenscreen, shadow_blur, shadow_offset,
        );
        return;
    }

    let Some((ibx0, iby0, ibx1, iby1)) = text_ink_bounds(text, weight, size_px) else {
        return;
    };
    let ink_w = (ibx1 - ibx0).max(1) as f32;
    let wipe_px = ink_w * clamp(wipe_frac, 0.0, 1.0) as f32;

    if !transparent {
        let sh_rgb = if greenscreen {
            [0, 0, 0]
        } else {
            lerp_color(BG_COLOR, [0, 0, 0], alpha_mul)
        };
        blit_glyphs_solid(
            canvas,
            x + shadow_offset as f32,
            y + shadow_offset as f32,
            text,
            weight,
            size_px,
            [sh_rgb[0], sh_rgb[1], sh_rgb[2], 255],
        );
        let c_base = if greenscreen {
            [
                (fill_base[0] as f64 * alpha_mul) as u8,
                (fill_base[1] as f64 * alpha_mul) as u8,
                (fill_base[2] as f64 * alpha_mul) as u8,
            ]
        } else {
            lerp_color(BG_COLOR, fill_base, alpha_mul)
        };
        let c_sung = if greenscreen {
            [
                (fill_sung[0] as f64 * alpha_mul) as u8,
                (fill_sung[1] as f64 * alpha_mul) as u8,
                (fill_sung[2] as f64 * alpha_mul) as u8,
            ]
        } else {
            lerp_color(BG_COLOR, fill_sung, alpha_mul)
        };
        let split_x = x + ibx0 as f32 + wipe_px;
        blit_glyphs_split(
            canvas,
            x,
            y,
            text,
            weight,
            size_px,
            [c_sung[0], c_sung[1], c_sung[2], 255],
            [c_base[0], c_base[1], c_base[2], 255],
            split_x,
        );
        return;
    }

    let pad = (shadow_blur as i32) * 2 + shadow_offset.abs() + 4;
    let ix = x.round() as i32;
    let iy = y.round() as i32;
    let bx0 = (ix + ibx0 - pad).max(0);
    let by0 = (iy + iby0 - pad).max(0);
    let bx1 = (ix + ibx1 + pad).min(canvas.width as i32);
    let by1 = (iy + iby1 + pad).min(canvas.height as i32);
    let bw = bx1 - bx0;
    let bh = by1 - by0;
    if bw <= 0 || bh <= 0 {
        return;
    }

    let lx = x - bx0 as f32;
    let ly = y - by0 as f32;
    let mut tile = Canvas::new(bw as u32, bh as u32, [0, 0, 0, 0]);
    blit_glyphs_solid(
        &mut tile,
        lx + shadow_offset as f32,
        ly + shadow_offset as f32,
        text,
        weight,
        size_px,
        [0, 0, 0, (180.0 * alpha_mul) as u8],
    );
    if shadow_blur > 0 {
        tile.gaussian_blur(shadow_blur);
    }

    let a_val = (255.0 * alpha_mul) as u8;
    let split_local_x = lx + ibx0 as f32 + wipe_px;
    blit_glyphs_split(
        &mut tile,
        lx,
        ly,
        text,
        weight,
        size_px,
        [fill_sung[0], fill_sung[1], fill_sung[2], a_val],
        [fill_base[0], fill_base[1], fill_base[2], a_val],
        split_local_x,
    );
    canvas.alpha_composite(&tile, bx0, by0);
}
