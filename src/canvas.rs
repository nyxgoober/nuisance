use std::fs::File;
use std::io::BufWriter;
use std::path::Path;

pub const WIDTH: u32 = 1920;
pub const HEIGHT: u32 = 1080;
pub const FPS: f64 = 60.0;

pub type Color = [u8; 3];
pub type Rgba = [u8; 4];

pub const BG_COLOR: Color = [8, 8, 12];
pub const GS_COLOR: Color = [0, 255, 0];

#[inline]
pub fn clamp(x: f64, lo: f64, hi: f64) -> f64 {
    if x < lo {
        lo
    } else if x > hi {
        hi
    } else {
        x
    }
}

#[inline]
pub fn ease_out(t: f64) -> f64 {
    let u = 1.0 - clamp(t, 0.0, 1.0);
    1.0 - u * u * u
}

#[inline]
pub fn ease_in_out(t: f64) -> f64 {
    // Cubic ease-in-out: monotonic, stays within [0, 1] — no bounce,
    // overshoot, or rebound.
    let t = clamp(t, 0.0, 1.0);
    if t < 0.5 {
        4.0 * t * t * t
    } else {
        let u = -2.0 * t + 2.0;
        1.0 - u * u * u * 0.5
    }
}

#[inline]
pub fn lerp(a: f64, b: f64, t: f64) -> f64 {
    a + (b - a) * clamp(t, 0.0, 1.0)
}

#[inline]
pub fn lerp_color(ca: Color, cb: Color, t: f64) -> Color {
    let t = clamp(t, 0.0, 1.0);
    [
        (ca[0] as f64 + (cb[0] as f64 - ca[0] as f64) * t) as u8,
        (ca[1] as f64 + (cb[1] as f64 - ca[1] as f64) * t) as u8,
        (ca[2] as f64 + (cb[2] as f64 - ca[2] as f64) * t) as u8,
    ]
}

#[inline]
pub fn lerp_color3(ca: Color, cm: Color, cb: Color, t: f64) -> Color {
    if t <= 0.5 {
        lerp_color(ca, cm, t * 2.0)
    } else {
        lerp_color(cm, cb, (t - 0.5) * 2.0)
    }
}

#[inline]
pub fn div255(x: u32) -> u32 {
    (x + 1 + (x >> 8)) >> 8
}

/// Straight-alpha Porter-Duff `source over destination` pixel blend.
#[inline]
pub fn blend_pixel(dst: &mut [u8], src: Rgba) {
    let sa = src[3] as u32;
    if sa == 0 {
        return;
    }
    if sa == 255 {
        dst[0] = src[0];
        dst[1] = src[1];
        dst[2] = src[2];
        dst[3] = 255;
        return;
    }
    let da = dst[3] as u32;
    if da == 255 {
        let inv = 255 - sa;
        dst[0] = div255(src[0] as u32 * sa + dst[0] as u32 * inv) as u8;
        dst[1] = div255(src[1] as u32 * sa + dst[1] as u32 * inv) as u8;
        dst[2] = div255(src[2] as u32 * sa + dst[2] as u32 * inv) as u8;
        return;
    }
    if da == 0 {
        dst[0] = src[0];
        dst[1] = src[1];
        dst[2] = src[2];
        dst[3] = src[3];
        return;
    }
    let inv = 255 - sa;
    let out_a255 = sa * 255 + da * inv;
    let half = out_a255 >> 1;
    dst[0] = ((src[0] as u32 * sa * 255 + dst[0] as u32 * da * inv + half) / out_a255) as u8;
    dst[1] = ((src[1] as u32 * sa * 255 + dst[1] as u32 * da * inv + half) / out_a255) as u8;
    dst[2] = ((src[2] as u32 * sa * 255 + dst[2] as u32 * da * inv + half) / out_a255) as u8;
    dst[3] = div255(out_a255) as u8;
}

#[derive(Clone)]
pub struct Canvas {
    pub width: u32,
    pub height: u32,
    pub data: Vec<u8>,
}

