"""Pins in boundary panel: choose a boundary, select the pins inside it, extract them.

A boundary is a region, province, town/city or barangay (NAMRIA/PSA boundaries
from HDX, OpenStreetMap as fallback), a polygon of any layer in the project, or
one you draw (kept in "My boundaries" for next time). Several can be chosen at
once. Select pins selects the pins inside in every ticked point layer; Extract
copies them to a new layer (GeoPackage), KMZ and/or Excel, each pin with a
"boundary" column naming the boundary it is in.
"""
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QCursor
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDockWidget, QFormLayout,
                                 QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                                 QPushButton, QToolButton, QVBoxLayout, QWidget)
from qgis.core import (Qgis, QgsCoordinateTransform, QgsCsException, QgsFillSymbol, QgsPalLayerSettings,
                       QgsProject, QgsSettings, QgsTextBufferSettings, QgsTextFormat, QgsVectorLayer,
                       QgsVectorLayerSimpleLabeling, QgsWkbTypes)
from qgis.gui import QgsMapTool, QgsRubberBand

from .boundary import (HDX, LEVELS, MY_TABLE, OSM, OSM_FILE, Source, admin_levels, copies_inside,
                       copy_style, file_of, find_admin, open_my_boundaries, pins_inside, save_my_boundary,
                       title_of, write_gpkg)
from .csv_import import unique_path
from .export import export_kmz, export_xlsx
from .latlon import name_field
from .map_tool import POINT, RouteTool
from .save_dialog import ask as ask_save_window

try:  # QGIS >= 3.30 and QGIS 4
    POLYGON = Qgis.GeometryType.Polygon
except AttributeError:
    POLYGON = QgsWkbTypes.PolygonGeometry
KEY = "route_drawer/boundary_"
KEY_ADMIN = KEY + "admin_file"
KEY_MY = KEY + "my_file"
KEY_SOURCE = KEY + "source"
EXTRACTED = "route_drawer/extracted"  # layer property: made by Extract (a copy: not searched by default)
ROAD_FILE = "route_drawer/road_layer/source"  # the remembered road layer (its OSM download has the admin areas)
MAX_SHOWN = 300  # names listed at once; typing narrows them down
PURPLE = QColor(142, 36, 170)
UNCHECKED, CHECKED = Qt.CheckState.Unchecked, Qt.CheckState.Checked
FID_ROLE = Qt.ItemDataRole.UserRole


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


class PickTool(QgsMapTool):
    """Click boundaries on the map to choose them; right-click or Esc when done."""

    def __init__(self, canvas, on_pick, on_done):
        super().__init__(canvas)
        self.on_pick, self.on_done = on_pick, on_done
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.on_pick(event.mapPoint())
        elif event.button() == Qt.MouseButton.RightButton:
            self.on_done()

    def keyPressEvent(self, event):
        # QGIS map canvas convention: a tool ignores an event it has handled
        if event.key() == Qt.Key.Key_Escape:
            self.on_done()
            event.ignore()
        else:
            event.accept()


class _Drawer:
    """What the drawing tool needs from its owner, for a boundary instead of a route."""

    def __init__(self, panel):
        self.panel, self.iface = panel, panel.iface

    def follow_roads(self):
        return self.panel.plugin.follow_roads()

    def make_leg(self, a, b, straight):
        return self.panel.plugin.make_leg(a, b, straight)

    def length(self, points):
        return self.panel.plugin.length(points)

    def warn(self, text):
        self.panel.plugin.warn(text)

    def save_route(self, points, methods):
        return self.panel.save_drawn(points)


