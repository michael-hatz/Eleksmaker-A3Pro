"""Plot-Auftrag: Platzierung auf dem Blatt, G-Code, Zeitschätzung, Fortschritt.

Koordinatensysteme
------------------
* Dokument-mm:  wie aus dem SVG geladen (x rechts, y unten).
* Blatt-mm (u, v): linke obere Ecke des Blatts, so wie man vor dem Plotter
  steht und auf das Blatt schaut (u rechts, v nach vorne/unten).
* Maschine (X, Y): GRBL-Koordinaten (MPos). Welche Blattecke am Nullpunkt
  liegt und in welche Richtung die Achsen zeigen, steht in `MachineSetup`.
  Liegt das Blatt schräg, dreht `sheet_angle` es um die Blattecke am
  Nullpunkt (gemessen über eine zweite Ecke, siehe `measure_sheet_angle`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .paths import join_close, path_length, simplify

PAPER_SIZES = {"A4": (210.0, 297.0), "A3": (297.0, 420.0)}

# grbl-servo / GRBL 0.9i
PLANNER_BLOCKS = 18


@dataclass
class PenSettings:
    up: int = 25
    down: int = 30
    down_delay_ms: int = 150
    up_delay_ms: int = 100
    draw_speed: float = 400.0     # mm/min
    travel_speed: float = 500.0   # mm/min


@dataclass
class MachineSetup:
    # Blattecke, an der der Maschinen-Nullpunkt liegt: "bl", "tl", "tr", "br"
    origin_corner: str = "bl"
    x_right: bool = True          # +X fährt nach rechts
    y_up: bool = True             # +Y fährt nach hinten (auf dem Blatt "nach oben")
    max_rate_x: float = 500.0     # $110
    max_rate_y: float = 500.0     # $111
    accel_x: float = 100.0        # $120 (mm/s²)
    accel_y: float = 100.0        # $121
    junction_deviation: float = 0.010   # $11
    origin_offset: tuple = (0.0, 0.0)   # MPos der Blattecke (per "Papierecke hier")
    sheet_angle: float = 0.0      # Drehung des Blatts in Grad (mathematisch positiv in X/Y)


@dataclass
class Placement:
    x: float = 0.0           # linke obere Ecke der Zeichnung auf dem Blatt (mm)
    y: float = 0.0
    scale: float = 1.0
    rotation: int = 0        # 0, 90, 180, 270 (im Uhrzeigersinn)


def rotate_points(p: np.ndarray, rotation: int) -> np.ndarray:
    r = rotation % 360
    if r == 0:
        return p
    x, y = p[:, 0], p[:, 1]
    if r == 90:     # im Uhrzeigersinn auf dem Bildschirm (y unten)
        return np.column_stack((-y, x))
    if r == 180:
        return np.column_stack((-x, -y))
    return np.column_stack((y, -x))


def rotated_bounds(paths, rotation):
    if not paths:
        return np.zeros(2), np.zeros(2)
    allp = rotate_points(np.vstack(paths), rotation)
    return allp.min(axis=0), allp.max(axis=0)


def place_paths(paths, placement: Placement, bounds=None):
    """Dokument-mm -> Blatt-mm."""
    lo, _ = bounds if bounds is not None else rotated_bounds(paths, placement.rotation)
    off = np.array([placement.x, placement.y])
    return [(rotate_points(p, placement.rotation) - lo) * placement.scale + off for p in paths]


class SheetToMachine:
    def __init__(self, setup: MachineSetup, paper_w: float, paper_h: float):
        c = setup.origin_corner
        self.u0 = paper_w if c in ("tr", "br") else 0.0
        self.v0 = paper_h if c in ("bl", "br") else 0.0
        self.sx = 1.0 if setup.x_right else -1.0
        self.sy = -1.0 if setup.y_up else 1.0
        self.ox, self.oy = setup.origin_offset
        a = math.radians(setup.sheet_angle)
        self.cos, self.sin = math.cos(a), math.sin(a)

    def to_machine(self, uv: np.ndarray) -> np.ndarray:
        dx = self.sx * (uv[:, 0] - self.u0)
        dy = self.sy * (uv[:, 1] - self.v0)
        return np.column_stack((self.ox + self.cos * dx - self.sin * dy,
                                self.oy + self.sin * dx + self.cos * dy))

    def to_sheet(self, x: float, y: float):
        rx, ry = x - self.ox, y - self.oy
        dx = self.cos * rx + self.sin * ry
        dy = -self.sin * rx + self.cos * ry
        return (self.u0 + dx / self.sx, self.v0 + dy / self.sy)


CORNER_NAMES = {"bl": "unten links", "tl": "oben links", "tr": "oben rechts", "br": "unten rechts"}


def second_corner(setup: MachineSetup, paper_w: float, paper_h: float):
    """Die Ecke, die an der kürzeren Blattseite neben der Nullpunkt-Ecke liegt.

    -> (Eckenkürzel, (u, v) in Blatt-mm, Seitenlänge in mm)
    """
    c = setup.origin_corner
    if paper_w <= paper_h:     # kurze Seite waagerecht: links <-> rechts tauschen
        c2 = c[0] + ("r" if c[1] == "l" else "l")
        side = paper_w
    else:                      # kurze Seite senkrecht: oben <-> unten tauschen
        c2 = ("t" if c[0] == "b" else "b") + c[1]
        side = paper_h
    u = paper_w if c2[1] == "r" else 0.0
    v = paper_h if c2[0] == "b" else 0.0
    return c2, (u, v), side


def measure_sheet_angle(setup: MachineSetup, paper_w: float, paper_h: float, x: float, y: float):
    """Drehwinkel des Blatts aus der Maschinenposition (x, y) der zweiten Ecke.

    -> (Winkel in Grad im Bereich (-180, 180], gemessene Kantenlänge in mm)
    """
    _, uv, _ = second_corner(setup, paper_w, paper_h)
    flat = SheetToMachine(MachineSetup(**{**vars(setup), "sheet_angle": 0.0}), paper_w, paper_h)
    ex, ey = flat.to_machine(np.array([uv]))[0]
    ox, oy = setup.origin_offset
    want = math.atan2(ey - oy, ex - ox)
    got = math.atan2(y - oy, x - ox)
    ang = math.degrees(got - want)
    ang = (ang + 180.0) % 360.0 - 180.0
    if ang == -180.0:
        ang = 180.0
    return ang, math.hypot(x - ox, y - oy)


# --------------------------------------------------------------------------

KIND_OTHER, KIND_TRAVEL, KIND_DRAW, KIND_PEN, KIND_DWELL = range(5)


@dataclass
class Job:
    lines: list                      # G-Code-Zeilen (ohne \n)
    kind: np.ndarray                 # pro Zeile
    start: np.ndarray                # (n,2) Maschinen-Startpunkt jeder Zeile
    end: np.ndarray                  # (n,2) Endpunkt
    duration: np.ndarray             # geschätzte Dauer pro Zeile (s)
    cum_end: np.ndarray              # kumulierte Zeit am Ende der Zeile (s)
    stroke_of_line: np.ndarray       # Strich-Index (-1 = keiner)
    strokes_sheet: list              # Striche in Blatt-mm (für die Vorschau)
    stroke_last_line: np.ndarray     # letzte G-Code-Zeile jedes Strichs
    draw_length: float = 0.0
    travel_length: float = 0.0
    pen_lifts: int = 0
    bounds_machine: tuple = ((0, 0), (0, 0))
    warnings: list = field(default_factory=list)

    @property
    def total_time(self) -> float:
        return float(self.cum_end[-1]) if len(self.cum_end) else 0.0

    def progress_time(self, line_idx: int, frac: float = 1.0) -> float:
        """Geschätzte Zeit, wenn Zeile `line_idx` zu `frac` abgearbeitet ist."""
        if line_idx < 0:
            return 0.0
        line_idx = min(line_idx, len(self.lines) - 1)
        return float(self.cum_end[line_idx] - (1 - frac) * self.duration[line_idx])

    def locate(self, pos, lo: int, hi: int):
        """Zeile im Bereich [lo, hi], deren Strecke `pos` am nächsten liegt -> (idx, frac)."""
        lo = max(0, lo)
        hi = min(hi, len(self.lines) - 1)
        if hi < lo:
            return lo, 0.0
        a = self.start[lo:hi + 1]
        b = self.end[lo:hi + 1]
        ab = b - a
        L2 = (ab ** 2).sum(axis=1)
        p = np.asarray(pos, dtype=float)
        t = np.where(L2 > 0, ((p - a) * ab).sum(axis=1) / np.where(L2 > 0, L2, 1), 1.0)
        t = np.clip(t, 0, 1)
        proj = a + t[:, None] * ab
        d = np.hypot(proj[:, 0] - p[0], proj[:, 1] - p[1])
        moving = np.isin(self.kind[lo:hi + 1], (KIND_TRAVEL, KIND_DRAW))
        d = np.where(moving, d, np.inf)
        if not np.isfinite(d).any():
            return lo, 0.0
        k = int(np.argmin(d))
        return lo + k, float(t[k])


def fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def build_job(layers_paths, placement: Placement, paper: tuple, pen: PenSettings,
              setup: MachineSetup, simplify_tol=0.0, join_tol=0.05,
              park=True, bounds=None) -> Job:
    """layers_paths: bereits sortierte Pfade in Dokument-mm (alle aktiven Ebenen hintereinander)."""
    paper_w, paper_h = paper
    sheet = place_paths(layers_paths, placement, bounds)
    if simplify_tol:
        sheet = [simplify(p, simplify_tol) for p in sheet]
    sheet = join_close(sheet, join_tol)
    s2m = SheetToMachine(setup, paper_w, paper_h)
    machine = [s2m.to_machine(p) for p in sheet]

    lines, kinds, strokes = [], [], []
    ends = []                      # Liste von (k,2)-Blöcken der Endpunkte
    stroke_last = []
    pos = np.array(setup.origin_offset, dtype=float)

    def emit(text, kind, end=None, stroke=-1):
        nonlocal pos
        lines.append(text)
        kinds.append(kind)
        if end is not None:
            pos = np.asarray(end, dtype=float)
        ends.append(pos[None, :])
        strokes.append(stroke)

    max_rate = min(setup.max_rate_x, setup.max_rate_y)
    draw_f = min(pen.draw_speed, max_rate)
    travel_g0 = pen.travel_speed >= max_rate
    up_cmd, down_cmd = f"M3S{pen.up}", f"M3S{pen.down}"
    up_dwell = f"G4P{pen.up_delay_ms / 1000:.2f}" if pen.up_delay_ms > 0 else None
    down_dwell = f"G4P{pen.down_delay_ms / 1000:.2f}" if pen.down_delay_ms > 0 else None
    fdraw = fmt(draw_f)

    emit("G21", KIND_OTHER)
    emit("G90", KIND_OTHER)
    emit(up_cmd, KIND_PEN)
    if up_dwell:
        emit(up_dwell, KIND_DWELL)

    travel_len = draw_len = 0.0
    for si, p in enumerate(machine):
        x, y = p[0]
        dist = float(np.hypot(*(p[0] - pos)))
        if dist > 0.005:
            travel_len += dist
            if travel_g0:
                emit(f"G0X{fmt(x)}Y{fmt(y)}", KIND_TRAVEL, p[0])
            else:
                emit(f"G1X{fmt(x)}Y{fmt(y)}F{fmt(pen.travel_speed)}", KIND_TRAVEL, p[0])
        emit(down_cmd, KIND_PEN, stroke=si)
        if down_dwell:
            emit(down_dwell, KIND_DWELL, stroke=si)
        pts = p[1:] if len(p) > 1 else p   # Punkt: auf der Stelle "zeichnen"
        # Vorschub in jeder ersten Zeichenzeile – danach modal
        txt = [f"G1X{a:.2f}Y{b:.2f}" for a, b in pts.tolist()]
        txt[0] += "F" + fdraw
        lines.extend(txt)
        kinds.extend([KIND_DRAW] * len(txt))
        strokes.extend([si] * len(txt))
        ends.append(pts)
        pos = pts[-1].copy()
        draw_len += path_length(p)
        emit(up_cmd, KIND_PEN, stroke=si)
        if up_dwell:
            emit(up_dwell, KIND_DWELL, stroke=si)
        stroke_last.append(len(lines) - 1)

    if park:
        ox, oy = setup.origin_offset
        dist = float(np.hypot(pos[0] - ox, pos[1] - oy))
        if dist > 0.005:
            travel_len += dist
            emit(f"G0X{fmt(ox)}Y{fmt(oy)}", KIND_TRAVEL, (ox, oy))
    emit("M5", KIND_PEN)

    end = np.vstack(ends)
    start = np.vstack([np.array(setup.origin_offset, dtype=float)[None, :], end[:-1]])
    kind = np.array(kinds, dtype=np.int8)
    dur = estimate_durations(kind, start, end, lines, pen, setup, draw_f)
    allm = np.vstack(machine) if machine else np.zeros((1, 2))
    warnings = []
    if pen.draw_speed > max_rate:
        warnings.append(f"Zeichengeschwindigkeit wird von GRBL auf {max_rate:.0f} mm/min begrenzt ($110/$111).")
    return Job(lines=lines, kind=kind, start=start, end=end, duration=dur,
               cum_end=np.cumsum(dur), stroke_of_line=np.array(strokes),
               strokes_sheet=sheet, stroke_last_line=np.array(stroke_last, dtype=int),
               draw_length=draw_len, travel_length=travel_len, pen_lifts=len(machine),
               bounds_machine=(tuple(allm.min(axis=0)), tuple(allm.max(axis=0))),
               warnings=warnings)


def frame_lines(sheet_rect, setup: MachineSetup, paper, speed=None):
    """G-Code, der mit gehobenem Stift den Rahmen (u0,v0,u1,v1 in Blatt-mm) abfährt."""
    u0, v0, u1, v1 = sheet_rect
    s2m = SheetToMachine(setup, *paper)
    pts = s2m.to_machine(np.array([(u0, v0), (u1, v0), (u1, v1), (u0, v1), (u0, v0)]))
    return ["G21", "G90"] + [f"G0X{fmt(x)}Y{fmt(y)}" for x, y in pts]


# --------------------------------------------------------------------------
# Zeitschätzung: vereinfachte Nachbildung des GRBL-Planers

def estimate_durations(kind, start, end, lines, pen: PenSettings, setup: MachineSetup, draw_f):
    """Dauer jeder Zeile in Sekunden.

    Bewegungen werden wie im GRBL-Planer geplant: Junction Deviation an den
    Ecken, Beschleunigungsrampen, Vorausschau über 18 Blöcke. M3 und G4
    synchronisieren (Planer läuft leer, Geschwindigkeit 0).
    """
    n = len(kind)
    dur = np.zeros(n)
    max_rate = min(setup.max_rate_x, setup.max_rate_y)
    travel_f = max_rate if pen.travel_speed >= max_rate else pen.travel_speed
    for i in np.flatnonzero(kind == KIND_DWELL):
        dur[i] = float(lines[i][3:])
    moving = (kind == KIND_TRAVEL) | (kind == KIND_DRAW)
    dd = end - start
    Lall = np.hypot(dd[:, 0], dd[:, 1])
    sel = moving & (Lall > 1e-6)
    ids = np.flatnonzero(sel)
    if not len(ids):
        return dur
    # Laufnummer: steigt bei jeder synchronisierenden Zeile (M3, G4, …)
    run_of_line = np.cumsum(~moving)
    run = run_of_line[ids]
    m = len(ids)
    d = dd[ids]
    L = Lall[ids]
    u = d / L[:, None]
    feed = np.where(kind[ids] == KIND_DRAW, draw_f, travel_f) / 60.0
    ax, ay = np.abs(u[:, 0]), np.abs(u[:, 1])
    safe_ax, safe_ay = np.where(ax > 0, ax, 1), np.where(ay > 0, ay, 1)
    lim = np.minimum(np.where(ax > 0, setup.max_rate_x / 60.0 / safe_ax, np.inf),
                     np.where(ay > 0, setup.max_rate_y / 60.0 / safe_ay, np.inf))
    acc = np.minimum(np.where(ax > 0, setup.accel_x / safe_ax, np.inf),
                     np.where(ay > 0, setup.accel_y / safe_ay, np.inf))
    vn = np.minimum(feed, lim)
    vn2 = vn ** 2
    first = np.ones(m, dtype=bool)
    first[1:] = run[1:] != run[:-1]
    # Übergangsgeschwindigkeit (Junction Deviation, wie planner.c)
    jmax2 = np.zeros(m)
    if m > 1:
        cos_t = -(u[1:] * u[:-1]).sum(axis=1)
        sharp = cos_t > 0.99
        cos_t = np.clip(cos_t, -0.99, 0.99)
        sin_d2 = np.sqrt(0.5 * (1 - cos_t))
        jd = acc[1:] * setup.junction_deviation * sin_d2 / (1 - sin_d2)
        jd = np.where(sharp, 0.0, jd)
        jmax2[1:] = np.minimum(jd, np.minimum(vn2[1:], vn2[:-1]))
    jmax2[first] = 0.0
    # Vorausschau: aus dem Pufferinhalt (max. 18 Blöcke, nur bis zum Laufende)
    # muss bis zum Stillstand gebremst werden können.
    csum = np.concatenate([[0], np.cumsum(L)])
    run_start = np.flatnonzero(first)
    run_end = np.append(run_start[1:], m)              # exklusiv
    end_of = np.repeat(run_end, np.diff(np.append(run_start, m)))
    hi = np.minimum(np.arange(m) + PLANNER_BLOCKS - 1, end_of)
    jmax2 = np.minimum(jmax2, 2 * acc * (csum[hi] - csum[:m]))
    # Rückwärts- und Vorwärtsdurchgang (Python-Listen sind hier schneller als numpy-Skalare)
    e2 = jmax2.tolist()
    two_aL = (2 * acc * L).tolist()
    fl = first.tolist()
    nxt = 0.0
    for k in range(m - 1, -1, -1):
        v = nxt + two_aL[k]
        if e2[k] > v:
            e2[k] = v
        nxt = 0.0 if fl[k] else e2[k]
    for k in range(m - 1):
        if not fl[k + 1]:
            v = e2[k] + two_aL[k]
            if e2[k + 1] > v:
                e2[k + 1] = v
    entry2 = np.array(e2)
    exit2 = np.append(entry2[1:], 0.0)
    exit2[np.append(first[1:], True)] = 0.0
    # Trapezprofil
    ve, vx, v, a = np.sqrt(entry2), np.sqrt(exit2), vn, acc
    d_acc = (vn2 - entry2) / (2 * a)
    d_dec = (vn2 - exit2) / (2 * a)
    cruise = L - d_acc - d_dec
    t_trap = (v - ve) / a + (v - vx) / a + np.maximum(cruise, 0) / np.maximum(v, 1e-9)
    vp = np.sqrt(np.maximum((2 * a * L + entry2 + exit2) / 2, 0))
    t_tri = (vp - ve) / a + (vp - vx) / a
    dur[ids] = np.where(cruise >= 0, t_trap, t_tri)
    return dur


def format_duration(seconds: float) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "–"
    s = int(round(max(0.0, seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"
