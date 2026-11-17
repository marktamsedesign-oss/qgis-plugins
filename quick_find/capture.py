"""Map tool: click the map to drop the Quick Find pin there."""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QCursor
from qgis.core import QgsPointLocator
from qgis.gui import QgsMapTool, QgsSnapIndicator


class CaptureTool(QgsMapTool):
    def __init__(self, canvas, on_point):
        super().__init__(canvas)
        self.on_point = on_point  # called with the clicked point in the map CRS
        self.snap = QgsSnapIndicator(canvas)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def canvasMoveEvent(self, event):
        self.snap.setMatch(self.canvas().snappingUtils().snapToMap(event.originalPixelPoint()))

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # uses the project's snapping settings, so a click can land exactly on a feature
        match = self.canvas().snappingUtils().snapToMap(event.originalPixelPoint())
        self.on_point(match.point() if match.isValid() else event.mapPoint())

    def deactivate(self):
        self.snap.setMatch(QgsPointLocator.Match())
        super().deactivate()
