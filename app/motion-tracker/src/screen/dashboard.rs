use super::{canvas, charts, quality};
use crate::tracker::{Reading, Snapshot};
use canvas::CanvasExt;
use charts::{History, Metric, RoutePoint, ROUTE_POINTS, SAMPLE_INTERVAL};
use display::graphics::{Area, Raster as Canvas};
use display::{Display, Error};
#[cfg(test)]
use display::{Rect, Size};
use maps::{Key, Position, Tile, View as MapView, Viewport, Zoom};
use telemetry::{LinkStatus, View};
use text::Text;
use ui::chart::{Columns, Point};
use ui::gesture::{Gesture, Recognizer};
use ui::{Surface, TextCanvas};
type PlotPoint = Point<3>;
use qrcodegen::{QrCode, QrCodeEcc};
use std::{
    fmt::Write,
    time::{Duration, Instant},
};

const STRIP: u16 = 8;
const CHART_INTERVAL: Duration = Duration::from_millis(100);
const PLOT_LABEL_CAPACITY: usize = 20;
const GRIP_HEIGHT: i32 = 8;
const MENU_HEIGHT: i32 = 40;
const QR_TOP: u16 = 80;
const BG: u16 = 0x0883;
const PANEL: u16 = 0x1126;
pub(super) const GRID: u16 = 0x226b;
const AXIS: u16 = 0x4350;
pub(super) const TEXT: u16 = 0xef9e;
pub(super) const MUTED: u16 = 0xa558;
const MINT: u16 = 0x57f4;
pub(super) const GOLD: u16 = 0xfda6;
pub(super) const RED: u16 = 0xfb0d;
const SIGNALS: [u16; 3] = [MINT, GOLD, 0x661f];

#[derive(Default, Clone, Copy, Debug, Eq, PartialEq)]
enum Page {
    #[default]
    Onboard,
    Module,
    Map,
    Gnss,
}

const TABS: [(Page, &[u8]); 4] = [
    (Page::Onboard, b"IMU 1"),
    (Page::Module, b"IMU 2"),
    (Page::Map, b"MAP"),
    (Page::Gnss, b"GNSS"),
];
const LINK_TAB: i32 = TABS.len() as i32;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Action {
    Pair,
    CancelPairing,
    Redraw,
}

struct Layout {
    width: i32,
    height: i32,
}

impl Layout {
    fn tab(&self, index: i32) -> Area {
        let width = (self.width - 44) / (LINK_TAB + 1);
        Area {
            x: 10 + index * (width + 6),
            y: 5,
            w: width,
            h: 30,
        }
    }

    fn metric(&self, index: i32, top: i32) -> Area {
        Area {
            x: self.width - 106 + index * 49,
            y: top + 1,
            w: 47,
            h: 23,
        }
    }

    fn plot(&self, top: i32) -> Area {
        let y = top + 30;
        Area {
            x: 12,
            y,
            w: self.width - 24,
            h: self.height - y - 32,
        }
    }
}

struct Dashboard {
    page: Page,
    metric: Metric,
    code: Option<QrCode>,
    menu: bool,
    gestures: Recognizer,
}

impl Default for Dashboard {
    fn default() -> Self {
        Self {
            page: Page::default(),
            metric: Metric::default(),
            code: None,
            menu: false,
            gestures: Recognizer::new(14, 32),
        }
    }
}

impl Dashboard {
    fn map_position(
        &self,
        link: LinkStatus,
        snapshot: &Snapshot,
        zoom: Zoom,
    ) -> Option<(Position, f32)> {
        if self.page != Page::Map || link == LinkStatus::Pairing {
            return None;
        }
        match snapshot.gnss {
            Reading::Live(fix) => Position::project(fix.latitude_deg, fix.longitude_deg, zoom)
                .map(|point| (point, fix.speed_mps)),
            _ => None,
        }
    }

    fn content_top(&self) -> i32 {
        if self.menu {
            MENU_HEIGHT
        } else {
            GRIP_HEIGHT
        }
    }

    fn show_code(&mut self, key: &[u8; 32]) -> Result<(), Error> {
        let mut code = String::with_capacity(70);
        code.push_str("NXRS1:");
        for byte in key {
            write!(code, "{byte:02X}").map_err(|_| Error::InvalidPixelCount)?;
        }
        self.code =
            Some(QrCode::encode_text(&code, QrCodeEcc::Low).map_err(|_| Error::InvalidPixelCount)?);
        self.menu = false;
        Ok(())
    }
}

pub struct Screen<D> {
    surface: Surface<D>,
    layout: Layout,
    present: Option<(Page, Metric, LinkStatus, bool)>,
    quality: Option<quality::View>,
    ui: Dashboard,
    history: History,
    map: MapView,
    zoom: Zoom,
    next_plot: Instant,
    next_sample: Instant,
    projected: Vec<Option<PlotPoint>>,
    damage: Vec<Columns>,
    presented_damage: Vec<Columns>,
}

impl<D: Display> Screen<D> {
    pub fn new(display: D, zoom: Zoom) -> Result<Self, Error> {
        let size = display.size();
        if size.width < 272 || size.height < 220 {
            return Err(Error::InvalidRegion);
        }
        let now = Instant::now();
        let layout = Layout {
            width: size.width.into(),
            height: size.height.into(),
        };
        let bands = (layout.plot(GRIP_HEIGHT).h as usize).div_ceil(usize::from(STRIP));
        Ok(Self {
            surface: Surface::new(display, STRIP)?,
            layout,
            present: None,
            quality: None,
            ui: Dashboard::default(),
            history: History::new(zoom),
            map: MapView::default(),
            zoom,
            next_plot: now,
            next_sample: now,
            projected: Vec::with_capacity(charts::MOTION_POINTS),
            damage: vec![Columns::default(); bands],
            presented_damage: vec![Columns::default(); bands],
        })
    }

