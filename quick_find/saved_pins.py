"""The "Saved pins" GeoPackage layer: created on first use, reused after.

Saving writes straight to the GeoPackage (no edit mode needed), so a saved
pin is kept even if the project is not saved.
"""
import os
from datetime import datetime

from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor
from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeature, QgsField, QgsFields, QgsGeometry, QgsMarkerSymbol,
                       QgsPalLayerSettings, QgsPointXY, QgsProject, QgsProperty, QgsProviderRegistry,
                       QgsSymbolLayer,
                       QgsTextBufferSettings, QgsTextFormat, QgsVectorFileWriter,
                       QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsWkbTypes)

LAYER_NAME = "saved_pins"
TITLE = "Saved pins"
DEFAULT_GPKG = os.path.join(os.path.expanduser("~"), "Documents", "QGIS Quick Find",
                            "saved_pins.gpkg")
FIELDS = (("name", "String"), ("latitude", "Double"), ("longitude", "Double"),
          ("source", "String"), ("created", "String"),
          ("remarks", "String"), ("color", "String"), ("size_mm", "Double"))
DEFAULT_COLOR, DEFAULT_SIZE = "#e53935", 6.0  # pushpin height in mm
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
try:  # QGIS >= 3.30 and QGIS 4
    POINT_TYPE = Qgis.WkbType.Point
except AttributeError:
    POINT_TYPE = QgsWkbTypes.Point
from .colour_legend import (build as build_colour_legend, is_colour_legend, rebuild_file,
                            watch as watch_colour_legend)
from .pushpin import FILL_COLOR, SIZE, is_pushpin, pushpin_symbol
from .save_group import add_to


def new_field(name, kind):
    """A QgsField of kind "String" or "Double" on QGIS 3.22+ and QGIS 4."""
    try:  # QGIS >= 3.38 and QGIS 4 take QMetaType
        return QgsField(name, getattr(QMetaType.Type, "QString" if kind == "String" else kind))
    except TypeError:  # older QGIS 3 takes QVariant
        from qgis.PyQt.QtCore import QVariant
        return QgsField(name, getattr(QVariant, kind))


def _same_file(a, b):
    # normcase: on Windows "C:/Users/..." and "c:\users\..." are the same file
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def is_pins_layer(layer, path):
    if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
        return False
    parts = QgsProviderRegistry.instance().decodeUri("ogr", layer.source())
    return parts.get("layerName") == LAYER_NAME and _same_file(parts.get("path", ""), path)


def open_or_create(path, group=None):
    """(layer in the project, error message or None); a new layer goes at the top of the
    group (the top level when None)."""
    for layer in QgsProject.instance().mapLayers().values():
        if is_pins_layer(layer, path):
            upgrade(layer)
            return layer, None
    uri = f"{path}|layername={LAYER_NAME}"
    layer = QgsVectorLayer(uri, TITLE, "ogr")
    if not layer.isValid():
        error = _create(path)
        if error:
            return None, error
        layer = QgsVectorLayer(uri, TITLE, "ogr")
        if not layer.isValid():
            return None, f"Could not open {path}."
    if layer.listStylesInDatabase()[0] == 0:  # no style saved in the GeoPackage: use ours
        _style(layer)
    upgrade(layer)
    add_to(group if group is not None else QgsProject.instance().layerTreeRoot(), layer)
    return layer, None


def _create(path):
    if os.path.exists(path):
        names = [d.name() for d in QgsProviderRegistry.instance().querySublayers(path)]
        if LAYER_NAME in names:  # it exists but would not open: never overwrite it
            return f"'{LAYER_NAME}' in {path} could not be opened; nothing was saved."
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = QgsFields()
    for name, kind in FIELDS:
        fields.append(new_field(name, kind))
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = LAYER_NAME
    options.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                    if os.path.exists(path)
                                    else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
    writer = QgsVectorFileWriter.create(path, fields, POINT_TYPE, WGS84,
                                        QgsProject.instance().transformContext(), options)
    failed = writer.hasError() != QgsVectorFileWriter.WriterError.NoError
    message = writer.errorMessage()
    del writer
    return f"Could not create {path}: {message}" if failed else None


