"""Info tool: click a feature to see all its data, with links that open in the browser.

Works on any visible vector layer, e.g. an imported KMZ, where it shows the
ExtendedData fields and the KML description; GDrive (and any other web)
links get their own buttons and are clickable in the table.
"""
import html
import re
from functools import partial

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QColor, QCursor, QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout, QLabel,
                                 QPushButton, QTextBrowser, QVBoxLayout)
from qgis.core import (QgsFeatureRequest, QgsGeometry, QgsProject, QgsRectangle,
                       QgsVectorLayer)
from qgis.gui import QgsHighlight, QgsMapTool

URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TOLERANCE_PX = 6
MAX_HITS = 20


def urls_in(value):
    return [u.rstrip(".,);]") for u in URL.findall(str(value))]


def link_buttons(attributes):
    """[(button label, url)], Google Drive links first, each url once."""
    found, seen = [], set()
    for name, value in attributes:
        for url in urls_in(value):
            if url not in seen:
                seen.add(url)
                found.append((name, url))
    found.sort(key=lambda item: "drive.google" not in item[1].lower() and "gdrive" not in item[0].lower())
    total = {}
    for name, _ in found:
        total[name] = total.get(name, 0) + 1
    labels, nth = [], {}
    for name, url in found:
        nth[name] = nth.get(name, 0) + 1
        labels.append((f"Open {name}" + (f" ({nth[name]})" if total[name] > 1 else ""), url))
    return labels


def table_html(attributes):
    """Attribute table; empty values left out, links clickable, long text kept whole."""
    rows = []
    for name, value in attributes:
        text = str(value).strip()
        if not text or text == "NULL":
            continue
        cell = html.escape(text)
        for url in sorted(set(urls_in(text)), key=len, reverse=True):
            safe = html.escape(url)
            cell = cell.replace(safe, f'<a href="{safe}">{safe}</a>')
        rows.append(f'<tr><td style="color:#555;padding:2px 10px 2px 0;vertical-align:top;'
                    f'white-space:nowrap"><b>{html.escape(name)}</b></td>'
                    f'<td style="padding:2px 0">{cell.replace(chr(10), "<br>")}</td></tr>')
    return "<table cellspacing=0>" + "".join(rows) + "</table>" if rows else "<i>No data.</i>"


class InfoDialog(QDialog):
    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.hits = []
        self.highlight = None
        self.setWindowTitle("Feature info")
        self.resize(560, 520)
        layout = QVBoxLayout(self)
        self.picker = QComboBox()
        self.picker.currentIndexChanged.connect(self._show)
        layout.addWidget(self.picker)
        self.title = QLabel()
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.title)
        self.links = QHBoxLayout()
        layout.addLayout(self.links)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)  # links open in the default browser
        layout.addWidget(self.browser, 1)
        buttons = QHBoxLayout()
        self.copy_button = QPushButton("Copy all")
        self.copy_button.clicked.connect(self._copy)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def show_hits(self, hits):
        self.hits = hits
        self.picker.blockSignals(True)
        self.picker.clear()
        for layer, feature in hits:
            self.picker.addItem(f"{layer.name()} · {self._name(layer, feature)}")
        self.picker.blockSignals(False)
        self.picker.setVisible(len(hits) > 1)
        self._show(0)
        self.show()
        self.raise_()
        self.activateWindow()

    @staticmethod
    def _name(layer, feature):
        for key in ("Name", "name", "NAME", "Site_Name", "Segment_Name", "label"):
            index = layer.fields().lookupField(key)
            if index >= 0 and str(feature.attribute(index) or "").strip() not in ("", "NULL"):
                return str(feature.attribute(index))
        return f"feature {feature.id()}"

    def attributes(self, layer, feature):
        return [(layer.fields().at(i).name(), feature.attribute(i))
                for i in range(layer.fields().count())]

    def _show(self, index):
        if not 0 <= index < len(self.hits):
            return
        layer, feature = self.hits[index]
        attributes = self.attributes(layer, feature)
        self.title.setText(f"<b>{html.escape(self._name(layer, feature))}</b> — {html.escape(layer.name())}")
        while self.links.count():
            item = self.links.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.link_urls = link_buttons(attributes)
        for label, url in self.link_urls[:6]:
            button = QPushButton(label)
            button.setToolTip(url)
            button.clicked.connect(partial(self.open_url, url))
            self.links.addWidget(button)
        self.links.addStretch(1)
        # fid (GeoPackage id) and kml_color (used for the map colour) are not worth showing
        body = table_html([(n, v) for n, v in attributes
                           if n.lower() not in ("description", "fid", "kml_color")])
        description = next((v for n, v in attributes if n.lower() == "description"), None)
        if description and str(description).strip() not in ("", "NULL"):
            body += "<hr><p><b>Description</b></p>" + str(description)  # KML balloons are HTML
        self.browser.setHtml(body)
        self._highlight(layer, feature)

    def open_url(self, url, *_):
        QDesktopServices.openUrl(QUrl(url))

    def _highlight(self, layer, feature):
        self._clear_highlight()
        if feature.hasGeometry():
            self.highlight = QgsHighlight(self.canvas, feature.geometry(), layer)
            self.highlight.setColor(QColor(255, 0, 170))
            self.highlight.setWidth(4)
            self.highlight.show()

    def _clear_highlight(self):
        if self.highlight is not None:
            self.canvas.scene().removeItem(self.highlight)
            self.highlight = None

    def _copy(self):
        if self.hits:
            layer, feature = self.hits[self.picker.currentIndex() if self.picker.count() else 0]
            lines = [f"{n}: {v}" for n, v in self.attributes(layer, feature)
                     if str(v).strip() not in ("", "NULL")]
            QApplication.clipboard().setText("\n".join(lines))

    def closeEvent(self, event):
        self._clear_highlight()
        super().closeEvent(event)


class InfoTool(QgsMapTool):
    def __init__(self, canvas, parent=None):
        super().__init__(canvas)
        self.setCursor(QCursor(Qt.CursorShape.WhatsThisCursor))
        self.dialog = InfoDialog(canvas, parent)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hits = self.features_at(event.originalPixelPoint())
        if hits:
            self.dialog.show_hits(hits)

    def features_at(self, pixel):
        """[(layer, feature)] under the pixel, top layer first, nearest first."""
        canvas = self.canvas()
        settings = canvas.mapSettings()
        point = canvas.getCoordinateTransform().toMapCoordinates(pixel)
        tolerance = TOLERANCE_PX * settings.mapUnitsPerPixel()
        area = QgsRectangle(point.x() - tolerance, point.y() - tolerance,
                            point.x() + tolerance, point.y() + tolerance)
        hits = []
        for order, layer in enumerate(canvas.layers()):
            if not isinstance(layer, QgsVectorLayer) or not layer.isSpatial():
                continue
            search = settings.mapToLayerCoordinates(layer, area)
            where = QgsGeometry.fromPointXY(settings.mapToLayerCoordinates(layer, point))
            reach = max(search.width(), search.height()) / 2
            for feature in layer.getFeatures(QgsFeatureRequest().setFilterRect(search).setLimit(MAX_HITS)):
                distance = feature.geometry().distance(where)
                if 0 <= distance <= reach:
                    hits.append((order, distance, layer, feature))
        hits.sort(key=lambda h: (h[0], h[1]))
        return [(layer, feature) for _, _, layer, feature in hits[:MAX_HITS]]

    def deactivate(self):
        self.dialog._clear_highlight()
        super().deactivate()