    pub fn update_link(&mut self, view: View) -> Result<(), Error> {
        if let Some(key) = view.key {
            self.ui.show_code(&key)?;
        } else {
            self.ui.code = None;
            if view.link == LinkStatus::Paired {
                self.ui.menu = true;
            }
        }
        Ok(())
    }

    pub fn input(
        &mut self,
        status: LinkStatus,
        event: touch::Event,
        missed: bool,
    ) -> Option<Action> {
        let ui = &mut self.ui;
        match ui.gestures.observe(event, missed)? {
            Gesture::Tap(point) => Self::tap(&self.layout, ui, status, point),
            Gesture::Down if !ui.menu && status != LinkStatus::Pairing => {
                ui.menu = true;
                Some(Action::Redraw)
            }
            Gesture::Up if ui.menu => {
                ui.menu = false;
                Some(Action::Redraw)
            }
            _ => None,
        }
    }

    fn tap(
        layout: &Layout,
        ui: &mut Dashboard,
        status: LinkStatus,
        point: touch::Point,
    ) -> Option<Action> {
        let (x, y) = (point.x.into(), point.y.into());
        if status == LinkStatus::Pairing {
            return layout
                .tab(LINK_TAB)
                .contains(x, y)
                .then_some(Action::CancelPairing);
        }
        if ui.menu && layout.tab(LINK_TAB).contains(x, y) {
            return match status {
                LinkStatus::Idle | LinkStatus::Paired | LinkStatus::Unavailable => {
                    Some(Action::Pair)
                }
                _ => None,
            };
        }
        if ui.menu {
            for (index, (tab, _)) in TABS.into_iter().enumerate() {
                if layout.tab(index as i32).contains(x, y) {
                    ui.page = tab;
                    ui.menu = false;
                    return Some(Action::Redraw);
                }
            }
        }
        if matches!(ui.page, Page::Onboard | Page::Module) {
            for (index, metric) in [Metric::Acceleration, Metric::Rotation]
                .into_iter()
                .enumerate()
            {
                if layout.metric(index as i32, ui.content_top()).contains(x, y)
                    && ui.metric != metric
                {
                    ui.metric = metric;
                    ui.menu = false;
                    return Some(Action::Redraw);
                }
            }
        }
        std::mem::take(&mut ui.menu).then_some(Action::Redraw)
    }

    pub fn deadline(&self) -> Instant {
        self.next_plot
    }

    pub fn draw(
        &mut self,
        link: LinkStatus,
        snapshot: &Snapshot,
        now: Instant,
        satellites: impl FnOnce() -> Reading<gnss::SatelliteReport>,
        exchange_tile: impl FnOnce(Option<Key>) -> Option<Tile>,
    ) -> Result<(), Error> {
        if now >= self.next_sample {
            self.history.record(snapshot, now);
            self.next_sample = now + SAMPLE_INTERVAL;
        }
        if now >= self.next_plot {
            let interval =
                if matches!(self.ui.page, Page::Map | Page::Gnss) || link == LinkStatus::Pairing {
                    SAMPLE_INTERVAL
                } else {
                    CHART_INTERVAL
                };
            self.next_plot += interval;
            if self.next_plot <= now {
                self.next_plot = now + interval;
            }
        }
        let viewport = self
            .map
            .select(self.ui.map_position(link, snapshot, self.zoom));
        let wanted = viewport.and_then(|viewport| self.map.wanted(viewport));
        if let (Some(tile), Some(viewport)) = (exchange_tile(wanted), viewport) {
            self.map.accept(viewport, tile);
        }
        if self.ui.page == Page::Gnss && link != LinkStatus::Pairing {
            let view = quality::View::new(snapshot.gnss, satellites());
            self.render_quality(link, view)?;
        } else {
            self.render(link, snapshot, now, viewport)?;
        }
        Ok(())
    }

    fn render_quality(&mut self, link: LinkStatus, view: quality::View) -> Result<(), Error> {
        let Self {
            surface,
            layout,
            ui,
            present,
            quality,
            ..
        } = self;
        let key = (ui.page, ui.metric, link, ui.menu);
        let full = *present != Some(key);
        if !full && quality.as_ref() == Some(&view) {
            return Ok(());
        }
        *present = None;
        let top = ui.content_top();
        let y = if full { 0 } else { top };
        let region = Area {
            x: 0,
            y,
            w: layout.width,
            h: layout.height - y,
        };
        surface.draw(region, BG, Some, |canvas| {
            if full && canvas.area().y < top {
                header(canvas, layout, ui, link);
            }
            if canvas.area().y + canvas.area().h > top {
                view.paint(canvas, layout.width, layout.height, top);
            }
        })?;
        *quality = Some(view);
        *present = Some(key);
        Ok(())
    }