def upgrade(layer):
    """Pins files from earlier versions get the new columns (pins in them are kept). The
    plugin's own style becomes a colour list: one Layers-panel entry per colour, labelled
    with the pins that use it, each drawn as a pushpin in its own colour and size. A style
    you made yourself is left alone (each pin's colour and size is added to it)."""
    missing = [new_field(n, k) for n, k in FIELDS if layer.fields().lookupField(n) < 0]
    if missing and layer.dataProvider().addAttributes(missing):
        layer.updateFields()
    if _is_our_style(layer):
        rebuild_legend(layer)
        watch_colour_legend(layer, rebuild_legend)
        return
    renderer = layer.renderer()
    if renderer is None or renderer.type() != "singleSymbol":
        return  # styled by the user (categories, rules...): leave it
    symbol_layer = renderer.symbol().symbolLayer(0)
    properties = symbol_layer.dataDefinedProperties()
    if not properties.isActive(FILL_COLOR):  # @symbol_color: the layer's own colour
        symbol_layer.setDataDefinedProperty(FILL_COLOR, QgsProperty.fromExpression("coalesce(\"color\", @symbol_color)"))
    if not properties.isActive(SIZE):
        symbol_layer.setDataDefinedProperty(
            SIZE, QgsProperty.fromExpression(f"coalesce(\"size_mm\", {renderer.symbol().size()})"))
    layer.triggerRepaint()


def _is_our_style(layer):
    """The colour list, the earlier single pushpin, or the earliest red dot."""
    if is_colour_legend(layer):
        return True
    renderer = layer.renderer()
    if renderer is None or renderer.type() != "singleSymbol":
        return False
    symbol = renderer.symbol()
    return is_pushpin(symbol) or (symbol.symbolLayer(0).layerType() == "SimpleMarker"
                                  and symbol.color().name() == DEFAULT_COLOR)


def rebuild_legend(layer):
    build_colour_legend(layer, lambda colour: pushpin_symbol(colour, DEFAULT_SIZE, size_field="size_mm"),
                        DEFAULT_COLOR)


def is_any_pins_file(layer):
    """A Saved pins layer from any pins file (also older, dated ones)."""
    if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
        return False
    return QgsProviderRegistry.instance().decodeUri("ogr", layer.source()).get("layerName") == LAYER_NAME


def count_in_file(path):
    """Pins already in the file (0 when there is no file yet)."""
    if not os.path.exists(path):
        return 0
    layer = QgsVectorLayer(f"{path}|layername={LAYER_NAME}", "count", "ogr")
    return max(layer.featureCount(), 0) if layer.isValid() else 0


def next_file(path):
    """A new pins file next to the old one: saved_pins_2026-10-26.gpkg, ..._2, ..."""
    folder = os.path.dirname(path)
    stem = f"saved_pins_{datetime.now():%Y-%m-%d}"
    candidate, n = os.path.join(folder, stem + ".gpkg"), 2
    while os.path.exists(candidate):
        candidate, n = os.path.join(folder, f"{stem}_{n}.gpkg"), n + 1
    return candidate


def _style(layer):
    layer.renderer().setSymbol(pushpin_symbol(DEFAULT_COLOR, DEFAULT_SIZE, "color", "size_mm"))
    label = QgsPalLayerSettings()
    label.fieldName = "name"
    try:  # beside the pin's head, not on its needle (QGIS >= 3.26 and QGIS 4)
        label.placement = Qgis.LabelPlacement.OverPoint
        label.quadOffset = Qgis.LabelQuadrantPosition.Right
    except AttributeError:
        label.placement = QgsPalLayerSettings.OverPoint
        label.quadOffset = QgsPalLayerSettings.QuadrantRight
    label.xOffset, label.yOffset = 1.6, -4.2  # millimetres, for the 6 mm pushpin
    text = QgsTextFormat()
    text.setSize(9)
    text.setColor(QColor(140, 20, 20))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize(1)
    buffer.setColor(QColor(255, 255, 255))
    text.setBuffer(buffer)
    label.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(label))
    layer.setLabelsEnabled(True)


def save_point(layer, lat, lon, name, source, remarks=None, color=None, size=None):
    """Write one pin; returns True when it was stored."""
    geometry = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
    geometry.transform(QgsCoordinateTransform(WGS84, layer.crs(), QgsProject.instance()))
    if QgsWkbTypes.isMultiType(layer.wkbType()):
        geometry.convertToMultiType()
    feature = QgsFeature(layer.fields())
    feature.setGeometry(geometry)
    values = {"name": name, "latitude": round(lat, 7), "longitude": round(lon, 7),
              "source": source, "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "remarks": remarks or None, "color": color, "size_mm": size}
    for key, value in values.items():
        index = layer.fields().lookupField(key)
        if index >= 0 and value is not None:
            feature.setAttribute(index, value)
    if layer.isEditable():
        ok = layer.addFeature(feature)
    else:
        ok, _ = layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
    if ok:
        rebuild_file(layer, rebuild_legend)
    layer.triggerRepaint()
    return bool(ok)