impl Canvas {
    pub fn new(width: u32, height: u32, bg: Rgba) -> Self {
        let n = (width as usize) * (height as usize);
        let mut data = vec![0u8; n * 4];
        if bg != [0, 0, 0, 0] {
            for px in data.chunks_exact_mut(4) {
                px.copy_from_slice(&bg);
            }
        }
        Self {
            width,
            height,
            data,
        }
    }

    pub fn copy_from(&mut self, other: &Canvas) {
        debug_assert_eq!(self.width, other.width);
        debug_assert_eq!(self.height, other.height);
        self.data.copy_from_slice(&other.data);
    }

    pub fn fill(&mut self, bg: Rgba) {
        if bg == [0, 0, 0, 0] {
            self.data.fill(0);
        } else {
            for px in self.data.chunks_exact_mut(4) {
                px.copy_from_slice(&bg);
            }
        }
    }

    /// Inclusive rectangle `[x0, y0, x1, y1]` matching Pillow's `draw.rectangle`.
    pub fn fill_rect(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, col: Rgba) {
        if col[3] == 0 || x1 < x0 || y1 < y0 {
            return;
        }
        let cx0 = x0.max(0) as u32;
        let cy0 = y0.max(0) as u32;
        let cx1 = (x1 + 1).min(self.width as i32) as u32;
        let cy1 = (y1 + 1).min(self.height as i32) as u32;
        if cx0 >= cx1 || cy0 >= cy1 {
            return;
        }
        let w = self.width as usize;
        if col[3] == 255 {
            for y in cy0..cy1 {
                let row_start = ((y as usize) * w + (cx0 as usize)) * 4;
                let row_end = ((y as usize) * w + (cx1 as usize)) * 4;
                for px in self.data[row_start..row_end].chunks_exact_mut(4) {
                    px.copy_from_slice(&col);
                }
            }
        } else {
            for y in cy0..cy1 {
                let row_start = ((y as usize) * w + (cx0 as usize)) * 4;
                let row_end = ((y as usize) * w + (cx1 as usize)) * 4;
                for px in self.data[row_start..row_end].chunks_exact_mut(4) {
                    blend_pixel(px, col);
                }
            }
        }
    }

    pub fn draw_hline(&mut self, x0: i32, x1: i32, y: i32, col: Rgba) {
        self.fill_rect(x0.min(x1), y, x0.max(x1), y, col);
    }

    pub fn draw_vline(&mut self, x: i32, y0: i32, y1: i32, col: Rgba) {
        self.fill_rect(x, y0.min(y1), x, y0.max(y1), col);
    }

