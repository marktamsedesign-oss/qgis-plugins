"""Map tool: click points to draw a line; each leg can follow the roads.

Hold the left button (or drag) to draw freehand: points follow the cursor
until the button is released. Arrow keys or W/A/S/D move the map, also
during a freehand stroke, to make room for more drawing.

Points are kept in WGS84, so changing the map CRS mid-drawing is safe.
"""
from qgis.PyQt.QtCore import QPoint, Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QLabel
from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsGeometry, QgsPointLocator, QgsProject, QgsWkbTypes)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsSnapIndicator

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
try:
    LINE, POINT = Qgis.GeometryType.Line, Qgis.GeometryType.Point
except AttributeError:  # QGIS < 3.30
    LINE, POINT = QgsWkbTypes.LineGeometry, QgsWkbTypes.PointGeometry

ORANGE = QColor(255, 102, 0)
HELP = ("click to add · hold or drag = freehand · Shift+click = straight · "
        "right-click or Backspace = undo · Enter = save · Esc = cancel · arrows/WASD = move map")
LABEL_STYLE = ("QLabel { background: rgba(255, 255, 255, 230); color: #222;"
               " border: 1px solid #ff6600; border-radius: 3px; padding: 2px 6px; }")

LONG_PRESS_MS = 400   # holding the button this long starts freehand drawing
DRAG_START_PX = 8     # ...and so does dragging this far before that
STREAM_STEP_PX = 6    # freehand: a new point every this many pixels of movement
PAN_STEP = 0.25       # one key press moves the map a quarter of the view
PAN_KEYS = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_A: (-1, 0), Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_D: (1, 0), Qt.Key.Key_Up: (0, 1), Qt.Key.Key_W: (0, 1),
            Qt.Key.Key_Down: (0, -1), Qt.Key.Key_S: (0, -1)}
NOT_PAN = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
           | Qt.KeyboardModifier.MetaModifier)  # e.g. Cmd+S still saves the project


def format_length(metres):
    return f"{metres:,.0f} m"