    /// Flush small strips to bound each synchronous display transaction.
    fn render(
        &mut self,
        link: LinkStatus,
        snapshot: &Snapshot,
        now: Instant,
        viewport: Option<Viewport>,
    ) -> Result<(), Error> {
        let ui = &self.ui;
        let key = (ui.page, ui.metric, link, ui.menu);
        let previous_metric = self
            .present
            .filter(|&(page, _, status, menu)| (page, status, menu) == (ui.page, link, ui.menu))
            .map(|(_, metric, _, _)| metric);
        let full = previous_metric.is_none();
        if !full && link == LinkStatus::Pairing {
            return Ok(());
        }
        self.present = None;
        let layout = &self.layout;
        let content_top = ui.content_top();
        let top = if full { 0 } else { content_top };
        let plot = layout.plot(content_top);
        let mut route = [RoutePoint::default(); ROUTE_POINTS];
        let body = Body::new(
            ui,
            snapshot,
            &self.history,
            now,
            plot,
            viewport.map(|viewport| (viewport, &self.map)),
            &mut route,
        );
        let chart = link != LinkStatus::Pairing && matches!(&body, Body::Motion { .. });
        if chart {
            body.project_motion(plot, ui.metric, &mut self.projected)?;
            ui::chart::damage(plot, STRIP, &self.projected, &mut self.damage)?;
        }
        let paint = |canvas: &mut Canvas<'_>| {
            if link == LinkStatus::Pairing {
                if let Some(code) = &ui.code {
                    pairing(canvas, layout, code);
                }
            } else {
                if full && canvas.area().y < content_top {
                    header(canvas, layout, ui, link);
                }
                if canvas.area().y + canvas.area().h > content_top {
                    body.paint(canvas, layout, ui, snapshot, &self.projected);
                }
            }
        };
        let whole = Some;
        if !full && chart {
            if previous_metric != Some(ui.metric) {
                // Present the selection before repainting its traces and values.
                let accel = layout.metric(0, content_top);
                let gyro = layout.metric(1, content_top);
                let controls = Area {
                    w: gyro.x + gyro.w - accel.x,
                    ..accel
                };
                self.surface.draw(controls, BG, whole, paint)?;
            }
            let label = (plot.y + 5, plot.y + 12);
            let changed = |strip: Area| {
                let band = (strip.y - plot.y) as usize / usize::from(STRIP);
                let mut range = self.damage[band];
                range.include(self.presented_damage[band]);
                if label.0 < strip.y + strip.h && label.1 > strip.y {
                    range.include(Columns {
                        left: (plot.x + 5) as u16,
                        right: (plot.x + 5 + PLOT_LABEL_CAPACITY as i32 * 6) as u16,
                    });
                }
                (range.left < range.right).then(|| Area {
                    x: range.left.into(),
                    w: (range.right - range.left).into(),
                    ..strip
                })
            };
            self.surface.draw(plot, BG, changed, paint)?;
            self.surface.draw(
                Area {
                    x: 0,
                    y: layout.height - 24,
                    w: layout.width,
                    h: 7,
                },
                BG,
                whole,
                paint,
            )?;
        } else {
            self.surface.draw(
                Area {
                    x: 0,
                    y: top,
                    w: layout.width,
                    h: layout.height - top,
                },
                BG,
                whole,
                paint,
            )?;
        }
        self.present = Some(key);
        if chart {
            std::mem::swap(&mut self.presented_damage, &mut self.damage);
        }
        Ok(())
    }
}

fn header(canvas: &mut Canvas<'_>, layout: &Layout, ui: &Dashboard, link: LinkStatus) {
    if !ui.menu {
        canvas.fill(
            Area {
                x: (layout.width - 24) / 2,
                y: 3,
                w: 24,
                h: 2,
            },
            AXIS,
        );
        return;
    }
    canvas.fill(
        Area {
            x: 0,
            y: 0,
            w: layout.width,
            h: MENU_HEIGHT,
        },
        PANEL,
    );
    let (label, color, clickable): (&[u8], _, _) = match link {
        LinkStatus::Idle | LinkStatus::Paired => (b"PAIR", MINT, true),
        LinkStatus::Unavailable => (b"RETRY", GOLD, true),
        LinkStatus::Pairing => unreachable!(),
        LinkStatus::Connected => (b"LINKED", MINT, false),
    };
    chip(canvas, layout.tab(LINK_TAB), label, color, clickable);
    for (index, (page, label)) in TABS.into_iter().enumerate() {
        let area = layout.tab(index as i32);
        chip(
            canvas,
            area,
            label,
            if page == ui.page { MINT } else { MUTED },
            page == ui.page,
        );
    }
}

fn chip(canvas: &mut Canvas<'_>, area: Area, text: &[u8], color: u16, highlighted: bool) {
    canvas.fill(area, if highlighted { GRID } else { PANEL });
    if highlighted {
        canvas.fill(
            Area {
                x: area.x,
                y: area.y + area.h - 2,
                w: area.w,
                h: 2,
            },
            color,
        );
    }
    let scale = if text.len() * 12 <= area.w as usize - 8 {
        2
    } else {
        1
    };
    canvas.text(
        area.x + (area.w - text.len() as i32 * 6 * scale) / 2,
        area.y + (area.h - 7 * scale) / 2,
        text,
        scale,
        color,
    );
}

fn pairing(canvas: &mut Canvas<'_>, layout: &Layout, code: &QrCode) {
    if canvas.area().y < MENU_HEIGHT {
        chip(canvas, layout.tab(LINK_TAB), b"CANCEL", TEXT, true);
    }
    canvas.text(
        (layout.width - 17 * 12) / 2,
        56,
        b"SCAN IN NXRS HOME",
        2,
        TEXT,
    );
    let modules = code.size() + 8;
    let top = i32::from(QR_TOP);
    let scale = (layout.height - top - 8).min(layout.width - 20) / modules;
    let side = modules * scale;
    let left = (layout.width - side) / 2;
    canvas.fill(
        Area {
            x: left,
            y: top,
            w: side,
            h: side,
        },
        0xffff,
    );
    let first_code_row = ((canvas.area().y - top).div_euclid(scale) - 4).max(0);
    let last_code_row =
        ((canvas.area().y + canvas.area().h - top).div_euclid(scale) - 3).min(code.size());
    for row in first_code_row..last_code_row {
        for col in 0..code.size() {
            if code.get_module(col, row) {
                canvas.fill(
                    Area {
                        x: left + (col + 4) * scale,
                        y: top + (row + 4) * scale,
                        w: scale,
                        h: scale,
                    },
                    0,
                );
            }
        }
    }
}

