use std::cell::RefCell;
use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, OnceLock};

use fontdue::{Font, FontSettings, Metrics};
use swash::scale::{Render, ScaleContext, Source};
use swash::{tag_from_bytes, FontRef, NormalizedCoord};

use crate::canvas::{
    blend_pixel, clamp, div255, lerp, lerp_color, Canvas, Color, Rgba, BG_COLOR,
};

static BOLD_TTF: &[u8] = include_bytes!("../ZenKakuGothicNew-Bold.ttf");
static REGULAR_TTF: &[u8] = include_bytes!("../ZenKakuGothicNew-Regular.ttf");

/// Regular/bold endpoints of the weight animation, as `wght` axis values.
const WGHT_REGULAR: f32 = 400.0;
const WGHT_BOLD: f32 = 700.0;
/// Weights below this resolve to the regular slot, above it to the bold slot.
const WEIGHT_SPLIT: u16 = 550;

/// A font weight expressed as an OpenType `wght` axis value (1..=1000).
/// `FontWeight::Regular` and `FontWeight::Bold` are the static endpoints;
/// any value in between renders a proportional instance of a variable font.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct FontWeight(u16);

#[allow(non_upper_case_globals)]
impl FontWeight {
    pub const Regular: FontWeight = FontWeight(WGHT_REGULAR as u16);
    pub const Bold: FontWeight = FontWeight(WGHT_BOLD as u16);

    /// Build a weight from a `wght` design value, clamped to 1..=1000.
    pub fn from_wght(w: f32) -> Self {
        FontWeight(w.round().clamp(1.0, 1000.0) as u16)
    }

    /// The `wght` design value of this weight.
    pub fn wght(self) -> u16 {
        self.0
    }

    /// Interpolate between two weights (`t` clamped to 0..=1).
    pub fn lerp(a: FontWeight, b: FontWeight, t: f64) -> Self {
        let t = t.clamp(0.0, 1.0);
        FontWeight::from_wght((a.0 as f64 + (b.0 as f64 - a.0 as f64) * t) as f32)
    }
}

/// The `wght` axis range of a variable font.
#[derive(Copy, Clone, Debug)]
struct WghtAxis {
    min: f32,
    max: f32,
    default: f32,
}

/// A font face rendered by swash (variable fonts, plus static fonts that
/// fontdue cannot parse, e.g. CFF2).
struct SwashFace {
    data: Vec<u8>,
    wght: Option<WghtAxis>,
}

/// One of the two loaded font slots.
#[derive(Clone)]
enum Face {
    /// Classic static face rendered with fontdue.
    Static(Font),
    /// swash-rendered face; takes precedence whenever present.
    Swash(Arc<SwashFace>),
}

struct FontPair {
    regular: Face,
    bold: Face,
}

static FONTS: OnceLock<FontPair> = OnceLock::new();

/// Detect the `wght` variation axis of a font file, if any.
fn wght_axis(data: &[u8]) -> Option<WghtAxis> {
    let font = FontRef::from_index(data, 0)?;
    let axis = font
        .variations()
        .find_by_tag(tag_from_bytes(b"wght"))?;
    Some(WghtAxis {
        min: axis.min_value(),
        max: axis.max_value(),
        default: axis.default_value(),
    })
}

impl Face {
    /// Build a face from raw font bytes. Variable fonts (with a `wght` axis)
    /// are rendered through swash so any weight can be instantiated; static
    /// fonts keep the classic fontdue rasteriser.
    fn from_bytes(bytes: &[u8], label: &str) -> Result<Face, String> {
        if let Some(wght) = wght_axis(bytes) {
            return Ok(Face::Swash(Arc::new(SwashFace {
                data: bytes.to_vec(),
                wght: Some(wght),
            })));
        }
        match Font::from_bytes(bytes, FontSettings::default()) {
            Ok(font) => Ok(Face::Static(font)),
            Err(e) => {
                // e.g. CFF2 static fonts: fontdue can't parse them, but swash can.
                if FontRef::from_index(bytes, 0).is_some() {
                    Ok(Face::Swash(Arc::new(SwashFace {
                        data: bytes.to_vec(),
                        wght: None,
                    })))
                } else {
                    Err(format!("cannot parse {} font: {}", label, e))
                }
            }
        }
    }
}

