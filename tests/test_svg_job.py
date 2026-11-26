import numpy as np
import pytest

from a3plot.job import (KIND_DRAW, MachineSetup, PenSettings, Placement, SheetToMachine,
                        build_job, frame_lines, measure_sheet_angle, rotated_bounds, second_corner)
from a3plot.paths import order_paths, simplify
from a3plot.svgload import load_svg, parse_path, parse_transform, IDENTITY


def svg(body, attrs='width="100mm" height="50mm" viewBox="0 0 200 100"'):
    return f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}>{body}</svg>'.encode()


def test_viewbox_scales_to_mm():
    d = load_svg(svg('<path d="M0 0 L200 100"/>'))
    assert (d.width_mm, d.height_mm) == (100, 50)
    p = d.layers[0].paths[0]
    assert np.allclose(p, [[0, 0], [100, 50]])


def test_relative_commands_and_implicit_lineto():
    pts = parse_path("m10 10 10 0 0 10 h-10 z", IDENTITY, 0.01)[0]
    assert np.allclose(pts, [[10, 10], [20, 10], [20, 20], [10, 20], [10, 10]])


def test_arc_compact_flags_and_endpoint():
    # Halbkreis von (0,0) nach (20,0), Flags ohne Trenner geschrieben
    pts = parse_path("M0 0a10 10 0 0110 10", IDENTITY, 0.01)[0]
    assert np.allclose(pts[-1], [10, 10])
    r = np.hypot(pts[:, 0] - 0, pts[:, 1] - 10)   # Mittelpunkt (0,10)
    assert np.allclose(r, 10, atol=0.02)


def test_cubic_flatten_tolerance():
    pts = parse_path("M0 0 C0 100 100 100 100 0", IDENTITY, 0.02)[0]
    t = np.linspace(0, 1, 2001)
    exact = np.column_stack((300 * t ** 2 * (1 - t) + 100 * t ** 3, 300 * t * (1 - t)))
    # jeder exakte Punkt liegt nah an der Polylinie
    d = min_dist_to_polyline(exact, pts)
    assert d.max() < 0.03


def min_dist_to_polyline(q, poly):
    a, b = poly[:-1], poly[1:]
    out = []
    for p in q:
        ab = b - a
        t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab ** 2).sum(1), 1e-12), 0, 1)
        proj = a + t[:, None] * ab
        out.append(np.hypot(*(proj - p).T).min())
    return np.array(out)


def test_transforms_groups_and_hidden():
    d = load_svg(svg('<g transform="translate(10,0) scale(2)"><line x1="0" y1="0" x2="5" y2="0"/></g>'
                     '<rect x="0" y="0" width="10" height="10" style="display:none"/>',
                     'width="100" height="100"'))
    pts = d.layers[0].paths
    assert len(pts) == 1
    k = 25.4 / 96
    assert np.allclose(pts[0], np.array([[10, 0], [20, 0]]) * k)


def test_inkscape_layers_and_text_warning():
    body = ('<g xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" inkscape:groupmode="layer" '
            'inkscape:label="Rot" stroke="#f00"><path d="M0 0 L1 1"/></g>'
            '<g xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" inkscape:groupmode="layer" '
            'inkscape:label="Blau"><path d="M0 0 L2 2"/><text>hi</text></g>')
    d = load_svg(svg(body))
    assert [l.name for l in d.layers] == ["Rot", "Blau"]
    assert d.layers[0].color == "#f00"
    assert d.warnings


def test_rotate_transform():
    m = parse_transform("rotate(90)")
    assert np.allclose(m, (0, 1, -1, 0, 0, 0), atol=1e-12)


def test_simplify_keeps_shape():
    x = np.linspace(0, 10, 1001)
    p = np.column_stack((x, np.zeros_like(x)))
    assert len(simplify(p, 0.01)) == 2