enum Body<'a> {
    Motion {
        samples: &'a History,
        now: Instant,
        index: usize,
        limit: f32,
    },
    Map {
        viewport: Viewport,
        map: &'a MapView,
        points: &'a [RoutePoint],
    },
    Route {
        points: &'a [RoutePoint],
        east: f32,
        north: f32,
        pixels_per_m: f32,
    },
}

impl<'a> Body<'a> {
    fn project_motion(
        &self,
        plot: Area,
        metric: Metric,
        points: &mut Vec<Option<PlotPoint>>,
    ) -> Result<(), Error> {
        let Self::Motion {
            samples,
            now,
            index,
            limit,
        } = self
        else {
            points.clear();
            return Ok(());
        };
        ui::chart::project(
            plot,
            4,
            *limit,
            samples.motion(*index, metric, *now),
            points,
        )
    }

    fn new(
        ui: &Dashboard,
        snapshot: &Snapshot,
        samples: &'a History,
        now: Instant,
        plot: Area,
        map: Option<(Viewport, &'a MapView)>,
        route: &'a mut [RoutePoint; ROUTE_POINTS],
    ) -> Self {
        if ui.page == Page::Map {
            if let Some((viewport, map)) = map.filter(|(viewport, map)| {
                viewport
                    .keys()
                    .into_iter()
                    .flatten()
                    .any(|key| map.tile(key).is_some())
            }) {
                let len = samples.project_viewport(viewport, route);
                return Self::Map {
                    viewport,
                    map,
                    points: &route[..len],
                };
            }
            let mut low_e = f32::INFINITY;
            let mut high_e = f32::NEG_INFINITY;
            let mut low_n = f32::INFINITY;
            let mut high_n = f32::NEG_INFINITY;
            let len = samples.project_route(route);
            let points = &route[..len];
            for pt in points {
                low_e = low_e.min(pt.east);
                high_e = high_e.max(pt.east);
                low_n = low_n.min(pt.north);
                high_n = high_n.max(pt.north);
            }
            if !low_e.is_finite() {
                return Self::Route {
                    points,
                    east: 0.0,
                    north: 0.0,
                    pixels_per_m: 1.0,
                };
            }
            let pixels_per_m = ((plot.w - 36) as f32 / (high_e - low_e).max(20.0))
                .min((plot.h - 28) as f32 / (high_n - low_n).max(20.0));
            Self::Route {
                points,
                east: (high_e + low_e) * 0.5,
                north: (high_n + low_n) * 0.5,
                pixels_per_m,
            }
        } else {
            let index = usize::from(ui.page == Page::Module);
            let mut limit = ui.metric.step();
            for (_, signal) in samples.motion(index, ui.metric, now) {
                if let Some(values) = signal {
                    for v in values {
                        limit = limit.max(v.abs());
                    }
                }
            }
            if let Reading::Live(sample) = snapshot.imu[index] {
                for value in ui.metric.read(sample) {
                    limit = limit.max(value.abs());
                }
            }
            let step = ui.metric.step();
            Self::Motion {
                samples,
                now,
                index,
                limit: (limit / step).ceil() * step,
            }
        }
    }

    fn paint(
        &self,
        canvas: &mut Canvas<'_>,
        layout: &Layout,
        ui: &Dashboard,
        snapshot: &Snapshot,
        projected: &[Option<PlotPoint>],
    ) {
        let top = ui.content_top();
        let plot = layout.plot(top);
        let footer = layout.height - 24;
        let center = plot.y + plot.h / 2;
        let right = plot.x + plot.w - 1;
        if let Self::Map {
            viewport,
            map,
            points,
        } = self
        {
            canvas.text(12, top + 7, b"GNSS MAP", 2, TEXT);
            let side = plot.w.min(plot.h);
            let area = Area {
                x: plot.x + (plot.w - side) / 2,
                y: plot.y,
                w: side,
                h: side,
            };
            canvas.fill(area, PANEL);
            for key in viewport.keys().into_iter().flatten() {
                if let Some(tile) = map.tile(key) {
                    let [x, y] = viewport.origin(key);
                    let origin = (
                        area.x + (x * side as f32).round() as i32,
                        area.y + (y * side as f32).round() as i32,
                    );
                    canvas.map(area, origin, tile);
                }
            }
            let locate = |east: f32, south: f32| {
                (
                    area.x + (east * side as f32) as i32,
                    area.y + (south * side as f32) as i32,
                )
            };
            let mut previous = None;
            for point in *points {
                let current = locate(point.east, point.north);
                if let Some(from) = previous.filter(|_| point.joins_previous) {
                    canvas.line(from, current, AXIS);
                }
                previous = Some(current);
            }
            let (x, y) = locate(0.5, 0.5);
            canvas.fill(
                Area {
                    x: x - 3,
                    y: y - 3,
                    w: 7,
                    h: 7,
                },
                BG,
            );
            canvas.fill(
                Area {
                    x: x - 2,
                    y: y - 2,
                    w: 5,
                    h: 5,
                },
                GOLD,
            );
            gnss_footer(canvas, layout.width, footer, snapshot.gnss);
            canvas.text(12, footer + 12, b"\xa9 OpenStreetMap contributors", 1, TEXT);
            return;
        }
        canvas.fill(plot, PANEL);
        for i in 1..4 {
            let x = plot.x + i * plot.w / 4;
            let y = plot.y + i * plot.h / 4;
            canvas.line((x, plot.y), (x, plot.y + plot.h - 1), GRID);
            canvas.line((plot.x, y), (right, y), GRID);
        }
        match self {
            Self::Map { .. } => unreachable!(),
            Self::Motion { index, limit, .. } => {
                let sensor = if *index == 0 { b"IMU 1" } else { b"IMU 2" };
                let unit = match ui.metric {
                    Metric::Acceleration => "M/S2",
                    Metric::Rotation => "RAD/S",
                };
                canvas.text(12, top + 7, sensor, 2, TEXT);
                chip(
                    canvas,
                    layout.metric(0, top),
                    b"ACCEL",
                    TEXT,
                    ui.metric == Metric::Acceleration,
                );
                chip(
                    canvas,
                    layout.metric(1, top),
                    b"GYRO",
                    TEXT,
                    ui.metric == Metric::Rotation,
                );
                canvas.line((plot.x, center), (right, center), AXIS);
                if canvas.intersects_rows(plot.y + 5, 7) {
                    let mut bound = Text::<PLOT_LABEL_CAPACITY>::new();
                    let _ = write!(bound, "+{limit:.0} {unit}");
                    canvas.text(plot.x + 5, plot.y + 5, bound.bytes(), 1, MUTED);
                }
                if canvas.intersects_rows(plot.y, plot.h) {
                    let mut previous: Option<PlotPoint> = None;
                    for point in projected {
                        if let Some(current) = point {
                            let x = i32::from(current.x);
                            for (axis, color) in SIGNALS.into_iter().enumerate() {
                                let y = i32::from(current.y[axis]);
                                if let Some(from) = previous {
                                    canvas.line(
                                        (from.x.into(), from.y[axis].into()),
                                        (x, y),
                                        color,
                                    );
                                } else {
                                    canvas.fill(
                                        Area {
                                            x,
                                            y,
                                            w: 2.min(right - x + 1),
                                            h: 2,
                                        },
                                        color,
                                    );
                                }
                            }
                        }
                        previous = *point;
                    }
                }
                match snapshot.imu[*index] {
                    Reading::Live(sample) if canvas.intersects_rows(footer, 7) => {
                        for (axis, value) in ui.metric.read(sample).into_iter().enumerate() {
                            let mut label = Text::<20>::new();
                            let _ = write!(label, "{} {value:+.2}", char::from(b'X' + axis as u8));
                            canvas.text(
                                12 + axis as i32 * (layout.width - 20) / 3,
                                footer,
                                label.bytes(),
                                1,
                                SIGNALS[axis],
                            );
                        }
                    }
                    Reading::Live(_) => {}
                    reading => status(canvas, footer, reading, b"WAITING FOR IMU"),
                }
                canvas.text(12, footer + 12, b"-12S", 1, MUTED);
                canvas.text(layout.width - 30, footer + 12, b"NOW", 1, MUTED);
            }
            Self::Route {
                points,
                east,
                north,
                pixels_per_m,
            } => {
                canvas.text(12, top + 7, b"GNSS ROUTE", 2, TEXT);
                let n = right - 14;
                canvas.text(n - 2, plot.y + 5, b"N", 1, MUTED);
                canvas.line((n, plot.y + 14), (n, plot.y + 28), MUTED);
                canvas.line((n, plot.y + 14), (n - 4, plot.y + 19), MUTED);
                canvas.line((n, plot.y + 14), (n + 4, plot.y + 19), MUTED);
                let mut last = None;
                for pt in *points {
                    let to = (
                        plot.x + plot.w / 2 + ((pt.east - east) * pixels_per_m).round() as i32,
                        center - ((pt.north - north) * pixels_per_m).round() as i32,
                    );
                    if let Some(from) = last.filter(|_| pt.joins_previous) {
                        canvas.line(from, to, MINT);
                    }
                    last = Some(to);
                }
                if let Some((x, y)) = last {
                    canvas.fill(
                        Area {
                            x: x - 2,
                            y: y - 2,
                            w: 5,
                            h: 5,
                        },
                        if matches!(snapshot.gnss, Reading::Live(_)) {
                            MINT
                        } else {
                            GOLD
                        },
                    );
                    let scale_y = plot.y + plot.h - 8;
                    if canvas.intersects_rows(scale_y - 12, 13) {
                        let mut meters = 1.0;
                        while meters * pixels_per_m < 18.0 {
                            meters *= 2.0;
                        }
                        let scale_x = plot.x + 7;
                        let span = (meters * pixels_per_m).round() as i32;
                        canvas.line((scale_x, scale_y), (scale_x + span, scale_y), MUTED);
                        canvas.line((scale_x, scale_y), (scale_x, scale_y - 3), MUTED);
                        canvas.line(
                            (scale_x + span, scale_y),
                            (scale_x + span, scale_y - 3),
                            MUTED,
                        );
                        let mut scale = Text::<16>::new();
                        if meters >= 1000.0 {
                            let _ = write!(scale, "{:.1} KM", meters / 1000.0);
                        } else {
                            let _ = write!(scale, "{meters:.0} M");
                        }
                        canvas.text(scale_x, scale_y - 12, scale.bytes(), 1, MUTED);
                    }
                }
                gnss_footer(canvas, layout.width, footer, snapshot.gnss);
                canvas.text(12, footer + 12, b"RECENT FIXES", 1, MUTED);
            }
        }
    }
}

fn gnss_footer(canvas: &mut Canvas<'_>, width: i32, y: i32, reading: Reading<gnss::GnssFix>) {
    match reading {
        Reading::Live(fix) if canvas.intersects_rows(y, 7) => {
            let mut coordinates = Text::<40>::new();
            let _ = write!(
                coordinates,
                "{:+.5}  {:+.5}",
                fix.latitude_deg, fix.longitude_deg
            );
            canvas.text(12, y, coordinates.bytes(), 1, TEXT);
            let mut speed = Text::<24>::new();
            let _ = write!(speed, "{:.1} KM/H", fix.speed_mps * 3.6);
            canvas.text(
                width - 12 - speed.bytes().len() as i32 * 6,
                y,
                speed.bytes(),
                1,
                MINT,
            );
        }
        Reading::Live(_) => {}
        reading => status(canvas, y, reading, b"WAITING FOR SATELLITES"),
    }
}

fn status<T>(canvas: &mut Canvas<'_>, y: i32, reading: Reading<T>, waiting: &[u8]) {
    let (text, color) = match reading {
        Reading::Waiting => (waiting, GOLD),
        Reading::Stale => (b"READING STALE".as_slice(), GOLD),
        Reading::Fault => (b"SENSOR FAULT".as_slice(), RED),
        Reading::Live(_) => unreachable!(),
    };
    canvas.text(12, y, text, 1, color);
}

#[cfg(test)]
mod tests {
    use super::*;
    use touch::{Event, Point};

