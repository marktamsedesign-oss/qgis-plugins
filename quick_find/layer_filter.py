"""Filter layers panel: show only the features with a chosen value in a chosen column.

For example AN_Loop_Name = MIN_R0R5_ANL79LTE on health_loop_links, and the same
on every other layer with an AN_Loop_Name column (health_loop_sites). It sets the
layer's own filter (as Layer > Filter… does), so the map, the attribute table and
exports all see the same features. Clear puts back each layer's earlier filter
and style exactly.
"""
import re
from functools import partial

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QCompleter, QDockWidget, QFormLayout,
                                 QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton,
                                 QVBoxLayout, QWidget)
from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsCoordinateTransform, QgsExpression,
                       QgsFeatureRequest,
                       QgsPalLayerSettings, QgsProject, QgsRectangle, QgsRendererCategory,
                       QgsSettings, QgsSymbol, QgsTextBufferSettings, QgsTextFormat,
                       QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsWkbTypes)
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox

from .remember import RememberedLayer
from .search import NO_GEOMETRY

try:  # QGIS >= 3.34 and QGIS 4
    VECTOR_LAYERS = Qgis.LayerFilter.VectorLayer
except AttributeError:
    from qgis.core import QgsMapLayerProxyModel
    VECTOR_LAYERS = QgsMapLayerProxyModel.VectorLayer
try:  # QGIS >= 3.30 and QGIS 4
    LINE, POINT = Qgis.GeometryType.Line, Qgis.GeometryType.Point
except AttributeError:
    LINE, POINT = QgsWkbTypes.LineGeometry, QgsWkbTypes.PointGeometry

MAX_VALUES = 20000      # values offered in the list
MAX_COLOURS = 30        # "Colour by" needs a column with at most this many values
PALETTE = ("#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6",
           "#9a6324", "#469990", "#808000", "#000075", "#e6beff", "#aaffc3", "#ffd8b1",
           "#800000", "#a9a9a9", "#bfef45", "#fabed4", "#dcbeff", "#ffe119")
KEY = "quick_find/filter_"


def natural_key(value):
    """MIN_R0R5_ANL9 before MIN_R0R5_ANL79; numbers by size."""
    if isinstance(value, (int, float)):
        return (0, value, [])
    parts = re.split(r"(\d+)", str(value).casefold())
    return (1, 0, [int(p) if p.isdigit() else p for p in parts])


def count_features(layer):
    """Features shown with the current filter. QGIS's quick count can return -1
    ("unknown") on a filtered GeoPackage layer that shares its file with other
    layers, so then they are counted one by one."""
    count = layer.featureCount()
    if count >= 0:
        return count
    request = QgsFeatureRequest().setNoAttributes().setFlags(NO_GEOMETRY)
    return sum(1 for _ in layer.getFeatures(request))


def features_extent(layer):
    """Bounding box of the features shown (layer CRS), or None; read from the
    features themselves, as the layer's cached extent may be for the whole layer."""
    extent = None
    for feature in layer.getFeatures(QgsFeatureRequest().setNoAttributes()):
        if feature.hasGeometry():
            box = feature.geometry().boundingBox()
            if extent is None:
                extent = QgsRectangle(box)
            else:
                extent.combineExtentWith(box)
    return extent


def filter_sql(layer, field_name, value, contains=False):
    """The layer filter for one value, in the syntax the layer's data source understands."""
    field = layer.fields().at(layer.fields().lookupField(field_name))
    column = QgsExpression.quotedColumnRef(field.name())
    if str(value).strip() == "NULL":
        return f"{column} IS NULL"
    if field.isNumeric():
        number = float(str(value).replace(",", "."))  # ValueError for text: reported by the caller
        return f"{column} = {int(number) if number.is_integer() else repr(number)}"
    if contains:
        # GeoPackage/shapefile SQL: LIKE ignores case; QGIS expressions/PostGIS need ILIKE
        operator = "LIKE" if layer.providerType() in ("ogr", "spatialite") else "ILIKE"
        return f"{column} {operator} {QgsExpression.quotedString('%' + str(value) + '%')}"
    return f"{column} = {QgsExpression.quotedString(str(value))}"


