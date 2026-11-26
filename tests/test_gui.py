"""GUI-Tests ohne Bildschirm und ohne Plotter (Einstellungen in einem Temp-Ordner)."""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="a3plot-test-")
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="a3plot-test-data-")  # Historie nicht ins echte ~

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
    if not allowed:
        assert "Während des Plots gesperrt" in win.log.toPlainText()
        assert win.console.text() == "G0X0Y0"       # Eingabe bleibt stehen
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
    label = win.file_label.text()
    assert "a.svg" in label
    win.file_label.setText("Lade b.svg …")
    win.on_loaded((str(svg), d, [list(l.paths) for l in d.layers]))
    assert win.drawing is before
    assert win.file_label.text() == label
    assert "b.svg nicht geöffnet" in win.statusBar().currentMessage()


def test_failed_load_keeps_label_of_loaded_drawing(win, monkeypatch):
    label = win.file_label.text()
    monkeypatch.setattr(gui.QMessageBox, "critical", lambda *a: None)
    win.on_loaded(("kaputt.svg", ValueError("kaputt"), None))
    assert win.file_label.text() == label and win.drawing is not None


def test_pause_point_reached_during_hold_stays_holding(win):
    _plotting(win, "holding")
    win.on_job_state("soft_paused")
    assert win.plot["state"] == "holding"
    assert win.manual_blocked()
    win.resume_plot()
    assert win.streamer.resumes == [win.resume_prefix()]   # Stift hoch nach dem Strich


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


def test_close_does_not_wait_for_vanished_streamer(win):
    import time
    s = FakeStreamer()
    s.aborting = {"phase": "hold"}       # Worker beendet, aborting bleibt stehen
    win.streamer = s
    _plotting(win, "running")
    win.ask = lambda t: True
    orig = gui.QApplication.processEvents

    def disconnect_once():
        win.streamer = None
        orig()

    gui.QApplication.processEvents = staticmethod(disconnect_once)
    try:
        t0 = time.monotonic()
        win.close()
        took = time.monotonic() - t0
    finally:
        gui.QApplication.processEvents = staticmethod(orig)
    assert s.stopped
    assert took < 1.0


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
    assert "Papierecke = hier" in win.log.toPlainText()
    # auch ohne Drehung muss gewarnt werden: die Blattecke stammt von der alten Ecke
    win.log.clear()
    win.setup.origin_corner = "bl"
    win.edit_settings()
    assert "Papierecke = hier" in win.log.toPlainText()
    win.log.clear()
    win.setup.sheet_angle = 1.5    # gleiche Ecke/Achsen erneut bestätigen: bleibt
    win.edit_settings()
    assert win.setup.sheet_angle == 1.5
    assert "Papierecke" not in win.log.toPlainText()


def test_handles_sit_on_drawing_corners_at_large_scale(app):
    from a3plot.sheetview import SheetView
    v = SheetView()
    v.set_paper(297, 420, 10)
    v.set_drawing([], 10, 10)
    v.set_placement(20, 30, 19)
    corners = [(c.x(), c.y()) for c in v._corners()]
    assert np.allclose(corners, [(20, 30), (210, 30), (210, 220), (20, 220)])


def _svg(tmp_path, name="b.svg"):
    svg = tmp_path / name
    svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm">'
                    b'<path d="M0 0 L10 10"/></svg>')
    return str(svg), load_svg(str(svg))


def test_new_plot_closes_drawing_and_clears_progress(win, tmp_path):
    win.ask = lambda t: True
    job = win.ensure_job()
    win.view.add_done(job.strokes_sheet)
    win.last_label.setText("<b>Fertig</b>")
    win.progress.setValue(1000)
    gen = win.job_gen
    win.new_plot()
    assert win.drawing is None and win.job is None and win.layers.count() == 0
    assert win.file_label.text() == "Keine Datei geladen"
    assert win.windowTitle() == "A3 Plot"
    assert not win.view.content.isVisible() and not win.view.done_items
    assert win.progress.value() == 0 and win.last_label.text() == ""
    assert win.job_gen > gen                      # laufender Build wird verworfen
    assert not win.btn_close_drawing.isEnabled() and not win.act_save.isEnabled()
    assert not win.btn_start.isEnabled()
    # danach lässt sich wieder laden
    fn, d = _svg(tmp_path)
    win.on_loaded((fn, d, [list(l.paths) for l in d.layers]))
    assert win.drawing is d and win.btn_close_drawing.isEnabled() and win.btn_start.isEnabled()


def test_new_plot_asks_and_keeps_drawing_when_declined(win):
    asked = []
    win.ask = lambda t: asked.append(t) or False
    before, label = win.drawing, win.file_label.text()
    win.new_plot()
    assert asked and win.drawing is before and win.file_label.text() == label
    win.ask = lambda t: True
    win.new_plot()
    assert win.drawing is None


def test_new_plot_keeps_placement_fields_in_sync(win):
    win.ask = lambda t: True
    win.pscale.setValue(50)
    p = win.placement
    win.new_plot()
    assert win.placement is p          # Modell bleibt, also passen die Felder weiter dazu
    assert win.pscale.value() == pytest.approx(p.scale * 100)
    assert win.px.value() == pytest.approx(p.x) and win.py.value() == pytest.approx(p.y)


def test_new_plot_blocked_during_plot(win, monkeypatch):
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a: None)
    before = win.drawing
    _plotting(win, "soft_paused")
    win.update_buttons()
    assert not win.act_new.isEnabled() and not win.btn_close_drawing.isEnabled()
    win.new_plot()
    assert win.drawing is before