    struct Panel(Vec<u16>, Vec<Rect>);

    impl Panel {
        fn new() -> Self {
            Self(vec![BG; 320 * 240], Vec::new())
        }
    }

    impl Display for Panel {
        fn size(&self) -> Size {
            Size {
                width: 320,
                height: 240,
            }
        }

        fn draw(&mut self, rect: Rect, pixels: &[u16]) -> Result<(), Error> {
            assert!(rect.x + rect.size.width <= self.size().width);
            assert!((1..=STRIP).contains(&rect.size.height));
            assert!(rect.y + rect.size.height <= self.size().height);
            assert_eq!(
                pixels.len(),
                usize::from(rect.size.width * rect.size.height)
            );
            let width = usize::from(rect.size.width);
            for (offset, row) in pixels.chunks_exact(width).enumerate() {
                let start = (usize::from(rect.y) + offset) * 320 + usize::from(rect.x);
                self.0[start..start + width].copy_from_slice(row);
            }
            self.1.push(rect);
            Ok(())
        }
    }

    #[test]
    fn partial_display_failure_invalidates_the_scene_until_a_full_retry_succeeds() {
        struct Interrupted {
            panel: Panel,
            failed: bool,
        }

        impl Display for Interrupted {
            fn size(&self) -> Size {
                self.panel.size()
            }

            fn draw(&mut self, rect: Rect, pixels: &[u16]) -> Result<(), Error> {
                self.panel.draw(rect, pixels)?;
                if !self.failed && self.panel.1.len() == 2 {
                    self.failed = true;
                    return Err(Error::Io);
                }
                Ok(())
            }
        }

        let display = Interrupted {
            panel: Panel::new(),
            failed: false,
        };
        let mut screen = Screen::new(display, Zoom::default()).unwrap();
        let now = screen.deadline();
        let snapshot = Snapshot {
            clock: None,
            imu: [Reading::Waiting; 2],
            gnss: Reading::Waiting,
        };
        assert_eq!(
            screen.draw(
                LinkStatus::Idle,
                &snapshot,
                now,
                || Reading::Waiting,
                |_| None
            ),
            Err(Error::Io)
        );
        assert!(screen.present.is_none());
        screen.surface.display_mut().panel.1.clear();
        screen
            .draw(
                LinkStatus::Idle,
                &snapshot,
                now,
                || Reading::Waiting,
                |_| None,
            )
            .unwrap();
        assert!(screen.present.is_some());
        assert_eq!(screen.surface.display().panel.1[0].y, 0);
        assert_eq!(
            screen.surface.display().panel.1.len(),
            240 / usize::from(STRIP)
        );
        let mut reference = Screen::new(Panel::new(), Zoom::default()).unwrap();
        reference
            .draw(
                LinkStatus::Idle,
                &snapshot,
                now,
                || Reading::Waiting,
                |_| None,
            )
            .unwrap();
        assert_eq!(
            screen.surface.display().panel.0,
            reference.surface.display().0
        );
    }