class LayerFilters:
    """Filters set by the panel, remembered so Clear restores exactly what was there."""

    def __init__(self):
        self.applied = {}  # layer id -> (the layer's own filter, the full filter we set)

    def apply(self, layer, sql):
        """Set the filter (combined with the layer's own one); False if the layer refuses."""
        own, ours = self.applied.get(layer.id(), (layer.subsetString(), None))
        if ours is not None and layer.subsetString() != ours:
            own = layer.subsetString()  # changed by hand since: that is now the layer's own
        combined = f"({own}) AND ({sql})" if own else sql
        if not layer.setSubsetString(combined):
            return False
        self.applied[layer.id()] = (own, combined)
        return True

    def own_filter(self, layer):
        own, ours = self.applied.get(layer.id(), (layer.subsetString(), None))
        return own if ours is not None and layer.subsetString() == ours else layer.subsetString()

    def is_filtered(self, layer):
        entry = self.applied.get(layer.id())
        return entry is not None and layer.subsetString() == entry[1]

    def clear(self):
        """Put back each layer's own filter; a filter changed by hand since is left alone."""
        restored = []
        for layer_id, (own, ours) in list(self.applied.items()):
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None and layer.subsetString() == ours and layer.setSubsetString(own):
                restored.append(layer)
        self.applied.clear()
        return restored


class LayerStyles:
    """Label / colour changes made by the panel, so Clear can undo them."""

    def __init__(self):
        self.saved = {}  # layer id -> (renderer, labeling, labels enabled)

    def _save(self, layer):
        if layer.id() not in self.saved:
            labeling = layer.labeling().clone() if layer.labeling() else None
            self.saved[layer.id()] = (layer.renderer().clone(), labeling, layer.labelsEnabled())

    def label(self, layer, field_name):
        self._save(layer)
        label = QgsPalLayerSettings()
        label.fieldName = field_name
        text = QgsTextFormat()
        text.setSize(9)
        buffer = QgsTextBufferSettings()
        buffer.setEnabled(True)
        buffer.setSize(1)
        buffer.setColor(QColor(255, 255, 255))
        text.setBuffer(buffer)
        label.setFormat(text)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(label))
        layer.setLabelsEnabled(True)
        layer.triggerRepaint()

    def colour(self, layer, field_name, values):
        """Categorized colours; returns False when the column has too many values."""
        if len(values) > MAX_COLOURS:
            return False
        self._save(layer)
        categories = []
        for i, value in enumerate(values):
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol.setColor(QColor(PALETTE[i % len(PALETTE)]))
            if layer.geometryType() == LINE:
                symbol.setWidth(0.9)
            elif layer.geometryType() == POINT:
                symbol.setSize(3.2)
            categories.append(QgsRendererCategory(value, symbol, str(value)))
        layer.setRenderer(QgsCategorizedSymbolRenderer(field_name, categories))
        layer.triggerRepaint()
        return True

    def restore(self):
        for layer_id, (renderer, labeling, enabled) in self.saved.items():
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is None:
                continue
            layer.setRenderer(renderer)
            if labeling is not None:
                layer.setLabeling(labeling)
            layer.setLabelsEnabled(enabled)
            layer.triggerRepaint()
        self.saved.clear()