    /// Anti-aliased rounded rectangle `[x0, y0, x1, y1]` (inclusive pixel bounds).
    pub fn fill_rounded_rect(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, radius: i32, col: Rgba) {
        if col[3] == 0 || x1 < x0 || y1 < y0 {
            return;
        }
        let rw = x1 - x0 + 1;
        let rh = y1 - y0 + 1;
        let r = radius.max(0).min(rw / 2).min(rh / 2) as f32;
        if r <= 0.5 {
            self.fill_rect(x0, y0, x1, y1, col);
            return;
        }

        let fx0 = x0 as f32;
        let fy0 = y0 as f32;
        let fx1 = (x1 + 1) as f32;
        let fy1 = (y1 + 1) as f32;
        let cx = 0.5 * (fx0 + fx1);
        let cy = 0.5 * (fy0 + fy1);
        let hx = 0.5 * (fx1 - fx0) - r;
        let hy = 0.5 * (fy1 - fy0) - r;

        let ix0 = x0.max(0);
        let iy0 = y0.max(0);
        let ix1 = (x1 + 1).min(self.width as i32);
        let iy1 = (y1 + 1).min(self.height as i32);
        if ix0 >= ix1 || iy0 >= iy1 {
            return;
        }

        let w = self.width as usize;
        let base_a = col[3] as f32;
        let inner_y0 = (y0 + radius + 1).max(iy0);
        let inner_y1 = (y1 - radius).min(iy1);

        for py in iy0..iy1 {
            if py >= inner_y0 && py < inner_y1 {
                // Full-opacity interior row
                let row_start = ((py as usize) * w + (ix0 as usize)) * 4;
                let row_end = ((py as usize) * w + (ix1 as usize)) * 4;
                if col[3] == 255 {
                    for px_slice in self.data[row_start..row_end].chunks_exact_mut(4) {
                        px_slice.copy_from_slice(&col);
                    }
                } else {
                    for px_slice in self.data[row_start..row_end].chunks_exact_mut(4) {
                        blend_pixel(px_slice, col);
                    }
                }
                continue;
            }

            let qy = (py as f32 + 0.5 - cy).abs() - hy;
            for px in ix0..ix1 {
                let qx = (px as f32 + 0.5 - cx).abs() - hx;
                let dist = if qx <= 0.0 && qy <= 0.0 {
                    qx.max(qy) - r
                } else {
                    let mx = qx.max(0.0);
                    let my = qy.max(0.0);
                    (mx * mx + my * my).sqrt() - r
                };
                let cov = (0.5 - dist).clamp(0.0, 1.0);
                if cov > 0.0 {
                    let a = (base_a * cov + 0.5) as u8;
                    if a > 0 {
                        let idx = ((py as usize) * w + (px as usize)) * 4;
                        blend_pixel(&mut self.data[idx..idx + 4], [col[0], col[1], col[2], a]);
                    }
                }
            }
        }
    }

    /// Anti-aliased filled ellipse inside `[x0, y0, x1, y1]`.
    pub fn fill_ellipse(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, col: Rgba) {
        if col[3] == 0 || x1 < x0 || y1 < y0 {
            return;
        }
        let fx0 = x0 as f32;
        let fy0 = y0 as f32;
        let fx1 = (x1 + 1) as f32;
        let fy1 = (y1 + 1) as f32;
        let cx = 0.5 * (fx0 + fx1);
        let cy = 0.5 * (fy0 + fy1);
        let rx = (0.5 * (fx1 - fx0)).max(0.5);
        let ry = (0.5 * (fy1 - fy0)).max(0.5);

        let ix0 = (x0 - 1).max(0);
        let iy0 = (y0 - 1).max(0);
        let ix1 = (x1 + 2).min(self.width as i32);
        let iy1 = (y1 + 2).min(self.height as i32);
        if ix0 >= ix1 || iy0 >= iy1 {
            return;
        }

        let w = self.width as usize;
        let base_a = col[3] as f32;
        let avg_r = 0.5 * (rx + ry);

        for py in iy0..iy1 {
            let ny = (py as f32 + 0.5 - cy) / ry;
            let ny2 = ny * ny;
            for px in ix0..ix1 {
                let nx = (px as f32 + 0.5 - cx) / rx;
                let norm_d = (nx * nx + ny2).sqrt();
                let approx_px_dist = (norm_d - 1.0) * avg_r;
                let cov = (0.5 - approx_px_dist).clamp(0.0, 1.0);
                if cov > 0.0 {
                    let a = (base_a * cov + 0.5) as u8;
                    if a > 0 {
                        let idx = ((py as usize) * w + (px as usize)) * 4;
                        blend_pixel(&mut self.data[idx..idx + 4], [col[0], col[1], col[2], a]);
                    }
                }
            }
        }
    }