    #[test]
    fn steady_chart_writes_only_damaged_columns_and_erases_a_disappeared_trace() {
        let mut screen = Screen::new(Panel::new(), Zoom::default()).unwrap();
        let now = screen.deadline();
        let mut snapshot = Snapshot {
            clock: None,
            imu: [Reading::Waiting; 2],
            gnss: Reading::Waiting,
        };
        screen
            .draw(
                LinkStatus::Idle,
                &snapshot,
                now,
                || Reading::Waiting,
                |_| None,
            )
            .unwrap();
        assert_eq!(screen.deadline(), now + CHART_INTERVAL);
        screen.surface.display_mut().1.clear();
        screen.presented_damage[(98 - 38) / usize::from(STRIP)] = Columns {
            left: 24,
            right: 26,
        };
        screen.surface.display_mut().0[98 * 320 + 24] = MINT;

        snapshot.imu[0] = Reading::Fault;
        let next = now + CHART_INTERVAL + Duration::from_millis(10);
        screen
            .draw(
                LinkStatus::Idle,
                &snapshot,
                next,
                || Reading::Waiting,
                |_| None,
            )
            .unwrap();
        assert_eq!(screen.deadline(), now + CHART_INTERVAL * 2);
        let writes = &screen.surface.display_mut().1;
        assert_eq!(writes.last().unwrap().y, 216);
        assert_eq!((writes[0].x, writes[0].size.width), (17, 120));
        assert_eq!((writes[1].x, writes[1].size.width), (17, 120));
        assert_eq!(
            (writes[2].x, writes[2].y, writes[2].size.width),
            (24, 94, 2)
        );
        assert_eq!(
            writes
                .iter()
                .map(|r| u32::from(r.size.width) * u32::from(r.size.height))
                .sum::<u32>(),
            (120 * 2 + 2) * 8 + 320 * 7
        );
        assert_ne!(screen.surface.display_mut().0[98 * 320 + 24], MINT);

        let partial = screen.surface.display_mut().0.clone();
        screen.present = None;
        screen
            .draw(
                LinkStatus::Idle,
                &snapshot,
                next,
                || Reading::Waiting,
                |_| None,
            )
            .unwrap();
        assert_eq!(screen.surface.display_mut().0, partial);
    }