def test_order_paths_reverses_when_closer():
    a = np.array([[0, 0], [10, 0]], float)
    b = np.array([[20, 0], [11, 0]], float)    # Ende liegt näher an a's Ende
    out = order_paths([b, a])
    assert np.allclose(out[0], a) and np.allclose(out[1], b[::-1])


def test_order_paths_kdtree_matches_count():
    rng = np.random.default_rng(1)
    paths = [rng.random((3, 2)) * 100 for _ in range(1000)]
    out = order_paths(paths)
    assert len(out) == 1000
    ids = {tuple(np.round(p[0], 9)) for p in out} | {tuple(np.round(p[-1], 9)) for p in out}
    assert len(ids) == 2000


def test_sheet_to_machine_bottom_left_y_up():
    s = SheetToMachine(MachineSetup(origin_corner="bl", x_right=True, y_up=True), 210, 297)
    m = s.to_machine(np.array([[0, 297], [10, 287], [210, 0]]))
    assert np.allclose(m, [[0, 0], [10, 10], [210, 297]])
    assert np.allclose(s.to_sheet(10, 10), (10, 287))


def test_sheet_to_machine_with_offset_and_top_right():
    st = MachineSetup(origin_corner="tr", x_right=False, y_up=False, origin_offset=(5, 7))
    s = SheetToMachine(st, 210, 297)
    assert np.allclose(s.to_machine(np.array([[210, 0], [200, 10]])), [[5, 7], [15, 17]])


def test_second_corner_is_on_short_side():
    # A3 hoch, Nullpunkt unten links: kurze Seite ist die untere Kante -> unten rechts
    assert second_corner(MachineSetup(origin_corner="bl"), 297, 420) == ("br", (297, 420), 297)
    # A3 quer: kurze Seite ist die linke Kante -> oben links
    assert second_corner(MachineSetup(origin_corner="bl"), 420, 297) == ("tl", (0, 0), 297)
    assert second_corner(MachineSetup(origin_corner="tr"), 210, 297) == ("tl", (0, 0), 210)
    assert second_corner(MachineSetup(origin_corner="tr"), 297, 210) == ("br", (297, 210), 210)


@pytest.mark.parametrize("corner,x_right,y_up,paper", [
    ("bl", True, True, (297, 420)), ("bl", True, True, (420, 297)),
    ("tr", False, False, (210, 297)), ("br", True, False, (297, 210)),
    ("tl", False, True, (297, 420)),
])
@pytest.mark.parametrize("angle", [0.0, 3.7, -12.5, 90.0, 179.0])
def test_measured_angle_reproduces_sheet(corner, x_right, y_up, paper, angle):
    """Blatt physikalisch gedreht hinlegen, 2. Ecke anfahren -> alle Ecken landen richtig."""
    w, h = paper
    true = MachineSetup(origin_corner=corner, x_right=x_right, y_up=y_up,
                        origin_offset=(40.0, 25.0), sheet_angle=angle)
    s_true = SheetToMachine(true, w, h)
    _, uv2, side = second_corner(true, w, h)
    x2, y2 = s_true.to_machine(np.array([uv2]))[0]
    measured = MachineSetup(**{**vars(true), "sheet_angle": 0.0})
    got, length = measure_sheet_angle(measured, w, h, x2, y2)
    assert got == pytest.approx(angle, abs=1e-9)
    assert length == pytest.approx(side)
    measured.sheet_angle = got
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h], [w / 3, h / 4]])
    assert np.allclose(SheetToMachine(measured, w, h).to_machine(corners), s_true.to_machine(corners))


def test_rotation_turns_around_origin_corner():
    # A4 hoch, unten links bei (10, 20), Blatt um 90° gegen den Uhrzeigersinn gedreht:
    # die untere Kante (u-Richtung) zeigt dann in +Y
    st = MachineSetup(origin_corner="bl", origin_offset=(10, 20), sheet_angle=90)
    s = SheetToMachine(st, 210, 297)
    m = s.to_machine(np.array([[0, 297], [210, 297], [0, 0]]))
    assert np.allclose(m, [[10, 20], [10, 230], [-287, 20]])
    for u, v in ((0, 297), (35.5, 12), (210, 0)):
        x, y = s.to_machine(np.array([[u, v]]))[0]
        assert np.allclose(s.to_sheet(x, y), (u, v))


