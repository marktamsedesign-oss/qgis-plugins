"""Export a vector layer to KMZ (Google Earth) or Excel (.xlsx).

KMZ: written here as one standard doc.kml, zipped. Each placemark gets the
colour it has on the map (also rule-based / data-defined colours, e.g. the
kml_color of an imported KMZ), a name-like field becomes its name, a
"description" field its balloon text, and every other attribute goes into
its ExtendedData. A "folder" field (from Import KMZ) rebuilds the folders.
Excel: one row per feature with all attributes, plus WGS84 coordinates
(points), length and start/end (lines) or area and centre (polygons).
"""
import os
import re
import shutil
import tempfile
import zipfile
from xml.sax.saxutils import escape, quoteattr

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsDistanceArea, QgsExpressionContext, QgsExpressionContextUtils,
                       QgsFeatureRequest, QgsGeometry, QgsProject, QgsRenderContext,
                       QgsSymbolLayer, QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes)

from .latlon import name_field
from .save_dialog import REMARKS_COLUMNS
from .storage import new_field

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
EXCEL_CELL_LIMIT = 32000  # Excel refuses longer text in a cell
try:  # QGIS >= 3.30 and QGIS 4
    POINT, LINE, POLYGON = Qgis.GeometryType.Point, Qgis.GeometryType.Line, Qgis.GeometryType.Polygon
except AttributeError:
    POINT, LINE, POLYGON = QgsWkbTypes.PointGeometry, QgsWkbTypes.LineGeometry, QgsWkbTypes.PolygonGeometry
try:
    FILL_COLOR, STROKE_COLOR = QgsSymbolLayer.Property.FillColor, QgsSymbolLayer.Property.StrokeColor
    STROKE_WIDTH, SIZE = QgsSymbolLayer.Property.StrokeWidth, QgsSymbolLayer.Property.Size
except AttributeError:
    FILL_COLOR, STROKE_COLOR = QgsSymbolLayer.PropertyFillColor, QgsSymbolLayer.PropertyStrokeColor
    STROKE_WIDTH, SIZE = QgsSymbolLayer.PropertyStrokeWidth, QgsSymbolLayer.PropertySize
PIN_REFERENCE_MM = 6.0  # a 6 mm pushpin is Google Earth's normal pin size (icon scale 1.0)
# Google Earth's own pushpin; a white one takes the pin colour exactly. The tip is at (20, 2) pixels.
PUSHPIN_ICON = "http://maps.google.com/mapfiles/kml/pushpin/wht-pushpin.png"
SKIP_IN_DATA = {"kml_color", "folder", "fid"}  # used for style/folders, or internal ids


def _write(layer, path, options):
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsProject.instance().transformContext(), options)
    error, message = result[0], result[1]
    if error != QgsVectorFileWriter.WriterError.NoError:
        raise OSError(message or "the file could not be written")


# ----- KMZ ---------------------------------------------------------------------
def _kml_color(color):
    """QColor -> KML aabbggrr."""
    return f"{color.alpha():02x}{color.blue():02x}{color.green():02x}{color.red():02x}"


def _cdata(text):
    return "<![CDATA[" + str(text).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _coords(points):
    return " ".join(f"{p.x():.7f},{p.y():.7f}" for p in points)


def _geometry_kml(geometry):
    """KML for a WGS84 QgsGeometry (multi-part -> MultiGeometry)."""
    kind = geometry.type()
    if kind == POINT:
        parts = [[p] for p in (geometry.asMultiPoint() if geometry.isMultipart() else [geometry.asPoint()])]
        items = [f"<Point><coordinates>{_coords(p)}</coordinates></Point>" for p in parts]
    elif kind == LINE:
        lines = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
        items = [f"<LineString><tessellate>1</tessellate><coordinates>{_coords(l)}</coordinates></LineString>"
                 for l in lines if len(l) >= 2]
    elif kind == POLYGON:
        polygons = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
        items = []
        for rings in polygons:
            if not rings:
                continue
            inner = "".join(f"<innerBoundaryIs><LinearRing><coordinates>{_coords(r)}</coordinates>"
                            f"</LinearRing></innerBoundaryIs>" for r in rings[1:])
            items.append(f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{_coords(rings[0])}"
                         f"</coordinates></LinearRing></outerBoundaryIs>{inner}</Polygon>")
    else:
        items = []
    if len(items) == 1:
        return items[0]
    return f"<MultiGeometry>{''.join(items)}</MultiGeometry>" if items else ""