    fn tap(screen: &mut Screen<Panel>, link: LinkStatus, point: Point) -> Option<Action> {
        assert_eq!(screen.input(link, Event::Pressed(point), false), None);
        screen.input(link, Event::Released, false)
    }

    #[test]
    fn gnss_tab_draws_constellation_bars_without_a_fix_and_clears_expired_signals() {
        use gnss::{Constellation, SatelliteReport, SatelliteSignal};

        let mut screen = Screen::new(Panel::new(), Zoom::default()).unwrap();
        screen.ui.menu = true;
        let link = LinkStatus::Idle;
        let tab = screen.layout.tab(3);
        assert_eq!(
            tap(
                &mut screen,
                link,
                Point {
                    x: (tab.x + 10) as u16,
                    y: 20
                }
            ),
            Some(Action::Redraw)
        );
        assert_eq!(screen.ui.page, Page::Gnss);
        let now = screen.deadline();
        let snapshot = Snapshot {
            clock: None,
            imu: [Reading::Waiting; 2],
            gnss: Reading::Waiting,
        };
        let report = SatelliteReport::new(
            3,
            2,
            Some(35),
            3000,
            2,
            [
                SatelliteSignal {
                    constellation: Constellation::Gps,
                    id: 3,
                    cn0_db_hz: 45,
                },
                SatelliteSignal {
                    constellation: Constellation::Galileo,
                    id: 11,
                    cn0_db_hz: 25,
                },
            ],
        );
        let draw = |screen: &mut Screen<Panel>, signals| {
            screen
                .draw(link, &snapshot, now, || signals, |_| None)
                .unwrap();
        };
        draw(&mut screen, Reading::Live(report));
        let at = |x: usize, y: usize| y * 320 + x;
        assert_eq!(screen.surface.display_mut().0[at(100, 150)], MINT);
        // The 45 dB-Hz peak sets a 50 dB-Hz ceiling, so 25 is half-height.
        assert_ne!(screen.surface.display_mut().0[at(238, 150)], 0x661f);
        assert_eq!(screen.surface.display_mut().0[at(238, 151)], 0x661f);
        screen.surface.display_mut().1.clear();
        draw(&mut screen, Reading::Live(report));
        assert!(screen.surface.display_mut().1.is_empty());
        let dense = SatelliteReport::new(
            24,
            24,
            Some(45),
            3000,
            24,
            (1..=24).map(|id| SatelliteSignal {
                constellation: Constellation::Gps,
                id,
                cn0_db_hz: 45,
            }),
        );
        draw(&mut screen, Reading::Live(dense));
        let row = at(32, 196)..at(309, 196);
        assert_eq!(
            screen.surface.display_mut().0[row.clone()]
                .split(|&pixel| pixel == BG)
                .filter(|bar| !bar.is_empty())
                .count(),
            24,
        );
        draw(&mut screen, Reading::Stale);
        assert!(screen.surface.display_mut().0[row]
            .iter()
            .all(|&pixel| pixel == BG));
    }

    #[test]
    fn metric_taps_repaint_controls_and_traces_without_resetting_the_chart_deadline() {
        let mut screen = Screen::new(Panel::new(), Zoom::default()).unwrap();
        let link = LinkStatus::Connected;
        let start = screen.deadline();
        let now = start + SAMPLE_INTERVAL;
        let mut sample = imu::ImuSample {
            sequence: 0,
            timestamp_us: 0,
            is_capture_time: false,
            sample_interval_us: 10_000,
            accel_mps2: [5.0, -3.0, 9.0],
            gyro_rps: [0.2, -0.45, 0.8],
        };
        let mut snapshot = Snapshot {
            imu: [Reading::Live(sample), Reading::Waiting],
            gnss: Reading::Waiting,
            clock: None,
        };
        screen
            .draw(link, &snapshot, start, || Reading::Waiting, |_| None)
            .unwrap();
        sample.accel_mps2 = [7.0, 4.0, -8.0];
        snapshot.imu[0] = Reading::Live(sample);
        screen
            .draw(link, &snapshot, now, || Reading::Waiting, |_| None)
            .unwrap();
        let deadline = screen.deadline();

        for x in [280, 230] {
            screen.surface.display_mut().1.clear();
            let before = screen.surface.display_mut().0.clone();
            assert_eq!(
                tap(&mut screen, link, Point { x, y: 20 }),
                Some(Action::Redraw)
            );
            screen
                .draw(link, &snapshot, now, || Reading::Waiting, |_| None)
                .unwrap();
            assert_eq!(screen.deadline(), deadline);

            let (controls, body) = screen.surface.display_mut().1.split_at(3);
            assert!(controls.iter().all(|r| (r.x, r.size.width) == (214, 96)));
            assert_eq!(controls[0].y, 9);
            assert!(body.iter().all(|r| (38..208).contains(&r.y) || r.y == 216));
            let partial = screen.surface.display_mut().0.clone();
            assert_ne!(partial, before);
            screen.present = None;
            screen
                .draw(link, &snapshot, now, || Reading::Waiting, |_| None)
                .unwrap();
            assert_eq!(screen.surface.display_mut().0, partial);
        }
    }