    /// Anti-aliased thick polyline without vertex double-blending artifacts.
    pub fn draw_polyline(
        &mut self,
        pts: &[(f32, f32)],
        stroke_width: f32,
        col: Rgba,
        mask: &mut Vec<u8>,
    ) {
        if pts.len() < 2 || col[3] == 0 || stroke_width <= 0.0 {
            return;
        }
        let hw = stroke_width * 0.5;
        let mut min_x = f32::INFINITY;
        let mut min_y = f32::INFINITY;
        let mut max_x = f32::NEG_INFINITY;
        let mut max_y = f32::NEG_INFINITY;
        for &(x, y) in pts {
            if x < min_x { min_x = x; }
            if y < min_y { min_y = y; }
            if x > max_x { max_x = x; }
            if y > max_y { max_y = y; }
        }
        let bx0 = ((min_x - hw - 2.0).floor() as i32).max(0);
        let by0 = ((min_y - hw - 2.0).floor() as i32).max(0);
        let bx1 = ((max_x + hw + 2.0).ceil() as i32).min(self.width as i32);
        let by1 = ((max_y + hw + 2.0).ceil() as i32).min(self.height as i32);
        if bx0 >= bx1 || by0 >= by1 {
            return;
        }

        let bw = (bx1 - bx0) as usize;
        let bh = (by1 - by0) as usize;
        mask.clear();
        mask.resize(bw * bh, 0);

        let r_bound = hw + 0.5;
        let r_bound_sq = r_bound * r_bound;

        for seg in pts.windows(2) {
            let (ax, ay) = seg[0];
            let (bx, by) = seg[1];
            let sx0 = ((ax.min(bx) - hw - 1.0).floor() as i32).max(bx0);
            let sy0 = ((ay.min(by) - hw - 1.0).floor() as i32).max(by0);
            let sx1 = ((ax.max(bx) + hw + 1.5).ceil() as i32).min(bx1);
            let sy1 = ((ay.max(by) + hw + 1.5).ceil() as i32).min(by1);
            if sx0 >= sx1 || sy0 >= sy1 {
                continue;
            }

            let dx = bx - ax;
            let dy = by - ay;
            let len_sq = dx * dx + dy * dy;
            let inv_len_sq = if len_sq > 1e-6 { 1.0 / len_sq } else { 0.0 };

            for py in sy0..sy1 {
                let pfy = py as f32 + 0.5;
                let mrow = (py - by0) as usize * bw;
                for px in sx0..sx1 {
                    let pfx = px as f32 + 0.5;
                    let t = (((pfx - ax) * dx + (pfy - ay) * dy) * inv_len_sq).clamp(0.0, 1.0);
                    let ex = pfx - (ax + t * dx);
                    let ey = pfy - (ay + t * dy);
                    let d2 = ex * ex + ey * ey;
                    if d2 < r_bound_sq {
                        let cov = (r_bound - d2.sqrt()).clamp(0.0, 1.0);
                        let cv = (cov * 255.0 + 0.5) as u8;
                        let cell = &mut mask[mrow + (px - bx0) as usize];
                        if cv > *cell {
                            *cell = cv;
                        }
                    }
                }
            }
        }

        let w = self.width as usize;
        let base_a = col[3] as u32;
        for ly in 0..bh {
            let dst_y = (by0 as usize) + ly;
            let mrow = ly * bw;
            for lx in 0..bw {
                let cov = mask[mrow + lx] as u32;
                if cov > 0 {
                    let a = div255(base_a * cov) as u8;
                    if a > 0 {
                        let dst_idx = (dst_y * w + (bx0 as usize) + lx) * 4;
                        blend_pixel(&mut self.data[dst_idx..dst_idx + 4], [col[0], col[1], col[2], a]);
                    }
                }
            }
        }
    }

