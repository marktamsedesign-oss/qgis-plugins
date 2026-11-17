"""A temporary map pin with its coordinates beside it.

It is drawn on the map canvas only: it is not a layer, is not saved in the
project, and disappears when Quick Find removes it or is unloaded.
"""
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QPainter,
                             QPainterPath, QPen)
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsCsException, QgsPointXY, QgsProject)
from qgis.gui import QgsMapCanvasItem

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
RED = QColor(229, 57, 53)
DARK_RED = QColor(140, 20, 20)


class PinItem(QgsMapCanvasItem):
    def __init__(self, canvas, lat, lon, text):
        super().__init__(canvas)
        self.canvas_ = canvas
        self.point = QgsPointXY(lon, lat)
        self.text = text
        self.font = QFont()
        self.font.setPointSizeF(9)
        self.font.setBold(True)
        metrics = QFontMetricsF(self.font)
        self.text_size = (metrics.horizontalAdvance(text), metrics.height())
        self.setZValue(1000)
        self.updatePosition()

    def map_point(self):
        """The pin in the canvas CRS, or None if it cannot be shown there."""
        transform = QgsCoordinateTransform(WGS84, self.canvas_.mapSettings().destinationCrs(),
                                           QgsProject.instance())
        try:
            return transform.transform(self.point)
        except QgsCsException:
            return None

    def updatePosition(self):
        point = self.map_point()
        self.setVisible(point is not None)
        if point is not None:
            self.setPos(self.toCanvasCoordinates(point))

    def boundingRect(self):
        width, height = self.text_size
        return QRectF(-13, -38, 13 + 16 + width + 14, max(42, height + 10))

    def paint(self, painter, *_):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # pin: the tip is exactly on the coordinate (0, 0)
        head = QPainterPath()
        head.addEllipse(QPointF(0, -25), 10, 10)
        tail = QPainterPath()
        tail.moveTo(0, 0)
        tail.lineTo(-8.6, -20)
        tail.lineTo(8.6, -20)
        tail.closeSubpath()
        painter.setPen(QPen(DARK_RED, 1.4))
        painter.setBrush(QBrush(RED))
        painter.drawPath(head.united(tail))
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(0, -25), 3.6, 3.6)

        # coordinates label to the right of the pin head
        width, height = self.text_size
        box = QRectF(15, -25 - height / 2 - 3, width + 12, height + 6)
        painter.setPen(QPen(RED, 1))
        painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
        painter.drawRoundedRect(box, 3, 3)
        painter.setFont(self.font)
        painter.setPen(QColor(40, 40, 40))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self.text)
