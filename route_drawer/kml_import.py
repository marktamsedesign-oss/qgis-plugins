"""Import a KMZ/KML into a GeoPackage, keeping every ExtendedData field.

The KML is read piece by piece (iterparse), so even a KMZ that unpacks to
hundreds of megabytes needs little memory, unlike opening it directly in
QGIS. Pass 1 learns the fields and styles; pass 2 writes all features.

Output layers (only those with features): points, lines, polygons. Fields:
Name, description, folder (the KML folder path), kml_color (#rrggbb from the
KML style) and then every ExtendedData name (Data and SchemaData), as text.
"""
import os
import warnings
import zipfile
import xml.etree.ElementTree as ET
from xml.parsers import expat

from osgeo import ogr, osr

# GDAL < 4 warns once that its error mode will change; this module checks results itself
warnings.filterwarnings("ignore", message=r"Neither (gdal|ogr|osr)\.UseExceptions", category=FutureWarning)

KINDS = {"points": ogr.wkbMultiPoint, "lines": ogr.wkbMultiLineString,
         "polygons": ogr.wkbMultiPolygon}
BASE_FIELDS = ("Name", "description", "folder", "kml_color")
STYLE_KEYS = {"points": ("IconStyle", "LabelStyle"), "lines": ("LineStyle",),
              "polygons": ("PolyStyle", "LineStyle")}


class Cancelled(Exception):
    pass


def _local(tag):
    """Tag name without namespace or prefix: '{...kml/2.2}Placemark' / 'gx:Track' -> 'Track'."""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1] if isinstance(tag, str) else ""


def _iterparse(stream, chunk=1 << 20):
    """Like ET.iterparse(events=("start", "end")), but without namespace checks.

    KML saved by Google Earth Pro can use prefixes it never declares (e.g.
    <ns0:link>); Google Earth accepts that, a strict XML parser does not.
    """
    builder = ET.TreeBuilder()
    events = []
    parser = expat.ParserCreate()  # no namespace processing: prefixes stay part of the name
    parser.buffer_text = True
    parser.StartElementHandler = lambda tag, attrs: events.append(("start", builder.start(tag, attrs)))
    parser.EndElementHandler = lambda tag: events.append(("end", builder.end(tag)))
    parser.CharacterDataHandler = builder.data
    try:
        while True:
            data = stream.read(chunk)
            parser.Parse(data, not data)
            yield from events
            events.clear()
            if not data:
                return
    except expat.ExpatError as error:
        raise ValueError(f"the KML could not be read ({error})") from None


def _child(elem, name):
    return next((c for c in elem if _local(c.tag) == name), None)


def _text(elem):
    return (elem.text or "").strip() if elem is not None else ""


class _Source:
    """The KML files of a KMZ (doc.kml first, then any others it links to, e.g.
    layers/*.kml) or a plain .kml, read one after another, counting bytes for progress."""

    def __init__(self, path):
        self.path = path
        self.zip = zipfile.ZipFile(path) if zipfile.is_zipfile(path) else None
        if self.zip:
            infos = [i for i in self.zip.infolist() if i.filename.lower().endswith(".kml")]
            if not infos:
                self.zip.close()
                raise ValueError("there is no KML file inside this KMZ")
            infos.sort(key=lambda i: (i.filename != "doc.kml", i.filename.count("/"), i.filename))
            self.members = [(i.filename, i.file_size) for i in infos]
        else:
            self.members = [(path, os.path.getsize(path))]
        self.size = max(sum(size for _, size in self.members), 1)
        self.done = 0
        self.raw = None

    def streams(self):
        for name, _ in self.members:
            self.raw = self.zip.open(name) if self.zip else open(name, "rb")
            try:
                yield self
            finally:
                self.raw.close()

    def read(self, n=-1):
        data = self.raw.read(n)
        self.done += len(data)
        return data

    def close(self):
        if self.zip:
            self.zip.close()


def _kml_color(text):
    """KML aabbggrr -> #rrggbb."""
    t = (text or "").strip().lstrip("#")
    if len(t) == 8:
        return f"#{t[6:8]}{t[4:6]}{t[2:4]}".lower()
    if len(t) == 6:
        return f"#{t[4:6]}{t[2:4]}{t[0:2]}".lower()
    return None