/// Initialise fonts from external .ttf/.otf files. Call once before rendering.
/// If `regular` or `bold` is `None`, the embedded default is used for that weight.
/// When only `regular` is given and it is a variable font, it also provides the
/// bold slot so lyric weights can be animated smoothly.
/// Returns `Err` if a file can't be read or parsed.
pub fn init_fonts(
    regular: Option<&Path>,
    bold: Option<&Path>,
) -> Result<(), String> {
    let read = |path: &Path, label: &str| -> Result<Vec<u8>, String> {
        std::fs::read(path)
            .map_err(|e| format!("cannot read {} font '{}': {}", label, path.display(), e))
    };

    let reg_bytes: Vec<u8> = match regular {
        Some(p) => read(p, "regular")?,
        None => REGULAR_TTF.to_vec(),
    };
    let reg_is_variable = wght_axis(&reg_bytes).is_some();
    let bld_bytes: Vec<u8> = match bold {
        Some(p) => read(p, "bold")?,
        // A lone variable font provides both the regular and the bold slot.
        None if regular.is_some() && reg_is_variable => reg_bytes.clone(),
        None => BOLD_TTF.to_vec(),
    };

    let reg_face = Face::from_bytes(&reg_bytes, "regular")?;
    // Identical bytes → share the face (and its Arc) so weights can animate.
    let bld_face = if bld_bytes == reg_bytes {
        reg_face.clone()
    } else {
        Face::from_bytes(&bld_bytes, "bold")?
    };

    FONTS.set(FontPair {
        regular: reg_face,
        bold: bld_face,
    })
    .map_err(|_| "fonts already initialised".to_owned())?;

    if let Some((r, b)) = animation_endpoints() {
        println!(
            "  variable font: lyric weights animate wght {} → {}",
            r, b
        );
    }
    Ok(())
}

fn fonts() -> &'static FontPair {
    FONTS.get_or_init(|| FontPair {
        regular: Face::Static(
            Font::from_bytes(REGULAR_TTF, FontSettings::default())
                .expect("Failed to parse embedded ZenKakuGothicNew-Regular.ttf"),
        ),
        bold: Face::Static(
            Font::from_bytes(BOLD_TTF, FontSettings::default())
                .expect("Failed to parse embedded ZenKakuGothicNew-Bold.ttf"),
        ),
    })
}

/// Resolved rendering source for a weight: the regular slot below
/// `WEIGHT_SPLIT`, the bold slot at or above it.
enum GlyphSource<'a> {
    Static(&'a Font),
    Swash(&'a SwashFace, u16),
}

fn source_for(weight: FontWeight) -> GlyphSource<'static> {
    let pair = fonts();
    let face = if weight.0 < WEIGHT_SPLIT {
        &pair.regular
    } else {
        &pair.bold
    };
    match face {
        Face::Static(font) => GlyphSource::Static(font),
        Face::Swash(face) => GlyphSource::Swash(face, weight.0),
    }
}

/// Normalized variation coordinates for a swash face at the given weight.
fn normalized_coords(face: &SwashFace, wght: u16) -> Vec<NormalizedCoord> {
    let font = FontRef::from_index(&face.data, 0)
        .expect("font bytes no longer parse");
    let settings = face.wght.map(|axis| {
        let w = if axis.min <= axis.max {
            (wght as f32).clamp(axis.min, axis.max)
        } else {
            axis.default
        };
        ("wght", w)
    });
    match settings {
        Some(s) => font.variations().normalized_coords([s]).collect(),
        None => font
            .variations()
            .normalized_coords(std::iter::empty::<(&str, f32)>())
            .collect(),
    }
}

