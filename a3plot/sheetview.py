"""Blattansicht: Papier, Zeichnung verschieben/skalieren, Plotfortschritt."""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QCursor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QGraphicsDropShadowEffect, QGraphicsEllipseItem, QGraphicsItem,
                             QGraphicsLineItem, QGraphicsPathItem, QGraphicsRectItem,
                             QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView)

HANDLE_PX = 9


def polylines_path(paths) -> QPainterPath:
    qp = QPainterPath()
    for p in paths:
        if len(p) == 0:
            continue
        pts = p.tolist()
        qp.moveTo(pts[0][0], pts[0][1])
        if len(pts) == 1 or (len(pts) == 2 and pts[0] == pts[1]):
            qp.lineTo(pts[0][0] + 0.05, pts[0][1])
            continue
        for x, y in pts[1:]:
            qp.lineTo(x, y)
    return qp


def cosmetic(color, width=1.0, style=Qt.PenStyle.SolidLine):
    pen = QPen(QColor(color), width)
    pen.setCosmetic(True)
    pen.setStyle(style)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


class SheetView(QGraphicsView):
    placementChanged = pyqtSignal(float, float, float)   # x, y, scale (live)
    placementCommitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.RenderHint.Antialiasing)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)
        self.setBackgroundBrush(QColor("#c9ccd2"))
        sc = self.scene()
        sc.setSceneRect(-200, -200, 820, 820)

        self.paper = QGraphicsRectItem(0, 0, 210, 297)
        self.paper.setBrush(QBrush(Qt.GlobalColor.white))
        self.paper.setPen(QPen(Qt.PenStyle.NoPen))
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(18)
        shadow.setOffset(2, 3)
        shadow.setColor(QColor(0, 0, 0, 90))
        self.paper.setGraphicsEffect(shadow)
        sc.addItem(self.paper)

        self.margin = QGraphicsRectItem()
        self.margin.setPen(cosmetic("#9aa4b1", 1, Qt.PenStyle.DashLine))
        sc.addItem(self.margin)

        self.origin_mark = QGraphicsEllipseItem(-4, -4, 8, 8)
        self.origin_mark.setBrush(QBrush(QColor("#d23c3c")))
        self.origin_mark.setPen(QPen(Qt.PenStyle.NoPen))
        self.origin_mark.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.origin_mark.setZValue(20)
        self.origin_label = QGraphicsSimpleTextItem("Nullpunkt", self.origin_mark)
        self.origin_label.setBrush(QBrush(QColor("#d23c3c")))
        self.origin_label.setPos(6, -18)
        sc.addItem(self.origin_mark)

        self.content = QGraphicsRectItem(0, 0, 1, 1)
        self.content.setPen(cosmetic("#3d7bd9", 1, Qt.PenStyle.DashLine))
        self.content.setZValue(5)
        sc.addItem(self.content)
        self.layer_items = []

        self.handles = []
        for _ in range(4):
            h = QGraphicsRectItem(-HANDLE_PX / 2, -HANDLE_PX / 2, HANDLE_PX, HANDLE_PX)
            h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            h.setBrush(QBrush(Qt.GlobalColor.white))
            h.setPen(QPen(QColor("#3d7bd9"), 1.5))
            h.setZValue(15)
            sc.addItem(h)
            self.handles.append(h)

        self.travel_item = QGraphicsPathItem()
        self.travel_item.setPen(cosmetic("#e08a8a", 0.8, Qt.PenStyle.DotLine))
        self.travel_item.setZValue(6)
        self.travel_item.setVisible(False)
        sc.addItem(self.travel_item)

        self.done_items = []
        self.marker = QGraphicsEllipseItem(-6, -6, 12, 12)
        self.marker.setPen(QPen(QColor("#d23c3c"), 2))
        self.marker.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.marker.setZValue(30)
        cross = QGraphicsLineItem(-9, 0, 9, 0, self.marker)
        cross.setPen(QPen(QColor("#d23c3c"), 1))
        cross2 = QGraphicsLineItem(0, -9, 0, 9, self.marker)
        cross2.setPen(QPen(QColor("#d23c3c"), 1))
        self.marker.setVisible(False)
        sc.addItem(self.marker)

        self.local_w = self.local_h = 1.0
        self.has_drawing = False
        self.locked = False
        self.drag = None
        self.pan_last = None
        self.paper_size = (210.0, 297.0)
        self._update_handles()
        for h in self.handles:
            h.setVisible(False)
        self.content.setVisible(False)

    # ---------------------------------------------------------------- Setup
    def set_paper(self, w, h, margin):
        self.paper_size = (w, h)
        self.paper.setRect(0, 0, w, h)
        self.margin.setRect(margin, margin, w - 2 * margin, h - 2 * margin)
        self.margin.setVisible(margin > 0)
        self.scene().setSceneRect(-w, -h, 3 * w, 3 * h)

    def set_origin(self, u, v):
        self.origin_mark.setPos(u, v)
        self.origin_label.setPos(6 if u < self.paper_size[0] / 2 else -70, -18 if v > 20 else 4)

    def set_drawing(self, layers, local_w, local_h):
        """layers: Liste (QPainterPath in lokalen Koordinaten 0..w/0..h, Farbe, aktiv)."""
        for it in self.layer_items:
            self.scene().removeItem(it)
        self.layer_items = []
        self.local_w, self.local_h = max(local_w, 1e-6), max(local_h, 1e-6)
        self.content.setRect(0, 0, self.local_w, self.local_h)
        for path, color, active in layers:
            it = QGraphicsPathItem(path, self.content)
            c = QColor(color)
            if not active:
                c.setAlpha(45)
            it.setPen(cosmetic(c, 1.0))
            self.layer_items.append(it)
        self.has_drawing = bool(layers)
        self.content.setVisible(self.has_drawing)
        for h in self.handles:
            h.setVisible(self.has_drawing and not self.locked)

    def set_placement(self, x, y, scale):
        self.content.setPos(x, y)
        self.content.setScale(scale)
        self._update_handles()

    def set_travel(self, path: QPainterPath | None):
        if path is None:
            self.travel_item.setPath(QPainterPath())
        else:
            self.travel_item.setPath(path)

    def show_travel(self, on):
        self.travel_item.setVisible(on)

    def fit(self):
        w, h = self.paper_size
        self.fitInView(QRectF(-8, -8, w + 16, h + 16), Qt.AspectRatioMode.KeepAspectRatio)

    # ----------------------------------------------------------- Plotten
    def set_locked(self, locked):
        self.locked = locked
        for h in self.handles:
            h.setVisible(self.has_drawing and not locked)
        for it in self.layer_items:
            it.setOpacity(0.35 if locked else 1.0)

    def clear_progress(self):
        for it in self.done_items:
            self.scene().removeItem(it)
        self.done_items = []
        self.marker.setVisible(False)

    def add_done(self, paths):
        if not paths:
            return
        it = QGraphicsPathItem(polylines_path(paths))
        it.setPen(cosmetic("#0b3d91", 1.4))
        it.setZValue(10)
        self.scene().addItem(it)
        self.done_items.append(it)

    def set_marker(self, u, v, visible=True):
        self.marker.setPos(u, v)
        self.marker.setVisible(visible)

    # ------------------------------------------------------------- Maus
    def _corners(self):
        r = self.content.sceneBoundingRect()
        return [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]

    def _update_handles(self):
        for h, c in zip(self.handles, self._corners()):
            h.setPos(c)

    def _hit_handle(self, view_pos):
        if not self.has_drawing or self.locked:
            return None
        for i, c in enumerate(self._corners()):
            vp = self.mapFromScene(c)
            if abs(vp.x() - view_pos.x()) <= HANDLE_PX and abs(vp.y() - view_pos.y()) <= HANDLE_PX:
                return i
        return None

    def mousePressEvent(self, e):
        vp = e.position().toPoint()
        sp = self.mapToScene(vp)
        if e.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton) or (
                e.button() == Qt.MouseButton.LeftButton and (self.locked or not self.has_drawing)):
            self.pan_last = vp
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        h = self._hit_handle(vp)
        if h is not None:
            anchor = self._corners()[(h + 2) % 4]
            self.drag = ("resize", h, anchor)
            return
        if self.content.sceneBoundingRect().contains(sp):
            self.drag = ("move", sp - self.content.pos())
            return
        self.pan_last = vp
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        vp = e.position().toPoint()
        sp = self.mapToScene(vp)
        if self.pan_last is not None:
            d = vp - self.pan_last
            self.pan_last = vp
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            return
        if self.drag:
            snap = not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if self.drag[0] == "move":
                p = sp - self.drag[1]
                x, y = p.x(), p.y()
                if snap:
                    x, y = round(x * 2) / 2, round(y * 2) / 2
                self.placementChanged.emit(x, y, self.content.scale())
            else:
                _, h, anchor = self.drag
                w = abs(sp.x() - anchor.x())
                hh = abs(sp.y() - anchor.y())
                s = max(w / self.local_w, hh / self.local_h, 1e-3)
                nw, nh = self.local_w * s, self.local_h * s
                if snap:   # auf 1 mm Breite runden
                    nw = max(1.0, round(nw))
                    s = nw / self.local_w
                    nh = self.local_h * s
                x = anchor.x() if sp.x() >= anchor.x() else anchor.x() - nw
                y = anchor.y() if sp.y() >= anchor.y() else anchor.y() - nh
                self.placementChanged.emit(x, y, s)
            return
        # Cursor
        h = self._hit_handle(vp)
        if h is not None:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if h in (0, 2) else Qt.CursorShape.SizeBDiagCursor)
        elif self.has_drawing and not self.locked and self.content.sceneBoundingRect().contains(sp):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if self.pan_last is not None:
            self.pan_last = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if self.drag:
            self.drag = None
            self.placementCommitted.emit()

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120
        f = 1.15 ** steps
        cur = self.transform().m11()
        if 0.3 < cur * f < 200:
            self.scale(f, f)

    def mouseDoubleClickEvent(self, e):
        self.fit()
