"""SVG laden und in Polylinien (Millimeter) umwandeln.

Ergebnis: ein `Drawing` mit Ebenen. Jede Ebene enthält Polylinien als
numpy-Arrays der Form (N, 2) in Millimetern, bezogen auf die linke obere
Ecke des SVG-Dokuments (x nach rechts, y nach unten).

Unterstützt: path (alle Befehle inkl. Bögen), line, polyline, polygon,
rect, circle, ellipse, g mit transform, viewBox/width/height mit Einheiten,
Inkscape-Ebenen, display:none / visibility:hidden. Text wird nicht geplottet
(Hinweis in `Drawing.warnings`).
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import numpy as np

# Mehr Genauigkeit braucht der Plotter nicht: ein Schritt sind 0,0125 mm.
FLATTEN_TOL_MM = 0.02

UNIT_MM = {
    "": 25.4 / 96, "px": 25.4 / 96, "mm": 1.0, "cm": 10.0, "in": 25.4,
    "pt": 25.4 / 72, "pc": 25.4 / 6, "q": 0.25,
}

INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SKIP_TAGS = {"defs", "clipPath", "mask", "symbol", "marker", "pattern", "style",
             "title", "desc", "metadata", "script", "linearGradient",
             "radialGradient", "filter", "namedview", "foreignObject"}


@dataclass
class Layer:
    name: str
    color: str = "#000000"
    paths: list = field(default_factory=list)   # list[np.ndarray (N,2)]

    @property
    def point_count(self):
        return sum(len(p) for p in self.paths)


@dataclass
class Drawing:
    width_mm: float
    height_mm: float
    layers: list
    warnings: list = field(default_factory=list)
    source: str = ""


# --------------------------------------------------------------------------
# Hilfsfunktionen

def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_length(value, default=None, percent_of=None):
    """'210mm' -> 210.0 (mm). Zahlen ohne Einheit sind px (96 dpi)."""
    if value is None:
        return default
    m = re.fullmatch(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z%]*)\s*", value)
    if not m:
        return default
    num, unit = float(m.group(1)), m.group(2).lower()
    if unit == "%":
        return num / 100 * percent_of if percent_of is not None else default
    if unit in ("em", "ex"):
        return num * 16 * UNIT_MM["px"]
    return num * UNIT_MM.get(unit, UNIT_MM["px"])


def _user_len(value, default=0.0):
    """Länge in Benutzereinheiten (Einheiten werden ignoriert bzw. px angenommen)."""
    if value is None:
        return default
    m = re.match(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", value)
    return float(m.group(1)) if m else default


NUM_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _numbers(s):
    return [float(x) for x in NUM_RE.findall(s or "")]


def parse_style(el):
    """Attribute + style="" zusammenführen (style hat Vorrang)."""
    props = {}
    for key in ("display", "visibility", "stroke", "fill"):
        if key in el.attrib:
            props[key] = el.attrib[key].strip()
    style = el.attrib.get("style")
    if style:
        for part in style.split(";"):
            if ":" in part:
                k, v = part.split(":", 1)
                props[k.strip()] = v.strip()
    return props


# Affine Matrizen als (a, b, c, d, e, f) wie in SVG: x' = a x + c y + e
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m1, m2):
    """m1 · m2 (erst m2, dann m1 anwenden)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def parse_transform(s):
    m = IDENTITY
    if not s:
        return m
    for name, args in re.findall(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", s):
        v = _numbers(args)
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate" and v:
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale" and v:
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            r = math.radians(v[0])
            cs, sn = math.cos(r), math.sin(r)
            t = (cs, sn, -sn, cs, 0, 0)
            if len(v) >= 3:
                t = mat_mul(mat_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX" and v:
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY" and v:
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = mat_mul(m, t)
    return m


def apply(m, pts):
    a, b, c, d, e, f = m
    pts = np.asarray(pts, dtype=float)
    x, y = pts[:, 0], pts[:, 1]
    return np.column_stack((a * x + c * y + e, b * x + d * y + f))


# --------------------------------------------------------------------------
# Kurven abtasten

def flatten_cubic(p0, p1, p2, p3, tol):
    """Kubische Bézierkurve (bereits in mm) -> Punkte ohne p0."""
    p = np.array([p0, p1, p2, p3], dtype=float)
    dd = max(np.hypot(*(p[0] - 2 * p[1] + p[2])), np.hypot(*(p[1] - 2 * p[2] + p[3])))
    # Wang's Formel: n = sqrt(d(d-1)/8 * M / tol) mit d = 3
    n = int(math.ceil(math.sqrt(0.75 * dd / tol))) if dd > 0 else 1
    n = max(1, min(n, 2000))
    t = np.linspace(0, 1, n + 1)[1:, None]
    mt = 1 - t
    return (mt ** 3) * p[0] + 3 * (mt ** 2) * t * p[1] + 3 * mt * (t ** 2) * p[2] + (t ** 3) * p[3]


def arc_to_cubics(x1, y1, rx, ry, phi_deg, large, sweep, x2, y2):
    """SVG-Bogen (Endpunkt-Parametrisierung) -> Liste kubischer Kontrollpunkte."""
    if (x1 == x2 and y1 == y2):
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [((x1, y1), (x1, y1), (x2, y2), (x2, y2))]
    phi = math.radians(phi_deg % 360)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p = cp * dx + sp * dy
    y1p = -sp * dx + cp * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    th1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dth > 0:
        dth -= 2 * math.pi
    elif sweep and dth < 0:
        dth += 2 * math.pi
    nseg = max(1, int(math.ceil(abs(dth) / (math.pi / 2) - 1e-9)))
    delta = dth / nseg
    k = 4 / 3 * math.tan(delta / 4)
    out = []
    th = th1

    def pt(t):
        ct, st = math.cos(t), math.sin(t)
        return (cx + rx * ct * cp - ry * st * sp, cy + rx * ct * sp + ry * st * cp)

    def deriv(t):
        ct, st = math.cos(t), math.sin(t)
        return (-rx * st * cp - ry * ct * sp, -rx * st * sp + ry * ct * cp)

    for _ in range(nseg):
        a, b = th, th + delta
        pa, pb = pt(a), pt(b)
        da, db = deriv(a), deriv(b)
        c1 = (pa[0] + k * da[0], pa[1] + k * da[1])
        c2 = (pb[0] - k * db[0], pb[1] - k * db[1])
        out.append((pa, c1, c2, pb))
        th = b
    # Endpunkt exakt setzen
    if out:
        pa, c1, c2, _ = out[-1]
        out[-1] = (pa, c1, c2, (x2, y2))
    return out


# --------------------------------------------------------------------------
# path d=""

PATH_TOKEN = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
ARG_COUNT = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


def _tokenize_path(d):
    """Liefert ('cmd', 'M') oder ('num', float). Bogen-Flags dürfen ohne Trenner stehen."""
    tokens = []
    i, n = 0, len(d)
    while i < n:
        ch = d[i]
        if ch in " \t\r\n,":
            i += 1
            continue
        m = PATH_TOKEN.match(d, i)
        if not m:
            i += 1      # unbekanntes Zeichen überspringen
            continue
        if m.group(1):
            tokens.append(("cmd", m.group(1)))
        else:
            tokens.append(("num", m.group(2)))
        i = m.end()
    return tokens


def parse_path(d, m, tol):
    """d-String -> Liste von Polylinien (np.ndarray, mm)."""
    tokens = _tokenize_path(d)
    subpaths = []
    cur = []          # Punkte der aktuellen Teilfigur (in mm)
    pos = (0.0, 0.0)  # aktuelle Position in Benutzereinheiten
    start = (0.0, 0.0)
    last_ctrl = None  # für S/T
    last_cmd = ""
    i = 0
    cmd = None

    def tm(p):
        a, b, c, dd, e, f = m
        return (a * p[0] + c * p[1] + e, b * p[0] + dd * p[1] + f)

    def finish():
        nonlocal cur
        if len(cur) >= 2:   # ein einzelnes M zeichnet nichts
            subpaths.append(np.array(cur, dtype=float))
        cur = []

    def add_cubic(p0, p1, p2, p3):
        pts = flatten_cubic(tm(p0), tm(p1), tm(p2), tm(p3), tol)
        cur.extend(map(tuple, pts))

    def read_nums(k):
        nonlocal i
        vals = []
        while len(vals) < k:
            if i >= len(tokens) or tokens[i][0] != "num":
                return None
            vals.append(tokens[i][1])
            i += 1
        return vals

    def read_arc_args():
        """Bogen: rx ry rot flag flag x y – Flags evtl. zusammengeschrieben ('01' oder '1.5')."""
        nonlocal i
        vals = read_nums(3)
        if vals is None:
            return None
        flags = []
        while len(flags) < 2:
            if i >= len(tokens) or tokens[i][0] != "num":
                return None
            s = tokens[i][1].lstrip("+")
            if s and s[0] in "01" and len(s) > 1:
                flags.append(float(s[0]))
                tokens[i] = ("num", s[1:])     # Rest bleibt als nächste Zahl
            else:
                flags.append(float(s))
                i += 1
        rest = read_nums(2)
        if rest is None:
            return None
        return [float(v) for v in vals] + flags + [float(v) for v in rest]

    while i < len(tokens):
        kind, val = tokens[i]
        if kind == "cmd":
            cmd = val
            i += 1
            if cmd in "Zz":
                if cur:
                    if cur[-1] != tm(start):
                        cur.append(tm(start))
                    finish()
                pos = start
                last_ctrl, last_cmd = None, "Z"
                continue
        elif cmd is None or cmd in "Zz":
            i += 1          # Zahlen ohne gültiges Kommando ignorieren
            continue
        up = cmd.upper()
        rel = cmd.islower()
        if up == "A":
            args = read_arc_args()
        else:
            raw = read_nums(ARG_COUNT[up])
            args = [float(v) for v in raw] if raw is not None else None
        if args is None:
            # Kaputte Daten: nächstes Kommando suchen
            while i < len(tokens) and tokens[i][0] != "cmd":
                i += 1
            continue
        ox, oy = pos if rel else (0.0, 0.0)
        if up == "M":
            finish()
            pos = (ox + args[0], oy + args[1])
            start = pos
            cur = [tm(pos)]
            last_ctrl = None
            # weitere Koordinatenpaare nach M sind L
            cmd = "l" if rel else "L"
            last_cmd = "M"
            continue
        if not cur:
            cur = [tm(pos)]
        if up == "L":
            pos = (ox + args[0], oy + args[1])
            cur.append(tm(pos))
            last_ctrl = None
        elif up == "H":
            pos = ((ox if rel else 0) + args[0], pos[1])
            cur.append(tm(pos))
            last_ctrl = None
        elif up == "V":
            pos = (pos[0], (oy if rel else 0) + args[0])
            cur.append(tm(pos))
            last_ctrl = None
        elif up == "C":
            c1 = (ox + args[0], oy + args[1])
            c2 = (ox + args[2], oy + args[3])
            end = (ox + args[4], oy + args[5])
            add_cubic(pos, c1, c2, end)
            last_ctrl, pos = c2, end
        elif up == "S":
            if last_ctrl is not None and last_cmd in "CS":
                c1 = (2 * pos[0] - last_ctrl[0], 2 * pos[1] - last_ctrl[1])
            else:
                c1 = pos
            c2 = (ox + args[0], oy + args[1])
            end = (ox + args[2], oy + args[3])
            add_cubic(pos, c1, c2, end)
            last_ctrl, pos = c2, end
        elif up in "QT":
            if up == "Q":
                q = (ox + args[0], oy + args[1])
                end = (ox + args[2], oy + args[3])
            else:
                if last_ctrl is not None and last_cmd in "QT":
                    q = (2 * pos[0] - last_ctrl[0], 2 * pos[1] - last_ctrl[1])
                else:
                    q = pos
                end = (ox + args[0], oy + args[1])
            c1 = (pos[0] + 2 / 3 * (q[0] - pos[0]), pos[1] + 2 / 3 * (q[1] - pos[1]))
            c2 = (end[0] + 2 / 3 * (q[0] - end[0]), end[1] + 2 / 3 * (q[1] - end[1]))
            add_cubic(pos, c1, c2, end)
            last_ctrl, pos = q, end
        elif up == "A":
            end = (ox + args[5], oy + args[6])
            for p0, c1, c2, p3 in arc_to_cubics(pos[0], pos[1], args[0], args[1], args[2],
                                                bool(args[3]), bool(args[4]), end[0], end[1]):
                add_cubic(p0, c1, c2, p3)
            pos = end
            last_ctrl = None
        last_cmd = up
    finish()
    return subpaths


def _ellipse(cx, cy, rx, ry, m, tol):
    d = (f"M{cx + rx},{cy} A{rx},{ry} 0 0 1 {cx},{cy + ry} A{rx},{ry} 0 0 1 {cx - rx},{cy} "
         f"A{rx},{ry} 0 0 1 {cx},{cy - ry} A{rx},{ry} 0 0 1 {cx + rx},{cy} Z")
    return parse_path(d, m, tol)


def element_paths(el, tag, m, tol):
    a = el.attrib
    if tag == "path":
        return parse_path(a.get("d", ""), m, tol)
    if tag == "line":
        pts = [(_user_len(a.get("x1")), _user_len(a.get("y1"))),
               (_user_len(a.get("x2")), _user_len(a.get("y2")))]
        return [apply(m, pts)]
    if tag in ("polyline", "polygon"):
        v = _numbers(a.get("points", ""))
        pts = list(zip(v[0::2], v[1::2]))
        if not pts:
            return []
        if tag == "polygon" and len(pts) > 1:
            pts.append(pts[0])
        return [apply(m, pts)]
    if tag == "rect":
        x, y = _user_len(a.get("x")), _user_len(a.get("y"))
        w, h = _user_len(a.get("width")), _user_len(a.get("height"))
        if w <= 0 or h <= 0:
            return []
        rx = a.get("rx")
        ry = a.get("ry")
        rxv = _user_len(rx if rx is not None else ry, 0.0)
        ryv = _user_len(ry if ry is not None else rx, 0.0)
        rxv, ryv = min(rxv, w / 2), min(ryv, h / 2)
        if rxv > 0 and ryv > 0:
            d = (f"M{x + rxv},{y} H{x + w - rxv} A{rxv},{ryv} 0 0 1 {x + w},{y + ryv} "
                 f"V{y + h - ryv} A{rxv},{ryv} 0 0 1 {x + w - rxv},{y + h} H{x + rxv} "
                 f"A{rxv},{ryv} 0 0 1 {x},{y + h - ryv} V{y + ryv} A{rxv},{ryv} 0 0 1 {x + rxv},{y} Z")
            return parse_path(d, m, tol)
        return [apply(m, [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)])]
    if tag == "circle":
        r = _user_len(a.get("r"))
        if r <= 0:
            return []
        return _ellipse(_user_len(a.get("cx")), _user_len(a.get("cy")), r, r, m, tol)
    if tag == "ellipse":
        rx, ry = _user_len(a.get("rx")), _user_len(a.get("ry"))
        if rx <= 0 or ry <= 0:
            return []
        return _ellipse(_user_len(a.get("cx")), _user_len(a.get("cy")), rx, ry, m, tol)
    return []


# --------------------------------------------------------------------------

def _doc_matrix(root):
    """Benutzereinheiten -> mm, plus Dokumentgröße in mm."""
    vb = _numbers(root.attrib.get("viewBox", ""))
    w_attr, h_attr = root.attrib.get("width"), root.attrib.get("height")
    if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
        vx, vy, vw, vh = vb
        w = parse_length(w_attr, None, percent_of=None)
        h = parse_length(h_attr, None, percent_of=None)
        if w is None and h is None:
            w, h = vw * UNIT_MM["px"], vh * UNIT_MM["px"]
        elif w is None:
            w = h * vw / vh
        elif h is None:
            h = w * vh / vw
        par = root.attrib.get("preserveAspectRatio", "xMidYMid meet").strip()
        sx, sy = w / vw, h / vh
        tx, ty = 0.0, 0.0
        if not par.startswith("none"):
            s = max(sx, sy) if "slice" in par else min(sx, sy)
            align = par.split()[0]
            ex, ey = w - vw * s, h - vh * s
            tx = 0 if "xMin" in align else ex if "xMax" in align else ex / 2
            ty = 0 if "YMin" in align else ey if "YMax" in align else ey / 2
            sx = sy = s
        m = (sx, 0, 0, sy, tx - vx * sx, ty - vy * sy)
        return m, w, h
    k = UNIT_MM["px"]
    w = parse_length(w_attr, None)
    h = parse_length(h_attr, None)
    return (k, 0, 0, k, 0, 0), w, h


def load_svg(path_or_bytes, tol=FLATTEN_TOL_MM) -> Drawing:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        root = ET.fromstring(path_or_bytes)
        source = ""
    else:
        root = ET.parse(path_or_bytes).getroot()
        source = str(path_or_bytes)
    if _local(root.tag) != "svg":
        raise ValueError("Keine SVG-Datei")
    doc_m, w, h = _doc_matrix(root)
    layers: list[Layer] = []
    loose = Layer("Ohne Ebene")
    warnings = set()
    has_layers = any(_local(ch.tag) == "g" and ch.attrib.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer"
                     for ch in root)

    def walk(el, m, hidden, layer, inherited_stroke):
        for ch in el:
            tag = _local(ch.tag)
            if not tag or tag in SKIP_TAGS:
                continue
            props = parse_style(ch)
            if props.get("display") == "none":
                continue
            h_here = hidden
            vis = props.get("visibility")
            if vis in ("hidden", "collapse"):
                h_here = True
            elif vis == "visible":
                h_here = False
            cm = mat_mul(m, parse_transform(ch.attrib.get("transform")))
            stroke = props.get("stroke", inherited_stroke)
            if tag in ("g", "a", "switch", "svg"):
                target = layer
                if layer is None and ch.attrib.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer":
                    name = ch.attrib.get(f"{{{INKSCAPE_NS}}}label") or ch.attrib.get("id") or f"Ebene {len(layers) + 1}"
                    target = Layer(name, _color(stroke))
                    layers.append(target)
                if tag == "svg" and ch is not root:
                    # Verschachteltes <svg>: nur x/y versetzen (vereinfachend)
                    cm = mat_mul(cm, (1, 0, 0, 1, _user_len(ch.attrib.get("x")), _user_len(ch.attrib.get("y"))))
                walk(ch, cm, h_here, target, stroke)
                continue
            if tag in ("text", "tspan", "textPath"):
                if not h_here:
                    warnings.add("Text wird nicht geplottet – in Inkscape vorher „Objekt in Pfad umwandeln“.")
                continue
            if tag in ("image",):
                warnings.add("Eingebettete Bilder werden ignoriert.")
                continue
            if tag == "use":
                warnings.add("<use>-Verweise werden ignoriert (in Inkscape: „Klon lösen“).")
                continue
            if h_here:
                continue
            target = layer if layer is not None else loose
            if layer is None and not loose.paths:
                loose.color = _color(stroke)
            for p in element_paths(ch, tag, cm, tol):
                if len(p) and np.all(np.isfinite(p)):
                    target.paths.append(p)

    walk(root, doc_m, False, None, root.attrib.get("stroke", "#000000"))
    if loose.paths:
        if not has_layers:
            loose.name = "Alle Pfade"
        layers.insert(0, loose)
    layers = [l for l in layers if l.paths]
    if w is None or h is None:
        allp = np.vstack([p for l in layers for p in l.paths]) if layers else np.zeros((1, 2))
        w = float(allp[:, 0].max()) if w is None else w
        h = float(allp[:, 1].max()) if h is None else h
    return Drawing(float(w), float(h), layers, sorted(warnings), source)


def _color(stroke):
    if not stroke or stroke in ("none", "currentColor", "inherit") or stroke.startswith("url("):
        return "#000000"
    return stroke