/// The `(regular, bold)` `wght` endpoints of the weight animation when both
/// font slots share one variable font with a `wght` axis; `None` otherwise
/// (static pair → weights snap between the two faces, as before).
fn animation_endpoints() -> Option<(u16, u16)> {
    pair_endpoints(fonts())
}

fn pair_endpoints(pair: &FontPair) -> Option<(u16, u16)> {
    let Face::Swash(reg) = &pair.regular else { return None };
    let Face::Swash(bld) = &pair.bold else { return None };
    if !Arc::ptr_eq(reg, bld) {
        return None;
    }
    let axis = reg.wght?;
    let clamp_w = |w: f32| {
        let w = if axis.min <= axis.max {
            w.clamp(axis.min, axis.max)
        } else {
            axis.default
        };
        FontWeight::from_wght(w).wght()
    };
    Some((clamp_w(WGHT_REGULAR), clamp_w(WGHT_BOLD)))
}

/// Weight for a lyric line whose activation animation is at progress `t`
/// (0 = idle/regular … 1 = fully active/bold).
///
/// With a variable font this is a proportional instance on the `wght` axis;
/// with a static pair it snaps to the bold face when `discrete_bold` is set
/// (active/shrinking lines) and to the regular face otherwise.
pub fn animated_weight(t: f64, discrete_bold: bool) -> FontWeight {
    match animation_endpoints() {
        Some((r, b)) => FontWeight::lerp(FontWeight(r), FontWeight(b), t),
        None if discrete_bold => FontWeight::Bold,
        None => FontWeight::Regular,
    }
}

struct CachedGlyph {
    metrics: Metrics,
    bitmap: Vec<u8>,
}

thread_local! {
    static GLYPH_CACHE: RefCell<HashMap<(FontWeight, u32, char), CachedGlyph>> =
        RefCell::new(HashMap::with_capacity(512));
    static SCALE_CTX: RefCell<ScaleContext> = RefCell::new(ScaleContext::new());
}

/// Rasterise one glyph of a swash face at the given `wght` design value.
fn rasterize_swatch(face: &SwashFace, wght: u16, sz: u32, ch: char) -> (Metrics, Vec<u8>) {
    let font = FontRef::from_index(&face.data, 0)
        .expect("font bytes no longer parse");
    let gid = font.charmap().map(ch);
    let coords = normalized_coords(face, wght);
    let setting = face.wght.map(|axis| {
        let w = if axis.min <= axis.max {
            (wght as f32).clamp(axis.min, axis.max)
        } else {
            axis.default
        };
        ("wght", w)
    });

    let advance = font
        .glyph_metrics(&coords)
        .scale(sz as f32)
        .advance_width(gid);

    let image = SCALE_CTX.with(|cell| {
        let mut ctx = cell.borrow_mut();
        let mut builder = ctx.builder(font).size(sz as f32);
        if let Some(s) = setting {
            builder = builder.variations([s]);
        }
        let mut scaler = builder.build();
        Render::new(&[Source::Outline]).render(&mut scaler, gid)
    });

    match image {
        Some(img) => (
            Metrics {
                xmin: img.placement.left,
                // fontdue convention: ymin is the bottom edge above the baseline.
                ymin: img.placement.top - img.placement.height as i32,
                width: img.placement.width as usize,
                height: img.placement.height as usize,
                advance_width: advance,
                advance_height: 0.0,
                bounds: Default::default(),
            },
            img.data,
        ),
        None => (
            Metrics {
                xmin: 0,
                ymin: 0,
                width: 0,
                height: 0,
                advance_width: advance,
                advance_height: 0.0,
                bounds: Default::default(),
            },
            Vec::new(),
        ),
    }
}

fn rasterize_glyph(weight: FontWeight, size_px: u32, ch: char) -> CachedGlyph {
    let sz = size_px.max(8);
    match source_for(weight) {
        GlyphSource::Static(font) => {
            let (metrics, bitmap) = font.rasterize(ch, sz as f32);
            CachedGlyph { metrics, bitmap }
        }
        GlyphSource::Swash(face, wght) => {
            let (metrics, bitmap) = rasterize_swatch(face, wght, sz, ch);
            CachedGlyph { metrics, bitmap }
        }
    }
}