def _style_colors(style):
    colors = {}
    for kind, keys in STYLE_KEYS.items():
        for key in keys:
            sub = _child(style, key)
            color = _kml_color(_text(_child(sub, "color"))) if sub is not None else None
            if color:
                colors[kind] = color
                break
    return colors


def _coords(text):
    """KML 'lon,lat[,alt] ...' -> ['lon lat', ...] (WKT order), invalid tuples dropped."""
    out = []
    for token in (text or "").split():
        parts = token.split(",")
        if len(parts) >= 2:
            try:
                lon, lat = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            out.append(f"{lon!r} {lat!r}")
    return out


def _geometries(elem):
    """[(kind, wkt part)] for a geometry element (MultiGeometry is flattened)."""
    tag = _local(elem.tag)
    if tag == "Point":
        c = _coords(_text(_child(elem, "coordinates")))
        return [("points", f"({c[0]})")] if c else []
    if tag in ("LineString", "LinearRing"):
        c = _coords(_text(_child(elem, "coordinates")))
        return [("lines", f"({', '.join(c)})")] if len(c) >= 2 else []
    if tag == "Polygon":
        rings = []
        for boundary in elem:
            if _local(boundary.tag) in ("outerBoundaryIs", "innerBoundaryIs"):
                ring = _child(boundary, "LinearRing")
                c = _coords(_text(_child(ring, "coordinates"))) if ring is not None else []
                if len(c) >= 3:
                    if c[0] != c[-1]:
                        c.append(c[0])
                    if _local(boundary.tag) == "outerBoundaryIs":
                        rings.insert(0, c)
                    elif rings:
                        rings.append(c)
        return [("polygons", "(" + ", ".join(f"({', '.join(r)})" for r in rings) + ")")] if rings else []
    if tag == "Track":  # gx:Track: <gx:coord>lon lat alt</gx:coord>
        c = []
        for coord in elem:
            if _local(coord.tag) == "coord":
                parts = _text(coord).split()
                if len(parts) >= 2:
                    c.append(f"{parts[0]} {parts[1]}")
        return [("lines", f"({', '.join(c)})")] if len(c) >= 2 else []
    if tag in ("MultiGeometry", "MultiTrack"):
        out = []
        for part in elem:
            out.extend(_geometries(part))
        return out
    return []


def _placemark(elem):
    base, extended, parts, style_url, inline = {}, {}, [], None, {}
    for child in elem:
        tag = _local(child.tag)
        if tag == "name":
            base["Name"] = _text(child)
        elif tag == "description":
            base["description"] = _text(child)
        elif tag == "styleUrl":
            style_url = _text(child).rsplit("#", 1)[-1]
        elif tag == "Style":
            inline = _style_colors(child)
        elif tag == "ExtendedData":
            for item in child.iter():
                kind = _local(item.tag)
                if kind == "Data" and item.get("name"):
                    extended[item.get("name")] = _text(_child(item, "value"))
                elif kind == "SimpleData" and item.get("name"):
                    extended[item.get("name")] = (item.text or "").strip()
        else:
            parts.extend(_geometries(child))
    return base, extended, parts, style_url, inline


def _walk(path, on_progress, styles=None, style_maps=None):
    """Yield (folder path, base, extended, parts, style_url, inline colors) per placemark."""
    source = _Source(path)
    try:
        if on_progress and on_progress(0.0) is False:
            raise Cancelled()
        seen = 0
        for stream in source.streams():
            for placemark in _walk_file(stream, styles, style_maps):
                yield placemark
                seen += 1
                if on_progress and seen % 500 == 0 and on_progress(source.done / source.size) is False:
                    raise Cancelled()
    finally:
        source.close()


