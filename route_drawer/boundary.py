"""Boundaries for "Pins in boundary": admin areas, any polygon layer, or ones you draw.

Admin areas come from the NAMRIA/PSA boundaries published on HDX (the main source:
phl_admin1..4 = region, province, town/city, barangay, read straight from the
downloaded zip, no unpacking needed) or, as a fallback, from OpenStreetMap's
gis_osm_adminareas_a_free_1 (one layer, the level is in its "code" column;
towns and barangays are incomplete there).

A pin on the boundary line counts as inside. The test uses a prepared geometry,
so even a province outline of 60,000 points checks thousands of pins at once.
"""
import os
import re
import zipfile
from datetime import datetime

from qgis.core import (Qgis, QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsFeatureRequest,
                       QgsFields, QgsGeometry, QgsMapLayerStyle, QgsMemoryProviderUtils,
                       QgsProject, QgsRectangle, QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes)

from .export import _write
from .map_tool import WGS84
from .storage import new_field

LEVELS = {1: "Region", 2: "Province", 3: "Town / City", 4: "Barangay"}
HDX, OSM = "NAMRIA/PSA (HDX)", "OpenStreetMap"
HDX_FILE = re.compile(r"(?:^|/)phl_admin([1-4])\.shp$", re.IGNORECASE)
OSM_FILE = "gis_osm_adminareas_a_free_1.shp"
OSM_CODES = {1: 1203, 2: 1204, 3: 1206, 4: 1210}  # OSM admin_level 3 / 4 / 6 / 10 in the Philippines
HDX_CONTEXT = {1: (), 2: ("adm1_name",), 3: ("adm2_name",), 4: ("adm3_name", "adm2_name")}
BOUNDARY_COLUMN = "boundary"
MY_TABLE = "my_boundaries"
MY_FIELDS = (("name", "String"), ("remarks", "String"), ("area_km2", "Double"), ("created", "String"))
try:  # QGIS >= 3.36 and QGIS 4
    NO_GEOMETRY = Qgis.FeatureRequestFlag.NoGeometry
except AttributeError:
    NO_GEOMETRY = QgsFeatureRequest.Flag.NoGeometry
try:  # QGIS >= 3.30 and QGIS 4 (multi: an outline that crosses itself becomes two parts)
    MULTIPOLYGON_TYPE = Qgis.WkbType.MultiPolygon
except AttributeError:
    MULTIPOLYGON_TYPE = QgsWkbTypes.MultiPolygon


def _text(feature, field):
    index = feature.fields().lookupField(field)
    value = feature.attribute(index) if index >= 0 else None
    return "" if value is None or str(value) in ("", "NULL") else str(value).strip()


def _natural(text):
    """'Barangay 2' before 'Barangay 10'."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text.casefold())]


class Source:
    """Where boundaries are picked from: one admin level, or a polygon layer and its name column."""

    def __init__(self, title, uri=None, layer=None, name_field="name", context_fields=(),
                 subset="", origin=""):
        self.title, self.uri, self.name_field = title, uri, name_field
        self.context_fields, self.subset, self.origin = tuple(context_fields), subset, origin
        self.parent = None  # a higher level, to tell apart places with the same name
        self._layer = layer
        self._entries = self._labels = None

    def layer(self):
        """The boundaries layer; admin files are opened without adding them to the map."""
        if self._layer is None:
            layer = QgsVectorLayer(self.uri, self.title, "ogr")
            if self.subset:
                layer.setSubsetString(self.subset)
            self._layer = layer
        return self._layer

    def label(self, feature):
        """'Bansalan, Davao del Sur': the name, then the province (or region...) it is in."""
        name = _text(feature, self.name_field) if self.name_field else ""
        parts = [name or f"#{feature.id()}"] + [t for t in (_text(feature, f) for f in self.context_fields) if t]
        return ", ".join(parts)

    def entries(self):
        """[(fid, label)] sorted by label; read once, without geometry (42,000 barangays: 0.2 s)."""
        if self._entries is None:
            layer = self.layer()
            wanted = [f for f in (self.name_field,) + self.context_fields
                      if f and layer.fields().lookupField(f) >= 0]
            request = QgsFeatureRequest().setFlags(NO_GEOMETRY).setSubsetOfAttributes(wanted, layer.fields())
            entries = [(f.id(), self.label(f)) for f in layer.getFeatures(request)] if layer.isValid() else []
            self._entries = sorted(self._tell_apart(entries), key=lambda e: _natural(e[1]))
            self._labels = dict(self._entries)
        return self._entries

    def label_of(self, fid):
        self.entries()
        return self._labels.get(fid, f"#{fid}")

    def refresh(self):
        self._entries = self._labels = None

    def _tell_apart(self, entries):
        """Places with the same label get the higher level they are in (e.g. the province);
        failing that, their feature number."""
        seen = {}
        for fid, label in entries:
            seen.setdefault(label, []).append(fid)
        twins = {fid for fids in seen.values() if len(fids) > 1 for fid in fids}
        if not twins:
            return entries
        within = {}
        if self.parent is not None:
            layer = self.layer()
            for feature in layer.getFeatures(QgsFeatureRequest().setFilterFids(list(twins)).setNoAttributes()):
                if feature.hasGeometry():
                    found = self.parent.at(feature.geometry().pointOnSurface().asPoint(), layer.crs())
                    if found:
                        within[feature.id()] = self.parent.label_of(found[0])
        return [(fid, f"{label}, {within[fid]}" if fid in within else f"{label} (#{fid})")
                if fid in twins else (fid, label) for fid, label in entries]

    def boundaries(self, fids):
        """[(label, geometry)] of the chosen features, in the layer's CRS."""
        request = QgsFeatureRequest().setFilterFids(list(fids))
        return [(self.label_of(f.id()), f.geometry()) for f in self.layer().getFeatures(request)
                if f.hasGeometry()]

    def at(self, point, crs):
        """fids of the boundaries at a point (given in crs)."""
        layer = self.layer()
        if crs != layer.crs():
            point = QgsCoordinateTransform(crs, layer.crs(), QgsProject.instance()).transform(point)
        probe = QgsGeometry.fromPointXY(point)
        tiny = 1e-9
        box = QgsRectangle(point.x() - tiny, point.y() - tiny, point.x() + tiny, point.y() + tiny)
        request = QgsFeatureRequest().setFilterRect(box).setNoAttributes()
        return [f.id() for f in layer.getFeatures(request) if f.hasGeometry() and f.geometry().intersects(probe)]