class _Colors:
    """The colour (and line width) each feature is drawn with on the map."""

    def __init__(self, layer):
        self.kind = layer.geometryType()
        self.renderer = layer.renderer().clone() if layer.renderer() else None
        self.context = QgsRenderContext()
        expression_context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        self.context.setExpressionContext(expression_context)
        if self.renderer:
            self.renderer.startRender(self.context, layer.fields())

    def close(self):
        if self.renderer:
            self.renderer.stopRender(self.context)

    def style(self, feature):
        """(kind, KML colour, width) or None when the layer has no symbol for the feature."""
        if not self.renderer:
            return None
        self.context.expressionContext().setFeature(feature)
        symbols = self.renderer.symbolsForFeature(feature, self.context)
        if not symbols:
            return None
        symbol = symbols[0]
        color = symbol.color()
        layer = symbol.symbolLayer(0) if symbol.symbolLayerCount() else None
        key = STROKE_COLOR if self.kind == LINE else FILL_COLOR
        if layer is not None and layer.dataDefinedProperties().isActive(key):
            value, ok = layer.dataDefinedProperties().valueAsColor(key, self.context.expressionContext(), color)
            if ok and value.isValid():
                color = value
        width_mm = symbol.width() if self.kind == LINE else 0
        if self.kind == LINE and layer is not None and layer.dataDefinedProperties().isActive(STROKE_WIDTH):
            value, ok = layer.dataDefinedProperties().valueAsDouble(STROKE_WIDTH, self.context.expressionContext(), width_mm)
            if ok and value:
                width_mm = value
        if self.kind == POINT:  # marker size (mm) -> KML icon scale
            size_mm = symbol.size()
            if layer is not None and layer.dataDefinedProperties().isActive(SIZE):
                value, ok = layer.dataDefinedProperties().valueAsDouble(SIZE, self.context.expressionContext(), size_mm)
                if ok and value:
                    size_mm = value
            return self.kind, _kml_color(color), round(min(max(size_mm / PIN_REFERENCE_MM, 0.6), 4.0), 2)
        width = round(max(width_mm, 0.26) * 3.78, 1) if self.kind == LINE else 2  # mm -> KML pixels
        return self.kind, _kml_color(color), width


def _style_kml(style_id, kind, color, width):
    if kind == POINT:  # "width" holds the icon scale here
        body = (f"<IconStyle><color>{color}</color><scale>{width}</scale>"
                f"<Icon><href>{PUSHPIN_ICON}</href></Icon>"
                '<hotSpot x="20" y="2" xunits="pixels" yunits="pixels"/></IconStyle>')
    elif kind == LINE:
        body = f"<LineStyle><color>{color}</color><width>{width}</width></LineStyle>"
    else:
        fill = "80" + color[2:]  # half transparent so the map shows through, as in QGIS
        body = (f"<LineStyle><color>ff{color[2:]}</color><width>{width}</width></LineStyle>"
                f"<PolyStyle><color>{fill}</color></PolyStyle>")
    return f'<Style id="{style_id}">{body}</Style>\n'


def _text(feature, index):
    """The attribute as text ('' for none / NULL)."""
    if index is None:
        return ""
    value = feature.attribute(index)
    return "" if value is None or str(value).strip() in ("", "NULL") else str(value).strip()


def _as_list(layers):
    return [layers] if isinstance(layers, QgsVectorLayer) else list(layers)