def test_small_tilt_moves_far_corner():
    st = MachineSetup(origin_corner="bl", sheet_angle=1.0)
    x, y = SheetToMachine(st, 297, 420).to_machine(np.array([[297, 420]]))[0]
    # 1° auf 297 mm: rund 5,2 mm nach oben, X kaum verändert
    assert x == pytest.approx(297 * np.cos(np.radians(1)))
    assert y == pytest.approx(297 * np.sin(np.radians(1)))


def test_job_and_frame_follow_rotation():
    path = np.array([[0, 0], [100, 0]], float)
    st = MachineSetup(sheet_angle=90)
    job = build_job([path], Placement(x=0, y=297, scale=1), (210, 297), PenSettings(), st,
                    bounds=rotated_bounds([path], 0), park=False)
    # Strich entlang der unteren Blattkante verläuft bei 90° Drehung entlang +Y
    assert "G1X0.00Y100.00F400" in job.lines
    fr = frame_lines((0, 0, 210, 297), st, (210, 297))
    assert fr[2:] == ["G0X-297Y0", "G0X-297Y210", "G0X0Y210", "G0X0Y0", "G0X-297Y0"]


def test_job_gcode_and_placement():
    path = np.array([[0, 0], [100, 0]], float)
    pl = Placement(x=10, y=20, scale=0.5)
    job = build_job([path], pl, (210, 297), PenSettings(), MachineSetup(), bounds=rotated_bounds([path], 0))
    # Blatt (10,20)->(60,20); Maschine bl/y_up: Y = 297-20 = 277
    assert "G0X10Y277" in job.lines
    assert "G1X60.00Y277.00F400" in job.lines
    assert job.lines.index("M3S30") < job.lines.index("G1X60.00Y277.00F400")
    assert job.lines[-1] == "M5"
    assert all(len(l) + 1 < 80 for l in job.lines)


def test_estimate_straight_line():
    # 100 mm bei 300 mm/min = 20 s, plus Rampen (a=100 mm/s²: vernachlässigbar), plus Dwells
    path = np.array([[0, 0], [100, 0]], float)
    pen = PenSettings(draw_speed=300, travel_speed=500, down_delay_ms=0, up_delay_ms=0)
    job = build_job([path], Placement(x=0, y=297, scale=1), (210, 297), pen, MachineSetup(),
                    bounds=rotated_bounds([path], 0), park=False)
    draw = job.duration[job.kind == KIND_DRAW].sum()
    assert draw == pytest.approx(20.0 + 5 / 100 / 1, rel=0.01)


def test_estimate_many_tiny_segments_is_slower_than_straight():
    n = 400
    zig = np.column_stack((np.linspace(0, 100, n), np.tile([0, 1], n // 2)))
    pen = PenSettings(draw_speed=400, down_delay_ms=0, up_delay_ms=0)
    j1 = build_job([zig], Placement(x=0, y=100, scale=1), (210, 297), pen, MachineSetup(),
                   bounds=rotated_bounds([zig], 0), park=False)
    straight_time = 100 / (400 / 60)
    assert j1.duration[j1.kind == KIND_DRAW].sum() > straight_time * 1.2


def test_locate_finds_segment():
    path = np.array([[0, 0], [50, 0], [50, 50]], float)
    job = build_job([path], Placement(x=0, y=297, scale=1), (210, 297), PenSettings(), MachineSetup(),
                    bounds=rotated_bounds([path], 0), park=False)
    i2 = job.lines.index("G1X50.00Y-50.00")   # unterhalb des Blatts: Y negativ
    idx, frac = job.locate((50, -25), 0, len(job.lines) - 1)
    assert idx == i2 and frac == pytest.approx(0.5)