# ----- admin areas -------------------------------------------------------------------
def hdx_source(level, uri):
    return Source(LEVELS[level], uri=uri, name_field=f"adm{level}_name",
                  context_fields=HDX_CONTEXT[level], origin=HDX)


def osm_sources(uri):
    sources = {level: Source(LEVELS[level], uri=uri, name_field="name", subset=f'"code" = {code}', origin=OSM)
               for level, code in OSM_CODES.items()}
    sources[2].parent = sources[1]
    sources[3].parent = sources[4].parent = sources[2]  # same-named towns: told apart by province
    return sources


def _vsi(path):
    return path.replace("\\", "/")  # GDAL reads inside a zip with forward slashes, on Windows too


def find_admin(path):
    """{level: Source} for admin boundaries at path: an HDX zip or folder (or one of its
    phl_admin*.shp), or OSM's gis_osm_adminareas_a_free_1.shp; {} when it is neither."""
    if not path or not os.path.exists(path):
        return {}
    if os.path.isdir(path):
        files = [(n, os.path.join(path, n)) for n in sorted(os.listdir(path))]
    elif path.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(path) as archive:
                files = [(n, f"/vsizip/{_vsi(path)}/{n}") for n in archive.namelist()]
        except (OSError, zipfile.BadZipFile):
            return {}
    else:  # one file: look at the files beside it (phl_admin1..4 come together)
        folder = os.path.dirname(path)
        files = [(n, os.path.join(folder, n)) for n in sorted(os.listdir(folder))]
    hdx = {}
    for name, uri in files:
        found = HDX_FILE.search(name.replace("\\", "/"))
        if found:
            hdx[int(found.group(1))] = hdx_source(int(found.group(1)), uri)
    if hdx:
        return hdx
    osm = next((uri for name, uri in files if os.path.basename(name).lower() == OSM_FILE), None)
    return osm_sources(osm) if osm else {}


def file_of(source):
    """The file on disk behind a layer source (the zip, for a file read inside a zip)."""
    path = source.split("|")[0]
    if path.startswith("/vsizip/"):
        path = path[len("/vsizip/"):]
        cut = path.lower().find(".zip")
        if cut >= 0:
            path = path[:cut + 4]
    return path


def admin_levels(paths):
    """{level: Source}: NAMRIA/PSA (HDX) first, OpenStreetMap for any level it lacks, and
    {origin: path} of the files used. paths: candidate files, the remembered one first."""
    hdx = osm = None
    for path in paths:
        sources = find_admin(path) if path else {}
        origin = next(iter(sources.values())).origin if sources else None
        if origin == HDX and hdx is None:
            hdx = (path, sources)
        elif origin == OSM and osm is None:
            osm = (path, sources)
    levels, used = {}, {}
    for origin, found in ((OSM, osm), (HDX, hdx)):
        if found:
            levels.update(found[1])
            used[origin] = found[0]
    return levels, used


# ----- pins inside -------------------------------------------------------------------
def pins_inside(layer, boundaries, crs):
    """{fid: boundary label} for the layer's features inside the boundaries ([(label, geometry)]
    in crs); a pin in two boundaries is named after the first."""
    to_layer = (QgsCoordinateTransform(crs, layer.crs(), QgsProject.instance())
                if layer.crs().isValid() and crs != layer.crs() else None)
    found = {}
    for label, geometry in boundaries:
        geometry = QgsGeometry(geometry)
        if to_layer is not None:
            geometry.transform(to_layer)
        engine = QgsGeometry.createGeometryEngine(geometry.constGet())
        engine.prepareGeometry()
        request = QgsFeatureRequest().setFilterRect(geometry.boundingBox()).setNoAttributes()
        for feature in layer.getFeatures(request):
            if (feature.id() not in found and feature.hasGeometry()
                    and engine.intersects(feature.geometry().constGet())):
                found[feature.id()] = label
    return found