def test_loading_new_file_clears_old_progress(win, tmp_path):
    win.view.add_done(win.ensure_job().strokes_sheet)
    win.last_label.setText("<b>Fertig</b>")
    fn, d = _svg(tmp_path)
    win.on_loaded((fn, d, [list(l.paths) for l in d.layers]))
    assert win.drawing is d
    assert not win.view.done_items and win.last_label.text() == ""


def test_load_overtaken_by_new_plot_is_dropped(win, tmp_path):
    win.ask = lambda t: True
    fn, d = _svg(tmp_path)
    QApplication.setOverrideCursor(gui.Qt.CursorShape.WaitCursor)   # wie in load_file
    win.load_gen += 1                 # Laden läuft …
    gen = win.load_gen
    win.new_plot()                    # … und der Benutzer räumt auf
    assert QApplication.overrideCursor() is None
    win.on_loaded((fn, d, [list(l.paths) for l in d.layers], gen))
    assert win.drawing is None and not win.loading
    assert QApplication.overrideCursor() is None
    # ein späteres Laden wird wieder angezeigt
    fn2, d2 = _svg(tmp_path, "c.svg")
    QApplication.setOverrideCursor(gui.Qt.CursorShape.WaitCursor)
    win.load_gen += 1
    win.on_loaded((fn2, d2, [list(l.paths) for l in d2.layers], win.load_gen))
    assert win.drawing is d2 and not win.loading
    assert QApplication.overrideCursor() is None


def test_wheel_only_changes_focused_fields(win):
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    sb = win.pen_down
    v0 = sb.value()

    def wheel():
        ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120),
                         gui.Qt.MouseButton.NoButton, gui.Qt.KeyboardModifier.NoModifier,
                         gui.Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(sb, ev)

    wheel()
    assert sb.value() == v0
    win.show()
    win.tabs.setCurrentIndex(1)
    sb.setFocus()
    QApplication.processEvents()
    assert sb.hasFocus()
    wheel()
    assert sb.value() == v0 + 1


def test_history_path_windows_uses_appdata():
    p = gui.history_path("win32", {"APPDATA": r"C:\Users\x\AppData\Roaming"})
    assert p == os.path.join(r"C:\Users\x\AppData\Roaming", "a3plot", "history.csv")


def test_history_path_windows_without_appdata_falls_back_to_roaming():
    p = gui.history_path("win32", {})
    assert p == os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "a3plot", "history.csv")


def test_wheel_over_unfocused_field_scrolls_page(win):
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    win.show()
    win.tabs.setCurrentIndex(1)
    page = win.tabs.currentWidget()
    win.resize(win.width(), 260)
    page.setFixedHeight(80)            # Reiter sicher niedriger als sein Inhalt
    QApplication.processEvents()
    bar = page.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(0)
    sb = win.pen_down
    sb.clearFocus()
    v0 = sb.value()
    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120),
                     gui.Qt.MouseButton.NoButton, gui.Qt.KeyboardModifier.NoModifier,
                     gui.Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(sb, ev)
    assert sb.value() == v0           # Feld unverändert …
    assert bar.value() > 0            # … stattdessen scrollt die Seite


def test_settings_dialog_fields_are_wheel_guarded(win, monkeypatch):
    seen = []

    def fake_exec(dlg):
        seen.extend(w for w in dlg.findChildren((gui.QAbstractSpinBox, gui.QComboBox)))
        return gui.QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui.SettingsDialog, "exec", fake_exec)
    win.edit_settings()
    assert seen and all(w.focusPolicy() == gui.Qt.FocusPolicy.StrongFocus for w in seen)


def test_history_path_linux_uses_xdg_data_home():
    p = gui.history_path("linux", {"XDG_DATA_HOME": "/data"})
    assert p == os.path.join("/data", "a3plot", "history.csv")


def test_history_in_tests_does_not_touch_real_home():
    assert not gui.HISTORY.startswith(os.path.join(os.path.expanduser("~"), ".local"))


def test_history_path_linux_unchanged():
    p = gui.history_path("linux", {"APPDATA": "ignored"})
    assert p == os.path.join(os.path.expanduser("~"), ".local", "share", "a3plot", "history.csv")


def test_port_choices_linux_always_offers_ttyusb0():
    assert gui.port_choices([], "linux") == ["/dev/ttyUSB0"]
    assert gui.port_choices(["/dev/ttyACM0"], "linux") == ["/dev/ttyACM0", "/dev/ttyUSB0"]


def test_port_choices_windows_lists_only_found_ports():
    assert gui.port_choices(["COM4", "COM3"], "win32") == ["COM3", "COM4"]
    assert gui.port_choices([], "win32") == []


def test_history_written_as_utf8(win, tmp_path, monkeypatch):
    hist = tmp_path / "sub" / "history.csv"
    monkeypatch.setattr(gui, "HISTORY", str(hist))
    real_open = open

    def cp1252_default_open(file, mode="r", *a, encoding=None, **kw):
        # simuliert Windows: ohne encoding= würde cp1252 geschrieben
        return real_open(file, mode, *a, encoding=encoding or "cp1252", **kw)

    monkeypatch.setattr("builtins.open", cp1252_default_open)
    win.drawing = type("D", (), {"source": "/x/Büro_ß.svg"})()
    win.plot = {"wall0": gui.datetime(2026, 11, 26, 12, 0, 0)}
    job = type("J", (), {"total_time": 60.0, "draw_length": 100.0})()
    win.write_history("finished", 61.0, 59.0, job)
    text = hist.read_bytes().decode("utf-8")
    assert text.splitlines()[0].startswith("start,datei,")
    assert "Büro_ß.svg,finished,61,59,60,100," in text
