"""Hauptfenster von A3 Plot."""
from __future__ import annotations

import csv
import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta

import numpy as np
from PyQt6.QtCore import QObject, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QKeySequence, QTextCursor
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox,
                             QToolButton, QVBoxLayout, QWidget)

from . import __version__
from .grbl import GrblStreamer
from .job import (CORNER_NAMES, KIND_DWELL, KIND_PEN, PAPER_SIZES, MachineSetup, Placement,
                  PenSettings, SheetToMachine, build_job, fmt, format_duration, frame_lines,
                  measure_sheet_angle, rotate_points, rotated_bounds, second_corner)
from .paths import order_paths, simplify
from .sheetview import SheetView, polylines_path
from .svgload import load_svg

LOAD_SIMPLIFY_MM = 0.01
HISTORY = os.path.join(os.path.expanduser("~"), ".local", "share", "a3plot", "history.csv")


class Bridge(QObject):
    grbl = pyqtSignal(str, object)
    built = pyqtSignal(int, object)
    loaded = pyqtSignal(object)


def spin(lo, hi, val, step=1.0, dec=1, suffix=""):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    return s


def ispin(lo, hi, val, suffix=""):
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    return s


# ==========================================================================

class SettingsDialog(QDialog):
    def __init__(self, parent, setup: MachineSetup, mode, budget, park):
        super().__init__(parent)
        self.setWindowTitle("Einstellungen")
        f = QFormLayout(self)
        self.corner = QComboBox()
        for k, t in (("bl", "unten links (vorne links)"), ("tl", "oben links (hinten links)"),
                     ("tr", "oben rechts (hinten rechts)"), ("br", "unten rechts (vorne rechts)")):
            self.corner.addItem(t, k)
        self.corner.setCurrentIndex(max(0, self.corner.findData(setup.origin_corner)))
        f.addRow("Blattecke am Nullpunkt:", self.corner)
        self.xdir = QComboBox()
        self.xdir.addItems(["nach rechts", "nach links"])
        self.xdir.setCurrentIndex(0 if setup.x_right else 1)
        f.addRow("+X fährt:", self.xdir)
        self.ydir = QComboBox()
        self.ydir.addItems(["nach hinten (auf dem Blatt nach oben)", "nach vorne (auf dem Blatt nach unten)"])
        self.ydir.setCurrentIndex(0 if setup.y_up else 1)
        f.addRow("+Y fährt:", self.ydir)
        f.addRow(QLabel("<i>Prüfen: Mit den Pfeiltasten unter „Maschine“ fahren – der Stift muss in "
                        "Pfeilrichtung fahren.</i>"))
        self.mode = QComboBox()
        self.mode.addItem("Sicher: Zeile für Zeile (empfohlen)", "safe")
        self.mode.addItem("Zeichen zählen (schneller bei sehr kurzen Segmenten)", "count")
        self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
        f.addRow("Übertragung:", self.mode)
        self.budget = ispin(16, 127, budget, " Byte")
        self.budget.setToolTip("Höchstens so viele unbestätigte Zeichen im 127-Byte-Empfangspuffer von GRBL.")
        f.addRow("Puffer-Obergrenze (Zählmodus):", self.budget)
        self.park = QCheckBox("Nach dem Plot zum Nullpunkt fahren")
        self.park.setChecked(park)
        f.addRow(self.park)
        f.addRow(QLabel(
            f"<small>Für die Zeitschätzung (aus $$ gelesen): max. {setup.max_rate_x:.0f}/{setup.max_rate_y:.0f} mm/min, "
            f"Beschleunigung {setup.accel_x:.0f}/{setup.accel_y:.0f} mm/s², Junction Deviation {setup.junction_deviation:.3f} mm</small>"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


# ==========================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("A3 Plot")
        self.resize(1400, 900)
        self.settings = QSettings("EleksmakerA3Pro", "A3Plot")
        self.bridge = Bridge()
        self.bridge.grbl.connect(self.on_grbl)
        self.bridge.built.connect(self.on_built)
        self.bridge.loaded.connect(self.on_loaded)

        st = self.settings
        self.pen = PenSettings(
            up=int(st.value("pen/up", 25)), down=int(st.value("pen/down", 30)),
            down_delay_ms=int(st.value("pen/down_delay", 150)), up_delay_ms=int(st.value("pen/up_delay", 100)),
            draw_speed=float(st.value("pen/draw_speed", 400)), travel_speed=float(st.value("pen/travel_speed", 500)))
        self.setup = MachineSetup(
            origin_corner=st.value("machine/corner", "bl"),
            x_right=st.value("machine/x_right", "true") == "true",
            y_up=st.value("machine/y_up", "true") == "true")
        self.mode = st.value("stream/mode", "safe")
        self.budget = int(st.value("stream/budget", 64))
        self.park = st.value("plot/park", "true") == "true"
        self.placement = Placement()
        self.drawing = None
        self.drawing_label = "Keine Datei geladen"   # Beschriftung der geladenen Zeichnung
        self.ordered = []            # pro Ebene: sortierte Pfade
        self.bounds = None           # Grenzen aller Ebenen (Dokument-mm, gedreht)
        self.job = None
        self.job_gen = 0
        self.job_dirty = True
        self.streamer = None
        self.connected = False
        self.grbl_state = "–"
        self.mpos = (0.0, 0.0)
        self.plot = None             # Laufzeitdaten eines Plots

        self._build_ui()
        self.on_paper_changed()
        self.refresh_ports()
        self.update_buttons()

        self.rebuild_timer = QTimer(self, singleShot=True, interval=350)
        self.rebuild_timer.timeout.connect(self.start_build)
        self.ui_timer = QTimer(self, interval=250)
        self.ui_timer.timeout.connect(self.update_progress)
        self.ui_timer.start()

    # ================================================================ UI
    def _build_ui(self):
        self.view = SheetView()
        self.view.placementChanged.connect(self.on_view_placement)
        self.view.placementCommitted.connect(self.schedule_build)
        self.setCentralWidget(self.view)

        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(6, 6, 6, 6)

        # --- Datei
        g = QGroupBox("Zeichnung")
        v = QVBoxLayout(g)
        b = QPushButton("SVG öffnen …")
        b.clicked.connect(self.open_svg)
        v.addWidget(b)
        self.file_label = QLabel("Keine Datei geladen")
        self.file_label.setWordWrap(True)
        v.addWidget(self.file_label)
        self.layers = QListWidget()
        self.layers.setMaximumHeight(80)
        self.layers.itemChanged.connect(self.on_layers_changed)
        v.addWidget(self.layers)
        lay.addWidget(g)

        # --- Blatt & Position
        g = QGroupBox("Blatt und Position")
        f = QFormLayout(g)
        self.paper = QComboBox()
        for name in ("A4", "A3"):
            self.paper.addItem(f"{name} Hochformat", (name, False))
            self.paper.addItem(f"{name} Querformat", (name, True))
        self.paper.setCurrentIndex(int(self.settings.value("paper/index", 0)))
        self.paper.currentIndexChanged.connect(self.on_paper_changed)
        f.addRow("Papier:", self.paper)
        self.margin = spin(0, 50, float(self.settings.value("paper/margin", 10)), 1, 0, " mm")
        self.margin.valueChanged.connect(self.on_paper_changed)
        f.addRow("Rand:", self.margin)
        row = QHBoxLayout()
        self.px = spin(-500, 900, 0, 1, 1, " mm")
        self.py = spin(-500, 900, 0, 1, 1, " mm")
        row.addWidget(QLabel("X"))
        row.addWidget(self.px)
        row.addWidget(QLabel("Y"))
        row.addWidget(self.py)
        f.addRow("Position:", row)
        row = QHBoxLayout()
        self.pw = spin(0.1, 2000, 100, 1, 1, " mm")
        self.ph = spin(0.1, 2000, 100, 1, 1, " mm")
        row.addWidget(QLabel("B"))
        row.addWidget(self.pw)
        row.addWidget(QLabel("H"))
        row.addWidget(self.ph)
        f.addRow("Größe:", row)
        self.pscale = spin(0.1, 10000, 100, 1, 1, " %")
        f.addRow("Skalierung:", self.pscale)
        for w in (self.px, self.py, self.pw, self.ph, self.pscale):
            w.valueChanged.connect(self.on_fields_changed)
        row = QHBoxLayout()
        self.place_buttons = []
        for text, fn, tip in (("Einpassen", self.fit_to_margin, "So groß wie möglich innerhalb des Rands"),
                              ("Zentrieren", self.center, "Auf dem Blatt zentrieren"),
                              ("↻ 90°", self.rotate, "Um 90° im Uhrzeigersinn drehen"),
                              ("1:1", self.original_size, "Originalgröße aus dem SVG")):
            bt = QPushButton(text)
            bt.setToolTip(tip)
            bt.clicked.connect(fn)
            row.addWidget(bt)
            self.place_buttons.append(bt)
        f.addRow(row)
        self.place_warn = QLabel()
        self.place_warn.setStyleSheet("color:#b5261e")
        self.place_warn.setWordWrap(True)
        f.addRow(self.place_warn)
        self.place_widgets = [self.paper, self.margin, self.px, self.py, self.pw, self.ph, self.pscale] + self.place_buttons
        lay.addWidget(g)

        # --- Stift
        g = QGroupBox("Stift")
        f = QFormLayout(g)
        row = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(20, 500)
        self.speed = ispin(20, 5000, int(self.pen.draw_speed), " mm/min")
        self.speed_slider.setValue(int(self.pen.draw_speed))
        self.speed_slider.valueChanged.connect(self.speed.setValue)
        self.speed.valueChanged.connect(lambda v: (self.speed_slider.blockSignals(True),
                                                   self.speed_slider.setValue(v),
                                                   self.speed_slider.blockSignals(False),
                                                   self.on_pen_changed()))
        row.addWidget(self.speed_slider, 1)
        row.addWidget(self.speed)
        f.addRow("Zeichnen:", row)
        self.travel = ispin(20, 5000, int(self.pen.travel_speed), " mm/min")
        self.travel.valueChanged.connect(self.on_pen_changed)
        f.addRow("Leerfahrt:", self.travel)
        row = QHBoxLayout()
        self.pen_up = ispin(0, 90, self.pen.up)
        self.pen_down = ispin(0, 90, self.pen.down)
        row.addWidget(QLabel("oben S"))
        row.addWidget(self.pen_up)
        row.addWidget(QLabel("unten S"))
        row.addWidget(self.pen_down)
        f.addRow("Servo:", row)
        row = QHBoxLayout()
        self.down_delay = ispin(0, 2000, self.pen.down_delay_ms, " ms")
        self.up_delay = ispin(0, 2000, self.pen.up_delay_ms, " ms")
        row.addWidget(QLabel("ab"))
        row.addWidget(self.down_delay)
        row.addWidget(QLabel("auf"))
        row.addWidget(self.up_delay)
        f.addRow("Warten nach:", row)
        for w in (self.pen_up, self.pen_down, self.down_delay, self.up_delay):
            w.valueChanged.connect(self.on_pen_changed)
        row = QHBoxLayout()
        self.btn_pen_up = QPushButton("Stift hoch")
        self.btn_pen_up.clicked.connect(lambda: self.manual([f"M3S{self.pen_up.value()}"]))
        self.btn_pen_down = QPushButton("Stift runter")
        self.btn_pen_down.clicked.connect(lambda: self.manual([f"M3S{self.pen_down.value()}"]))
        row.addWidget(self.btn_pen_up)
        row.addWidget(self.btn_pen_down)
        f.addRow(row)
        self.pen_widgets = [self.speed, self.speed_slider, self.travel, self.pen_up, self.pen_down,
                            self.down_delay, self.up_delay]
        lay.addWidget(g)

        # --- Maschine
        g = QGroupBox("Maschine")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.port = QComboBox()
        self.port.setEditable(True)
        row.addWidget(self.port, 1)
        bt = QToolButton()
        bt.setText("↻")
        bt.setToolTip("Ports neu suchen")
        bt.clicked.connect(self.refresh_ports)
        row.addWidget(bt)
        self.btn_connect = QPushButton("Verbinden")
        self.btn_connect.clicked.connect(self.toggle_connect)
        row.addWidget(self.btn_connect)
        v.addLayout(row)
        self.state_label = QLabel("Nicht verbunden")
        v.addWidget(self.state_label)
        jog = QGridLayout()
        self.jog_buttons = []
        for text, r, c, du, dv in (("↑", 0, 1, 0, -1), ("←", 1, 0, -1, 0), ("→", 1, 2, 1, 0), ("↓", 2, 1, 0, 1)):
            bt = QPushButton(text)
            bt.setFixedWidth(40)
            bt.clicked.connect(lambda _=False, du=du, dv=dv: self.jog(du, dv))
            jog.addWidget(bt, r, c)
            self.jog_buttons.append(bt)
        self.jog_step = QComboBox()
        for s in ("0.1", "1", "5", "10", "50"):
            self.jog_step.addItem(f"{s} mm", float(s))
        self.jog_step.setCurrentIndex(3)
        jog.addWidget(self.jog_step, 1, 1)
        jw = QWidget()
        jw.setLayout(jog)
        row = QHBoxLayout()
        row.addWidget(jw)
        col = QVBoxLayout()
        self.btn_set_origin = QPushButton("Papierecke = hier")
        self.btn_set_origin.setToolTip("Die aktuelle Stiftposition ist die Blattecke am Nullpunkt")
        self.btn_set_origin.clicked.connect(self.set_origin_here)
        self.btn_goto_origin = QPushButton("Zum Nullpunkt")
        self.btn_goto_origin.clicked.connect(self.goto_origin)
        self.btn_goto_corner2 = QPushButton("Zur 2. Ecke")
        self.btn_goto_corner2.clicked.connect(self.goto_second_corner)
        self.btn_set_corner2 = QPushButton("2. Ecke = hier")
        self.btn_set_corner2.clicked.connect(self.set_second_corner_here)
        self.btn_frame = QPushButton("Rahmen abfahren")
        self.btn_frame.setToolTip("Fährt mit gehobenem Stift um die Zeichnung")
        self.btn_frame.clicked.connect(self.trace_frame)
        self.btn_unlock = QPushButton("Entsperren ($X)")
        self.btn_unlock.clicked.connect(lambda: self.manual(["$X"]))
        for bt in (self.btn_set_origin, self.btn_goto_origin, self.btn_goto_corner2,
                   self.btn_set_corner2, self.btn_frame, self.btn_unlock):
            col.addWidget(bt)
        row.addLayout(col)
        v.addLayout(row)
        self.origin_label = QLabel()
        self.origin_label.setWordWrap(True)
        v.addWidget(self.origin_label)
        lay.addWidget(g)

        # --- Plot (eigenes, immer sichtbares Dock)
        g = QWidget()
        v = QVBoxLayout(g)
        v.setContentsMargins(8, 4, 8, 4)
        self.estimate_label = QLabel("–")
        self.estimate_label.setWordWrap(True)
        v.addWidget(self.estimate_label)
        self.show_travel = QCheckBox("Leerfahrten anzeigen")
        self.show_travel.toggled.connect(self.view.show_travel)
        v.addWidget(self.show_travel)
        row = QHBoxLayout()
        self.btn_start = QPushButton("▶ Plot starten")
        self.btn_start.setStyleSheet("font-weight:bold")
        self.btn_start.clicked.connect(self.start_plot)
        self.btn_pause = QPushButton("⏸ Pause")
        self.btn_pause.setToolTip("Hält nach dem aktuellen Strich an – Stift oben")
        self.btn_pause.clicked.connect(self.pause_plot)
        self.btn_resume = QPushButton("⏵ Weiter")
        self.btn_resume.clicked.connect(self.resume_plot)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_pause)
        row.addWidget(self.btn_resume)
        v.addLayout(row)
        row = QHBoxLayout()
        self.btn_hold = QPushButton("Sofort halt")
        self.btn_hold.setToolTip("Feed Hold: bremst sofort, Stift bleibt, wo er ist")
        self.btn_hold.clicked.connect(self.hold_plot)
        self.btn_abort = QPushButton("■ Abbrechen")
        self.btn_abort.clicked.connect(self.abort_plot)
        row.addWidget(self.btn_hold)
        row.addWidget(self.btn_abort)
        v.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFormat("%p %")
        v.addWidget(self.progress)
        f = QFormLayout()
        self.l_total = QLabel("–")
        self.l_elapsed = QLabel("–")
        self.l_remaining = QLabel("–")
        self.l_eta = QLabel("–")
        self.l_lines = QLabel("–")
        big = QFont()
        big.setPointSizeF(big.pointSizeF() * 1.25)
        big.setBold(True)
        self.l_remaining.setFont(big)
        f.addRow("Geschätzt gesamt:", self.l_total)
        f.addRow("Vergangen:", self.l_elapsed)
        f.addRow("Verbleibend:", self.l_remaining)
        f.addRow("Fertig um:", self.l_eta)
        f.addRow("Zeilen:", self.l_lines)
        v.addLayout(f)
        self.last_label = QLabel()
        self.last_label.setWordWrap(True)
        v.addWidget(self.last_label)
        plot_widget = g
        lay.addStretch(1)

        sa = QScrollArea()
        sa.setWidget(panel)
        sa.setWidgetResizable(True)
        sa.setMinimumWidth(400)
        dock = QDockWidget("Steuerung")
        dock.setObjectName("panel")
        dock.setWidget(sa)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        pdock = QDockWidget("Plot")
        pdock.setObjectName("plot")
        pdock.setWidget(plot_widget)
        pdock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, pdock)
        self.splitDockWidget(dock, pdock, Qt.Orientation.Vertical)

        # --- Protokoll
        logw = QWidget()
        lv = QVBoxLayout(logw)
        lv.setContentsMargins(4, 4, 4, 4)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setFont(QFont("Monospace", 9))
        lv.addWidget(self.log)
        row = QHBoxLayout()
        self.console = QLineEdit()
        self.console.setPlaceholderText("G-Code oder $-Befehl senden (Enter)")
        self.console.returnPressed.connect(self.send_console)
        row.addWidget(self.console)
        self.log_tx = QCheckBox("Plot-Zeilen protokollieren")
        row.addWidget(self.log_tx)
        lv.addLayout(row)
        ld = QDockWidget("Protokoll")
        ld.setObjectName("log")
        ld.setWidget(logw)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, ld)
        self.resizeDocks([ld], [140], Qt.Orientation.Vertical)

        # --- Menü
        m = self.menuBar().addMenu("&Datei")
        a = QAction("SVG öffnen …", self, shortcut=QKeySequence.StandardKey.Open)
        a.triggered.connect(self.open_svg)
        m.addAction(a)
        a = QAction("G-Code speichern …", self)
        a.triggered.connect(self.save_gcode)
        m.addAction(a)
        m.addSeparator()
        a = QAction("Beenden", self, shortcut=QKeySequence.StandardKey.Quit)
        a.triggered.connect(self.close)
        m.addAction(a)
        m = self.menuBar().addMenu("&Einstellungen")
        a = QAction("Maschine und Übertragung …", self)
        a.triggered.connect(self.edit_settings)
        m.addAction(a)
        m = self.menuBar().addMenu("&Hilfe")
        a = QAction("Über A3 Plot", self)
        a.triggered.connect(lambda: QMessageBox.about(
            self, "A3 Plot", f"<b>A3 Plot {__version__}</b><br>SVG-Plotter für den EleksMaker A3 Pro "
            "mit grbl-servo.<br><br>Maus: Zeichnung ziehen, Ecken zum Skalieren (Umschalt = ohne Raster), "
            "Mausrad = Zoom, rechte/mittlere Taste = verschieben, Doppelklick = ganzes Blatt."))
        m.addAction(a)
        self.statusBar().showMessage("Bereit")

    # ============================================================ Logging
    def log_line(self, kind, text):
        prefix = {"tx": "→ ", "rx": "← ", "info": "· ", "warn": "⚠ ", "error": "✖ "}.get(kind, "")
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')} {prefix}{text}")
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    # ============================================================ Datei
    def open_svg(self):
        if self.plot:
            QMessageBox.information(self, "SVG öffnen", "Während eines Plots nicht möglich.")
            return
        start = self.settings.value("last_dir", os.path.expanduser("~"))
        fn, _ = QFileDialog.getOpenFileName(self, "SVG öffnen", start, "SVG (*.svg);;Alle Dateien (*)")
        if fn:
            self.load_file(fn)

    def load_file(self, fn):
        self.settings.setValue("last_dir", os.path.dirname(fn))
        self.file_label.setText(f"Lade {os.path.basename(fn)} …")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        def work():
            try:
                d = load_svg(fn)
                ordered = []
                for layer in d.layers:
                    ps = [simplify(p, LOAD_SIMPLIFY_MM) for p in layer.paths]
                    ordered.append(order_paths(ps, start=(0.0, d.height_mm)))
                self.bridge.loaded.emit((fn, d, ordered))
            except Exception as e:
                self.bridge.loaded.emit((fn, e, None))

        threading.Thread(target=work, daemon=True).start()

    def on_loaded(self, res):
        QApplication.restoreOverrideCursor()
        fn, d, ordered = res
        if self.plot:     # Plot wurde gestartet, während die Datei noch geladen hat
            self.file_label.setText(self.drawing_label)
            self.statusBar().showMessage(f"{os.path.basename(fn)} nicht geöffnet: es läuft ein Plot.", 8000)
            return
        if isinstance(d, Exception):
            self.file_label.setText(self.drawing_label)   # bisherige Zeichnung bleibt geladen
            QMessageBox.critical(self, "SVG", f"Datei kann nicht gelesen werden:\n{d}")
            return
        if not d.layers:
            self.file_label.setText(self.drawing_label)
            QMessageBox.warning(self, "SVG", "Die Datei enthält keine zeichenbaren Linien.\n"
                                + "\n".join(d.warnings))
            return
        self.drawing, self.ordered = d, ordered
        self.placement.rotation = 0
        self.layers.blockSignals(True)
        self.layers.clear()
        for layer in d.layers:
            it = QListWidgetItem(f"{layer.name}  ({len(layer.paths)} Pfade)")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.layers.addItem(it)
        self.layers.blockSignals(False)
        npts = sum(l.point_count for l in d.layers)
        text = (f"<b>{os.path.basename(fn)}</b><br>SVG-Größe {d.width_mm:.1f} × {d.height_mm:.1f} mm · "
                f"{sum(len(l.paths) for l in d.layers)} Pfade · {npts} Punkte")
        if d.warnings:
            text += "<br><span style='color:#b5261e'>" + "<br>".join(d.warnings) + "</span>"
        self.drawing_label = text
        self.file_label.setText(text)
        self.setWindowTitle(f"A3 Plot – {os.path.basename(fn)}")
        self.update_drawing_geometry()
        self.fit_to_margin()
        self.view.fit()

    def active_layers(self):
        return [i for i in range(self.layers.count())
                if self.layers.item(i).checkState() == Qt.CheckState.Checked]

    def on_layers_changed(self, *_):
        self.update_drawing_items()
        self.schedule_build()

    def update_drawing_geometry(self):
        """Nach Laden oder Drehen: Grenzen (aller Ebenen) und Vorschau neu berechnen."""
        allp = [p for ps in self.ordered for p in ps]
        self.bounds = rotated_bounds(allp, self.placement.rotation)
        self.update_drawing_items()

    def update_drawing_items(self):
        if not self.drawing:
            return
        lo, hi = self.bounds
        act = set(self.active_layers())
        items = []
        for i, (layer, ps) in enumerate(zip(self.drawing.layers, self.ordered)):
            local = [rotate_points(p, self.placement.rotation) - lo for p in ps]
            items.append((polylines_path(local), layer.color, i in act))
        w, h = hi - lo
        self.view.set_drawing(items, float(w), float(h))
        self.apply_placement_to_view()

    # ===================================================== Platzierung
    def doc_size(self):
        if self.bounds is None:
            return 1.0, 1.0
        lo, hi = self.bounds
        w, h = hi - lo
        return max(float(w), 1e-6), max(float(h), 1e-6)

    def paper_wh(self):
        name, landscape = self.paper.currentData()
        w, h = PAPER_SIZES[name]
        return (h, w) if landscape else (w, h)

    def on_paper_changed(self, *_):
        w, h = self.paper_wh()
        self.view.set_paper(w, h, self.margin.value())
        self.settings.setValue("paper/index", self.paper.currentIndex())
        self.settings.setValue("paper/margin", self.margin.value())
        self.update_origin_marker()
        self.view.fit()
        self.check_placement()
        self.schedule_build()

    def apply_placement_to_view(self):
        p = self.placement
        self.view.set_placement(p.x, p.y, p.scale)
        dw, dh = self.doc_size()
        for wdg, val in ((self.px, p.x), (self.py, p.y), (self.pw, dw * p.scale),
                         (self.ph, dh * p.scale), (self.pscale, p.scale * 100)):
            wdg.blockSignals(True)
            wdg.setValue(val)
            wdg.blockSignals(False)
        self.check_placement()

    def on_view_placement(self, x, y, s):
        self.placement.x, self.placement.y, self.placement.scale = x, y, s
        self.apply_placement_to_view()

    def on_fields_changed(self, *_):
        if not self.drawing:
            return
        dw, dh = self.doc_size()
        src = self.sender()
        p = self.placement
        if src is self.pw:
            p.scale = self.pw.value() / dw
        elif src is self.ph:
            p.scale = self.ph.value() / dh
        elif src is self.pscale:
            p.scale = self.pscale.value() / 100
        p.x, p.y = self.px.value(), self.py.value()
        self.apply_placement_to_view()
        self.schedule_build()

    def fit_to_margin(self):
        if not self.drawing:
            return
        w, h = self.paper_wh()
        m = self.margin.value()
        dw, dh = self.doc_size()
        s = min((w - 2 * m) / dw, (h - 2 * m) / dh)
        self.placement.scale = s
        self.placement.x = (w - dw * s) / 2
        self.placement.y = (h - dh * s) / 2
        self.apply_placement_to_view()
        self.schedule_build()

    def center(self):
        if not self.drawing:
            return
        w, h = self.paper_wh()
        dw, dh = self.doc_size()
        self.placement.x = (w - dw * self.placement.scale) / 2
        self.placement.y = (h - dh * self.placement.scale) / 2
        self.apply_placement_to_view()
        self.schedule_build()

    def rotate(self):
        if not self.drawing:
            return
        # Mittelpunkt beibehalten
        dw, dh = self.doc_size()
        cx = self.placement.x + dw * self.placement.scale / 2
        cy = self.placement.y + dh * self.placement.scale / 2
        self.placement.rotation = (self.placement.rotation + 90) % 360
        self.update_drawing_geometry()
        dw, dh = self.doc_size()
        self.placement.x = cx - dw * self.placement.scale / 2
        self.placement.y = cy - dh * self.placement.scale / 2
        self.apply_placement_to_view()
        self.schedule_build()

    def original_size(self):
        if not self.drawing:
            return
        self.placement.scale = 1.0
        self.center()

    def check_placement(self):
        if not self.drawing:
            self.place_warn.setText("")
            return
        w, h = self.paper_wh()
        m = self.margin.value()
        dw, dh = self.doc_size()
        p = self.placement
        x1, y1 = p.x + dw * p.scale, p.y + dh * p.scale
        if p.x < -0.01 or p.y < -0.01 or x1 > w + 0.01 or y1 > h + 0.01:
            self.place_warn.setText("Die Zeichnung ragt über das Papier hinaus!")
        elif p.x < m - 0.01 or p.y < m - 0.01 or x1 > w - m + 0.01 or y1 > h - m + 0.01:
            self.place_warn.setText("Die Zeichnung liegt im Rand.")
        else:
            self.place_warn.setText("")

    # ========================================================= Job bauen
    def on_pen_changed(self, *_):
        self.pen = PenSettings(up=self.pen_up.value(), down=self.pen_down.value(),
                               down_delay_ms=self.down_delay.value(), up_delay_ms=self.up_delay.value(),
                               draw_speed=float(self.speed.value()), travel_speed=float(self.travel.value()))
        st = self.settings
        st.setValue("pen/up", self.pen.up)
        st.setValue("pen/down", self.pen.down)
        st.setValue("pen/down_delay", self.pen.down_delay_ms)
        st.setValue("pen/up_delay", self.pen.up_delay_ms)
        st.setValue("pen/draw_speed", self.pen.draw_speed)
        st.setValue("pen/travel_speed", self.pen.travel_speed)
        self.schedule_build()

    def schedule_build(self):
        self.job_dirty = True
        self.job_gen += 1   # ein noch laufender Build mit alten Werten wird verworfen
        if self.plot:
            return          # nach dem Plot wird neu berechnet
        if self.drawing:
            self.estimate_label.setText("Berechne …")
            self.rebuild_timer.start()
        self.update_buttons()

    def job_inputs(self):
        act = self.active_layers()
        paths = [p for i in act for p in self.ordered[i]]
        return (paths, Placement(**vars(self.placement)), self.paper_wh(), PenSettings(**vars(self.pen)),
                MachineSetup(**vars(self.setup)), self.park, self.bounds)

    def start_build(self):
        if not self.drawing or self.plot:
            return
        self.job_gen += 1
        gen = self.job_gen
        paths, pl, paper, pen, setup, park, bounds = self.job_inputs()
        if not paths:
            self.job = None
            self.estimate_label.setText("Keine Ebene ausgewählt.")
            self.update_buttons()
            return

        def work():
            try:
                j = build_job(paths, pl, paper, pen, setup, park=park, bounds=bounds)
                self.bridge.built.emit(gen, j)
            except Exception as e:   # pragma: no cover
                self.bridge.built.emit(gen, e)

        threading.Thread(target=work, daemon=True).start()

    def on_built(self, gen, job):
        if gen != self.job_gen:
            return       # veraltet
        if isinstance(job, Exception):
            self.estimate_label.setText(f"Fehler: {job}")
            return
        self.job = job
        self.job_dirty = False
        lines = [
            f"<b>Geschätzte Dauer: {format_duration(job.total_time)}</b>",
            f"Zeichnen {job.draw_length / 1000:.1f} m · Leerfahrt {job.travel_length / 1000:.1f} m · "
            f"{job.pen_lifts} Striche · {len(job.lines)} G-Code-Zeilen",
        ]
        lines += [f"<span style='color:#b5261e'>{w}</span>" for w in job.warnings]
        self.estimate_label.setText("<br>".join(lines))
        self.l_total.setText(format_duration(job.total_time))
        # Leerfahrten für die Vorschau
        travel = []
        prev = None
        s2m = SheetToMachine(self.setup, *self.paper_wh())
        ou, ov = s2m.to_sheet(*self.setup.origin_offset)
        prev = np.array([ou, ov])
        for p in job.strokes_sheet:
            travel.append(np.array([prev, p[0]]))
            prev = p[-1]
        self.view.set_travel(polylines_path(travel))
        self.update_buttons()

    def ensure_job(self):
        if self.job is None or self.job_dirty:
            paths, pl, paper, pen, setup, park, bounds = self.job_inputs()
            if not paths:
                return None
            self.job_gen += 1
            self.job = build_job(paths, pl, paper, pen, setup, park=park, bounds=bounds)
            self.job_dirty = False
        return self.job

    def save_gcode(self):
        if not self.drawing:
            return
        job = self.ensure_job()
        if not job:
            return
        base = os.path.splitext(self.drawing.source)[0] + ".gcode"
        fn, _ = QFileDialog.getSaveFileName(self, "G-Code speichern", base, "G-Code (*.gcode *.nc)")
        if fn:
            with open(fn, "w") as f:
                f.write("\n".join(job.lines) + "\n")
            self.statusBar().showMessage(f"Gespeichert: {fn}", 5000)

    # ======================================================= Einstellungen
    def edit_settings(self):
        if self.plot:
            QMessageBox.information(self, "Einstellungen", "Während eines Plots nicht möglich.")
            return
        dlg = SettingsDialog(self, self.setup, self.mode, self.budget, self.park)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        old = (self.setup.origin_corner, self.setup.x_right, self.setup.y_up)
        self.setup.origin_corner = dlg.corner.currentData()
        self.setup.x_right = dlg.xdir.currentIndex() == 0
        self.setup.y_up = dlg.ydir.currentIndex() == 0
        if old != (self.setup.origin_corner, self.setup.x_right, self.setup.y_up):
            # die gespeicherte Blattecke wurde an der alten Ecke gemessen
            self.setup.sheet_angle = 0.0
            self.log_line("warn", "Nullpunkt-Ecke oder Achsrichtung geändert: Drehung zurückgesetzt. "
                                  "Bitte „Papierecke = hier“ (und ggf. 2. Ecke) neu setzen.")
            self.statusBar().showMessage("Bitte Papierecke neu setzen.", 10000)
        self.mode = dlg.mode.currentData()
        self.budget = dlg.budget.value()
        self.park = dlg.park.isChecked()
        st = self.settings
        st.setValue("machine/corner", self.setup.origin_corner)
        st.setValue("machine/x_right", "true" if self.setup.x_right else "false")
        st.setValue("machine/y_up", "true" if self.setup.y_up else "false")
        st.setValue("stream/mode", self.mode)
        st.setValue("stream/budget", self.budget)
        st.setValue("plot/park", "true" if self.park else "false")
        if self.streamer:
            self.streamer.mode = self.mode
            self.streamer.budget = self.budget
        self.update_origin_marker()
        self.schedule_build()

    def update_origin_marker(self):
        w, h = self.paper_wh()
        s2m = SheetToMachine(self.setup, w, h)
        self.view.set_origin(s2m.u0, s2m.v0)
        ox, oy = self.setup.origin_offset
        c2, _, side = second_corner(self.setup, w, h)
        self.origin_label.setText(f"Blattecke liegt bei Maschinen-X {ox:.1f} / Y {oy:.1f} mm · "
                                  f"Drehung {self.setup.sheet_angle:.2f}°")
        tip = (f"Ecke {CORNER_NAMES[c2]} (kurze Seite, {side:.0f} mm von der Nullpunkt-Ecke)")
        self.btn_goto_corner2.setToolTip("Fährt mit gehobenem Stift dahin, wo die " + tip +
                                         " nach der aktuellen Messung liegen müsste")
        self.btn_set_corner2.setToolTip("Die aktuelle Stiftposition ist die " + tip +
                                        ". Daraus wird die Schräglage des Blatts berechnet.")

    # ========================================================= Verbindung
    def refresh_ports(self):
        cur = self.port.currentText() or self.settings.value("port", "/dev/ttyUSB0")
        self.port.clear()
        try:
            from serial.tools import list_ports
            ports = [p.device for p in list_ports.comports()]
        except Exception:
            ports = []
        for p in sorted(set(ports) | {"/dev/ttyUSB0"}):
            self.port.addItem(p)
        self.port.setCurrentText(cur)

    def toggle_connect(self):
        if self.streamer:
            if self.plot and not self.ask("Es läuft ein Plot. Trotzdem trennen?"):
                return
            self.streamer.stop()
            self.streamer = None
            self.connected = False
            self.state_label.setText("Nicht verbunden")
            self.update_buttons()
            return
        import serial
        port = self.port.currentText().strip()
        self.settings.setValue("port", port)

        def factory():
            return serial.Serial(port, 115200, timeout=0.02, write_timeout=2)

        self.streamer = GrblStreamer(factory, self.bridge.grbl.emit, mode=self.mode, budget=self.budget)
        self.state_label.setText(f"Verbinde mit {port} …")
        self.btn_connect.setEnabled(False)
        self.streamer.start()

    # =========================================================== GRBL
    def on_grbl(self, name, data):
        if name == "log":
            kind, text = data
            self.log_line(kind, text)
        elif name == "connected":
            version, reset = data
            self.connected = True
            self.btn_connect.setEnabled(True)
            self.btn_connect.setText("Trennen")
            self.log_line("info", f"Verbunden: {version}" + ("" if reset else " (ohne Neustart)"))
            if reset:
                self.reset_sheet_position()
            self.update_buttons()
        elif name == "disconnected":
            self.log_line("warn", data)
            self.connected = False
            self.streamer = None
            self.btn_connect.setEnabled(True)
            self.btn_connect.setText("Verbinden")
            self.state_label.setText("Nicht verbunden")
            if self.plot:
                self.finish_plot("failed", "Verbindung verloren: " + data)
            self.update_buttons()
        elif name == "status":
            state, x, y = data
            self.grbl_state = state
            self.mpos = (x, y)
            self.state_label.setText(f"<b>{state}</b> · X {x:.2f} · Y {y:.2f} mm")
            u, v = SheetToMachine(self.setup, *self.paper_wh()).to_sheet(x, y)
            self.view.set_marker(u, v, True)
            self.update_buttons()
        elif name == "setting":
            key, val = data
            mapping = {110: "max_rate_x", 111: "max_rate_y", 120: "accel_x", 121: "accel_y",
                       11: "junction_deviation"}
            if key in mapping:
                setattr(self.setup, mapping[key], val)
                self.schedule_build()
            if key in (110, 111):
                self.speed_slider.setMaximum(int(min(self.setup.max_rate_x, self.setup.max_rate_y)))
        elif name == "job_sent" and self.plot:
            self.plot["sent"] = data
            if self.log_tx.isChecked():
                self.log_line("tx", f"[{data + 1}] {self.plot['job'].lines[data]}")
        elif name == "job_acked" and self.plot:
            self.plot["acked"] = data
            if self.plot["job"].kind[data] in (KIND_PEN, KIND_DWELL):
                self.plot["sync"] = data
        elif name == "repeats" and self.plot:
            self.plot["repeats"] = data
        elif name == "job_state":
            self.on_job_state(data)
        elif name == "job_problem":
            self.log_line("error", data)
            if self.plot:
                self.plot["problem"] = data
            QMessageBox.warning(self, "Plot", data)
        elif name == "alarm":
            self.statusBar().showMessage(f"GRBL-Alarm: {data} – ggf. „Entsperren“", 10000)
        elif name == "controller_reset":
            if not self.plot:
                self.reset_sheet_position()

    def on_job_state(self, state):
        if not self.plot:
            return
        p = self.plot
        now = time.monotonic()
        if state in ("soft_paused", "holding"):
            if state == "soft_paused":
                p["soft_pause"] = True    # Stift darf bewegt worden sein -> beim Weiter anheben
                if p["state"] == "holding":
                    state = "holding"     # Pausenpunkt im Hold erreicht: Maschine steht noch im Hold
            if p["paused_at"] is None:
                p["paused_at"] = now
            p["state"] = state
        elif state == "running":
            if p["paused_at"] is not None:
                p["pause_total"] += now - p["paused_at"]
                p["paused_at"] = None
            p["state"] = "running"
        elif state in ("finished", "aborted", "failed"):
            self.finish_plot(state, p.get("problem"))
        self.update_buttons()

    # ===================================================== manuelle Befehle
    def manual_blocked(self):
        """Während eines Plots nur in der weichen Pause – sonst landen Befehle mitten im Job."""
        return bool(self.plot) and self.plot["state"] != "soft_paused"

    def manual(self, lines):
        if not self.connected or self.manual_blocked():
            return
        for l in lines:
            self.streamer.command(l)

    def send_console(self):
        text = self.console.text().strip()
        if not text:
            return
        if not self.connected:
            self.log_line("warn", "Nicht verbunden.")
            return
        if text in ("!", "~", "?"):
            self.streamer.realtime(text.encode())
        elif self.manual_blocked():
            self.log_line("warn", "Während des Plots gesperrt (außer in der Pause). "
                                  "Echtzeitbefehle ! ~ ? gehen immer.")
            return
        else:
            self.streamer.command(text)
        self.console.clear()

    def jog(self, du, dv):
        step = self.jog_step.currentData()
        sx = 1 if self.setup.x_right else -1
        sy = -1 if self.setup.y_up else 1
        dx, dy = du * step * sx, dv * step * sy
        parts = "G91G0" + (f"X{fmt(dx)}" if dx else "") + (f"Y{fmt(dy)}" if dy else "")
        self.manual([parts, "G90"])

    def reset_sheet_position(self):
        """Nach einem Controller-Neustart ist die Position 0/0 – die Messung gilt nicht mehr."""
        self.setup.origin_offset = (0.0, 0.0)
        self.setup.sheet_angle = 0.0
        self.update_origin_marker()
        self.schedule_build()

    def set_origin_here(self):
        self.setup.origin_offset = (round(self.mpos[0], 3), round(self.mpos[1], 3))
        msg = f"Blattecke gesetzt: X {self.mpos[0]:.2f} Y {self.mpos[1]:.2f}"
        if self.setup.sheet_angle:
            self.setup.sheet_angle = 0.0
            msg += " – Drehung zurückgesetzt, bitte 2. Ecke neu setzen"
        self.update_origin_marker()
        self.log_line("info", msg)
        self.schedule_build()

    def goto_second_corner(self):
        w, h = self.paper_wh()
        _, uv, _ = second_corner(self.setup, w, h)
        x, y = SheetToMachine(self.setup, w, h).to_machine(np.array([uv]))[0]
        self.manual([f"M3S{self.pen_up.value()}", f"G4P{self.up_delay.value() / 1000:.2f}", "G90",
                     f"G0X{fmt(x)}Y{fmt(y)}"])

    def set_second_corner_here(self):
        if self.grbl_state != "Idle":
            QMessageBox.information(self, "2. Ecke", "Bitte warten, bis der Plotter steht (Idle).")
            return
        w, h = self.paper_wh()
        c2, _, side = second_corner(self.setup, w, h)
        x, y = self.mpos
        length = math.hypot(x - self.setup.origin_offset[0], y - self.setup.origin_offset[1])
        if length < side / 2:
            QMessageBox.warning(self, "2. Ecke",
                                f"Der Stift ist nur {length:.1f} mm von der Blattecke am Nullpunkt entfernt. "
                                f"Bitte zur Ecke {CORNER_NAMES[c2]} fahren ({side:.0f} mm entfernt).")
            return
        angle, length = measure_sheet_angle(self.setup, w, h, x, y)
        problems = []
        if abs(length - side) > 5:
            problems.append(f"Gemessener Abstand {length:.1f} mm, die kurze Seite ist aber {side:.0f} mm lang.")
        if abs(angle) > 20:
            problems.append(f"Das Blatt wäre um {angle:.1f}° gedreht.")
        if problems and not self.ask("\n".join(problems) + f"\n\nIst das wirklich die Ecke "
                                     f"{CORNER_NAMES[c2]}? Drehung trotzdem übernehmen?"):
            return
        self.setup.sheet_angle = round(angle, 4)
        self.update_origin_marker()
        self.log_line("info", f"2. Ecke ({CORNER_NAMES[c2]}) bei X {x:.2f} Y {y:.2f}: "
                              f"Blatt um {angle:.2f}° gedreht, Kante {length:.1f} mm (soll {side:.0f} mm)")
        self.schedule_build()

    def goto_origin(self):
        ox, oy = self.setup.origin_offset
        self.manual([f"M3S{self.pen_up.value()}", f"G4P{self.up_delay.value() / 1000:.2f}", "G90",
                     f"G0X{fmt(ox)}Y{fmt(oy)}"])

    def trace_frame(self):
        if not self.drawing or not self.connected:
            return
        dw, dh = self.doc_size()
        p = self.placement
        rect = (p.x, p.y, p.x + dw * p.scale, p.y + dh * p.scale)
        lines = [f"M3S{self.pen_up.value()}", f"G4P{self.up_delay.value() / 1000:.2f}"]
        lines += frame_lines(rect, self.setup, self.paper_wh())
        ox, oy = self.setup.origin_offset
        lines.append(f"G0X{fmt(ox)}Y{fmt(oy)}")
        self.manual(lines)

    # ============================================================= Plot
    def ask(self, text):
        return QMessageBox.question(self, "A3 Plot", text) == QMessageBox.StandardButton.Yes

    def start_plot(self):
        if not self.connected or not self.drawing or self.plot:
            return
        if self.grbl_state not in ("Idle",):
            QMessageBox.warning(self, "Plot", f"GRBL ist nicht bereit (Zustand: {self.grbl_state}).")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            job = self.ensure_job()
        finally:
            QApplication.restoreOverrideCursor()
        if not job:
            return
        warn = self.place_warn.text()
        text = (f"Plot starten?\n\nGeschätzte Dauer: {format_duration(job.total_time)}\n"
                f"Geschwindigkeit: {self.pen.draw_speed:.0f} mm/min\n\n"
                "Netzteil an? Papier fixiert? Blattecke (und ggf. 2. Ecke) gesetzt?")
        if warn:
            text += f"\n\n⚠ {warn}"
        if not self.ask(text):
            return
        self.plot = {"job": job, "t0": time.monotonic(), "wall0": datetime.now(), "sent": -1,
                     "acked": -1, "sync": -1, "exec": 0, "repeats": 0, "paused_at": None,
                     "pause_total": 0.0, "state": "running", "done_strokes": 0, "problem": None,
                     "prog": 0.0}
        self.view.clear_progress()
        self.view.set_locked(True)
        self.last_label.setText("")
        self.log_line("info", f"Plot gestartet: {len(job.lines)} Zeilen, geschätzt {format_duration(job.total_time)}")
        pause_points = job.stroke_last_line.tolist()
        self.streamer.start_job(job.lines, pause_points)
        self.update_buttons()

    def resume_prefix(self):
        return ["G21", "G90", f"M3S{self.pen.up}", f"G4P{self.pen.up_delay_ms / 1000:.2f}"]

    def pause_plot(self):
        if self.plot and self.plot["state"] == "running":
            self.streamer.pause_soft()
            self.btn_pause.setEnabled(False)
            self.statusBar().showMessage("Pause nach dem aktuellen Strich …", 5000)

    def hold_plot(self):
        if self.plot:
            self.streamer.hold()

    def resume_plot(self):
        if not self.plot:
            return
        if self.plot["state"] == "soft_paused" or self.plot.get("soft_pause"):
            self.streamer.resume(self.resume_prefix())
        else:
            self.streamer.resume([])
        self.plot["soft_pause"] = False

    def abort_plot(self):
        if not self.plot:
            if self.connected:
                self.streamer.abort()
            return
        if self.ask("Plot wirklich abbrechen?"):
            self.streamer.abort()

    def finish_plot(self, state, problem=None):
        p = self.plot
        if not p:
            return
        now = time.monotonic()
        if p["paused_at"] is not None:
            p["pause_total"] += now - p["paused_at"]
        wall = now - p["t0"]
        active = wall - p["pause_total"]
        job = p["job"]
        text = {"finished": "Fertig", "aborted": "Abgebrochen", "failed": "Fehlgeschlagen"}[state]
        msg = (f"<b>{text}</b> nach {format_duration(wall)}"
               + (f" (davon Pause {format_duration(p['pause_total'])})" if p["pause_total"] > 1 else "")
               + f" · geschätzt waren {format_duration(job.total_time)}")
        if p["repeats"]:
            msg += f" · {p['repeats']} Zeile(n) wiederholt"
        self.last_label.setText(msg)
        self.log_line("info" if state == "finished" else "warn",
                      f"{text}: Dauer {format_duration(wall)}, aktiv {format_duration(active)}, "
                      f"Schätzung {format_duration(job.total_time)}")
        if state == "finished":
            self.progress.setValue(1000)
            self.l_remaining.setText("0 s")
            self.view.add_done(job.strokes_sheet[p["done_strokes"]:])
        self.write_history(state, wall, active, job)
        self.plot = None
        self.view.set_locked(False)
        if state == "aborted" and self.connected:
            # Reset hat den Servo auf S0 gestellt – auf die normale Stift-oben-Stellung bringen
            self.streamer.command(f"M3S{self.pen.up}")
        self.update_buttons()
        self.schedule_build()
        if state == "finished":
            QApplication.beep()

    def write_history(self, state, wall, active, job):
        try:
            os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
            new = not os.path.exists(HISTORY)
            with open(HISTORY, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["start", "datei", "ergebnis", "dauer_s", "aktiv_s", "schaetzung_s",
                                "zeichnen_mm", "geschwindigkeit"])
                w.writerow([self.plot["wall0"].isoformat(timespec="seconds"),
                            os.path.basename(self.drawing.source if self.drawing else ""), state,
                            round(wall), round(active), round(job.total_time), round(job.draw_length),
                            self.pen.draw_speed])
        except OSError:
            pass

    def update_progress(self):
        p = self.plot
        if not p:
            return
        job = p["job"]
        now = time.monotonic()
        paused = p["pause_total"] + (now - p["paused_at"] if p["paused_at"] is not None else 0)
        wall = now - p["t0"]
        active = wall - paused
        # Wo steht die Maschine? Zwischen der letzten Synchronisation und der letzten
        # bestätigten Zeile das Segment suchen, das der gemeldeten Position am nächsten liegt.
        lo = max(p["exec"], p["sync"])
        hi = p["acked"]
        if hi >= 0:
            idx, frac = job.locate(self.mpos, lo, hi)
            if idx < p["exec"]:
                idx, frac = p["exec"], 0.0
            p["exec"] = idx
            prog = max(job.progress_time(idx, frac), job.progress_time(p["sync"]) if p["sync"] >= 0 else 0.0)
            p["prog"] = max(p["prog"], prog)
        total = job.total_time
        prog = p["prog"]
        est_rem = max(0.0, total - prog)
        # Schätzung an die tatsächliche Geschwindigkeit anpassen
        ratio = 1.0
        if prog > 60 and active > 30:
            ratio = min(2.0, max(0.5, active / prog))
        remaining = est_rem * ratio
        self.progress.setValue(int(1000 * prog / total) if total > 0 else 0)
        self.l_total.setText(format_duration(total) + (f"  (hochgerechnet {format_duration(active + remaining)})"
                                                       if abs(ratio - 1) > 0.05 else ""))
        el = format_duration(wall)
        if paused > 1:
            el += f" (Pause {format_duration(paused)})"
        self.l_elapsed.setText(el)
        if p["state"] == "running":
            self.l_remaining.setText(format_duration(remaining))
            self.l_eta.setText((datetime.now() + timedelta(seconds=remaining)).strftime("%H:%M Uhr"))
        else:
            self.l_remaining.setText(format_duration(remaining) + " (pausiert)")
            self.l_eta.setText("–")
        rep = f" · {p['repeats']} wiederholt" if p["repeats"] else ""
        self.l_lines.setText(f"{max(0, p['acked'] + 1)} / {len(job.lines)} bestätigt{rep}")
        # fertige Striche einfärben
        n_done = int(np.searchsorted(job.stroke_last_line, p["exec"], side="left"))
        if n_done > p["done_strokes"]:
            self.view.add_done(job.strokes_sheet[p["done_strokes"]:n_done])
            p["done_strokes"] = n_done

    # ========================================================== Buttons
    def update_buttons(self):
        c = self.connected
        plotting = self.plot is not None
        st = self.plot["state"] if plotting else None
        idle_manual = c and (not plotting or st == "soft_paused")
        for w in self.jog_buttons + [self.btn_pen_up, self.btn_pen_down, self.btn_set_origin,
                                     self.btn_goto_origin, self.btn_goto_corner2, self.btn_set_corner2,
                                     self.btn_unlock]:
            w.setEnabled(idle_manual)
        self.btn_frame.setEnabled(idle_manual and self.drawing is not None and not plotting)
        self.btn_start.setEnabled(c and self.drawing is not None and not plotting and self.grbl_state == "Idle")
        self.btn_pause.setEnabled(plotting and st == "running")
        self.btn_hold.setEnabled(plotting and st in ("running", "soft_paused"))
        self.btn_resume.setEnabled(plotting and st in ("soft_paused", "holding"))
        self.btn_abort.setEnabled(c and (plotting or self.grbl_state in ("Run", "Hold")))
        for w in self.place_widgets + self.pen_widgets:
            w.setEnabled(not plotting)
        self.layers.setEnabled(not plotting)

    # ========================================================== Schließen
    def closeEvent(self, e):
        if self.plot and not self.ask("Es läuft ein Plot. Wirklich beenden?\n(Der Plotter bleibt stehen.)"):
            e.ignore()
            return
        s = self.streamer   # kann während processEvents() durch "disconnected" None werden
        if s:
            if self.plot:
                # anhalten und Reset abwarten, damit der Plotter sicher steht
                s.abort()
                t = time.monotonic()
                while time.monotonic() - t < 0.3 or (s.aborting and self.streamer is s
                                                     and time.monotonic() - t < 5):
                    QApplication.processEvents()
                    time.sleep(0.02)
            s.stop()
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("A3 Plot")
    app.setDesktopFileName("a3plot")
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        w.load_file(sys.argv[1])
    sys.exit(app.exec())
