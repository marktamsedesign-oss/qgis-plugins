"""A Google-Earth-style pushpin marker, in each feature's own colour and size.

The SVG is embedded in the style (no file to lose when a project is moved or
opened on another computer) and the needle tip is on the exact point. (The
same file is in Quick Find and Route Drawer, so each plugin installs on its own.)
"""
import base64

from qgis.PyQt.QtGui import QColor
from qgis.core import Qgis, QgsMarkerSymbol, QgsProperty, QgsSvgMarkerSymbolLayer, QgsSymbolLayer

# head in the pin colour (param(fill)), white highlight, darker collar, steel needle; tip at the bottom centre
PUSHPIN_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">'
    '<path d="M16 17.5 L16 31.4" stroke="#3c4043" stroke-width="2.6" stroke-linecap="round"/>'
    '<ellipse cx="16" cy="17" rx="4.2" ry="1.6" fill="param(fill) #e53935" '
    'stroke="param(outline) #6d1414" stroke-width="param(outline-width) 0.9"/>'
    '<circle cx="16" cy="9.5" r="7.6" fill="param(fill) #e53935" '
    'stroke="param(outline) #6d1414" stroke-width="param(outline-width) 0.9"/>'
    '<ellipse cx="13.4" cy="6.8" rx="2.6" ry="1.9" fill="#ffffff" fill-opacity="0.55"/>'
    '</svg>')
PUSHPIN_PATH = "base64:" + base64.b64encode(PUSHPIN_SVG.encode()).decode()

try:  # QGIS >= 3.34 and QGIS 4
    BOTTOM, H_CENTER = Qgis.VerticalAnchorPoint.Bottom, Qgis.HorizontalAnchorPoint.Center
except AttributeError:
    BOTTOM, H_CENTER = QgsSvgMarkerSymbolLayer.Bottom, QgsSvgMarkerSymbolLayer.HCenter
try:  # QGIS >= 3.30 and QGIS 4
    FILL_COLOR, SIZE = QgsSymbolLayer.Property.FillColor, QgsSymbolLayer.Property.Size
except AttributeError:
    FILL_COLOR, SIZE = QgsSymbolLayer.PropertyFillColor, QgsSymbolLayer.PropertySize


def pushpin_symbol(color, size_mm, color_field=None, size_field=None):
    """A pushpin marker symbol; optional fields give each feature its own colour / size."""
    layer = QgsSvgMarkerSymbolLayer(PUSHPIN_PATH)
    layer.setSize(size_mm)
    layer.setFillColor(QColor(color))
    layer.setStrokeColor(QColor(color).darker(220))
    layer.setStrokeWidth(0.15)
    layer.setVerticalAnchorPoint(BOTTOM)
    layer.setHorizontalAnchorPoint(H_CENTER)
    if color_field:  # @symbol_color: the layer's own colour for features without one
        layer.setDataDefinedProperty(
            FILL_COLOR, QgsProperty.fromExpression(f'coalesce("{color_field}", @symbol_color)'))
    if size_field:
        layer.setDataDefinedProperty(SIZE, QgsProperty.fromExpression(f'coalesce("{size_field}", {size_mm})'))
    symbol = QgsMarkerSymbol([layer])
    symbol.setColor(QColor(color))
    return symbol


def is_pushpin(symbol):
    layer = symbol.symbolLayer(0) if symbol is not None and symbol.symbolLayerCount() else None
    return isinstance(layer, QgsSvgMarkerSymbolLayer) and layer.path() == PUSHPIN_PATH