def _placemarks(layer, body, styles, only_selected):
    """Write one layer's placemarks (rebuilding its KMZ folders); returns the count."""
    fields = layer.fields()
    names = fields.names()
    name_index = name_field(names)
    description_index = next((i for i, n in enumerate(names) if n.lower() == "description"), None)
    folder_index = next((i for i, n in enumerate(names) if n.lower() == "folder"), None)
    remarks_index = next((names.index(n) for n in names if n.lower() in REMARKS_COLUMNS), None)
    data_indexes = [i for i, n in enumerate(names)
                    if i not in (name_index, description_index, remarks_index) and n.lower() not in SKIP_IN_DATA]
    to_wgs = QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance())
    request = QgsFeatureRequest()
    if only_selected:
        request.setFilterFids(layer.selectedFeatureIds())
    count, open_folders = 0, []
    colors = _Colors(layer)
    try:
        for feature in layer.getFeatures(request):
            if folder_index is not None:  # rebuild the folder tree of an imported KMZ
                wanted = [p for p in str(feature.attribute(folder_index) or "").split("/") if p]
                shared = 0
                while shared < min(len(wanted), len(open_folders)) and wanted[shared] == open_folders[shared]:
                    shared += 1
                for _ in range(len(open_folders) - shared):
                    body.write("</Folder>\n")
                for part in wanted[shared:]:
                    body.write(f"<Folder><name>{escape(part)}</name>\n")
                open_folders = wanted
            geometry = QgsGeometry(feature.geometry())
            kml_geometry = ""
            if not geometry.isNull():
                geometry.transform(to_wgs)
                kml_geometry = _geometry_kml(geometry)
            parts = ["<Placemark>"]
            if name_index is not None and str(feature.attribute(name_index) or "").strip() not in ("", "NULL"):
                parts.append(f"<name>{escape(str(feature.attribute(name_index)))}</name>")
            description = _text(feature, description_index)
            remarks = _text(feature, remarks_index)
            if remarks:  # the remarks are the balloon text, below any description of its own
                remarks_html = escape(remarks).replace("\n", "<br>")
                description = f"{description}<br><br><b>Remarks:</b> {remarks_html}" if description else remarks_html
            if description:
                parts.append(f"<description>{_cdata(description)}</description>")
            style = colors.style(feature)
            if style:
                style_id = styles.setdefault(style, f"s{len(styles) + 1}")
                parts.append(f"<styleUrl>#{style_id}</styleUrl>")
            data = []
            for i in data_indexes:
                value = feature.attribute(i)
                if value is not None and str(value) != "NULL":
                    data.append(f"<Data name={quoteattr(names[i])}><value>{escape(str(value))}</value></Data>")
            if data:
                parts.append("<ExtendedData>" + "".join(data) + "</ExtendedData>")
            parts.append(kml_geometry + "</Placemark>\n")
            body.write("".join(parts))
            count += 1
        body.write("</Folder>\n" * len(open_folders))
    finally:
        colors.close()
    return count


def export_kmz(layers, path, only_selected=False, title=None):
    """Write a standard single-file KMZ (doc.kml inside) for one or several layers.

    Several layers (e.g. a group with pins and lines) each become a folder named
    after the layer. Returns {layer id: placemarks written}.
    """
    layers = [l for l in _as_list(layers) if l.isSpatial()]
    title = title or (layers[0].name() if layers else "export")
    work = tempfile.mkdtemp(prefix="route_drawer_kmz_")
    body_path, kml_path = os.path.join(work, "body.kml"), os.path.join(work, "doc.kml")
    styles, counts = {}, {}
    try:
        with open(body_path, "w", encoding="utf-8") as body:
            for layer in layers:
                if len(layers) > 1:
                    body.write(f"<Folder><name>{escape(layer.name())}</name>\n")
                counts[layer.id()] = _placemarks(layer, body, styles, only_selected)
                if len(layers) > 1:
                    body.write("</Folder>\n")
        with open(kml_path, "w", encoding="utf-8") as kml:
            kml.write('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">\n'
                      f"<Document><name>{escape(title)}</name>\n")
            for (kind, color, width), style_id in styles.items():
                kml.write(_style_kml(style_id, kind, color, width))
            with open(body_path, encoding="utf-8") as body:
                shutil.copyfileobj(body, kml)
            kml.write("</Document>\n</kml>\n")
        partial = path + ".part"
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(kml_path, "doc.kml")
        os.replace(partial, path)  # only a complete file replaces an existing one
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return counts