    /// Composite `src` onto `self` at `(dst_x, dst_y)` with automatic bounds clipping.
    pub fn alpha_composite(&mut self, src: &Canvas, dst_x: i32, dst_y: i32) {
        let sx0 = (-dst_x).max(0) as u32;
        let sy0 = (-dst_y).max(0) as u32;
        let dx0 = dst_x.max(0) as u32;
        let dy0 = dst_y.max(0) as u32;
        let w = (src.width.saturating_sub(sx0)).min(self.width.saturating_sub(dx0));
        let h = (src.height.saturating_sub(sy0)).min(self.height.saturating_sub(dy0));
        if w == 0 || h == 0 {
            return;
        }

        let dw = self.width as usize;
        let sw = src.width as usize;
        let span = w as usize;

        for row in 0..(h as usize) {
            let s_off = ((sy0 as usize + row) * sw + sx0 as usize) * 4;
            let d_off = ((dy0 as usize + row) * dw + dx0 as usize) * 4;
            let s_slice = &src.data[s_off..s_off + span * 4];
            let d_slice = &mut self.data[d_off..d_off + span * 4];
            for (d_px, s_px) in d_slice.chunks_exact_mut(4).zip(s_slice.chunks_exact(4)) {
                blend_pixel(d_px, [s_px[0], s_px[1], s_px[2], s_px[3]]);
            }
        }
    }

    /// Fast O(1)-per-pixel 3-pass separable box blur (approximating Gaussian blur of `radius`)
    /// in premultiplied alpha space so glow colours never darken at transparent edges.
    pub fn gaussian_blur(&mut self, radius: u32) {
        let mut tmp = Vec::new();
        self.gaussian_blur_with_scratch(radius, &mut tmp);
    }

    pub fn gaussian_blur_with_scratch(&mut self, radius: u32, tmp: &mut Vec<u8>) {
        if radius == 0 || self.width < 2 || self.height < 2 {
            return;
        }
        let w = self.width as usize;
        let h = self.height as usize;

        for px in self.data.chunks_exact_mut(4) {
            let a = px[3] as u32;
            if a == 0 {
                px[0] = 0;
                px[1] = 0;
                px[2] = 0;
            } else if a < 255 {
                px[0] = div255(px[0] as u32 * a) as u8;
                px[1] = div255(px[1] as u32 * a) as u8;
                px[2] = div255(px[2] as u32 * a) as u8;
            }
        }

        let sigma = radius as f64;
        let w_ideal = ((12.0 * sigma * sigma / 3.0) + 1.0).sqrt();
        let mut r_box = ((w_ideal - 1.0) * 0.5).round() as usize;
        if r_box == 0 {
            r_box = 1;
        }

        tmp.resize(w * h * 4, 0);
        for _ in 0..3 {
            box_blur_h(&self.data, tmp, w, h, r_box);
            box_blur_v(tmp, &mut self.data, w, h, r_box);
        }

        for px in self.data.chunks_exact_mut(4) {
            let a = px[3] as u32;
            if a == 0 {
                px[0] = 0;
                px[1] = 0;
                px[2] = 0;
            } else if a < 255 {
                px[0] = ((px[0] as u32 * 255 + (a >> 1)) / a).min(255) as u8;
                px[1] = ((px[1] as u32 * 255 + (a >> 1)) / a).min(255) as u8;
                px[2] = ((px[2] as u32 * 255 + (a >> 1)) / a).min(255) as u8;
            }
        }
    }

    /// Encode to PNG using fast pure-Rust `fdeflate` compression.
    pub fn save_png(&self, path: &Path, transparent: bool, rgb_scratch: &mut Vec<u8>) -> Result<(), String> {
        let file = File::create(path)
            .map_err(|e| format!("failed to create '{}': {}", path.display(), e))?;
        let writer = BufWriter::with_capacity(128 * 1024, file);
        let mut encoder = png::Encoder::new(writer, self.width, self.height);
        encoder.set_depth(png::BitDepth::Eight);
        encoder.set_compression(png::Compression::Fast);

        if transparent {
            encoder.set_color(png::ColorType::Rgba);
            let mut png_writer = encoder
                .write_header()
                .map_err(|e| format!("PNG header error: {}", e))?;
            png_writer
                .write_image_data(&self.data)
                .map_err(|e| format!("PNG write error: {}", e))?;
        } else {
            encoder.set_color(png::ColorType::Rgb);
            let n = (self.width as usize) * (self.height as usize);
            rgb_scratch.resize(n * 3, 0);
            for (dst, src) in rgb_scratch.chunks_exact_mut(3).zip(self.data.chunks_exact(4)) {
                dst[0] = src[0];
                dst[1] = src[1];
                dst[2] = src[2];
            }
            let mut png_writer = encoder
                .write_header()
                .map_err(|e| format!("PNG header error: {}", e))?;
            png_writer
                .write_image_data(rgb_scratch)
                .map_err(|e| format!("PNG write error: {}", e))?;
        }
        Ok(())
    }
}

