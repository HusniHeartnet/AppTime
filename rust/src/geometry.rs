//! Rectangle math for working out how much of each window is visible.
//! Rectangles are (left, top, right, bottom) in screen coordinates.

pub type Rect = (f64, f64, f64, f64);

/// A window counts as "on screen" when at least this much of it is uncovered.
const MIN_VISIBLE_FRACTION: f64 = 0.15;
const MIN_VISIBLE_PIXELS: f64 = 200.0 * 200.0;

pub fn area(r: Rect) -> f64 {
    (r.2 - r.0).max(0.0) * (r.3 - r.1).max(0.0)
}

fn intersect(a: Rect, b: Rect) -> Rect {
    (a.0.max(b.0), a.1.max(b.1), a.2.min(b.2), a.3.min(b.3))
}

/// Remove `cut` from a list of rectangles, splitting them as needed.
fn subtract(rects: &[Rect], cut: Rect) -> Vec<Rect> {
    let mut out = Vec::new();
    for &r in rects {
        let i = intersect(r, cut);
        if area(i) == 0.0 {
            out.push(r);
            continue;
        }
        let (left, top, right, bottom) = r;
        if i.1 > top {
            out.push((left, top, right, i.1));
        }
        if i.3 < bottom {
            out.push((left, i.3, right, bottom));
        }
        if i.0 > left {
            out.push((left, i.1, i.0, i.3));
        }
        if i.2 < right {
            out.push((i.2, i.1, right, i.3));
        }
    }
    out
}

/// Given the screens and the windows front to back as (owner, rect), return the
/// owners with a window that is meaningfully uncovered.
pub fn visible_owners<T: Copy>(screens: Vec<Rect>, windows: &[(T, Rect)]) -> Vec<T> {
    let mut owners = Vec::new();
    let mut uncovered = screens;
    for &(owner, rect) in windows {
        if uncovered.is_empty() {
            break;
        }
        let shown: f64 = uncovered.iter().map(|r| area(intersect(*r, rect))).sum();
        if shown >= MIN_VISIBLE_PIXELS && shown >= MIN_VISIBLE_FRACTION * area(rect) {
            owners.push(owner);
        }
        uncovered = subtract(&uncovered, rect);
    }
    owners
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn covered_window_is_not_visible() {
        let screen = vec![(0.0, 0.0, 1920.0, 1080.0)];
        let windows = [(1, (0.0, 0.0, 1920.0, 1080.0)), (2, (100.0, 100.0, 800.0, 600.0))];
        assert_eq!(visible_owners(screen, &windows), vec![1]);
    }

    #[test]
    fn side_by_side_windows_are_both_visible() {
        let screen = vec![(0.0, 0.0, 1920.0, 1080.0)];
        let windows = [(1, (0.0, 0.0, 960.0, 1080.0)), (2, (960.0, 0.0, 1920.0, 1080.0))];
        assert_eq!(visible_owners(screen, &windows), vec![1, 2]);
    }

    #[test]
    fn a_sliver_does_not_count() {
        let screen = vec![(0.0, 0.0, 1920.0, 1080.0)];
        let windows = [(1, (0.0, 0.0, 1900.0, 1080.0)), (2, (0.0, 0.0, 1920.0, 1080.0))];
        assert_eq!(visible_owners(screen, &windows), vec![1]);
    }
}