# ----- Excel ---------------------------------------------------------------------
def _unique(fields, wanted):
    taken = {f.lower() for f in fields.names()}
    out = []
    for base in wanted:
        name, n = base, 2
        while name.lower() in taken:
            name, n = f"{base}_{n}", n + 1
        taken.add(name.lower())
        out.append(name)
    return out


def _excel_table(layer, only_selected):
    """An in-memory copy of the layer with coordinate / length / area columns added."""
    request = QgsFeatureRequest()
    if only_selected:
        request.setFilterFids(layer.selectedFeatureIds())
    table = layer.materialize(request)  # a copy in memory; the layer itself is untouched

    kind = layer.geometryType() if layer.isSpatial() else None
    wanted = {POINT: ["latitude", "longitude"],
              LINE: ["length_m", "start_lat", "start_lon", "end_lat", "end_lon"],
              POLYGON: ["area_m2", "centre_lat", "centre_lon"]}.get(kind, [])
    names = _unique(table.fields(), wanted)
    provider = table.dataProvider()
    provider.addAttributes([new_field(n, "Double") for n in names])
    table.updateFields()
    first = table.fields().count() - len(names)

    to_wgs = QgsCoordinateTransform(table.crs(), WGS84, QgsProject.instance())
    measure = QgsDistanceArea()
    measure.setSourceCrs(table.crs(), QgsProject.instance().transformContext())
    measure.setEllipsoid("WGS84")
    text_fields = [i for i in range(first) if not table.fields().at(i).isNumeric()]

    changes = {}
    for feature in table.getFeatures():
        values = {}
        for i in text_fields:  # Excel refuses very long cells (e.g. JSON)
            value = feature.attribute(i)
            if isinstance(value, str) and len(value) > EXCEL_CELL_LIMIT:
                values[i] = value[:EXCEL_CELL_LIMIT] + "…"
        geometry = feature.geometry()
        if not geometry.isNull() and wanted:
            wgs = QgsGeometry(geometry)
            wgs.transform(to_wgs)
            if kind == POINT:
                p = wgs.asPoint() if not wgs.isMultipart() else wgs.asMultiPoint()[0]
                numbers = [p.y(), p.x()]
            elif kind == LINE:
                vertices = list(wgs.vertices())
                numbers = [measure.measureLength(geometry), vertices[0].y(), vertices[0].x(),
                           vertices[-1].y(), vertices[-1].x()]
            else:
                c = wgs.pointOnSurface().asPoint()
                numbers = [measure.measureArea(geometry), c.y(), c.x()]
            for offset, number in enumerate(numbers):
                values[first + offset] = round(number, 7 if offset or kind == POINT else 2)
        if values:
            changes[feature.id()] = values
    provider.changeAttributeValues(changes)
    return table, names


def sheet_names(layers):
    """Unique Excel sheet names: at most 31 characters, none of []:*?/\\."""
    out, taken = [], set()
    for layer in layers:
        base = re.sub(r"[\[\]:*?/\\]", "_", layer.name()).strip()[:31] or "Sheet"
        name, n = base, 2
        while name.lower() in taken:
            suffix = f" ({n})"
            name, n = base[:31 - len(suffix)] + suffix, n + 1
        taken.add(name.lower())
        out.append(name)
    return out


def export_xlsx(layers, path, only_selected=False):
    """One workbook, one sheet per layer. Returns [(sheet, rows, added columns)]."""
    layers = _as_list(layers)
    partial = os.path.splitext(path)[0] + ".partial.xlsx"
    if os.path.exists(partial):
        os.remove(partial)
    written = []
    try:
        for i, (layer, sheet) in enumerate(zip(layers, sheet_names(layers))):
            table, added = _excel_table(layer, only_selected)
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "XLSX"
            options.fileEncoding = "UTF-8"
            options.layerName = sheet
            options.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile if i == 0
                                            else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer)
            _write(table, partial, options)
            written.append((sheet, table.featureCount(), added))
        os.replace(partial, path)  # only a complete workbook replaces an existing one
    finally:
        if os.path.exists(partial):
            os.remove(partial)
    return written