fn box_blur_h(src: &[u8], dst: &mut [u8], w: usize, h: usize, r: usize) {
    let diam = (2 * r + 1) as u32;
    let half = diam >> 1;
    let max_x = (w - 1) as isize;

    for y in 0..h {
        let row = y * w * 4;
        let row_slice = &src[row..row + w * 4];
        if row_slice.iter().all(|&b| b == 0) {
            dst[row..row + w * 4].fill(0);
            continue;
        }
        let mut sr = 0u32;
        let mut sg = 0u32;
        let mut sb = 0u32;
        let mut sa = 0u32;

        for ix in -(r as isize)..=(r as isize) {
            let cx = ix.clamp(0, max_x) as usize;
            let p = row + cx * 4;
            sr += src[p] as u32;
            sg += src[p + 1] as u32;
            sb += src[p + 2] as u32;
            sa += src[p + 3] as u32;
        }

        for x in 0..w {
            let d = row + x * 4;
            dst[d] = ((sr + half) / diam) as u8;
            dst[d + 1] = ((sg + half) / diam) as u8;
            dst[d + 2] = ((sb + half) / diam) as u8;
            dst[d + 3] = ((sa + half) / diam) as u8;

            let out_x = (x as isize - r as isize).clamp(0, max_x) as usize;
            let in_x = (x as isize + r as isize + 1).clamp(0, max_x) as usize;
            let po = row + out_x * 4;
            let pi = row + in_x * 4;

            sr = sr + src[pi] as u32 - src[po] as u32;
            sg = sg + src[pi + 1] as u32 - src[po + 1] as u32;
            sb = sb + src[pi + 2] as u32 - src[po + 2] as u32;
            sa = sa + src[pi + 3] as u32 - src[po + 3] as u32;
        }
    }
}

fn box_blur_v(src: &[u8], dst: &mut [u8], w: usize, h: usize, r: usize) {
    let diam = (2 * r + 1) as u32;
    let half = diam >> 1;
    let max_y = (h - 1) as isize;
    let stride = w * 4;

    for x in 0..w {
        let col_off = x * 4;
        let mut sr = 0u32;
        let mut sg = 0u32;
        let mut sb = 0u32;
        let mut sa = 0u32;

        for iy in -(r as isize)..=(r as isize) {
            let cy = iy.clamp(0, max_y) as usize;
            let p = cy * stride + col_off;
            sr += src[p] as u32;
            sg += src[p + 1] as u32;
            sb += src[p + 2] as u32;
            sa += src[p + 3] as u32;
        }

        for y in 0..h {
            let d = y * stride + col_off;
            dst[d] = ((sr + half) / diam) as u8;
            dst[d + 1] = ((sg + half) / diam) as u8;
            dst[d + 2] = ((sb + half) / diam) as u8;
            dst[d + 3] = ((sa + half) / diam) as u8;

            let out_y = (y as isize - r as isize).clamp(0, max_y) as usize;
            let in_y = (y as isize + r as isize + 1).clamp(0, max_y) as usize;
            let po = out_y * stride + col_off;
            let pi = in_y * stride + col_off;

            sr = sr + src[pi] as u32 - src[po] as u32;
            sg = sg + src[pi + 1] as u32 - src[po + 1] as u32;
            sb = sb + src[pi + 2] as u32 - src[po + 2] as u32;
            sa = sa + src[pi + 3] as u32 - src[po + 3] as u32;
        }
    }
}