fn with_glyph<R>(weight: FontWeight, size_px: u32, ch: char, f: impl FnOnce(&CachedGlyph) -> R) -> R {
    let sz = size_px.max(8);
    GLYPH_CACHE.with(|cell| {
        let mut map = cell.borrow_mut();
        let entry = map
            .entry((weight, sz, ch))
            .or_insert_with(|| rasterize_glyph(weight, sz, ch));
        f(entry)
    })
}

/// Kerning between two characters, when the resolved face provides it.
/// (The swash path has no legacy `kern` table lookup — pair kerning lives in
/// GPOS there — so variable fonts are laid out without kerning.)
fn kern(weight: FontWeight, left: char, right: char, sz: f32) -> Option<f32> {
    match source_for(weight) {
        GlyphSource::Static(font) => font.horizontal_kern(left, right, sz),
        GlyphSource::Swash(..) => None,
    }
}

#[derive(Copy, Clone, Debug)]
pub struct LineBox {
    pub ascent: f32,
    pub line_height: f32,
}

fn line_box(weight: FontWeight, size_px: u32) -> LineBox {
    let sz = size_px.max(8) as f32;
    match source_for(weight) {
        GlyphSource::Static(font) => {
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
        GlyphSource::Swash(face, wght) => {
            let font = FontRef::from_index(&face.data, 0)
                .expect("font bytes no longer parse");
            let coords = normalized_coords(face, wght);
            // swash reports a positive descent (fontdue reports it negative).
            let m = font.metrics(&coords).scale(sz);
            let h = (m.ascent + m.descent).max(sz * 0.9);
            LineBox {
                ascent: m.ascent,
                line_height: h,
            }
        }
    }
}

pub fn line_metrics(weight: FontWeight, size_px: u32) -> LineBox {
    line_box(weight, size_px)
}

/// Measure horizontal advance width of `text` in pixels (includes spaces and kerning).
pub fn text_width(text: &str, weight: FontWeight, size_px: u32) -> f32 {
    if text.is_empty() {
        return 0.0;
    }
    let sz = size_px.max(8) as f32;
    let mut w = 0.0f32;
    let mut prev: Option<char> = None;
    for ch in text.chars() {
        if ch == '\r' || ch == '\n' {
            continue;
        }
        if let Some(p) = prev {
            if let Some(k) = kern(weight, p, ch, sz) {
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
            if let Some(k) = kern(weight, p, ch, sz) {
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
            if let Some(k) = kern(weight, p, ch, sz) {
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

/// Blit glyphs with a soft left-to-right karaoke sweep: the sung/base
/// boundary is a feathered gradient band (`feather` px wide) rather than a
/// hard 1px edge, so the highlight fades across the word.
fn blit_glyphs_sweep(
    canvas: &mut Canvas,
    x: f32,
    y: f32,
    text: &str,
    weight: FontWeight,
    size_px: u32,
    col_sung: Rgba,
    col_base: Rgba,
    edge_x: f32,
    feather: f32,
) {
    if text.is_empty() {
        return;
    }
    let sz = size_px.max(8) as f32;
    let lm = line_metrics(weight, size_px);
    let baseline_y = y + lm.ascent;
    let mut pen_x = x;
    let cw = canvas.width as i32;
    let ch_h = canvas.height as i32;
    let stride = canvas.width as usize;
    let feather = feather.max(1.0);
    let mut prev: Option<char> = None;

    for ch in text.chars() {
        if ch == '\r' || ch == '\n' {
            continue;
        }
        if let Some(p) = prev {
            if let Some(k) = kern(weight, p, ch, sz) {
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
                                // Smoothstep across the feather band: fully
                                // sung behind it, fully base ahead of it.
                                let u = ((edge_x - sx) / feather).clamp(0.0, 1.0) as f64;
                                let sung_mix = u * u * (3.0 - 2.0 * u);
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
                                let a_src =
                                    lerp(col_base[3] as f64, col_sung[3] as f64, sung_mix) as u32;
                                let a = div255(a_src * cov) as u8;
                                if a > 0 {
                                    let dx = (gx0 + lx) as usize;
                                    let idx = (dy * stride + dx) * 4;
                                    blend_pixel(
                                        &mut canvas.data[idx..idx + 4],
                                        [rgb[0], rgb[1], rgb[2], a],
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

/// Karaoke reveal: a soft feathered sweep across the word (zero full-frame
/// buffers — shadow/fill are handled like `draw_text`).
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
            canvas, x, y, text, weight, size_px, fill_base, alpha_mul, transparent, greenscreen,
            shadow_blur, shadow_offset,
        );
        return;
    }
    if wipe_frac >= 1.0 {
        draw_text(
            canvas, x, y, text, weight, size_px, fill_sung, alpha_mul, transparent, greenscreen,
            shadow_blur, shadow_offset,
        );
        return;
    }

    let Some((ibx0, iby0, ibx1, iby1)) = text_ink_bounds(text, weight, size_px) else {
        return;
    };
    let ink_w = (ibx1 - ibx0).max(1) as f32;
    // Gradient band scaled to the word: wide enough to read as a fade
    // rather than a straight sweep line, without smearing short words.
    let feather = (ink_w * 0.35).clamp(36.0, 120.0);
    // Edge starts at the word's left ink and ends one feather past its
    // right edge, so 0 → fully base and 1 → fully sung.
    let edge_x = x + ibx0 as f32 + wipe_frac as f32 * (ink_w + feather);

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
        blit_glyphs_sweep(
            canvas,
            x,
            y,
            text,
            weight,
            size_px,
            [c_sung[0], c_sung[1], c_sung[2], 255],
            [c_base[0], c_base[1], c_base[2], 255],
            edge_x,
            feather,
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
    blit_glyphs_sweep(
        &mut tile,
        lx,
        ly,
        text,
        weight,
        size_px,
        [fill_sung[0], fill_sung[1], fill_sung[2], a_val],
        [fill_base[0], fill_base[1], fill_base[2], a_val],
        edge_x - bx0 as f32,
        feather,
    );
    canvas.alpha_composite(&tile, bx0, by0);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn weight_constants_and_math() {
        assert_eq!(FontWeight::Regular.wght(), 400);
        assert_eq!(FontWeight::Bold.wght(), 700);
        assert_eq!(FontWeight::from_wght(660.4).wght(), 660);
        assert_eq!(FontWeight::from_wght(-50.0).wght(), 1);
        assert_eq!(FontWeight::from_wght(5000.0).wght(), 1000);
        assert_eq!(
            FontWeight::lerp(FontWeight::Regular, FontWeight::Bold, 0.0),
            FontWeight::Regular
        );
        assert_eq!(
            FontWeight::lerp(FontWeight::Regular, FontWeight::Bold, 1.0),
            FontWeight::Bold
        );
        assert_eq!(
            FontWeight::lerp(FontWeight::Regular, FontWeight::Bold, 0.5).wght(),
            550
        );
        assert_eq!(
            FontWeight::lerp(FontWeight::Regular, FontWeight::Bold, -1.0),
            FontWeight::Regular
        );
    }

    #[test]
    fn embedded_pair_disables_weight_animation() {
        let _ = fonts();
        assert_eq!(animation_endpoints(), None);
        // Static fallback: bold for active/shrinking lines, regular otherwise.
        assert_eq!(animated_weight(0.0, true), FontWeight::Bold);
        assert_eq!(animated_weight(1.0, true), FontWeight::Bold);
        assert_eq!(animated_weight(0.5, false), FontWeight::Regular);
    }

    #[test]
    fn static_pair_renders_and_measures() {
        let w = text_width("Hello, world", FontWeight::Regular, 32);
        assert!(w > 0.0);
        assert!(line_metrics(FontWeight::Bold, 32).ascent > 0.0);
        assert!(text_ink_bounds("Test", FontWeight::Bold, 40).is_some());
        // Rasterising goes through the glyph cache without panicking.
        let _ = with_glyph(FontWeight::Regular, 32, 'A', |g| g.bitmap.len());
        let _ = with_glyph(FontWeight::Bold, 32, 'A', |g| g.bitmap.len());
    }

    /// Variable-font pipeline check: uses a system font with a `wght` axis
    /// when one is available (skipped otherwise).
    #[test]
    fn variable_face_rasterizes_at_weights() {
        let candidates = [
            "/system/fonts/Roboto-Regular.ttf",
            "/usr/share/fonts/truetype/roboto/unhinted/Roboto-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ];
        let Some(path) = candidates.iter().find(|p| Path::new(p).is_file()) else {
            eprintln!("skipping variable-font test: no candidate font found");
            return;
        };
        let Ok(bytes) = std::fs::read(path) else {
            eprintln!("skipping variable-font test: cannot read {}", path);
            return;
        };
        let Some(axis) = wght_axis(&bytes) else {
            eprintln!("skipping variable-font test: {} has no wght axis", path);
            return;
        };
        let face = Face::from_bytes(&bytes, "test").expect("font should parse");
        let Face::Swash(sf) = &face else {
            panic!("variable font should produce a swash face");
        };
        assert!(sf.wght.is_some());

        let lo = FontWeight::from_wght(axis.min).wght();
        let hi = FontWeight::from_wght(axis.max).wght();
        let light = rasterize_swatch(sf, lo, 48, 'A');
        let heavy = rasterize_swatch(sf, hi, 48, 'A');
        assert!(light.0.width > 0 && light.0.height > 0);
        assert!(heavy.0.width > 0 && heavy.0.height > 0);

        if axis.max - axis.min >= 100.0 {
            let ink = |g: &(Metrics, Vec<u8>)| g.1.iter().map(|&v| v as u64).sum::<u64>();
            assert!(
                ink(&heavy) > ink(&light),
                "heavier instance should have more ink ({} !<= {})",
                ink(&heavy),
                ink(&light)
            );
            assert!(
                heavy.0.advance_width >= light.0.advance_width - 0.01,
                "advance should not shrink with weight"
            );
        }

        // Space glyph: no ink, but a sane advance.
        let space = rasterize_swatch(sf, FontWeight::Bold.wght(), 32, ' ');
        assert_eq!(space.1.iter().filter(|&&v| v > 0).count(), 0);
        assert!(space.0.advance_width > 0.0);

        // Line metrics are positive at both ends of the axis.
        for w in [lo, hi] {
            let font = FontRef::from_index(&sf.data, 0).unwrap();
            let coords = normalized_coords(sf, w);
            let m = font.metrics(&coords).scale(32.0);
            assert!(m.ascent > 0.0);
        }
    }

    /// The animation endpoints must come from a *shared* variable face.
    #[test]
    fn animation_endpoints_shared_face_only() {
        let static_face = Face::from_bytes(REGULAR_TTF, "regular").unwrap();
        assert!(matches!(static_face, Face::Static(_)));
        assert_eq!(
            pair_endpoints(&FontPair {
                regular: static_face.clone(),
                bold: static_face,
            }),
            None
        );

        // If a system variable font is available: one shared swash face
        // enables the animation, two distinct faces must not.
        let path = "/system/fonts/Roboto-Regular.ttf";
        let Ok(bytes) = std::fs::read(path) else {
            eprintln!("skipping: {} not found", path);
            return;
        };
        let Ok(face) = Face::from_bytes(&bytes, "test") else {
            eprintln!("skipping: {} does not parse", path);
            return;
        };
        let Face::Swash(_) = &face else {
            // No wght axis — nothing to assert.
            return;
        };

        let shared = pair_endpoints(&FontPair {
            regular: face.clone(),
            bold: face.clone(),
        });
        assert_eq!(shared, Some((400, 700)));

        let other = Face::from_bytes(REGULAR_TTF, "bold").unwrap();
        let split = pair_endpoints(&FontPair {
            regular: face,
            bold: other,
        });
        assert_eq!(split, None);
    }
}