def title_of(labels):
    """'Davao del Sur', 'Bansalan + Hagonoy', 'Bansalan + 4 more' (names without their province)."""
    names = list(dict.fromkeys(label.split(", ")[0] for label in labels))
    if len(names) <= 2:
        return " + ".join(names)
    return f"{names[0]} + {len(names) - 1} more"


def copies_inside(found):
    """Memory copies of the pins found ([(layer, {fid: boundary label})]), styled like their
    layers, with a "boundary" column naming the boundary each pin is in."""
    copies = []
    for layer, labels in found:
        fields = QgsFields(layer.fields())
        column, n = BOUNDARY_COLUMN, 2
        while fields.lookupField(column) >= 0:
            column, n = f"{BOUNDARY_COLUMN}_{n}", n + 1
        fields.append(new_field(column, "String"))
        copy = QgsMemoryProviderUtils.createMemoryLayer(layer.name(), fields, layer.wkbType(), layer.crs())
        features = []
        for feature in layer.getFeatures(QgsFeatureRequest().setFilterFids(list(labels))):
            out = QgsFeature(fields)
            out.setGeometry(feature.geometry())
            out.setAttributes(list(feature.attributes()) + [labels[feature.id()]])
            features.append(out)
        copy.dataProvider().addFeatures(features)
        copy_style(layer, copy)
        copies.append(copy)
    return copies


def copy_style(source, target):
    style = QgsMapLayerStyle()
    style.readFromLayer(source)
    style.writeToLayer(target)


def write_gpkg(copies, path):
    """Each copy as a table of one new GeoPackage; returns the table names."""
    tables = []
    for copy in copies:
        stem = re.sub(r"\W+", "_", copy.name()).strip("_").lower()[:40] or "pins"
        table, n = f"{stem}_inside", 2
        while table in tables:
            table, n = f"{stem}_inside_{n}", n + 1
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = table
        options.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer if tables
                                        else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
        _write(copy, path, options)
        tables.append(table)
    return tables


# ----- your own boundaries --------------------------------------------------------------
def open_my_boundaries(path, create=False):
    """The "My boundaries" layer of the file (not added to the map); None if there is none
    and create is False. Never overwrites an existing file."""
    uri = f"{path}|layername={MY_TABLE}"
    layer = QgsVectorLayer(uri, "My boundaries", "ogr")
    if layer.isValid() or not create:
        return layer if layer.isValid() else None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = QgsFields()
    for name, kind in MY_FIELDS:
        fields.append(new_field(name, kind))
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = MY_TABLE
    options.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                    if os.path.exists(path)
                                    else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
    writer = QgsVectorFileWriter.create(path, fields, MULTIPOLYGON_TYPE, WGS84,
                                        QgsProject.instance().transformContext(), options)
    failed = writer.hasError() != QgsVectorFileWriter.WriterError.NoError
    message = writer.errorMessage()
    del writer
    if failed:
        raise OSError(message or f"could not create {path}")
    layer = QgsVectorLayer(uri, "My boundaries", "ogr")
    return layer if layer.isValid() else None


def boundary_polygon(points):
    """The closed outline of clicked points (WGS84), made valid if it crosses itself."""
    geometry = QgsGeometry.fromPolygonXY([list(points) + [points[0]]])
    return geometry if geometry.isGeosValid() else geometry.makeValid()


def save_my_boundary(layer, points, name, remarks=None):
    """Store a drawn boundary (WGS84 points); returns its feature id, or None."""
    geometry = boundary_polygon(points)
    area = QgsDistanceArea()
    area.setSourceCrs(WGS84, QgsProject.instance().transformContext())
    area.setEllipsoid("WGS84")
    km2 = round(area.measureArea(geometry) / 1e6, 3)
    geometry.transform(QgsCoordinateTransform(WGS84, layer.crs(), QgsProject.instance()))
    if QgsWkbTypes.isMultiType(layer.wkbType()):
        geometry.convertToMultiType()
    feature = QgsFeature(layer.fields())
    feature.setGeometry(geometry)
    for key, value in (("name", name), ("remarks", remarks or None), ("area_km2", km2),
                       ("created", datetime.now().strftime("%Y-%m-%d %H:%M"))):
        index = layer.fields().lookupField(key)
        if index >= 0 and value is not None:
            feature.setAttribute(index, value)
    if layer.isEditable():
        ok = layer.addFeature(feature)
        return feature.id() if ok else None
    ok, added = layer.dataProvider().addFeatures([feature])
    layer.updateExtents()
    layer.triggerRepaint()
    return added[0].id() if ok and added else None