def _walk_file(stream, styles, style_maps):
    stack, folders = [], []
    for event, elem in _iterparse(stream):
        tag = _local(elem.tag)
        if event == "start":
            stack.append(tag)
            if tag in ("Folder", "Document"):
                folders.append("")
            continue
        stack.pop()
        parent = stack[-1] if stack else ""
        if tag == "name" and parent in ("Folder", "Document") and folders and not folders[-1]:
            folders[-1] = _text(elem)
        elif tag == "Placemark":
            yield ("/".join(f for f in folders if f),) + _placemark(elem)
            elem.clear()
        elif tag == "Style" and parent != "Placemark" and styles is not None and elem.get("id"):
            styles[elem.get("id")] = _style_colors(elem)
            elem.clear()
        elif tag == "StyleMap" and style_maps is not None and elem.get("id"):
            for pair in elem:
                if _local(pair.tag) == "Pair" and _text(_child(pair, "key")) == "normal":
                    style_maps[elem.get("id")] = _text(_child(pair, "styleUrl")).rsplit("#", 1)[-1]
            elem.clear()
        elif tag in ("Folder", "Document"):
            folders.pop()
            elem.clear()


def import_kml(path, out_path, on_progress=None):
    """Convert path (.kmz/.kml) to out_path (.gpkg); returns {kind: feature count}.

    on_progress(fraction 0..1) is called now and then; return False to cancel.
    """
    step = (lambda base: (lambda f: on_progress(base + f / 2))) if on_progress else (lambda base: None)

    # pass 1: fields per kind, counts, styles
    styles, style_maps = {}, {}
    fields = {kind: list(BASE_FIELDS) for kind in KINDS}
    # names already taken; fid and geom are the GeoPackage's own columns
    lower = {kind: {**{f.lower(): f for f in BASE_FIELDS}, "fid": "fid", "geom": "geom"}
             for kind in KINDS}
    renames = {kind: {} for kind in KINDS}
    counts = dict.fromkeys(KINDS, 0)
    for _, base, extended, parts, _, _ in _walk(path, step(0.0), styles, style_maps):
        kinds = {kind for kind, _ in parts}
        for kind in kinds:
            counts[kind] += 1
            for key in extended:
                if key in renames[kind]:
                    continue
                name, n = key, 2
                while name.lower() in lower[kind]:
                    name, n = f"{key}_{n}", n + 1
                renames[kind][key] = name
                lower[kind][name.lower()] = name
                fields[kind].append(name)

    # pass 2: write every kind through one GeoPackage connection
    present = [kind for kind in KINDS if counts[kind]]
    if not present:
        return counts
    driver = ogr.GetDriverByName("GPKG")
    dataset = driver.CreateDataSource(out_path)
    if dataset is None:
        raise OSError(f"could not create {out_path}")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    if hasattr(srs, "SetAxisMappingStrategy"):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    layers, index = {}, {}
    for kind in present:
        layer = dataset.CreateLayer(kind, srs, KINDS[kind], options=["GEOMETRY_NAME=geom"])
        for name in fields[kind]:
            layer.CreateField(ogr.FieldDefn(name, ogr.OFTString))
        layers[kind] = layer
        defn = layer.GetLayerDefn()
        index[kind] = {defn.GetFieldDefn(i).GetName(): i for i in range(defn.GetFieldCount())}

    wkt_type = {"points": "MULTIPOINT", "lines": "MULTILINESTRING", "polygons": "MULTIPOLYGON"}
    written = dict.fromkeys(KINDS, 0)
    dataset.StartTransaction()
    try:
        for folder, base, extended, parts, style_url, inline in _walk(path, step(0.5)):
            style = styles.get(style_maps.get(style_url, style_url), {})
            by_kind = {}
            for kind, part in parts:
                by_kind.setdefault(kind, []).append(part)
            for kind, kind_parts in by_kind.items():
                layer = layers[kind]
                feature = ogr.Feature(layer.GetLayerDefn())
                values = dict(base, folder=folder, kml_color=inline.get(kind) or style.get(kind))
                for key, value in extended.items():
                    values[renames[kind][key]] = value
                for name, value in values.items():
                    if value:
                        feature.SetField(index[kind][name], value)
                geometry = ogr.CreateGeometryFromWkt(f"{wkt_type[kind]}({', '.join(kind_parts)})")
                if geometry is not None:
                    feature.SetGeometry(geometry)
                layer.CreateFeature(feature)
                written[kind] += 1
                if sum(written.values()) % 20000 == 0:
                    dataset.CommitTransaction()
                    dataset.StartTransaction()
        dataset.CommitTransaction()
    except BaseException:
        dataset.RollbackTransaction()
        dataset = None
        if os.path.exists(out_path):
            os.remove(out_path)
        raise
    dataset = None  # closes the file
    return written