    fn swipe(
        screen: &mut Screen<Panel>,
        link: LinkStatus,
        from: Point,
        to: Point,
    ) -> Option<Action> {
        assert_eq!(screen.input(link, Event::Pressed(from), false), None);
        let action = screen.input(link, Event::Moved(to), false);
        assert_eq!(screen.input(link, Event::Released, false), None);
        action
    }

    #[test]
    fn a_swipe_reveals_the_menu_and_pairing_returns_to_the_graph() {
        let mut screen = Screen::new(Panel::new(), Zoom::default()).unwrap();
        let pair = Point { x: 275, y: 20 };
        let module = Point { x: 115, y: 20 };
        let map = Point { x: 155, y: 20 };
        let below = Point { x: 275, y: 80 };
        let gyro = Point { x: 280, y: 20 };
        assert_eq!(
            tap(&mut screen, LinkStatus::Idle, pair),
            Some(Action::Redraw)
        );
        assert_eq!(screen.ui.metric, Metric::Rotation);
        let now = Instant::now();
        let snapshot = Snapshot {
            clock: None,
            imu: [Reading::Waiting; 2],
            gnss: Reading::Waiting,
        };
        screen
            .draw(
                LinkStatus::Idle,
                &snapshot,
                now,
                || Reading::Waiting,
                |_| None,
            )
            .unwrap();
        assert!(!screen.surface.display_mut().0.contains(&0xffff));
        assert_eq!(screen.surface.display_mut().0[320 * 45 + 12], PANEL);
        assert_eq!(
            swipe(&mut screen, LinkStatus::Idle, pair, below),
            Some(Action::Redraw)
        );
        assert!(screen.ui.menu);
        screen
            .draw(
                LinkStatus::Idle,
                &snapshot,
                now,
                || Reading::Waiting,
                |_| None,
            )
            .unwrap();
        assert_eq!(screen.surface.display_mut().0[320 * 39], PANEL);
        assert_eq!(screen.surface.display_mut().0[320 * 45 + 12], BG);
        assert_eq!(
            tap(&mut screen, LinkStatus::Idle, module),
            Some(Action::Redraw)
        );
        assert_eq!(screen.ui.page, Page::Module);
        assert!(!screen.ui.menu);
        assert_eq!(
            swipe(&mut screen, LinkStatus::Idle, pair, below),
            Some(Action::Redraw)
        );
        assert_eq!(tap(&mut screen, LinkStatus::Idle, pair), Some(Action::Pair));
        screen
            .update_link(View {
                link: LinkStatus::Pairing,
                key: Some([42; 32]),
            })
            .unwrap();
        assert!(!screen.ui.menu);
        screen
            .draw(
                LinkStatus::Pairing,
                &snapshot,
                now,
                || Reading::Waiting,
                |_| None,
            )
            .unwrap();
        let code =
            QrCode::encode_text(&format!("NXRS1:{}", "2A".repeat(32)), QrCodeEcc::Low).unwrap();
        let side = 4 * (code.size() + 8);
        let left = (320 - side) / 2;
        for y in 0..side {
            for x in 0..side {
                let color = if code.get_module(x / 4 - 4, y / 4 - 4) {
                    0
                } else {
                    0xffff
                };
                assert_eq!(
                    screen.surface.display_mut().0[((80 + y) * 320 + left + x) as usize],
                    color
                );
            }
        }
        assert_eq!(swipe(&mut screen, LinkStatus::Pairing, pair, below), None);
        assert_eq!(tap(&mut screen, LinkStatus::Pairing, module), None);
        assert_eq!(
            tap(&mut screen, LinkStatus::Pairing, pair),
            Some(Action::CancelPairing)
        );
        screen
            .update_link(View {
                link: LinkStatus::Connected,
                key: None,
            })
            .unwrap();
        assert_eq!(tap(&mut screen, LinkStatus::Connected, gyro), None);
        assert_eq!(
            swipe(&mut screen, LinkStatus::Connected, pair, below),
            Some(Action::Redraw)
        );
        assert_eq!(tap(&mut screen, LinkStatus::Connected, pair), None);
        assert_eq!(
            tap(&mut screen, LinkStatus::Connected, map),
            Some(Action::Redraw)
        );
        assert!(!screen.ui.menu);
        assert_eq!(screen.ui.page, Page::Map);
        screen
            .draw(
                LinkStatus::Connected,
                &snapshot,
                now,
                || Reading::Waiting,
                |_| None,
            )
            .unwrap();
    }

    #[test]
    fn upward_swipe_dismisses_and_interrupted_contacts_cannot_activate_pairing() {
        let mut screen = Screen::new(Panel::new(), Zoom::default()).unwrap();
        let status = LinkStatus::Idle;
        let pair = Point { x: 275, y: 20 };
        let below = Point { x: 275, y: 80 };
        assert_eq!(
            swipe(&mut screen, status, pair, Point { x: 200, y: 28 }),
            None
        );
        assert_eq!(
            swipe(&mut screen, status, pair, below),
            Some(Action::Redraw)
        );
        assert_eq!(screen.input(status, Event::Pressed(pair), false), None);
        assert_eq!(screen.input(status, Event::Released, true), None);
        assert!(screen.ui.menu);
        assert_eq!(
            swipe(&mut screen, status, below, pair),
            Some(Action::Redraw)
        );
        assert!(!screen.ui.menu);
    }
}