class RouteTool(QgsMapTool):
    def __init__(self, canvas, plugin, title="Draw Route", closed=False):
        """closed: draw an area outline (a boundary): the last point joins the first."""
        super().__init__(canvas)
        self.plugin = plugin
        self.title, self.closed = title, closed
        self.start = None
        self.legs = []  # (points after the previous point up to this one, method)
        self._press = None   # (pixel, WGS84 point, modifiers) while the left button is down
        self._stroke = None  # points of the freehand stroke being drawn
        self._stroke_px = None
        self._hold = QTimer()
        self._hold.setSingleShot(True)
        self._hold.setInterval(LONG_PRESS_MS)
        self._hold.timeout.connect(self._start_stroke)

        self.line = QgsRubberBand(canvas, LINE)
        self.line.setColor(ORANGE)
        self.line.setWidth(3)
        self.preview = QgsRubberBand(canvas, LINE)
        self.preview.setColor(QColor(255, 102, 0, 170))
        self.preview.setWidth(2)
        self.preview.setLineStyle(Qt.PenStyle.DashLine)
        self.marks = QgsRubberBand(canvas, POINT)
        self.marks.setColor(ORANGE)
        self.marks.setFillColor(QColor(255, 255, 255))
        self.marks.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
        self.marks.setIconSize(9)
        self.marks.setWidth(2)
        self.snap = QgsSnapIndicator(canvas)
        # measurement that follows the cursor while drawing
        self.label = QLabel(canvas)
        self.label.setStyleSheet(LABEL_STYLE)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.label.hide()
        self._cursor = None
        canvas.destinationCrsChanged.connect(self._redraw)
        canvas.keyPressed.connect(self._key_during_stroke)

    # ----- state -------------------------------------------------------
    def vertices(self):
        if self.start is None:
            return []
        return [self.start] + [p for points, _ in self.legs for p in points]

    def reset(self):
        self._end_stroke()
        self.start, self.legs = None, []
        self._redraw()

    def undo(self):
        """Remove the last point, or the whole last freehand stroke."""
        if self.legs:
            self.legs.pop()
        else:
            self.start = None
        self._redraw()

    def finish(self):
        points = self.vertices()
        if len(points) < (3 if self.closed else 2):
            self.plugin.warn(f"Click at least {'three' if self.closed else 'two'} points before saving.")
            return
        if self.plugin.save_route(points, {method for _, method in self.legs}):
            self.reset()

    def cleanup(self):
        canvas = self.canvas()
        canvas.destinationCrsChanged.disconnect(self._redraw)
        canvas.keyPressed.disconnect(self._key_during_stroke)
        self._end_stroke()
        self.start, self.legs = None, []
        self.snap.setMatch(QgsPointLocator.Match())
        for band in (self.line, self.preview, self.marks):
            canvas.scene().removeItem(band)
        self.label.deleteLater()

    def pan(self, dx, dy):
        """Move the map by a quarter view; dx/dy are -1, 0 or 1 (screen directions)."""
        canvas = self.canvas()
        size = canvas.mapSettings().outputSize()
        to_map = canvas.getCoordinateTransform()
        canvas.setCenter(to_map.toMapCoordinates(QPoint(round(size.width() * (0.5 + dx * PAN_STEP)),
                                                        round(size.height() * (0.5 - dy * PAN_STEP)))))
        canvas.refresh()
        if self._cursor is None:
            return
        # the cursor did not move on screen, but it now points somewhere else on the map
        point = self._to_wgs(canvas.getCoordinateTransform().toMapCoordinates(self._cursor))
        if self._stroke is not None:
            self._stroke.append(point)
            self._redraw()
        elif self.start is not None:
            self._show_preview(point)

    # ----- freehand ----------------------------------------------------
    def _add_point(self, point, modifiers):
        if self.start is None:
            self.start = point
        else:
            straight = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            self.legs.append(self.plugin.make_leg(self.vertices()[-1], point, straight))

    def _start_stroke(self):
        if self._press is None or self._stroke is not None:
            return
        pixel, point, modifiers = self._press
        self._add_point(point, modifiers)  # reach the stroke start like a click would
        self._stroke = []
        self.legs.append((self._stroke, "freehand"))
        self._stroke_px = pixel
        self._redraw()

    def _end_stroke(self):
        self._hold.stop()
        self._press = None
        if self._stroke is not None:
            if not self._stroke:  # held without moving: just the point
                self.legs.pop()
            self._stroke = None

    def _key_during_stroke(self, event):
        # while a mouse button is down the canvas does not pass keys to the
        # tool, it only emits keyPressed: handle panning here during a stroke
        if (self._stroke is not None and event.key() in PAN_KEYS
                and not event.modifiers() & NOT_PAN):
            self.pan(*PAN_KEYS[event.key()])

    # ----- events ------------------------------------------------------
    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            point, _ = self._point(event)
            self._press = (event.originalPixelPoint(), point, event.modifiers())
            self._hold.start()

    def canvasMoveEvent(self, event):
        self._cursor = event.originalPixelPoint()
        if self._press is not None and self._stroke is None and \
                (self._cursor - self._press[0]).manhattanLength() >= DRAG_START_PX:
            self._hold.stop()
            self._start_stroke()
        if self._stroke is not None:
            if (self._cursor - self._stroke_px).manhattanLength() >= STREAM_STEP_PX:
                self._stroke.append(self._to_wgs(event.mapPoint()))  # freehand: no snapping
                self._stroke_px = self._cursor
                self._redraw()
            return
        point, match = self._point(event)
        self.snap.setMatch(match)
        if self.start is not None:
            self._show_preview(point)

    def canvasReleaseEvent(self, event):
        # every right-click release undoes one point, so quick repeated
        # right-clicks (even a double-click) undo several; only Enter saves
        if event.button() == Qt.MouseButton.RightButton:
            if self.start is not None and self._stroke is None:
                self.undo()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._cursor = event.originalPixelPoint()
        if self._stroke is not None:
            if self._cursor != self._stroke_px:
                self._stroke.append(self._to_wgs(event.mapPoint()))
            self._end_stroke()
        else:
            self._end_stroke()
            point, _ = self._point(event)
            self._add_point(point, event.modifiers())
        self._redraw()

    def keyPressEvent(self, event):
        # QGIS map canvas convention (backwards on purpose): a tool *ignores*
        # an event it has handled; an accepted event gets the canvas default
        key = event.key()
        if key in PAN_KEYS and not event.modifiers() & NOT_PAN:
            self.pan(*PAN_KEYS[key])
        elif key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self.start is not None:
            self.undo()
        elif key == Qt.Key.Key_Escape and self.start is not None:
            self.reset()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.finish()
        else:
            event.accept()
            return
        event.ignore()

    def activate(self):
        super().activate()
        self._status()

    def deactivate(self):
        # keep an unfinished line: pick the tool again to carry on
        self._end_stroke()
        self.preview.reset(LINE)
        self.label.hide()
        self.snap.setMatch(QgsPointLocator.Match())
        self.plugin.iface.statusBarIface().clearMessage()
        super().deactivate()

    # ----- helpers -----------------------------------------------------
    def _to_wgs(self, map_point):
        return QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(),
                                      WGS84, QgsProject.instance()).transform(map_point)

    def _point(self, event):
        """Clicked point in WGS84, using the project's snapping settings."""
        match = self.canvas().snappingUtils().snapToMap(event.originalPixelPoint())
        return self._to_wgs(match.point() if match.isValid() else event.mapPoint()), match

    def _show_preview(self, point):
        points = self.vertices()
        last = points[-1]
        ahead = [last, point, points[0]] if self.closed and len(points) > 1 else [last, point]
        self.preview.setToGeometry(QgsGeometry.fromPolylineXY(ahead), WGS84)
        to_cursor = self.plugin.length([last, point])
        self._status(to_cursor)
        self._measure(to_cursor)

    def _redraw(self, *_):
        points = self.vertices()
        self.line.reset(LINE)
        self.preview.reset(LINE)
        self.marks.reset(POINT)
        if len(points) >= 2:
            outline = points + [points[0]] if self.closed and len(points) >= 3 else points
            self.line.setToGeometry(QgsGeometry.fromPolylineXY(outline), WGS84)
        if self.start is not None:
            # circles at clicked points and at the ends of freehand strokes
            clicks = [self.start] + [leg_points[-1] for leg_points, _ in self.legs if leg_points]
            self.marks.setToGeometry(QgsGeometry.fromMultiPointXY(clicks), WGS84)
        self._status()
        self._measure()

    def _measure(self, to_cursor=None):
        """Show the total length (and the distance to the cursor) beside the cursor."""
        points = self.vertices()
        if not points or self._cursor is None:
            self.label.hide()
            return
        text = f"Total: <b>{format_length(self.plugin.length(points))}</b>"
        if to_cursor:
            text += f"<br>To cursor: +{format_length(to_cursor)}"
        self.label.setText(text)
        self.label.adjustSize()
        canvas = self.canvas()
        x, y = self._cursor.x() + 16, self._cursor.y() + 16
        if x + self.label.width() > canvas.width():
            x = self._cursor.x() - 16 - self.label.width()
        if y + self.label.height() > canvas.height():
            y = self._cursor.y() - 16 - self.label.height()
        self.label.move(QPoint(max(0, x), max(0, y)))
        self.label.show()
        self.label.raise_()

    def _status(self, to_cursor=None):
        roads = "Follow Roads on" if self.plugin.follow_roads() else "straight lines"
        points = self.vertices()
        if self._stroke is not None:
            text = (f"{self.title}: freehand, {format_length(self.plugin.length(points))} · "
                    "release the button to stop · arrows/WASD move the map")
        elif not points:
            text = f"{self.title} ({roads}): click the start point · arrows/WASD move the map"
        else:
            length = self.plugin.length(points) if len(points) > 1 else 0.0
            text = f"{self.title} ({roads}): {format_length(length)}"
            if to_cursor:
                text += f" (+{format_length(to_cursor)} to cursor)"
            text += " · " + HELP
        self.plugin.iface.statusBarIface().showMessage(text)
