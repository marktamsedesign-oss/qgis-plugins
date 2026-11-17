"""Route Drawer: draw a line on the map by clicking, optionally along the roads.

Saved lines go to a GeoPackage layer "Drawn routes" (created on first save),
or into the active line layer when that layer is in edit mode.
"""
import os
from datetime import datetime
from functools import partial

import xml.etree.ElementTree as ET

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QCursor, QIcon
from qgis.PyQt.QtWidgets import (QApplication, QFileDialog, QInputDialog, QMenu,
                                 QMessageBox, QProgressDialog, QToolButton)
try:  # Qt6 (QGIS 4) keeps QAction/QShortcut in QtGui; Qt5 (QGIS 3) in QtWidgets
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction
from qgis.core import (Qgis, QgsApplication, QgsCoordinateTransform, QgsLayerTree,
                       QgsDistanceArea, QgsFeature, QgsFields,
                       QgsGeometry, QgsLineSymbol, QgsMarkerSymbol, QgsPalLayerSettings,
                       QgsProject, QgsProperty, QgsProviderRegistry, QgsRectangle, QgsSettings,
                       QgsSymbolLayer,
                       QgsTextBufferSettings, QgsTextFormat,
                       QgsVectorFileWriter, QgsVectorLayer,
                       QgsVectorLayerSimpleLabeling, QgsWkbTypes)

from .boundary_panel import BoundaryPanel
from .csv_import import guess, read_csv, unique_path, write_pins
from .export import export_kmz, export_xlsx
from .info_tool import InfoTool
from .kml_import import Cancelled, import_kml
from .latlon import name_field
from .map_tool import LINE, WGS84, RouteTool, format_length
from .remember import RememberedLayer
from .colour_legend import (build as build_colour_legend, is_colour_legend, rebuild_file,
                            watch as watch_colour_legend)
from .road_router import RoadRouter, RouteError
from .pushpin import pushpin_symbol
from .save_dialog import COLOR_COLUMNS, REMARKS_COLUMNS, ask as ask_save_window, find_column
from .save_group import add_to, group_file, is_top, layer_in, selected_group, where
from .storage import new_field
from .updater import Updater

MENU = "&Route Drawer"
LAYER_NAME = "drawn_routes"
KEY_ROADS = "route_drawer/follow_roads"
KEY_GPKG = "route_drawer/gpkg_path"
KEY_LABELS = "route_drawer/length_labels"
KEY_IMPORT_FROM = "route_drawer/import_folder"
LENGTH_LABEL = "format_number(\"length_m\", 0) || ' m'"
DEFAULT_GPKG = os.path.join(os.path.expanduser("~"), "Documents",
                            "QGIS Drawn Routes", "drawn_routes.gpkg")
FIELDS = (("name", "String"), ("length_m", "Double"), ("method", "String"), ("created", "String"),
          ("remarks", "String"), ("color", "String"), ("width_mm", "Double"))
KEY_COLOR = "route_drawer/last_color"
KEY_WIDTH = "route_drawer/last_width"
DEFAULT_COLOR, DEFAULT_WIDTH = "#ff6600", 0.8
try:  # QGIS >= 3.30 and QGIS 4
    LINESTRING = Qgis.WkbType.LineString
except AttributeError:
    LINESTRING = QgsWkbTypes.LineString
try:  # QGIS >= 3.30 and QGIS 4
    FILL_COLOR, STROKE_COLOR = QgsSymbolLayer.Property.FillColor, QgsSymbolLayer.Property.StrokeColor
    STROKE_WIDTH = QgsSymbolLayer.Property.StrokeWidth
except AttributeError:
    FILL_COLOR, STROKE_COLOR = QgsSymbolLayer.PropertyFillColor, QgsSymbolLayer.PropertyStrokeColor
    STROKE_WIDTH = QgsSymbolLayer.PropertyStrokeWidth
KML_COLOR = "coalesce(\"kml_color\", '#ff6600')"


class RouteDrawerPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.settings = QgsSettings()
        self.router = RoadRouter()
        self.road_layer_id = None
        self.roads_memory = RememberedLayer("route_drawer/road_layer")  # kept between sessions
        self.private_roads = None  # the remembered road file, opened without adding it to the map
        self.distance = QgsDistanceArea()
        self.distance.setSourceCrs(WGS84, QgsProject.instance().transformContext())
        self.distance.setEllipsoid("WGS84")

    # ----- setup -------------------------------------------------------
    def initGui(self):
        folder = os.path.dirname(__file__)
        window = self.iface.mainWindow()

        self.tool = RouteTool(self.iface.mapCanvas(), self)
        self.draw_action = QAction(QIcon(os.path.join(folder, "icon.svg")), "Draw Route", window)
        self.draw_action.setObjectName("RouteDrawerDraw")
        self.draw_action.setCheckable(True)
        self.draw_action.setToolTip("Draw Route: click points (hold or drag for freehand); right-click undoes, Enter saves; arrows/WASD move the map")
        self.draw_action.triggered.connect(self.activate)
        self.tool.setAction(self.draw_action)

        self.roads_action = QAction(QIcon(os.path.join(folder, "roads.svg")), "Follow Roads", window)
        self.roads_action.setObjectName("RouteDrawerFollowRoads")
        self.roads_action.setCheckable(True)
        self.roads_action.setChecked(self.settings.value(KEY_ROADS, True, type=bool))
        self.roads_action.setToolTip("Follow Roads: each click joins the last one along the "
                                     "shortest road path (Shift+click for a straight segment)")
        self.roads_action.toggled.connect(self._roads_toggled)

        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._fill_menu)
        self.button = QToolButton()
        self.button.setIcon(QgsApplication.getThemeIcon("/mActionOptions.svg"))
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.button.setToolTip("Road layer and where routes are saved")
        self.button.setMenu(self.menu)

        self.info_tool = InfoTool(self.iface.mapCanvas(), window)
        self.info_action = QAction(QIcon(os.path.join(folder, "info.svg")), "Info", window)
        self.info_action.setObjectName("RouteDrawerInfo")
        self.info_action.setCheckable(True)
        self.info_action.setToolTip("Info: click a feature to see all its data (e.g. KMZ extended "
                                    "data); links such as GDrive open in your browser")
        self.info_action.triggered.connect(lambda checked: self.switch_tool(self.info_tool, checked))
        self.info_tool.setAction(self.info_action)

        self.boundary_panel = None  # created when first opened
        self.boundary_action = QAction(QIcon(os.path.join(folder, "boundary.svg")), "Pins in boundary", window)
        self.boundary_action.setObjectName("RouteDrawerPinsInBoundary")
        self.boundary_action.setCheckable(True)
        self.boundary_action.setToolTip("Pins in boundary: choose a region, province, town/city or barangay "
                                        "(or a polygon, or draw one), select the pins inside and extract "
                                        "them to a new layer, KMZ or Excel")
        self.boundary_action.triggered.connect(self.show_boundary_panel)  # clicks only

        self.data_menu = QMenu()
        self.data_actions = []
        for label, slot in (("Import CSV as pins…", self.import_csv),
                            ("Import KMZ / KML…", self.import_kmz),
                            (None, None),
                            ("Export selected layer to KMZ…", partial(self.export_layer, "kmz")),
                            ("Export selected layer to Excel…", partial(self.export_layer, "xlsx"))):
            if label is None:
                self.data_menu.addSeparator()
                continue
            action = self.data_menu.addAction(label)
            action.triggered.connect(slot)
            self.data_actions.append(action)
        self.data_button = QToolButton()
        self.data_button.setIcon(QIcon(os.path.join(folder, "data.svg")))
        self.data_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.data_button.setToolTip("Import CSV/KMZ, export the selected layer to KMZ/Excel")
        self.data_button.setMenu(self.data_menu)

        self.toolbar = self.iface.addToolBar("Route Drawer")
        self.toolbar.setObjectName("RouteDrawerToolbar")
        self.toolbar.addAction(self.draw_action)
        self.toolbar.addAction(self.roads_action)
        self.toolbar.addAction(self.info_action)
        self.toolbar.addAction(self.boundary_action)
        self.toolbar.addWidget(self.data_button)
        self.toolbar.addWidget(self.button)

        # no default keys; assign them in Settings > Keyboard Shortcuts
        for action in (self.draw_action, self.roads_action, self.info_action, self.boundary_action):
            self.iface.registerMainWindowAction(action, "")
            self.iface.addPluginToMenu(MENU, action)
        for action in self.data_actions:
            self.iface.addPluginToMenu(MENU, action)
        QgsProject.instance().layersWillBeRemoved.connect(self._layers_removed)
        QgsProject.instance().layersAdded.connect(self._project_layers_added)
        self._project_layers_added(QgsProject.instance().mapLayers().values())
        self.updater = Updater(self.iface, MENU)  # update messages + Check for updates…
        self.updater.start()

    def unload(self):
        self.updater.stop()
        QgsProject.instance().layersWillBeRemoved.disconnect(self._layers_removed)
        QgsProject.instance().layersAdded.disconnect(self._project_layers_added)
        if self.boundary_panel is not None:
            self.boundary_panel.cleanup()
            self.iface.removeDockWidget(self.boundary_panel)
            self.boundary_panel.deleteLater()
            self.boundary_panel = None
        canvas = self.iface.mapCanvas()
        for tool in (self.tool, self.info_tool):
            if canvas.mapTool() is tool:
                canvas.unsetMapTool(tool)
        self.tool.cleanup()
        self.info_tool.dialog.close()
        for action in (self.draw_action, self.roads_action, self.info_action, self.boundary_action):
            self.iface.unregisterMainWindowAction(action)
            self.iface.removePluginMenu(MENU, action)
        for action in self.data_actions:
            self.iface.removePluginMenu(MENU, action)
        self.data_menu.deleteLater()
        self.iface.mainWindow().removeToolBar(self.toolbar)
        self.toolbar.deleteLater()
        self.menu.deleteLater()
        self.router.clear()
        self.private_roads = None

    def switch_tool(self, tool, checked=True):
        """Button pressed: use the tool; pressed again while in use: back to Pan Map."""
        canvas = self.iface.mapCanvas()
        if checked:
            canvas.setMapTool(tool)
        elif canvas.mapTool() is tool:
            pan = self.iface.actionPan() if hasattr(self.iface, "actionPan") else None
            if pan is not None:
                pan.trigger()
            else:
                canvas.unsetMapTool(tool)

    def show_boundary_panel(self, visible=True):
        if self.boundary_panel is None:
            if not visible:
                return
            self.boundary_panel = BoundaryPanel(self, self.iface.mainWindow())
            self.boundary_panel.visibilityChanged.connect(self.boundary_action.setChecked)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.boundary_panel)
        self.boundary_panel.setVisible(visible)
        if visible:
            self.boundary_panel.raise_()

    def activate(self, checked=True):
        self.switch_tool(self.tool, checked)
        if not checked:
            return  # an unfinished line stays; pick Draw Route again to carry on
        if self.follow_roads() and self.road_layer() is None:
            self.warn("Follow Roads is on but no road layer was found. Choose one from the "
                      "options button, or turn Follow Roads off to draw straight lines.")

    # ----- used by the map tool ----------------------------------------
    def follow_roads(self):
        return self.roads_action.isChecked()

    def length(self, points):
        return self.distance.measureLine(points) if len(points) > 1 else 0.0

    def warn(self, text):
        self.iface.messageBar().pushMessage("Route Drawer", text, Qgis.MessageLevel.Warning, 6)

    def make_leg(self, a, b, straight):
        """Points after a up to b (WGS84), and how they were made."""
        if straight or not self.follow_roads():
            return [b], "straight"
        layer = self.road_layer()
        if layer is None:
            self.warn("No road layer chosen; drew a straight segment.")
            return [b], "straight"
        self.iface.statusBarIface().showMessage("Draw Route: finding the road path…")
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            to_roads = QgsCoordinateTransform(WGS84, layer.crs(), QgsProject.instance())
            path = self.router.route(layer, to_roads.transform(a), to_roads.transform(b))
        except RouteError as error:
            self.warn(f"No road path: {error}. Drew a straight segment instead "
                      "(Backspace removes it).")
            return [b], "straight"
        finally:
            QApplication.restoreOverrideCursor()
        back = QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance())
        return [back.transform(p) for p in path[1:-1]] + [b], "roads"

    def save_route(self, points, methods):
        """Save into the line layer being edited, else into the Drawn routes layer of the group
        selected in the Layers panel (a new file for that group when it has none yet)."""
        group = selected_group(self.iface)
        target, path, kept = self._target(group)
        if target is not None:
            default_name, note = self._next_name(target), f"Adds to '{target.name()}'{where(group)}."
            has_remarks = find_column(target, REMARKS_COLUMNS) >= 0
            has_color = find_column(target, COLOR_COLUMNS) >= 0
            has_width = target.fields().lookupField("width_mm") >= 0
        else:  # a new Drawn routes file has every column
            default_name, note = "Route 1", f"Starts a new file{where(group)}: {os.path.basename(path)}."
            has_remarks = has_color = has_width = True
        answer = self.ask_save(note, default_name, remarks=has_remarks,
                               color=self.settings.value(KEY_COLOR, DEFAULT_COLOR) if has_color else None,
                               size=self.settings.value(KEY_WIDTH, DEFAULT_WIDTH, type=float) if has_width else None)
        if answer is None:
            return False  # cancelled: no file was made
        name = answer["name"]
        if answer.get("color"):
            self.settings.setValue(KEY_COLOR, answer["color"])  # the default next time
        if answer.get("size"):
            self.settings.setValue(KEY_WIDTH, answer["size"])
        new_file = target is None
        if new_file:
            target = self._open_routes_file(path, group)
            if target is None:
                return False
            if is_top(group):
                self.settings.setValue(KEY_GPKG, path)
        color_index = find_column(target, COLOR_COLUMNS)
        remarks_index = find_column(target, REMARKS_COLUMNS)
        width_index = target.fields().lookupField("width_mm")
        geometry = QgsGeometry.fromPolylineXY(points)
        geometry.transform(QgsCoordinateTransform(WGS84, target.crs(), QgsProject.instance()))
        if QgsWkbTypes.hasZ(target.wkbType()):
            geometry.get().addZValue(0)
        if QgsWkbTypes.hasM(target.wkbType()):
            geometry.get().addMValue(0)
        if QgsWkbTypes.isMultiType(target.wkbType()):
            geometry.convertToMultiType()
        length = self.length(points)
        values = {"name": name, "length_m": round(length, 1),
                  "method": " + ".join(sorted(methods)) or "straight",
                  "created": datetime.now().strftime("%Y-%m-%d %H:%M")}
        feature = QgsFeature(target.fields())
        feature.setGeometry(geometry)
        names = {field.name().lower(): i for i, field in enumerate(target.fields())}
        for key, value in values.items():
            if key in names:
                feature.setAttribute(names[key], value)
        for index, value in ((remarks_index, answer.get("remarks")), (color_index, answer.get("color")),
                             (width_index, answer.get("size"))):
            if index >= 0 and value not in (None, ""):
                feature.setAttribute(index, value)

        if target.isEditable():
            ok = target.addFeature(feature)
            note = "; save the layer edits to keep it"
        else:
            ok, _ = target.dataProvider().addFeatures([feature])
            target.updateExtents()
            target.triggerRepaint()
            note = ""
        if not ok:
            self.warn(f"Could not save the route to {target.name()}.")
            return False
        rebuild_file(target, self._rebuild_route_legend)
        message = f"Saved '{name}' ({format_length(length)}) to {target.name()}{where(group)}{note}"
        if new_file:
            message += f", a new file: {os.path.basename(path)}"
        if kept:  # its layer was taken off the map: you are starting afresh; the old file is kept
            message += (f". The previous file ({kept[0]} route(s)) is kept: {kept[1]} "
                        "(options button > Save routes in… opens it again)")
        self.iface.messageBar().pushMessage("Route Drawer", message + ".", Qgis.MessageLevel.Success,
                                            12 if new_file else 5)
        return True

    def ask_save(self, note, default_name, remarks=True, color=None, size=None):
        """The Save window; {name, remarks, color, size} or None when cancelled."""
        return ask_save_window(self.iface.mainWindow(), "Save route", default_name, remarks=remarks,
                               color=color, size=size, size_label="Line width", note=note)

    # ----- layers ------------------------------------------------------
    def road_layer(self):
        """The road layer for Follow Roads: the one chosen before (remembered between
        sessions), else a line layer named like "road"."""
        layer = QgsProject.instance().mapLayer(self.road_layer_id) if self.road_layer_id else None
        if layer is not None:
            return layer
        candidates = [l for l in self._line_layers() if not self._is_any_routes_file(l)]
        layer = self.roads_memory.find(candidates)
        if layer is None and self.roads_memory.known():
            # remembered, but not in this project: use its file without adding it to the map
            if self.private_roads is None or not self.roads_memory.is_this(self.private_roads):
                self.private_roads = self.roads_memory.open_file()
            if self.private_roads is not None:
                return self.private_roads
        if layer is None:
            layer = next((l for l in candidates if "road" in l.name().lower()), None)
            if layer is not None and not self.roads_memory.known():
                self.roads_memory.save(layer)
        self.road_layer_id = layer.id() if layer else None
        if layer is not None:
            self.private_roads = None
        return layer

    def _line_layers(self):
        layers = (node.layer() for node in QgsProject.instance().layerTreeRoot().findLayers())
        return [l for l in layers
                if isinstance(l, QgsVectorLayer) and l.isValid() and l.geometryType() == LINE]

    def _gpkg_path(self):
        return self.settings.value(KEY_GPKG, DEFAULT_GPKG)

    def _is_routes_layer(self, layer):
        """A Drawn routes layer of the remembered file."""
        return self._is_any_routes_file(layer) and self._same_file(layer, self._gpkg_path())

    @staticmethod
    def _same_file(layer, path):
        source = QgsProviderRegistry.instance().decodeUri("ogr", layer.source()).get("path", "")
        # normcase: on Windows "C:/Users/..." and "c:\users\..." are the same file
        return os.path.normcase(os.path.realpath(source)) == os.path.normcase(os.path.realpath(path))

    def _target(self, group):
        """(the line layer being edited or the group's Drawn routes layer, None, None), or
        (None, a new file for the group, and (routes, file) of a previous top-level file
        whose layer was taken off the map, or None)."""
        active = self.iface.activeLayer()
        if (isinstance(active, QgsVectorLayer) and active.isEditable()
                and active.geometryType() == LINE):
            return active, None, None
        path = self._gpkg_path()
        layer = layer_in(group, self._is_routes_layer) or layer_in(group, self._is_any_routes_file)
        if layer is not None:
            if layer.labeling() is None and self._labels_on():  # keep any labels the user styled
                self._set_length_labels(layer, True)
            self._upgrade_routes_layer(layer)
            return layer, None, None
        if not is_top(group):
            return None, group_file(os.path.dirname(path), "drawn_routes", group), None
        loaded = self._loaded_routes_layer() is not None
        count = 0 if loaded else self._routes_in_file(path)
        if loaded or count:  # in use in a group, or taken off the map: a new top-level file
            return None, self._next_routes_file(path), ((count, path) if count else None)
        return None, path, None

    def _loaded_routes_layer(self):
        return next((l for l in QgsProject.instance().mapLayers().values()
                     if isinstance(l, QgsVectorLayer) and self._is_routes_layer(l)), None)

    def _open_routes_file(self, path, group):
        """The Drawn routes layer of the file (created if needed), added at the top of the group."""
        loaded = next((l for l in QgsProject.instance().mapLayers().values()
                       if self._is_any_routes_file(l) and self._same_file(l, path)), None)
        if loaded is not None:
            self._upgrade_routes_layer(loaded)
            return loaded
        uri = f"{path}|layername={LAYER_NAME}"
        layer = QgsVectorLayer(uri, "Drawn routes", "ogr")
        if not layer.isValid():
            if not self._create_layer(path):
                return None
            layer = QgsVectorLayer(uri, "Drawn routes", "ogr")
            if not layer.isValid():
                self.warn(f"Could not open {path}.")
                return None
        if layer.listStylesInDatabase()[0] == 0:  # no style saved in the GeoPackage: use ours
            layer.renderer().setSymbol(QgsLineSymbol.createSimple(
                {"color": "255,102,0", "width": "0.8", "capstyle": "round", "joinstyle": "round"}))
            if self._labels_on():
                self._set_length_labels(layer, True)
        self._upgrade_routes_layer(layer)
        add_to(group, layer)
        return layer

    def _routes_in_file(self, path):
        if not os.path.exists(path):
            return 0
        layer = QgsVectorLayer(f"{path}|layername={LAYER_NAME}", "count", "ogr")
        return max(layer.featureCount(), 0) if layer.isValid() else 0

    def _next_routes_file(self, path):
        """A new routes file next to the old one: drawn_routes_2026-10-26.gpkg, ..._2, ..."""
        stem = f"drawn_routes_{datetime.now():%Y-%m-%d}"
        candidate, n = os.path.join(os.path.dirname(path), stem + ".gpkg"), 2
        while os.path.exists(candidate):
            candidate, n = os.path.join(os.path.dirname(path), f"{stem}_{n}.gpkg"), n + 1
        return candidate

    def _upgrade_routes_layer(self, layer):
        """Routes files from earlier versions get the new columns (routes in them are kept).
        The plugin's own style becomes a colour list: one Layers-panel entry per colour,
        labelled with the routes that use it, each route drawn in its own colour and width.
        A style you made yourself is left alone (each route's colour and width is added)."""
        provider = layer.dataProvider()
        missing = [new_field(n, k) for n, k in FIELDS if layer.fields().lookupField(n) < 0]
        if missing and provider.addAttributes(missing):
            layer.updateFields()
        if self._is_our_route_style(layer):
            self._rebuild_route_legend(layer)
            watch_colour_legend(layer, self._rebuild_route_legend)
            return
        renderer = layer.renderer()
        if renderer is None or renderer.type() != "singleSymbol":
            return  # styled by the user (categories, rules...): leave it
        symbol_layer = renderer.symbol().symbolLayer(0)
        properties = symbol_layer.dataDefinedProperties()
        if not properties.isActive(STROKE_COLOR):  # @symbol_color: the layer's own colour
            symbol_layer.setDataDefinedProperty(
                STROKE_COLOR, QgsProperty.fromExpression("coalesce(\"color\", @symbol_color)"))
        if not properties.isActive(STROKE_WIDTH):
            symbol_layer.setDataDefinedProperty(
                STROKE_WIDTH, QgsProperty.fromExpression(f"coalesce(\"width_mm\", {renderer.symbol().width()})"))
        layer.triggerRepaint()

    @staticmethod
    def _is_our_route_style(layer):
        """The colour list, or the plugin's own earlier single orange / per-route-colour style."""
        if is_colour_legend(layer):
            return True
        renderer = layer.renderer()
        if renderer is None or renderer.type() != "singleSymbol":
            return False
        symbol_layer = renderer.symbol().symbolLayer(0)
        if symbol_layer.layerType() != "SimpleLine":
            return False
        properties = symbol_layer.dataDefinedProperties()
        colour_rule = properties.property(STROKE_COLOR).expressionString() if properties.isActive(STROKE_COLOR) else ""
        return renderer.symbol().color().name() == DEFAULT_COLOR or colour_rule == 'coalesce("color", @symbol_color)'

    def _rebuild_route_legend(self, layer):
        build_colour_legend(layer, self._route_symbol, DEFAULT_COLOR)

    @staticmethod
    def _route_symbol(colour):
        symbol = QgsLineSymbol.createSimple({"color": QColor(colour).name(), "width": str(DEFAULT_WIDTH),
                                             "capstyle": "round", "joinstyle": "round"})
        symbol.symbolLayer(0).setDataDefinedProperty(
            STROKE_WIDTH, QgsProperty.fromExpression(f'coalesce("width_mm", {DEFAULT_WIDTH})'))
        return symbol

    def _project_layers_added(self, layers):
        """A Drawn routes layer added to the project (e.g. a project opened) gets the colour list."""
        for layer in layers:
            if self._is_any_routes_file(layer):
                self._upgrade_routes_layer(layer)

    @staticmethod
    def _is_any_routes_file(layer):
        if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
            return False
        return QgsProviderRegistry.instance().decodeUri("ogr", layer.source()).get("layerName") == LAYER_NAME

    def _labels_on(self):
        return self.settings.value(KEY_LABELS, True, type=bool)

    def _set_length_labels(self, layer, on):
        """Label each saved route with its length in metres, along the line."""
        if on:
            label = QgsPalLayerSettings()
            label.fieldName = LENGTH_LABEL
            label.isExpression = True
            try:
                label.placement = Qgis.LabelPlacement.Line
            except AttributeError:  # QGIS < 3.26
                label.placement = QgsPalLayerSettings.Line
            text = QgsTextFormat()
            text.setSize(9)
            text.setColor(QColor(150, 55, 0))
            buffer = QgsTextBufferSettings()
            buffer.setEnabled(True)
            buffer.setSize(1)
            buffer.setColor(QColor(255, 255, 255))
            text.setBuffer(buffer)
            label.setFormat(text)
            layer.setLabeling(QgsVectorLayerSimpleLabeling(label))
        layer.setLabelsEnabled(on)
        layer.triggerRepaint()

    def _create_layer(self, path):
        if os.path.exists(path):
            names = [d.name() for d in QgsProviderRegistry.instance().querySublayers(path)]
            if LAYER_NAME in names:  # it exists but would not open: never overwrite it
                self.warn(f"'{LAYER_NAME}' in {path} could not be opened; nothing was saved.")
                return False
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
        writer = QgsVectorFileWriter.create(path, fields, LINESTRING, WGS84,
                                            QgsProject.instance().transformContext(), options)
        failed = writer.hasError() != QgsVectorFileWriter.WriterError.NoError
        message = writer.errorMessage()
        del writer
        if failed:
            self.warn(f"Could not create {path}: {message}")
        return not failed

    def _next_name(self, layer):
        return f"Route {layer.featureCount() + 1}"

    def _layers_removed(self, layer_ids):
        if self.road_layer_id in layer_ids:
            self.road_layer_id = None
            self.router.clear()

    # ----- menu ----------------------------------------------------------
    def _roads_toggled(self, checked):
        self.settings.setValue(KEY_ROADS, checked)
        if self.iface.mapCanvas().mapTool() is self.tool:
            self.tool._status()

    def _labels_toggled(self, checked):
        self.settings.setValue(KEY_LABELS, checked)
        for layer in QgsProject.instance().mapLayers().values():  # every group's Drawn routes
            if self._is_any_routes_file(layer):
                self._set_length_labels(layer, checked)

    def _choose_roads(self, layer_id, *_):
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None:
            return
        self.road_layer_id = layer_id
        self.roads_memory.save(layer)  # remembered for the next sessions too
        self.private_roads = None
        self.router.clear()

    def _choose_gpkg(self, *_):
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(), "Save drawn routes in", self._gpkg_path(),
            "GeoPackage (*.gpkg)", options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            self.settings.setValue(KEY_GPKG, path)
            # opened at once in the selected group, so the next save there adds to it
            self._open_routes_file(path, selected_group(self.iface))

    def _fill_menu(self):
        self.menu.clear()
        self.menu.addSection("Road layer for Follow Roads")
        current = self.road_layer()
        layers = [l for l in self._line_layers() if not self._is_routes_layer(l)]
        if current is not None and current is self.private_roads:
            action = self.menu.addAction(f"{current.name()} (remembered file, not on the map)")
            action.setCheckable(True)
            action.setChecked(True)
        elif not layers:
            self.menu.addAction("No line layers in the project").setEnabled(False)
        for layer in layers:
            action = self.menu.addAction(layer.name())
            action.setCheckable(True)
            action.setChecked(layer is current)
            action.triggered.connect(partial(self._choose_roads, layer.id()))
        self.menu.addSection("Saving")
        path = self._gpkg_path().replace(os.path.expanduser("~"), "~")
        self.menu.addAction(f"Save routes in {path}…").triggered.connect(self._choose_gpkg)
        labels = self.menu.addAction("Show length (m) on saved routes")
        labels.setCheckable(True)
        labels.setChecked(self._labels_on())
        labels.toggled.connect(self._labels_toggled)
        self.menu.addAction("(A line layer in edit mode and selected gets the route instead)"
                            ).setEnabled(False)

    # ----- import / export -------------------------------------------------
    def pick_file(self, title, filters, save=False, start=""):
        if save:
            path, _ = QFileDialog.getSaveFileName(self.iface.mainWindow(), title, start, filters)
        else:
            path, _ = QFileDialog.getOpenFileName(self.iface.mainWindow(), title, start, filters)
        return path

    def ask_yes_no(self, title, text):
        """True, False, or None when cancelled."""
        answer = QMessageBox.question(
            self.iface.mainWindow(), title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Cancel:
            return None
        return answer == QMessageBox.StandardButton.Yes

    def ask_columns(self, headers):
        """Let the user pick the coordinate columns when they cannot be detected."""
        window = self.iface.mainWindow()
        together = "(latitude and longitude are in one column)"
        lat, ok = QInputDialog.getItem(window, "Import CSV as pins",
                                       "Latitude/longitude columns were not recognised.\n"
                                       "Which column holds the latitude?", headers + [together], 0, False)
        if not ok:
            return None
        if lat == together:
            column, ok = QInputDialog.getItem(window, "Import CSV as pins",
                                              "Which column holds latitude, longitude?", headers, 0, False)
            return ("single", headers.index(column), None, f"column '{column}'") if ok else None
        lon, ok = QInputDialog.getItem(window, "Import CSV as pins", "Which column holds the longitude?",
                                       headers, min(headers.index(lat) + 1, len(headers) - 1), False)
        if not ok:
            return None
        return "pair", headers.index(lat), headers.index(lon), f"columns '{lat}' and '{lon}'"

    def _import_folder(self):
        return os.path.join(os.path.dirname(self._gpkg_path()), "Imported")

    def _zoom_to(self, layers):
        canvas = self.iface.mapCanvas()
        extent = None
        for layer in layers:
            layer.updateExtents()
            if layer.featureCount() == 0:
                continue
            box = QgsCoordinateTransform(layer.crs(), canvas.mapSettings().destinationCrs(),
                                         QgsProject.instance()).transformBoundingBox(layer.extent())
            if extent is None:
                extent = QgsRectangle(box)
            else:
                extent.combineExtentWith(box)
        if extent is None:
            return
        if extent.width() == 0 and extent.height() == 0:  # a single point
            canvas.setCenter(extent.center())
            canvas.zoomScale(5000)
        else:
            extent.grow(max(extent.width(), extent.height()) * 0.05)
            canvas.setExtent(extent)
        canvas.refresh()

    def _import_start(self):
        return self.settings.value(KEY_IMPORT_FROM, os.path.join(os.path.expanduser("~"), "Documents"))

    def import_csv(self, *_):
        path = self.pick_file("Import CSV as pins", "CSV / text (*.csv *.txt *.tsv);;All files (*)",
                              start=self._import_start())
        if not path:
            return
        self.settings.setValue(KEY_IMPORT_FROM, os.path.dirname(path))
        try:
            headers, rows, _, _ = read_csv(path)
        except OSError as error:
            self.warn(f"Could not read {os.path.basename(path)}: {error}")
            return
        if not headers or not rows:
            self.warn(f"{os.path.basename(path)} has no data rows.")
            return
        how = guess(headers, rows) or self.ask_columns(headers)
        if how is None:
            return
        stem = os.path.splitext(os.path.basename(path))[0]
        out = unique_path(self._import_folder(), stem)
        try:
            written, skipped = write_pins(out, "pins", headers, rows, how)
        except OSError as error:
            self.warn(f"Could not write {out}: {error}")
            return
        if not written:
            os.remove(out)
            self.warn(f"No row of {os.path.basename(path)} had a valid latitude/longitude in {how[3]}.")
            return
        layer = QgsVectorLayer(f"{out}|layername=pins", stem, "ogr")
        layer.renderer().setSymbol(pushpin_symbol("#1f5fa8", 6.0))
        label_index = name_field(headers)
        if label_index is not None:
            self._label(layer, headers[label_index])
        QgsProject.instance().addMapLayer(layer, False)  # at the top of the Layers panel
        QgsProject.instance().layerTreeRoot().insertLayer(0, layer)
        self._zoom_to([layer])
        note = ""
        if skipped:
            shown = ", ".join(str(n) for n in skipped[:10]) + ("…" if len(skipped) > 10 else "")
            note = f" {len(skipped)} row(s) had no valid latitude/longitude and were skipped (data rows {shown})."
        self.iface.messageBar().pushMessage(
            "Route Drawer", f"Imported {written} pins from {os.path.basename(path)} using {how[3]}.{note} "
            f"Saved as {out}.", Qgis.MessageLevel.Success if not skipped else Qgis.MessageLevel.Warning, 12)

    def _label(self, layer, field):
        label = QgsPalLayerSettings()
        label.fieldName = field
        text = QgsTextFormat()
        text.setSize(8.5)
        buffer = QgsTextBufferSettings()
        buffer.setEnabled(True)
        buffer.setSize(1)
        buffer.setColor(QColor(255, 255, 255))
        text.setBuffer(buffer)
        label.setFormat(text)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(label))
        layer.setLabelsEnabled(True)

    def import_kmz(self, *_):
        path = self.pick_file("Import KMZ / KML", "Google Earth (*.kmz *.kml);;All files (*)",
                              start=self._import_start())
        if not path:
            return
        self.settings.setValue(KEY_IMPORT_FROM, os.path.dirname(path))
        stem = os.path.splitext(os.path.basename(path))[0]
        out = unique_path(self._import_folder(), stem)
        progress = QProgressDialog(f"Reading {os.path.basename(path)}…", "Cancel", 0, 100,
                                   self.iface.mainWindow())
        progress.setWindowTitle("Import KMZ / KML")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)

        def on_progress(fraction):
            progress.setValue(int(fraction * 100))
            QApplication.processEvents()
            return not progress.wasCanceled()

        try:
            counts = import_kml(path, out, on_progress)
        except Cancelled:
            self.iface.messageBar().pushMessage("Route Drawer", "Import cancelled.", Qgis.MessageLevel.Info, 4)
            return
        except (OSError, ValueError, ET.ParseError) as error:
            self.warn(f"Could not import {os.path.basename(path)}: {error}")
            return
        finally:
            progress.close()
        if not any(counts.values()):
            self.warn(f"{os.path.basename(path)} has no placemarks with a location.")
            return
        group = QgsProject.instance().layerTreeRoot().insertGroup(0, stem)
        layers = []
        for kind in ("points", "lines", "polygons"):
            if counts.get(kind):
                layer = QgsVectorLayer(f"{out}|layername={kind}", f"{stem} ({kind})", "ogr")
                self._style_kml(layer, kind)
                QgsProject.instance().addMapLayer(layer, False)
                group.addLayer(layer)
                layers.append(layer)
        self._zoom_to(layers)
        parts = ", ".join(f"{counts[k]:,} {k}" for k in ("points", "lines", "polygons") if counts.get(k))
        self.iface.messageBar().pushMessage(
            "Route Drawer", f"Imported {parts} from {os.path.basename(path)} with all their data. "
            f"Use the Info tool to see it and open links. Saved as {out}.", Qgis.MessageLevel.Success, 12)

    def _style_kml(self, layer, kind):
        """Colours from the KML styles (kml_color), orange where a placemark has none."""
        color = QgsProperty.fromExpression(KML_COLOR)
        if kind == "points":
            symbol = QgsMarkerSymbol.createSimple({"name": "circle", "size": "2.6", "outline_color": "255,255,255"})
            symbol.symbolLayer(0).setDataDefinedProperty(FILL_COLOR, color)
        elif kind == "lines":
            symbol = QgsLineSymbol.createSimple({"width": "0.6"})
            symbol.symbolLayer(0).setDataDefinedProperty(STROKE_COLOR, color)
        else:
            symbol = layer.renderer().symbol().clone()
            symbol.setOpacity(0.55)
            symbol.symbolLayer(0).setDataDefinedProperty(FILL_COLOR, color)
        layer.renderer().setSymbol(symbol)

    def export_selection(self):
        """(name, vector layers) for what is highlighted in the Layers panel: a group
        (all its layers, also in sub-groups), several layers, or one layer."""
        layers, groups = [], []
        view = self.iface.layerTreeView() if hasattr(self.iface, "layerTreeView") else None
        nodes = view.selectedNodes() if view is not None else []
        if not nodes and view is not None and view.currentNode() is not None:
            nodes = [view.currentNode()]  # highlighted but not selected
        # the root of the layer list is what QGIS reports when nothing is highlighted:
        # it must not mean "export every layer of the project"
        nodes = [n for n in nodes if n.parent() is not None]
        for node in nodes:
            if QgsLayerTree.isGroup(node):
                groups.append(node.name())
                layers.extend(child.layer() for child in node.findLayers())
            elif QgsLayerTree.isLayer(node):
                layers.append(node.layer())
        if not layers and self.iface.activeLayer() is not None:
            layers = [self.iface.activeLayer()]
        unique = []
        for layer in layers:
            if isinstance(layer, QgsVectorLayer) and layer.isValid() and layer not in unique:
                unique.append(layer)
        if len(groups) == 1 and len(nodes) == 1:
            name = groups[0]
        elif len(unique) == 1:
            name = unique[0].name()
        else:
            name = groups[0] if groups else "layers"
        return name, unique

    def export_layer(self, kind, *_):
        name, layers = self.export_selection()
        if kind == "kmz":
            layers = [l for l in layers if l.isSpatial()]  # tables without a location have no place in a KMZ
        if not layers:
            self.warn("Select a layer or a group to export in the Layers panel first.")
            return
        only_selected = False
        selected = sum(l.selectedFeatureCount() for l in layers)
        if selected:
            with_selection = [l for l in layers if l.selectedFeatureCount()]
            note = ("" if len(layers) == 1 else
                    f" (in {len(with_selection)} of the {len(layers)} layers; layers without a "
                    "selection are then left out)")
            only_selected = self.ask_yes_no(
                "Export", f"Export only the {selected:,} selected features{note}?\n\n"
                f"No exports all features of '{name}'.")
            if only_selected is None:
                return
            if only_selected:
                layers = with_selection
        extension, label, filters = ((".kmz", "KMZ", "Google Earth KMZ (*.kmz)") if kind == "kmz"
                                     else (".xlsx", "Excel", "Excel workbook (*.xlsx)"))
        start = os.path.join(self.settings.value("route_drawer/export_folder",
                                                 os.path.join(os.path.expanduser("~"), "Documents")),
                             name + extension)
        path = self.pick_file(f"Export '{name}' to {label}", filters, save=True, start=start)
        if not path:
            return
        if not path.lower().endswith(extension):
            path += extension
        self.settings.setValue("route_drawer/export_folder", os.path.dirname(path))
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            if kind == "kmz":
                counts = export_kmz(layers, path, bool(only_selected), title=name)
                parts = [f"{l.name()}: {counts.get(l.id(), 0):,}" for l in layers]
                extra = ("Each layer is a folder in it. " if len(layers) > 1 else "") + \
                    "Open it in Google Earth; every attribute is in the placemark data."
            else:
                sheets = export_xlsx(layers, path, bool(only_selected))
                parts = [f"{sheet}: {rows:,}" for sheet, rows, _ in sheets]
                added = sorted({c for _, _, cols in sheets for c in cols})
                extra = ("One sheet per layer. " if len(layers) > 1 else "") + \
                    (f"Added columns: {', '.join(added)}." if added else "")
        except OSError as error:
            self.warn(f"Could not export '{name}': {error}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        what = f"{len(layers)} layers of '{name}'" if len(layers) > 1 else f"'{name}'"
        self.iface.messageBar().pushMessage(
            "Route Drawer", f"Exported {what} to {path} ({' · '.join(parts)}). {extra}",
            Qgis.MessageLevel.Success, 12)