class FilterPanel(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Filter layers", parent)
        self.setObjectName("QuickFindFilterPanel")
        self.iface = iface
        self.settings = QgsSettings()
        self.memory = RememberedLayer(KEY + "layer")
        self.filters = LayerFilters()
        self.styles = LayerStyles()
        self.values = []

        body = QWidget()
        outer = QVBoxLayout(body)
        form = QFormLayout()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(VECTOR_LAYERS)
        self.field_combo = QgsFieldComboBox()
        self.value_combo = QComboBox()
        self.value_combo.setEditable(True)
        self.value_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.value_combo.setMaxVisibleItems(20)
        self.value_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.value_combo.lineEdit().setPlaceholderText("type or pick a value")
        self.previous_button = QToolButton()
        self.previous_button.setText("◀")
        self.previous_button.setToolTip("Previous value")
        self.next_button = QToolButton()
        self.next_button.setText("▶")
        self.next_button.setToolTip("Next value")
        value_row = QHBoxLayout()
        value_row.setContentsMargins(0, 0, 0, 0)
        value_row.addWidget(self.previous_button)
        value_row.addWidget(self.value_combo, 1)
        value_row.addWidget(self.next_button)
        self.match_combo = QComboBox()
        self.match_combo.addItems(["is exactly", "contains"])
        form.addRow("Layer", self.layer_combo)
        form.addRow("Column", self.field_combo)
        form.addRow("Value", value_row)
        form.addRow("Match", self.match_combo)
        outer.addLayout(form)

        self.linked_check = QCheckBox("Also filter other layers with this column")
        self.linked_label = QLabel()
        self.linked_label.setWordWrap(True)
        self.linked_label.setStyleSheet("color: #555; margin-left: 20px;")
        self.zoom_check = QCheckBox("Zoom to the result")
        outer.addWidget(self.linked_check)
        outer.addWidget(self.linked_label)
        outer.addWidget(self.zoom_check)

        style_form = QFormLayout()
        self.label_combo = QgsFieldComboBox()
        self.label_combo.setAllowEmptyFieldName(True)
        self.colour_combo = QgsFieldComboBox()
        self.colour_combo.setAllowEmptyFieldName(True)
        style_form.addRow("Label by", self.label_combo)
        style_form.addRow("Colour by", self.colour_combo)
        outer.addLayout(style_form)

        buttons = QHBoxLayout()
        self.apply_button = QPushButton("Apply")
        self.apply_button.setDefault(True)
        self.clear_button = QPushButton("Clear")
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.clear_button)
        outer.addLayout(buttons)
        self.status = QLabel("Pick a layer, a column and a value.")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        outer.addStretch(1)
        self.setWidget(body)

        self._restore_options()
        self.layer_combo.layerChanged.connect(self._layer_changed)
        self.field_combo.fieldChanged.connect(self._field_changed)
        self.value_combo.lineEdit().returnPressed.connect(self.apply)
        self.value_combo.activated.connect(lambda *_: self.apply())
        self.previous_button.clicked.connect(partial(self.step, -1))
        self.next_button.clicked.connect(partial(self.step, 1))
        self.apply_button.clicked.connect(self.apply)
        self.clear_button.clicked.connect(self.clear)
        for check, key in ((self.linked_check, "linked"), (self.zoom_check, "zoom")):
            check.toggled.connect(partial(self._save_option, key))
        self.match_combo.currentIndexChanged.connect(lambda i: self._save_option("contains", i == 1))
        self.linked_check.toggled.connect(lambda *_: self._show_linked())
        self._project_changed = lambda *_: self._show_linked()
        QgsProject.instance().layersAdded.connect(self._project_changed)
        QgsProject.instance().layersRemoved.connect(self._project_changed)
        self._layer_changed(self.layer_combo.currentLayer())

    def cleanup(self):
        """Stop listening to the project (the panel is about to be deleted)."""
        QgsProject.instance().layersAdded.disconnect(self._project_changed)
        QgsProject.instance().layersRemoved.disconnect(self._project_changed)

    # ----- options remembered between sessions -------------------------------
    def _save_option(self, key, value, *_):
        self.settings.setValue(KEY + key, value)

    def _option(self, key, default):
        return self.settings.value(KEY + key, default, type=bool)

    def _restore_options(self):
        self.linked_check.setChecked(self._option("linked", True))
        self.zoom_check.setChecked(self._option("zoom", True))
        self.match_combo.setCurrentIndex(1 if self._option("contains", False) else 0)
        layers = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
        remembered = self.memory.find(layers)
        if remembered is not None:
            self.layer_combo.setLayer(remembered)
        elif isinstance(self.iface.activeLayer(), QgsVectorLayer):
            self.layer_combo.setLayer(self.iface.activeLayer())

    # ----- layer / column / values -----------------------------------------------
    def layer(self):
        layer = self.layer_combo.currentLayer()
        return layer if isinstance(layer, QgsVectorLayer) and layer.isValid() else None

    def _layer_changed(self, layer):
        for combo in (self.field_combo, self.label_combo, self.colour_combo):
            combo.setLayer(layer)
        if layer is None:
            self._field_changed("")
            return
        names = layer.fields().names()
        wanted = self.settings.value(KEY + "column", "")
        if wanted in names:
            self.field_combo.setField(wanted)
        for combo, key in ((self.label_combo, "label"), (self.colour_combo, "colour")):
            remembered = self.settings.value(KEY + key, "")
            combo.setField(remembered if remembered in names else "")
        self._field_changed(self.field_combo.currentField())

    def _field_changed(self, field_name):
        layer = self.layer()
        numeric = False
        if layer is not None and field_name:
            self.settings.setValue(KEY + "column", field_name)
            numeric = layer.fields().at(layer.fields().lookupField(field_name)).isNumeric()
        self.match_combo.setEnabled(not numeric)  # "contains" is for text columns
        self._load_values()
        self._show_linked()

    def all_values(self, layer, field_name, limit=MAX_VALUES):
        """The column's values over the whole layer, also while the panel's filter is on."""
        index = layer.fields().lookupField(field_name)
        current = layer.subsetString()
        filtered = self.filters.is_filtered(layer)
        if filtered:  # read without our filter, then put it back
            layer.setSubsetString(self.filters.own_filter(layer))
        try:
            raw = layer.uniqueValues(index, limit)
        finally:
            if filtered:
                layer.setSubsetString(current)
        return sorted((v for v in raw if v is not None and str(v) != "NULL"), key=natural_key)

    def _load_values(self):
        layer, field_name = self.layer(), self.field_combo.currentField()
        typed = self.value_combo.currentText()
        self.values = self.all_values(layer, field_name) if layer is not None and field_name else []
        self.value_combo.blockSignals(True)
        self.value_combo.clear()
        self.value_combo.addItems([str(v) for v in self.values])
        completer = QCompleter([str(v) for v in self.values], self.value_combo)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.value_combo.setCompleter(completer)
        self.value_combo.setEditText(typed if typed in [str(v) for v in self.values] else "")
        self.value_combo.blockSignals(False)

    def linked_layers(self):
        """Other vector layers in the project that have the chosen column."""
        layer, field_name = self.layer(), self.field_combo.currentField()
        if layer is None or not field_name:
            return []
        out = []
        for node in QgsProject.instance().layerTreeRoot().findLayers():
            other = node.layer()
            if (isinstance(other, QgsVectorLayer) and other.isValid() and other.id() != layer.id()
                    and other.fields().lookupField(field_name) >= 0 and other not in out):
                out.append(other)
        return out

    def _show_linked(self):
        if not self.linked_check.isChecked():
            self.linked_label.setText("")
            return
        names = [l.name() for l in self.linked_layers()]
        column = self.field_combo.currentField()
        self.linked_label.setText(", ".join(names) if names else
                                  f"(no other layer has a {column} column)" if column else "")

    # ----- actions -----------------------------------------------------------------
    def step(self, direction, *_):
        if not self.values:
            return
        texts = [str(v) for v in self.values]
        current = self.value_combo.currentText()
        if current in texts:
            index = (texts.index(current) + direction) % len(texts)
        else:
            index = 0 if direction > 0 else len(texts) - 1
        self.value_combo.setEditText(texts[index])
        self.apply()

    def apply(self, *_):
        layer, field_name = self.layer(), self.field_combo.currentField()
        value = self.value_combo.currentText().strip()
        if layer is None or not field_name:
            self._say("Pick a layer and a column first.", error=True)
            return
        if not value:
            self._say("Type or pick a value.", error=True)
            return
        contains = self.match_combo.isEnabled() and self.match_combo.currentIndex() == 1
        targets = [layer] + (self.linked_layers() if self.linked_check.isChecked() else [])
        done, problems = [], []
        for target in targets:
            if target.isEditable():
                problems.append(f"{target.name()} is in edit mode (save or stop editing first)")
                continue
            try:
                sql = filter_sql(target, field_name, value, contains)
            except ValueError:
                problems.append(f"{field_name} in {target.name()} holds numbers, not '{value}'")
                continue
            if self.filters.apply(target, sql):
                done.append(target)
            else:
                problems.append(f"{target.name()} did not accept the filter")
        self.memory.save(layer)
        if self.label_combo.currentField():
            self.styles.label(layer, self.label_combo.currentField())
        self.settings.setValue(KEY + "label", self.label_combo.currentField())
        colour = self.colour_combo.currentField()
        self.settings.setValue(KEY + "colour", colour)
        if colour:
            values = self.all_values(layer, colour, MAX_COLOURS + 1)  # every value gets a colour
            if not self.styles.colour(layer, colour, values):
                problems.append(f"Colour by: {colour} has {len(values)} values, too many to colour "
                                f"(at most {MAX_COLOURS})")
        shown = {l.id(): count_features(l) for l in done}
        if done and self.zoom_check.isChecked():
            self._zoom(done)
        counts = " · ".join(f"{l.name()}: {shown[l.id()]:,}" for l in done)
        how = "contains" if contains else "="
        text = f"{field_name} {how} {value} — {counts}" if done else "Nothing was filtered."
        if done and not any(shown.values()):
            text += "  (no features have this value)"
        if problems:
            text += "\n" + "\n".join(problems)
        self._say(text, error=bool(problems) and not done)

    def clear(self, *_):
        restored = self.filters.clear()
        self.styles.restore()
        self.iface.mapCanvas().refresh()
        self._load_values()
        self._say(f"Filter cleared on {len(restored)} layer(s)." if restored else "No filter to clear.")

    def _zoom(self, layers):
        canvas = self.iface.mapCanvas()
        extent = None
        for layer in layers:
            own = features_extent(layer)
            if own is None:
                continue
            box = QgsCoordinateTransform(layer.crs(), canvas.mapSettings().destinationCrs(),
                                         QgsProject.instance()).transformBoundingBox(own)
            if extent is None:
                extent = QgsRectangle(box)
            else:
                extent.combineExtentWith(box)
        if extent is None:
            return
        if extent.width() == 0 and extent.height() == 0:
            canvas.setCenter(extent.center())
            canvas.zoomScale(5000)
        else:
            extent.grow(max(extent.width(), extent.height()) * 0.08)
            canvas.setExtent(extent)
        canvas.refresh()

    def _say(self, text, error=False):
        self.status.setStyleSheet("color: #b00020;" if error else "color: #222;")
        self.status.setText(text)
