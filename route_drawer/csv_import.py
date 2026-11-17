"""Import a CSV as a point layer ("pins"), finding its latitude/longitude columns.

The CSV is only read. Its rows are copied into a GeoPackage in the import
folder, so the layer is fast, editable and keeps working if the CSV moves.
"""
import csv
import io
import os

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsFeature, QgsFields,
                       QgsGeometry, QgsPointXY, QgsProject, QgsVectorFileWriter, QgsWkbTypes)

from .latlon import detect_columns, row_lat_lon
from .storage import new_field

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
try:  # QGIS >= 3.30 and QGIS 4
    POINT_TYPE = Qgis.WkbType.Point
except AttributeError:
    POINT_TYPE = QgsWkbTypes.Point


def read_csv(path):
    """(headers, rows, encoding, delimiter). Excel/Windows encodings are handled."""
    with open(path, "rb") as handle:
        raw = handle.read()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in row)]
    if not rows:
        return [], [], encoding, dialect.delimiter
    return unique_headers(rows[0]), rows[1:], encoding, dialect.delimiter


def unique_headers(raw):
    out, seen = [], set()
    for i, header in enumerate(raw):
        name = header.strip() or f"column_{i + 1}"
        base, n = name, 2
        while name.lower() in seen:
            name, n = f"{base}_{n}", n + 1
        seen.add(name.lower())
        out.append(name)
    return out


def guess(headers, rows):
    return detect_columns(headers, rows)


def write_pins(path, layer_name, headers, rows, how):
    """Write the rows with valid coordinates; returns (written, skipped data-row numbers)."""
    kind, a, b, _ = how
    fields = QgsFields()
    numeric = {a, b} if kind == "pair" else set()
    for i, header in enumerate(headers):
        fields.append(new_field(header, "Double" if i in numeric else "String"))
    extra = []
    if kind == "single":  # keep the original text column and add numeric lat/long
        taken = {h.lower() for h in headers}
        for base in ("latitude", "longitude"):
            name, n = base, 2
            while name.lower() in taken:
                name, n = f"{base}_{n}", n + 1
            taken.add(name.lower())
            extra.append(name)
            fields.append(new_field(name, "Double"))

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = layer_name
    options.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
    writer = QgsVectorFileWriter.create(path, fields, POINT_TYPE, WGS84,
                                        QgsProject.instance().transformContext(), options)
    if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
        message = writer.errorMessage()
        del writer
        raise OSError(message)
    written, skipped = 0, []
    for number, row in enumerate(rows, start=1):
        coordinate = row_lat_lon(row, how)
        if coordinate is None:
            skipped.append(number)
            continue
        lat, lon = coordinate
        values = []
        for i in range(len(headers)):
            cell = row[i].strip() if i < len(row) else ""
            if i in numeric:
                values.append(lat if i == a else lon)
            else:
                values.append(cell if cell else None)
        values.extend([lat, lon][:len(extra)])
        feature = QgsFeature(fields)
        feature.setAttributes(values)
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        writer.addFeature(feature)
        written += 1
    del writer
    return written, skipped


def unique_path(folder, stem, extension=".gpkg"):
    """folder/stem.gpkg, or 'stem (2).gpkg' ... so an earlier import is never overwritten."""
    os.makedirs(folder, exist_ok=True)
    # only what Windows or Mac file names cannot hold: "Region XI (Davao Region) + more" stays readable
    safe = "".join(c if c.isprintable() and c not in '\\/:*?"<>|' else "_" for c in stem).strip(" .") or "import"
    path, n = os.path.join(folder, safe + extension), 2
    while os.path.exists(path):
        path, n = os.path.join(folder, f"{safe} ({n}){extension}"), n + 1
    return path
