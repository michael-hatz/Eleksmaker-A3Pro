"""GUI-Tests ohne Bildschirm und ohne Plotter (Einstellungen in einem Temp-Ordner)."""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="a3plot-test-")

import numpy as np
import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import QApplication, QDialog

from a3plot import gui
from a3plot.svgload import load_svg


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeStreamer:
    def __init__(self):
        self.commands, self.resumes, self.realtimes = [], [], []
        self.aborting = None
        self.stopped = False

    def command(self, l):
        self.commands.append(l)

    def realtime(self, c):
        self.realtimes.append(c)

    def resume(self, prefix=()):
        self.resumes.append(list(prefix))

    def hold(self):
        pass

    def abort(self):
        pass

    def stop(self):
        self.stopped = True


@pytest.fixture
def win(app, tmp_path):
    w = gui.MainWindow()
    w.streamer = FakeStreamer()
    w.connected, w.grbl_state = True, "Idle"
    svg = tmp_path / "a.svg"
    svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" '
                    b'viewBox="0 0 100 50"><path d="M0 0 L100 50"/></svg>')
    d = load_svg(str(svg))
    w.on_loaded((str(svg), d, [list(l.paths) for l in d.layers]))
    yield w
    w.plot = None
    w.streamer = None
    w.close()


def test_stale_build_does_not_clear_dirty(win):
    win.start_build()              # Build mit alten Werten läuft …
    gen_old = win.job_gen
    old_job = gui.build_job(*win.job_inputs()[:5])
    win.placement.x += 30          # … und währenddessen ändert man etwas
    win.schedule_build()
    win.on_built(gen_old, old_job)  # alter Build meldet sich fertig
    assert win.job_dirty
    job = win.ensure_job()
    assert job is not old_job
    assert job.strokes_sheet[0][:, 0].min() == pytest.approx(old_job.strokes_sheet[0][:, 0].min() + 30)


def _plotting(win, state):
    win.plot = {"state": "running", "paused_at": None, "pause_total": 0.0}
    win.on_job_state(state)


def test_resume_after_soft_pause_then_hold_lifts_pen(win):
    _plotting(win, "soft_paused")
    win.on_job_state("holding")
    win.resume_plot()
    assert win.streamer.resumes == [win.resume_prefix()]
    # nächstes Halten mitten im Strich: ohne Präfix (Stift soll unten bleiben)
    win.on_job_state("running")
    win.on_job_state("holding")
    win.resume_plot()
    assert win.streamer.resumes[-1] == []


@pytest.mark.parametrize("state,allowed", [("holding", False), ("running", False), ("soft_paused", True)])
def test_console_during_plot(win, state, allowed):
    _plotting(win, state)
    win.console.setText("G0X0Y0")
    win.send_console()
    assert (win.streamer.commands == ["G0X0Y0"]) == allowed
    win.console.setText("!")
    win.send_console()
    assert win.streamer.realtimes == [b"!"]          # Echtzeitbefehle immer


def test_loading_during_plot_keeps_drawing(win, tmp_path):
    before = win.drawing
    _plotting(win, "running")
    svg = tmp_path / "b.svg"
    svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm">'
                    b'<path d="M0 0 L10 10"/></svg>')
    d = load_svg(str(svg))
    win.on_loaded((str(svg), d, [list(l.paths) for l in d.layers]))
    assert win.drawing is before


def test_close_survives_disconnect_while_waiting(win):
    class Vanishing(FakeStreamer):
        @property
        def aborting(self):
            win.streamer = None          # "disconnected" kommt während processEvents()
            return None

        @aborting.setter
        def aborting(self, v):
            pass

    s = Vanishing()
    win.streamer = s
    _plotting(win, "running")
    win.ask = lambda t: True
    win.close()
    assert s.stopped


def test_settings_change_resets_angle(win, monkeypatch):
    win.setup.sheet_angle = 1.5

    class Dlg:
        def __init__(self, parent, setup, mode, budget, park):
            self.corner = type("C", (), {"currentData": lambda _s: "tl"})()
            self.xdir = type("C", (), {"currentIndex": lambda _s: 0})()
            self.ydir = type("C", (), {"currentIndex": lambda _s: 0})()
            self.mode = type("C", (), {"currentData": lambda _s: mode})()
            self.budget = type("C", (), {"value": lambda _s: budget})()
            self.park = type("C", (), {"isChecked": lambda _s: park})()

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui, "SettingsDialog", Dlg)
    win.setup.origin_corner = "bl"
    win.edit_settings()
    assert win.setup.sheet_angle == 0.0
    win.setup.sheet_angle = 1.5    # gleiche Ecke/Achsen erneut bestätigen: bleibt
    win.edit_settings()
    assert win.setup.sheet_angle == 1.5


def test_handles_sit_on_drawing_corners_at_large_scale(app):
    from a3plot.sheetview import SheetView
    v = SheetView()
    v.set_paper(297, 420, 10)
    v.set_drawing([], 10, 10)
    v.set_placement(20, 30, 19)
    corners = [(c.x(), c.y()) for c in v._corners()]
    assert np.allclose(corners, [(20, 30), (210, 30), (210, 220), (20, 220)])