class BoundaryPanel(QDockWidget):
    def __init__(self, plugin, parent=None):
        super().__init__("Pins in boundary", parent)
        self.setObjectName("RouteDrawerBoundaryPanel")
        self.plugin, self.iface = plugin, plugin.iface
        self.canvas = self.iface.mapCanvas()
        self.settings = QgsSettings()
        self.admin, self.admin_files = {}, {}
        self.sources = {}       # combo key -> Source
        self.chosen = {}        # fid -> label, in the current source
        self.layer_ticks = {}   # point layer id -> ticked (your choice, this session)
        self.found = None       # [(layer, {fid: boundary label})] of the last Select pins
        self._my_private = None  # My boundaries opened without adding it to the map
        self._toggled = None
        self.pick_tool = PickTool(self.canvas, self.pick_at, self.stop_picking)
        self.draw_tool = RouteTool(self.canvas, _Drawer(self), title="Draw boundary", closed=True)
        self.band = QgsRubberBand(self.canvas, POLYGON)
        self.band.setStrokeColor(PURPLE)
        self.band.setFillColor(QColor(142, 36, 170, 28))
        self.band.setWidth(2)

        body = QWidget()
        outer = QVBoxLayout(body)
        form = QFormLayout()
        self.source_combo = QComboBox()
        self.source_combo.setToolTip("Where the boundary comes from: an admin level, your own drawn "
                                     "boundaries, or any polygon layer in the project")
        self.column_label = QLabel("Name column")
        self.column_combo = QComboBox()
        form.addRow("Boundary", self.source_combo)
        form.addRow(self.column_label, self.column_combo)
        outer.addLayout(form)
        admin_row = QHBoxLayout()
        self.admin_label = QLabel()
        self.admin_label.setWordWrap(True)
        self.admin_label.setStyleSheet("color: #666;")
        self.admin_button = QToolButton()
        self.admin_button.setText("Choose…")
        self.admin_button.setToolTip("Choose the admin boundaries file: the NAMRIA/PSA zip from HDX "
                                     f"(phl_admin_boundaries.shp.zip), or OpenStreetMap's {OSM_FILE}")
        admin_row.addWidget(self.admin_label, 1)
        admin_row.addWidget(self.admin_button)
        outer.addLayout(admin_row)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Type to find, e.g. Davao del Sur")
        self.search.setClearButtonEnabled(True)
        self.list = QListWidget()
        self.list.setMinimumHeight(150)
        self.hint = QLabel()
        self.hint.setStyleSheet("color: #666;")
        outer.addWidget(self.search)
        outer.addWidget(self.list, 2)
        outer.addWidget(self.hint)

        chosen_row = QHBoxLayout()
        self.chosen_label = QLabel()
        self.chosen_label.setWordWrap(True)
        self.zoom_button = QToolButton()
        self.zoom_button.setText("Zoom")
        self.zoom_button.setToolTip("Zoom to the chosen boundaries")
        self.clear_chosen_button = QToolButton()
        self.clear_chosen_button.setText("Clear")
        self.clear_chosen_button.setToolTip("Untick all chosen boundaries")
        chosen_row.addWidget(self.chosen_label, 1)
        chosen_row.addWidget(self.zoom_button)
        chosen_row.addWidget(self.clear_chosen_button)
        outer.addLayout(chosen_row)

        tools_row = QHBoxLayout()
        self.pick_button = QPushButton("Pick on map")
        self.pick_button.setToolTip("Click boundaries on the map to choose them (click again to drop "
                                    "one); right-click or Esc when done")
        self.draw_button = QPushButton("Draw new…")
        self.draw_button.setToolTip("Draw your own boundary (click corners, hold for freehand; Follow "
                                    "Roads works too), Enter to save it to My boundaries")
        self.use_selected_button = QPushButton("Use selected")
        self.use_selected_button.setToolTip("Choose the polygons selected on the map in this layer")
        for button in (self.pick_button, self.draw_button, self.use_selected_button):
            tools_row.addWidget(button)
        outer.addLayout(tools_row)

        outer.addWidget(QLabel("Look in (point layers)"))
        self.layers_list = QListWidget()
        self.layers_list.setMaximumHeight(110)
        outer.addWidget(self.layers_list, 1)

        select_row = QHBoxLayout()
        self.select_button = QPushButton("Select pins")
        self.select_button.setDefault(True)
        self.unselect_button = QPushButton("Clear selection")
        select_row.addWidget(self.select_button)
        select_row.addWidget(self.unselect_button)
        outer.addLayout(select_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        extract_row = QHBoxLayout()
        extract_row.addWidget(QLabel("Extract to"))
        self.outputs = {}
        for kind, label, default in (("layer", "New layer", True), ("kmz", "KMZ", False),
                                     ("xlsx", "Excel", False)):
            check = QCheckBox(label)
            check.setChecked(self.settings.value(KEY + "out_" + kind, default, type=bool))
            check.toggled.connect(lambda on, kind=kind: self.settings.setValue(KEY + "out_" + kind, on))
            self.outputs[kind] = check
            extract_row.addWidget(check)
        extract_row.addStretch(1)
        outer.addLayout(extract_row)
        self.extract_button = QPushButton("Extract…")
        self.extract_button.setToolTip("Copy the pins inside to a new layer (in a group named after the "
                                       "boundary), KMZ and/or Excel, with a 'boundary' column")
        outer.addWidget(self.extract_button)
        self.setWidget(body)

        self.source_combo.currentIndexChanged.connect(self._source_changed)
        self.column_combo.currentIndexChanged.connect(self._column_changed)
        self.admin_button.clicked.connect(self.choose_admin_file)
        self.search.textChanged.connect(self._fill_list)
        self.list.itemChanged.connect(self._item_changed)
        self.list.itemClicked.connect(self._item_clicked)
        self.zoom_button.clicked.connect(self.zoom)
        self.clear_chosen_button.clicked.connect(self.clear_chosen)
        self.pick_button.clicked.connect(self.pick_on_map)
        self.draw_button.clicked.connect(self.draw_new)
        self.use_selected_button.clicked.connect(self.use_selected)
        self.layers_list.itemChanged.connect(self._layer_ticked)
        self.select_button.clicked.connect(self.select_pins)
        self.unselect_button.clicked.connect(self.clear_selection)
        self.extract_button.clicked.connect(self.extract)
        self._project_changed = lambda *_: self.refresh()
        QgsProject.instance().layersAdded.connect(self._project_changed)
        QgsProject.instance().layersRemoved.connect(self._project_changed)

        self.load_admin()
        self.refresh()
        self._source_changed()

    def cleanup(self):
        """Stop listening to the project and take the drawings off the map (the panel is
        about to be deleted)."""
        QgsProject.instance().layersAdded.disconnect(self._project_changed)
        QgsProject.instance().layersRemoved.disconnect(self._project_changed)
        for tool in (self.pick_tool, self.draw_tool):
            if self.canvas.mapTool() is tool:
                self.canvas.unsetMapTool(tool)
        self.draw_tool.cleanup()
        self.canvas.scene().removeItem(self.band)

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def say(self, text, error=False):
        self.status.setStyleSheet("color: #b00020;" if error else "")
        self.status.setText(text)

    # ----- admin boundaries ---------------------------------------------------------------
    def _candidate_files(self):
        """The remembered admin file first, then the files of the project's layers and of the
        remembered road layer: the admin areas often sit beside them (same OSM download)."""
        paths = [self.settings.value(KEY_ADMIN, "") or ""]
        sources = [l.source() for l in QgsProject.instance().mapLayers().values()
                   if isinstance(l, QgsVectorLayer) and l.providerType() == "ogr"]
        sources.append(self.settings.value(ROAD_FILE, "") or "")
        paths += [file_of(s) for s in sources if s]
        return list(dict.fromkeys(p for p in paths if p))

    def load_admin(self):
        self.admin, self.admin_files = admin_levels(self._candidate_files())
        parts = []
        if HDX in self.admin_files:
            parts.append(f"NAMRIA/PSA (HDX), {os.path.basename(self.admin_files[HDX])}")
        from_osm = [LEVELS[level].lower() for level, s in sorted(self.admin.items()) if s.origin == OSM]
        if from_osm:
            parts.append(f"OpenStreetMap for {', '.join(from_osm)} (towns and barangays incomplete)")
        self.admin_label.setText("Admin areas: " + ("; ".join(parts) if parts else
                                                    "none found yet. Choose the NAMRIA/PSA file from HDX "
                                                    "(phl_admin_boundaries.shp.zip)."))
        self.admin_button.setText("Change…" if HDX in self.admin_files else "Choose…")

    def choose_admin_file(self, *_):
        start = self.settings.value(KEY_ADMIN, "") or os.path.join(os.path.expanduser("~"), "Downloads")
        path = self.plugin.pick_file("Admin boundaries: the NAMRIA/PSA zip from HDX, or OSM admin areas",
                                     "Boundaries (*.zip *.shp);;All files (*)", start=start)
        if not path:
            return
        if not find_admin(path):
            self.say(f"{os.path.basename(path)} holds no admin boundaries this panel knows: phl_admin1–4 "
                     f"(NAMRIA/PSA from HDX) or {OSM_FILE} (OpenStreetMap).", error=True)
            return
        self.settings.setValue(KEY_ADMIN, path)
        self.load_admin()
        self.refresh()
        self.say(f"Using {os.path.basename(path)} for the admin areas.")

    # ----- My boundaries ------------------------------------------------------------------
    def _my_path(self):
        return self.settings.value(KEY_MY, "") or os.path.join(
            os.path.dirname(self.plugin._gpkg_path()), "my_boundaries.gpkg")

    def _is_my_layer(self, layer):
        return (isinstance(layer, QgsVectorLayer) and layer.providerType() == "ogr"
                and f"layername={MY_TABLE}" in layer.source()
                and _same_file(file_of(layer.source()), self._my_path()))

    def _my_layer(self, create=False):
        """My boundaries: the layer on the map, else the file opened privately (None if none yet)."""
        loaded = next((l for l in QgsProject.instance().mapLayers().values() if self._is_my_layer(l)), None)
        if loaded is not None:
            return loaded
        if self._my_private is None or (create and not self._my_private.isValid()):
            self._my_private = open_my_boundaries(self._my_path(), create)
        return self._my_private

    def _show_my_layer(self, layer):
        """Put My boundaries on the map (dashed purple outlines with names), at the top."""
        layer.renderer().setSymbol(QgsFillSymbol.createSimple(
            {"style": "no", "outline_color": "142,36,170", "outline_width": "0.7", "outline_style": "dash"}))
        label = QgsPalLayerSettings()
        label.fieldName = "name"
        text = QgsTextFormat()
        text.setSize(9)
        text.setColor(QColor(110, 20, 140))
        buffer = QgsTextBufferSettings()
        buffer.setEnabled(True)
        buffer.setSize(1)
        buffer.setColor(QColor(255, 255, 255))
        text.setBuffer(buffer)
        label.setFormat(text)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(label))
        layer.setLabelsEnabled(True)
        self._my_private = None  # the project owns it now
        QgsProject.instance().addMapLayer(layer, False)
        QgsProject.instance().layerTreeRoot().insertLayer(0, layer)

    # ----- sources and the list of names -----------------------------------------------------
    def refresh(self):
        """Rebuild the boundary choices and the point layers after the project changed."""
        if not self.admin:
            self.load_admin()  # e.g. the road layer (and its OSM admin areas) was just added
        self.found = None
        self._fill_sources()
        self._fill_layers()

    def _fill_sources(self):
        current = self.source_key()
        wanted = current or self.settings.value(KEY_SOURCE, "admin:2")
        old, self.sources = self.sources, {}
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for level, source in sorted(self.admin.items()):
            key = f"admin:{level}"
            self.sources[key] = source
            self.source_combo.addItem(source.title + (" (OpenStreetMap)" if source.origin == OSM else ""), key)
        my_layer = self._my_layer()
        mine = old.get("mine")
        if mine is None or mine.layer() is not my_layer:
            mine = Source("My boundaries", layer=my_layer, name_field="name") if my_layer is not None \
                else Source("My boundaries", uri=f"{self._my_path()}|layername={MY_TABLE}")
        self.sources["mine"] = mine
        self.source_combo.addItem("My boundaries (drawn)", "mine")
        root = QgsProject.instance().layerTreeRoot()
        polygons = [n.layer() for n in root.findLayers()
                    if isinstance(n.layer(), QgsVectorLayer) and n.layer().isValid()
                    and n.layer().geometryType() == POLYGON and not self._is_my_layer(n.layer())]
        if polygons:
            self.source_combo.insertSeparator(self.source_combo.count())
        for layer in polygons:
            key = f"layer:{layer.id()}"
            self.sources[key] = old.get(key) or Source(layer.name(), layer=layer, name_field=self._name_column(layer))
            self.source_combo.addItem(f"Layer: {layer.name()}", key)
        index = self.source_combo.findData(wanted)
        self.source_combo.setCurrentIndex(index if index >= 0 else 0)
        self.source_combo.blockSignals(False)
        if self.source_key() != current:
            self._source_changed()

    @staticmethod
    def _name_column(layer):
        """A name-like column (name, site_name, label...), else the first text column."""
        fields = layer.fields()
        index = name_field(fields.names())
        if index is None:
            index = next((i for i, f in enumerate(fields) if not f.isNumeric() and not f.isDateOrTime()), None)
        return fields[index].name() if index is not None else ""

    def source_key(self):
        return self.source_combo.currentData()

    def source(self):
        return self.sources.get(self.source_key())

    def _source_changed(self, *_):
        key = self.source_key() or ""
        if key:
            self.settings.setValue(KEY_SOURCE, key)
        self.chosen = {}
        source = self.source()
        from_layer = key.startswith("layer:")
        for widget in (self.column_label, self.column_combo):
            widget.setVisible(from_layer)
        self.use_selected_button.setVisible(from_layer)
        self.pick_button.setEnabled(source is not None)
        if from_layer:
            self.column_combo.blockSignals(True)
            self.column_combo.clear()
            for name in source.layer().fields().names():
                self.column_combo.addItem(name, name)
            self.column_combo.setCurrentIndex(max(self.column_combo.findData(source.name_field), 0))
            self.column_combo.blockSignals(False)
            if self.column_combo.currentData() != source.name_field:  # the list always shows the column shown
                source.name_field = self.column_combo.currentData() or ""
                source.refresh()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            self._fill_list()  # reads the names (all barangays: about 0.2 s)
        finally:
            QApplication.restoreOverrideCursor()
        self._chosen_changed()

    def _column_changed(self, *_):
        source, field = self.source(), self.column_combo.currentData()
        if source is None or field is None or field == source.name_field:
            return
        source.name_field = field
        source.refresh()
        self.chosen = {fid: source.label_of(fid) for fid in self.chosen}
        self._fill_list()
        self._chosen_changed()

    def _fill_list(self, *_):
        source = self.source()
        entries = source.entries() if source is not None else []
        words = self.search.text().casefold().split()
        shown = [e for e in entries if all(w in e[1].casefold() for w in words)] if words else entries
        self.list.blockSignals(True)
        self.list.clear()
        for fid, label in shown[:MAX_SHOWN]:
            item = QListWidgetItem(label)
            item.setData(FID_ROLE, fid)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(CHECKED if fid in self.chosen else UNCHECKED)
            self.list.addItem(item)
        self.list.blockSignals(False)
        if not entries:
            hint = ("None yet: draw one with Draw new…" if self.source_key() == "mine"
                    else "Nothing to choose here.")
        elif len(shown) > MAX_SHOWN:
            hint = f"{len(shown):,} match; showing the first {MAX_SHOWN}. Type more to narrow down."
        elif words:
            hint = f"{len(shown):,} of {len(entries):,} match."
        else:
            hint = f"{len(entries):,} to choose from. Tick one or more."
        self.hint.setText(hint)

    def _item_changed(self, item):
        fid = item.data(FID_ROLE)
        if item.checkState() == CHECKED:
            self.chosen[fid] = item.text()
        else:
            self.chosen.pop(fid, None)
        self._toggled = item
        self._chosen_changed()

    def _item_clicked(self, item):
        """A click on the name ticks it too (a click on the box already did)."""
        if self._toggled is item:
            self._toggled = None
            return
        item.setCheckState(UNCHECKED if item.checkState() == CHECKED else CHECKED)
        self._toggled = None

    def clear_chosen(self, *_):
        self.chosen = {}
        self._fill_list()
        self._chosen_changed()

    def _chosen_changed(self):
        labels = list(self.chosen.values())
        if labels:
            text = "; ".join(labels[:4]) + (f"; and {len(labels) - 4} more" if len(labels) > 4 else "")
        else:
            text = "nothing yet"
        self.chosen_label.setText(f"<b>Chosen:</b> {text}")
        self.zoom_button.setEnabled(bool(labels))
        self.clear_chosen_button.setEnabled(bool(labels))
        self.found = None
        self._draw_band()

    # ----- boundaries on the map ---------------------------------------------------------------
    def boundaries(self):
        source = self.source()
        return source.boundaries(self.chosen) if source is not None and self.chosen else []

    def _draw_band(self):
        self.band.reset(POLYGON)
        source = self.source()
        if source is None:
            return
        crs = source.layer().crs()
        for _, geometry in self.boundaries():
            self.band.addGeometry(geometry, crs)

    def zoom(self, *_):
        items, source = self.boundaries(), self.source()
        if not items:
            return
        box = None
        for _, geometry in items:
            if box is None:
                box = geometry.boundingBox()
            else:
                box.combineExtentWith(geometry.boundingBox())
        try:
            box = QgsCoordinateTransform(source.layer().crs(), self.canvas.mapSettings().destinationCrs(),
                                         QgsProject.instance()).transformBoundingBox(box)
        except QgsCsException:
            return
        box.scale(1.1)
        self.canvas.setExtent(box)
        self.canvas.refresh()

    def pick_on_map(self, *_):
        source = self.source()
        if source is None:
            return
        self.canvas.setMapTool(self.pick_tool)
        what = source.title.lower() if self.source_key().startswith("admin:") else f"polygon of '{source.title}'"
        self.iface.statusBarIface().showMessage(
            f"Pins in boundary: click a {what} on the map to choose it (again to drop it) · "
            "right-click or Esc when done")

    def pick_at(self, map_point):
        source = self.source()
        if source is None:
            return
        try:
            fids = source.at(map_point, self.canvas.mapSettings().destinationCrs())
        except QgsCsException:
            fids = []
        if not fids:
            self.say(f"Nothing from '{source.title}' there.", error=True)
            return
        for fid in fids:
            if fid in self.chosen:
                del self.chosen[fid]
            else:
                self.chosen[fid] = source.label_of(fid)
        self.say("")
        self._fill_list()
        self._chosen_changed()

    def stop_picking(self):
        self.plugin.switch_tool(self.pick_tool, False)
        self.iface.statusBarIface().clearMessage()

    def use_selected(self, *_):
        source = self.source()
        fids = source.layer().selectedFeatureIds() if source is not None else []
        if not fids:
            self.say(f"Select polygons of '{source.title}' on the map first (e.g. with Select Features), "
                     "then press Use selected.", error=True)
            return
        for fid in fids:
            self.chosen[fid] = source.label_of(fid)
        self._fill_list()
        self._chosen_changed()

    def draw_new(self, *_):
        self.canvas.setMapTool(self.draw_tool)
        self.say("Draw the boundary: click its corners (hold or drag for freehand; Follow Roads works "
                 "too). Enter saves it, right-click undoes, Esc cancels.")

    def ask_save(self, title, default_name):
        """The Save window (name and remarks); {name, remarks, ...} or None when cancelled."""
        return ask_save_window(self.iface.mainWindow(), title, default_name, remarks=True,
                               note="Kept in My boundaries, so you can use it again later.")

    def save_drawn(self, points):
        """Enter in Draw new: keep the boundary in My boundaries and choose it."""
        try:
            layer = self._my_layer(create=True)
        except OSError as error:
            self.plugin.warn(f"Could not create My boundaries: {error}")
            return False
        if layer is None:
            self.plugin.warn(f"Could not open {self._my_path()}.")
            return False
        answer = self.ask_save("Save boundary", f"Boundary {max(layer.featureCount(), 0) + 1}")
        if answer is None:
            return False
        fid = save_my_boundary(layer, points, answer["name"], answer.get("remarks"))
        if fid is None:
            self.plugin.warn(f"Could not save the boundary to {self._my_path()}.")
            return False
        if not any(l is layer for l in QgsProject.instance().mapLayers().values()):
            self._show_my_layer(layer)  # also refreshes the choices (layersAdded)
        self.sources["mine"] = Source("My boundaries", layer=layer, name_field="name")
        self.source_combo.blockSignals(True)
        self.source_combo.setCurrentIndex(self.source_combo.findData("mine"))
        self.source_combo.blockSignals(False)
        self._source_changed()
        self.chosen = {fid: self.sources["mine"].label_of(fid)}
        self._fill_list()
        self._chosen_changed()
        self.plugin.switch_tool(self.draw_tool, False)
        self.say(f"Saved '{answer['name']}' to My boundaries and chose it. Press Select pins to find "
                 "the pins inside.")
        return True

    # ----- point layers, Select, Extract ---------------------------------------------------------
    def _fill_layers(self):
        """Point layers in Layers panel order: visible ones ticked, earlier extracts (copies) not;
        a repeated name shows its group."""
        nodes = [n for n in QgsProject.instance().layerTreeRoot().findLayers()
                 if isinstance(n.layer(), QgsVectorLayer) and n.layer().isValid()
                 and n.layer().geometryType() == POINT]
        names = [n.layer().name() for n in nodes]
        self.layers_list.blockSignals(True)
        self.layers_list.clear()
        for node in nodes:
            layer = node.layer()
            text = layer.name()
            if names.count(text) > 1 and node.parent() is not None and node.parent().parent() is not None:
                text += f" · {node.parent().name()}"
            item = QListWidgetItem(text)
            item.setData(FID_ROLE, layer.id())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            default = node.isVisible() and not layer.customProperty(EXTRACTED, False)
            item.setCheckState(CHECKED if self.layer_ticks.get(layer.id(), default) else UNCHECKED)
            self.layers_list.addItem(item)
        self.layers_list.blockSignals(False)

    def _layer_ticked(self, item):
        self.layer_ticks[item.data(FID_ROLE)] = item.checkState() == CHECKED
        self.found = None

    def ticked_layers(self):
        items = (self.layers_list.item(i) for i in range(self.layers_list.count()))
        layers = (QgsProject.instance().mapLayer(item.data(FID_ROLE)) for item in items
                  if item.checkState() == CHECKED)
        return [layer for layer in layers if layer is not None]

    def select_pins(self, *_):
        """Select the pins inside the chosen boundaries; [(layer, {fid: boundary})] with pins, or None."""
        source = self.source()
        if source is None or not self.chosen:
            self.say("Choose a boundary first: tick one in the list, Pick on map, or Draw new…", error=True)
            return None
        layers = self.ticked_layers()
        if not layers:
            self.say("Tick at least one point layer under Look in.", error=True)
            return None
        boundaries, crs = self.boundaries(), source.layer().crs()
        found = []
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            for layer in layers:
                labels = pins_inside(layer, boundaries, crs)
                layer.selectByIds(list(labels))
                found.append((layer, labels))
        except QgsCsException as error:
            self.say(f"Could not compare the boundary with the pins: {error}", error=True)
            return None
        finally:
            QApplication.restoreOverrideCursor()
        title = title_of(self.chosen.values())
        total = sum(len(labels) for _, labels in found)
        counts = " · ".join(f"{layer.name()}: {len(labels):,}" for layer, labels in found)
        self.say(f"{title}: {total:,} pin(s) selected ({counts})." if total
                 else f"No pins inside {title} in the ticked layers ({counts}).", error=not total)
        self.zoom()
        self.found = [(layer, labels) for layer, labels in found if labels]
        return self.found

    def clear_selection(self, *_):
        for layer in self.ticked_layers():
            layer.removeSelection()
        self.found = None
        self.say("")

    def extract(self, *_):
        wants = {kind: check.isChecked() for kind, check in self.outputs.items()}
        if not any(wants.values()):
            self.say("Tick New layer, KMZ and/or Excel first.", error=True)
            return
        found = self.select_pins()  # always the current boundaries and layers
        if not found:
            return
        title = f"Pins in {title_of(self.chosen.values())}"
        folder = self.settings.value("route_drawer/export_folder",
                                     os.path.join(os.path.expanduser("~"), "Documents"))
        paths = {}
        for kind, extension, label, filters in (("kmz", ".kmz", "KMZ", "Google Earth KMZ (*.kmz)"),
                                                ("xlsx", ".xlsx", "Excel", "Excel workbook (*.xlsx)")):
            if not wants[kind]:
                continue
            path = self.plugin.pick_file(f"Extract '{title}' to {label}", filters, save=True,
                                         start=os.path.join(folder, title + extension))
            if not path:
                return  # cancelled: nothing is written
            if not path.lower().endswith(extension):
                path += extension
            paths[kind] = path
            folder = os.path.dirname(path)
        if paths:
            self.settings.setValue("route_drawer/export_folder", folder)
        done = []  # (in the message bar, in the panel): full paths there, file names here
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            copies = copies_inside(found)
            if wants["layer"]:
                gpkg = unique_path(os.path.join(os.path.dirname(self.plugin._gpkg_path()), "Extracts"), title)
                tables = write_gpkg(copies, gpkg)
                group = QgsProject.instance().layerTreeRoot().insertGroup(0, title)
                for copy, table in zip(copies, tables):
                    layer = QgsVectorLayer(f"{gpkg}|layername={table}", copy.name(), "ogr")
                    copy_style(copy, layer)
                    layer.setCustomProperty(EXTRACTED, True)
                    QgsProject.instance().addMapLayer(layer, False)
                    group.addLayer(layer)
                done.append((f"a new group '{title}' in the Layers panel ({gpkg})",
                             f"a new group '{title}' ({os.path.basename(gpkg)})"))
            if "kmz" in paths:
                export_kmz(copies, paths["kmz"], False, title=title)
                done.append((paths["kmz"], os.path.basename(paths["kmz"])))
            if "xlsx" in paths:
                export_xlsx(copies, paths["xlsx"], False)
                done.append((paths["xlsx"], os.path.basename(paths["xlsx"])))
        except OSError as error:
            self.say(f"Could not extract: {error}", error=True)
            return
        finally:
            QApplication.restoreOverrideCursor()
        total = sum(len(labels) for _, labels in found)
        column = "Each has a 'boundary' column naming the boundary it is in."
        self.iface.messageBar().pushMessage(
            "Route Drawer", f"Extracted {total:,} pin(s) to {'; '.join(full for full, _ in done)}. {column}",
            Qgis.MessageLevel.Success, 12)
        self.say(f"Extracted {total:,} pin(s) to {'; '.join(short for _, short in done)}. {column}")
