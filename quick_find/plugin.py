"""Quick Find: a toolbar box that finds a value in any layer and zooms to it."""
import os
from functools import partial

from qgis.PyQt.QtCore import QProcess, Qt, QUrl
from qgis.PyQt.QtGui import QCursor, QDesktopServices, QIcon, QKeySequence
from qgis.PyQt.QtWidgets import (QApplication, QFileDialog, QInputDialog, QLineEdit,
                                 QMenu, QMessageBox, QPushButton, QToolButton)
try:  # Qt6 (QGIS 4) keeps QAction/QShortcut in QtGui; Qt5 (QGIS 3) in QtWidgets
    from qgis.PyQt.QtGui import QAction, QShortcut
except ImportError:
    from qgis.PyQt.QtWidgets import QAction, QShortcut
from qgis.core import (Qgis, QgsCoordinateTransform, QgsCsException,
                       QgsGeometry, QgsMessageLog, QgsPointXY, QgsProject,
                       QgsSettings, QgsVectorLayer, QgsVectorLayerUtils,
                       QgsWkbTypes)
from qgis.gui import QgsMessageBar

from .capture import CaptureTool
from .coords import (format_lat_lon, lat_lon_fields, name_field, parse_lat_lon,
                     street_view_url)
from .layer_filter import FilterPanel
from .locate import IS_MAC, MAC_SETUP, Locator
from .pin import WGS84, PinItem
from .remember import RememberedLayer, project_layer
from .save_dialog import COLOR_COLUMNS, REMARKS_COLUMNS, ask as ask_save_window, find_column
from .saved_pins import DEFAULT_COLOR as PIN_COLOR, DEFAULT_SIZE as PIN_SIZE
from .saved_pins import DEFAULT_GPKG as DEFAULT_PINS_GPKG, TITLE as PINS_TITLE, open_or_create, save_point
from .saved_pins import count_in_file, is_any_pins_file, is_pins_layer, next_file, upgrade as upgrade_pins_layer
from .save_group import group_file, is_top, layer_in, selected_group, where
from .search import LARGE_LAYER, MAX_MATCHES, SearchCache, is_large, search, snippet
from .updater import Updater

MENU = "&Quick Find"
SHORTCUT = "Ctrl+F"  # Qt maps Ctrl to Cmd on a Mac; change it in Settings > Keyboard Shortcuts
POINT_SCALE = 2500  # zoom in to at least 1:2,500 on a point
KEY_HIDDEN = "quick_find/include_hidden_layers"
KEY_SELECT = "quick_find/select_found"
KEY_LARGE = "quick_find/include_large_layers"
KEY_PINS = "quick_find/saved_pins_gpkg"
KEY_PIN_COLOR = "quick_find/last_pin_color"
KEY_PIN_SIZE = "quick_find/last_pin_height"  # pushpin height (the old dot size is not reused)
try:
    POINT = Qgis.GeometryType.Point
except AttributeError:  # QGIS < 3.30
    POINT = QgsWkbTypes.PointGeometry


class QuickFindPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.matches = []
        self.current = -1
        self.query = None
        self.pin = None
        self.pin_source = None  # "search", "capture" or "my location"
        self.last_layer_id = None  # point layer the pin was last added to
        self.pin_layer_memory = RememberedLayer("quick_find/pin_layer")  # ...also in later sessions
        self.settings = QgsSettings()
        self.cache = SearchCache()  # ID/name values per layer, kept between searches

    # ----- setup -------------------------------------------------------
    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))

        self.box = QLineEdit()
        self.box.setPlaceholderText("Find… e.g. MIN164 or 7.1193, 125.6281")
        self.box.setClearButtonEnabled(True)
        self.box.setMinimumWidth(220)
        self.box.setToolTip(
            "Type a value and press Enter to zoom to it.\n"
            "Searches every text field of the visible layers.\n"
            "Press Enter again for the next match.\n"
            "Or type/paste a latitude, longitude to drop a temporary pin there.\n"
            "Cmd+F (Mac) / Ctrl+F (Windows) jumps here and back; Esc returns to the map.")
        self.box.returnPressed.connect(self.on_enter)
        self.box.textEdited.connect(self._forget)
        self.escape = QShortcut(QKeySequence("Esc"), self.box)
        self.escape.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.escape.activated.connect(self.leave_box)

        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._fill_menu)
        self.button = QToolButton()
        self.button.setIcon(icon)
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setToolTip("Matches and options")
        self.button.setMenu(self.menu)

        self.capture_tool = CaptureTool(self.iface.mapCanvas(), self.capture_point)
        self.capture_action = QAction(QIcon(os.path.join(os.path.dirname(__file__), "capture.svg")),
                                      "Capture lat/long", self.iface.mainWindow())
        self.capture_action.setObjectName("QuickFindCapture")
        self.capture_action.setCheckable(True)
        self.capture_action.setToolTip("Capture lat/long: click the map to drop a pin there and copy its "
                                       "coordinates; add it to a point layer from the ▾ menu")
        self.capture_action.triggered.connect(lambda checked: self.switch_tool(self.capture_tool, checked))
        self.capture_tool.setAction(self.capture_action)

        self.street_action = QAction(QIcon(os.path.join(os.path.dirname(__file__), "streetview.svg")),
                                     "Open in Street View", self.iface.mainWindow())
        self.street_action.setObjectName("QuickFindStreetView")
        self.street_action.setToolTip("Open Google Street View at the pin, in your browser "
                                      "(only the pin's coordinates are sent to Google)")
        self.street_action.setEnabled(False)  # needs a pin
        self.street_action.triggered.connect(self.open_street_view)

        here = os.path.dirname(__file__)
        self.locator = Locator(self.iface.mainWindow())
        self.locator.found.connect(self._located)
        self.locator.failed.connect(self._locate_failed)
        self.locate_action = QAction(QIcon(os.path.join(here, "locate.svg")), "My location",
                                     self.iface.mainWindow())
        self.locate_action.setObjectName("QuickFindMyLocation")
        self.locate_action.setToolTip(
            "My location: drop a pin where this computer is, using "
            + ("the Shortcuts app (set up once)" if IS_MAC else "Windows Location"))
        self.locate_action.triggered.connect(self.find_my_location)

        self.save_action = QAction(QIcon(os.path.join(here, "savepin.svg")), "Save pin",
                                   self.iface.mainWindow())
        self.save_action.setObjectName("QuickFindSavePin")
        self.save_action.setToolTip(f"Save pin: keep the pin as a point in the '{PINS_TITLE}' layer, "
                                    "with its name and latitude/longitude")
        self.save_action.setEnabled(False)  # needs a pin
        self.save_action.triggered.connect(self.save_pin)

        self.toolbar = self.iface.addToolBar("Quick Find")
        self.toolbar.setObjectName("QuickFindToolbar")
        self.toolbar.addWidget(self.box)
        self.toolbar.addAction(self.capture_action)
        self.toolbar.addAction(self.locate_action)
        self.toolbar.addAction(self.street_action)
        self.toolbar.addAction(self.save_action)

        self.filter_panel = None  # created when first opened
        self.filter_action = QAction(QIcon(os.path.join(here, "filter.svg")), "Filter layers",
                                     self.iface.mainWindow())
        self.filter_action.setObjectName("QuickFindFilterLayers")
        self.filter_action.setCheckable(True)
        self.filter_action.setToolTip("Filter layers: show only the features with a chosen value in a "
                                      "chosen column (e.g. one AN_Loop_Name), on every layer that has it")
        self.filter_action.triggered.connect(self.show_filter_panel)  # clicks only
        self.toolbar.addAction(self.filter_action)
        self.toolbar.addWidget(self.button)

        self.focus_action = QAction(icon, "Quick Find", self.iface.mainWindow())
        self.focus_action.setObjectName("QuickFindFocus")
        self.focus_action.setToolTip("Jump to the Quick Find box, or back to the map")
        self.focus_action.triggered.connect(self.toggle_box)
        self.iface.registerMainWindowAction(self.focus_action, SHORTCUT)
        self.iface.addPluginToMenu(MENU, self.focus_action)
        for action in (self.capture_action, self.locate_action, self.street_action, self.save_action,
                       self.filter_action):
            self.iface.registerMainWindowAction(action, "")  # assign a key in Settings if wanted
            self.iface.addPluginToMenu(MENU, action)
        self._take_shortcut()
        QgsProject.instance().layersWillBeRemoved.connect(self._layers_removed)
        QgsProject.instance().layersAdded.connect(self._project_layers_added)
        self._project_layers_added(QgsProject.instance().mapLayers().values())
        self.updater = Updater(self.iface, MENU)  # update messages + Check for updates…
        self.updater.start()

    def unload(self):
        self.updater.stop()
        QgsProject.instance().layersWillBeRemoved.disconnect(self._layers_removed)
        QgsProject.instance().layersAdded.disconnect(self._project_layers_added)
        self.cache.clear()
        self.remove_pin()
        if self.iface.mapCanvas().mapTool() is self.capture_tool:
            self.iface.mapCanvas().unsetMapTool(self.capture_tool)
        self._give_back_shortcut()
        if self.filter_panel is not None:  # filters already set stay, as normal layer filters
            self.filter_panel.cleanup()
            self.iface.removeDockWidget(self.filter_panel)
            self.filter_panel.deleteLater()
            self.filter_panel = None
        for action in (self.focus_action, self.capture_action, self.locate_action,
                       self.street_action, self.save_action, self.filter_action):
            self.iface.unregisterMainWindowAction(action)
            self.iface.removePluginMenu(MENU, action)
        self.iface.mainWindow().removeToolBar(self.toolbar)
        self.toolbar.deleteLater()
        self.menu.deleteLater()

    def _take_shortcut(self):
        """QGIS binds Ctrl+F to Layer > Filter…, and Qt fires neither action
        when two share a key. Take the key from the other action while this
        plugin is loaded; unload gives it back. Nothing is saved to settings."""
        self.taken = []
        key = self.focus_action.shortcut()
        if key.isEmpty():
            return
        for action in self.iface.mainWindow().findChildren(QAction):
            if action is not self.focus_action and action.shortcut() == key:
                self.taken.append((action, QKeySequence(action.shortcut())))
                action.setShortcut(QKeySequence())
                QgsMessageLog.logMessage(
                    f"{key.toString(QKeySequence.SequenceFormat.NativeText)} now opens Quick Find; "
                    f"'{action.text().replace('&', '')}' is still in its menu.",
                    "Quick Find", Qgis.MessageLevel.Info)

    def _give_back_shortcut(self):
        for action, key in self.taken:
            try:
                if action.shortcut().isEmpty():
                    action.setShortcut(key)
            except RuntimeError:  # action already deleted
                pass
        self.taken = []

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

    def show_filter_panel(self, visible=True):
        if self.filter_panel is None:
            if not visible:
                return
            self.filter_panel = FilterPanel(self.iface, self.iface.mainWindow())
            self.filter_panel.visibilityChanged.connect(self.filter_action.setChecked)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.filter_panel)
        self.filter_panel.setVisible(visible)
        if visible:
            self.filter_panel.raise_()

    def toggle_box(self):
        if self.box.hasFocus():
            self.leave_box()
        else:
            self.focus_box()

    def focus_box(self):
        self.toolbar.show()
        self.box.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.box.selectAll()

    def leave_box(self):
        self.iface.mapCanvas().setFocus(Qt.FocusReason.ShortcutFocusReason)

    # ----- searching ---------------------------------------------------
    def _option(self, key, default):
        return self.settings.value(key, default, type=bool)

    def _set_option(self, key, checked):
        self.settings.setValue(key, checked)
        self._forget()

    def _forget(self, *_):
        self.matches, self.current, self.query = [], -1, None
        self.button.setText("")
        if not self.box.text().strip():  # clearing the box also removes the pin
            self.remove_pin()

    def _layers(self):
        """(layers to search, large layers skipped)."""
        include_hidden = self._option(KEY_HIDDEN, False)
        include_large = self._option(KEY_LARGE, False)
        layers, skipped = [], []
        for node in QgsProject.instance().layerTreeRoot().findLayers():
            layer = node.layer()
            if (isinstance(layer, QgsVectorLayer) and layer.isValid() and layer.isSpatial()
                    and (include_hidden or node.isVisible())):
                (skipped if is_large(layer) and not include_large else layers).append(layer)
        return layers, skipped

    def _project_layers_added(self, layers):
        """A Saved pins layer added to the project (e.g. a project opened) gets the colour list."""
        for layer in layers:
            if is_any_pins_file(layer):
                upgrade_pins_layer(layer)

    def _layers_removed(self, layer_ids):
        for layer_id in layer_ids:
            self.cache.clear(layer_id)

    def on_enter(self):
        text = self.box.text().strip()
        if not text:
            return
        coordinate = parse_lat_lon(text)
        if coordinate:
            self.show_coordinate(*coordinate)
            return
        if text == self.query and self.matches:
            self.show_match((self.current + 1) % len(self.matches))
            return
        self.remove_pin()
        self._run_search(text, deep=None)

    def _run_search(self, text, deep, *_):
        layers, skipped = self._layers()
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            self.matches, deep_skipped = search(layers, text, cache=self.cache, deep=deep,
                                                on_second_pass=self._note_slow_pass,
                                                on_read=self._note_reading)
        finally:
            QApplication.restoreOverrideCursor()
        self.query, self.current = text, -1
        skipped_note = ""
        if skipped:
            names = ", ".join(f"{l.name()} ({l.featureCount():,})" for l in skipped)
            skipped_note = (f" Skipped large layer(s): {names}; turn on 'Search large layers "
                            "too' in the ▾ menu to include them.")
        if self.matches:
            self.show_match(0, note=skipped_note)
            return
        self.button.setText("0")
        where = "layers" if self._option(KEY_HIDDEN, False) else "visible layers"
        if deep_skipped:
            self._offer_deep_search(text, len(layers), where, skipped_note)
        else:
            self.iface.messageBar().pushMessage(
                "Quick Find", f"No match for '{text}' in {len(layers)} {where}.{skipped_note}",
                Qgis.MessageLevel.Warning, 6)

    def _offer_deep_search(self, text, count, where, skipped_note):
        """Nothing in ID/name fields: offer the slow all-fields search instead of running it."""
        item = QgsMessageBar.createMessage(
            "Quick Find", f"No ID or name matches '{text}' in {count} {where}. Remarks, "
            f"descriptions and other fields were not searched because that is slow here.{skipped_note}")
        button = QPushButton("Search all fields")
        button.clicked.connect(partial(self._run_search, text, True))
        button.clicked.connect(partial(self.iface.messageBar().popWidget, item))
        item.layout().addWidget(button)
        self.iface.messageBar().pushWidget(item, Qgis.MessageLevel.Warning, 15)

    def _note_reading(self, layer):
        if layer.featureCount() > 5000:
            self.iface.statusBarIface().showMessage(
                f"Quick Find: reading names and IDs of '{layer.name()}' ({layer.featureCount():,} "
                "features); only the first search of a layer waits for this…", 10000)
            QApplication.processEvents()

    def show_coordinate(self, lat, lon, source="search", note=""):
        """Zoom to a WGS84 latitude/longitude and drop a temporary pin there."""
        self.matches, self.current, self.query = [], -1, None
        self.button.setText("")
        canvas = self.iface.mapCanvas()
        try:
            point = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(),
                                           QgsProject.instance()).transform(QgsPointXY(lon, lat))
        except QgsCsException:
            self._warn(f"{format_lat_lon(lat, lon)} cannot be shown in the map's CRS.")
            return
        canvas.setCenter(point)
        if canvas.scale() > POINT_SCALE:
            canvas.zoomScale(POINT_SCALE)
        canvas.refresh()
        text = self._drop_pin(lat, lon, source)
        self.iface.statusBarIface().showMessage(
            f"Quick Find: pin at {text} (lat, long WGS84){note}. It is temporary: Save pin keeps it; "
            "clearing the box or searching again removes it.", 10000)

    def capture_point(self, map_point):
        """Capture tool click: drop the pin there and copy its lat/long."""
        canvas = self.iface.mapCanvas()
        try:
            wgs = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), WGS84,
                                         QgsProject.instance()).transform(map_point)
        except QgsCsException:
            self._warn("That point cannot be converted to latitude/longitude.")
            return
        self.matches, self.current, self.query = [], -1, None
        self.button.setText("")
        text = self._drop_pin(wgs.y(), wgs.x(), "capture")
        self.box.setText(text)
        QApplication.clipboard().setText(text)
        self.iface.statusBarIface().showMessage(
            f"Quick Find: captured {text} (copied). Click again to move the pin; Save pin keeps it.",
            10000)

    def _drop_pin(self, lat, lon, source):
        self.remove_pin()
        text = format_lat_lon(lat, lon)
        self.pin = PinItem(self.iface.mapCanvas(), lat, lon, text)
        self.pin_source = source
        self.street_action.setEnabled(True)
        self.save_action.setEnabled(True)
        return text

    def remove_pin(self, *_):
        if self.pin is not None:
            self.iface.mapCanvas().scene().removeItem(self.pin)
            self.pin = None
            self.pin_source = None
            self.street_action.setEnabled(False)
            self.save_action.setEnabled(False)

    def open_street_view(self, *_):
        if self.pin is None:
            return
        QDesktopServices.openUrl(QUrl(street_view_url(self.pin.point.y(), self.pin.point.x())))
        self.iface.statusBarIface().showMessage(
            f"Quick Find: opened Street View at {self.pin.text} in your browser. If there is no "
            "imagery there, Google shows the nearest available view or the map.", 10000)

    # ----- my location ---------------------------------------------------
    def find_my_location(self, *_):
        if self.locator.busy():
            return
        self.locate_action.setEnabled(False)
        self.iface.statusBarIface().showMessage("Quick Find: finding your location…", 30000)
        self.locator.locate()

    def _located(self, lat, lon, accuracy):
        self.locate_action.setEnabled(True)
        note = f", about ±{accuracy:,.0f} m" if accuracy > 0 else ""
        self.show_coordinate(lat, lon, source="my location", note=f" — your location{note}, copied")
        self.box.setText(format_lat_lon(lat, lon))
        QApplication.clipboard().setText(format_lat_lon(lat, lon))

    def _locate_failed(self, message, setup):
        self.locate_action.setEnabled(True)
        self.iface.statusBarIface().clearMessage()
        if setup and IS_MAC:
            self.show_mac_setup(message)
        else:
            self.iface.messageBar().pushMessage("Quick Find", message, Qgis.MessageLevel.Warning, 12)

    def show_mac_setup(self, message):
        box = QMessageBox(QMessageBox.Icon.Information, "My location on a Mac",
                          f"{message}\n\n{MAC_SETUP}", QMessageBox.StandardButton.Close,
                          self.iface.mainWindow())
        open_button = box.addButton("Open Shortcuts", QMessageBox.ButtonRole.ActionRole)
        box.exec()
        if box.clickedButton() is open_button:
            QProcess.startDetached("open", ["-a", "Shortcuts"])

    # ----- saving pins ----------------------------------------------------
    def _pins_path(self):
        return self.settings.value(KEY_PINS, DEFAULT_PINS_GPKG)

    def save_pin(self, *_):
        """Keep the pin as a point in the Saved pins layer of the group selected in the
        Layers panel (a new file for that group when it has none yet)."""
        pin = self.pin
        if pin is None:
            return
        group = selected_group(self.iface)
        layer, path, kept = self._pins_target(group)
        if layer is not None:
            default_name, note = f"Pin {layer.featureCount() + 1}", f"Adds to '{layer.name()}'{where(group)}."
        else:
            default_name, note = "Pin 1", f"Starts a new file{where(group)}: {os.path.basename(path)}."
        answer = self.ask_save(f"Save pin at {pin.text}", default_name, remarks=True,
                               color=self.settings.value(KEY_PIN_COLOR, PIN_COLOR),
                               size=self.settings.value(KEY_PIN_SIZE, PIN_SIZE, type=float), note=note)
        if answer is None:
            return  # cancelled: the pin stays, and no file was made
        name = answer["name"]
        self.settings.setValue(KEY_PIN_COLOR, answer["color"])  # the defaults next time
        self.settings.setValue(KEY_PIN_SIZE, answer["size"])
        new_file = layer is None
        if new_file:
            layer, error = open_or_create(path, group)
            if layer is None:
                self._warn(error)
                return
            if is_top(group):
                self.settings.setValue(KEY_PINS, path)
        if not save_point(layer, pin.point.y(), pin.point.x(), name, self.pin_source or "",
                          answer["remarks"], answer["color"], answer["size"]):
            self._warn(f"Could not save the pin to '{layer.name()}'.")
            return
        text = pin.text
        self.remove_pin()
        self.iface.mapCanvas().refresh()
        message = f"Saved '{name}' ({text}) to '{layer.name()}'{where(group)}"
        if new_file:
            message += f", a new file: {os.path.basename(path)}"
        if kept:  # its layer was taken off the map: you are starting afresh; the old file is kept
            message += (f". The previous file ({kept[0]} pin(s)) is kept: {kept[1]} "
                        "(⚙ menu > Keep saved pins in… opens it again)")
        self.iface.messageBar().pushMessage("Quick Find", message + ".", Qgis.MessageLevel.Success,
                                            12 if new_file else 5)

    def _pins_target(self, group):
        """(the group's Saved pins layer, None, None), or (None, a new file for it, and
        (pins, file) of a previous top-level file whose layer was taken off the map, or None)."""
        path = self._pins_path()
        layer = layer_in(group, lambda l: is_pins_layer(l, path)) or layer_in(group, is_any_pins_file)
        if layer is not None:
            upgrade_pins_layer(layer)
            return layer, None, None
        if not is_top(group):
            return None, group_file(os.path.dirname(path), "saved_pins", group), None
        loaded = any(is_pins_layer(l, path) for l in QgsProject.instance().mapLayers().values())
        count = 0 if loaded else count_in_file(path)
        if loaded or count:  # in use in a group, or taken off the map: a new top-level file
            return None, next_file(path), ((count, path) if count else None)
        return None, path, None

    def _choose_pins_file(self, *_):
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(), "Keep saved pins in", self._pins_path(),
            "GeoPackage (*.gpkg)", options=QFileDialog.Option.DontConfirmOverwrite)
        if path:  # opened at once in the selected group, so the next save there adds to it
            path = path if path.lower().endswith(".gpkg") else path + ".gpkg"
            self.settings.setValue(KEY_PINS, path)
            layer, error = open_or_create(path, selected_group(self.iface))
            if layer is None:
                self._warn(error)

    def _copy_pin(self, *_):
        if self.pin is not None:
            QApplication.clipboard().setText(self.pin.text)

    def _point_layers(self):
        layers = (node.layer() for node in QgsProject.instance().layerTreeRoot().findLayers())
        return [l for l in layers
                if isinstance(l, QgsVectorLayer) and l.isValid() and l.geometryType() == POINT]

    def ask_save(self, title, default_name, remarks=True, color=None, size=None, note=""):
        """The Save window; {name, remarks, color, size} or None when cancelled."""
        return ask_save_window(self.iface.mainWindow(), title, default_name, remarks=remarks,
                               color=color, size=size, size_label="Marker size",
                               size_range=(2.0, 20.0), note=note)

    def ask_text(self, title, label):
        text, ok = QInputDialog.getText(self.iface.mainWindow(), title, label)
        return text.strip() if ok else None

    def add_pin_to_layer(self, layer_id, *_):
        """Add the pin as a point in an existing point layer, with its lat/long.

        The layer is put in edit mode and the point is left unsaved, as with
        normal QGIS digitizing: the user saves (or rolls back) the edits.
        """
        layer = QgsProject.instance().mapLayer(layer_id)
        pin = self.pin
        if layer is None or pin is None:
            return
        names = layer.fields().names()
        values = {}
        filled = []
        for index, value in zip(lat_lon_fields(names), (pin.point.y(), pin.point.x())):
            if index is not None:
                field = layer.fields().at(index)
                values[index] = round(value, 7) if field.isNumeric() else f"{value:.6f}"
                filled.append(field.name())
        # the Save window, with only the rows this layer has columns for
        name_index = name_field(names)
        remarks_index = find_column(layer, REMARKS_COLUMNS)
        color_index = find_column(layer, COLOR_COLUMNS)
        if name_index is not None or remarks_index >= 0 or color_index >= 0:
            answer = self.ask_save(f"Add pin to {layer.name()}", "", remarks=remarks_index >= 0,
                                   color=self.settings.value(KEY_PIN_COLOR, PIN_COLOR) if color_index >= 0 else None,
                                   note=f"Added to '{layer.name()}' (pin at {pin.text}).")
            if answer is None:
                return  # cancelled: nothing changed
            for index, value in ((name_index, answer["name"]), (remarks_index, answer["remarks"]),
                                 (color_index, answer["color"])):
                if index is not None and index >= 0 and value:
                    values[index] = value
            if answer["color"]:
                self.settings.setValue(KEY_PIN_COLOR, answer["color"])

        geometry = QgsGeometry.fromPointXY(pin.point)
        try:
            geometry.transform(QgsCoordinateTransform(WGS84, layer.crs(), QgsProject.instance()))
        except QgsCsException:
            self._warn(f"The pin cannot be converted to the CRS of {layer.name()}.")
            return
        if QgsWkbTypes.hasZ(layer.wkbType()):
            geometry.get().addZValue(0)
        if QgsWkbTypes.hasM(layer.wkbType()):
            geometry.get().addMValue(0)
        if QgsWkbTypes.isMultiType(layer.wkbType()):
            geometry.convertToMultiType()

        if not layer.isEditable() and not layer.startEditing():
            self._warn(f"'{layer.name()}' is read-only, so the pin cannot be added to it. "
                       "Save it as a GeoPackage or shapefile first.")
            return
        feature = QgsVectorLayerUtils.createFeature(layer, geometry, values,
                                                    layer.createExpressionContext())
        if not layer.addFeature(feature):
            self._warn(f"Could not add the pin to '{layer.name()}'.")
            return
        self.last_layer_id = layer.id()
        self.pin_layer_memory.save(layer)
        layer.selectByIds([feature.id()])
        layer.triggerRepaint()
        self.iface.mapCanvas().flashGeometries([geometry], layer.crs())
        text = pin.text
        self.remove_pin()
        stored = (f"lat/long in {' and '.join(filled)}" if filled
                  else "no latitude/longitude fields found, so only the point was added")
        self.iface.messageBar().pushMessage(
            "Quick Find", f"Added {text} to '{layer.name()}' ({stored}). The layer is in edit "
            "mode: save the edits to keep it.", Qgis.MessageLevel.Success, 8)

    def _note_slow_pass(self):
        self.iface.statusBarIface().showMessage(
            "Quick Find: nothing in ID or name fields, searching all fields…", 8000)
        QApplication.processEvents()

    def show_match(self, index, *_, note=""):
        self.current = index
        match = self.matches[index]
        total = len(self.matches)
        self.button.setText(f"{index + 1}/{total}")
        layer = QgsProject.instance().mapLayer(match.layer_id)
        if layer is None:
            self._warn(f"Layer '{match.layer_name}' is no longer in the project.")
            return
        geometry = layer.getFeature(match.fid).geometry()
        if geometry.isNull() or geometry.isEmpty():
            self._warn(f"'{match.value}' in {layer.name()} has no geometry.")
            return

        canvas = self.iface.mapCanvas()
        geometry = QgsGeometry(geometry)
        try:
            geometry.transform(QgsCoordinateTransform(
                layer.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance()))
        except QgsCsException:
            self._warn(f"Cannot show '{match.value}' in the map's CRS.")
            return
        box = geometry.boundingBox()
        if box.width() == 0 and box.height() == 0:
            canvas.setCenter(box.center())
            if canvas.scale() > POINT_SCALE:
                canvas.zoomScale(POINT_SCALE)
        else:
            box.grow(max(box.width(), box.height()) * 0.1)
            canvas.setExtent(box)
        canvas.refresh()
        if self._option(KEY_SELECT, True):
            layer.selectByIds([match.fid])
        canvas.flashFeatureIds(layer, [match.fid])

        more = f" ({index + 1} of {total}, Enter for next)" if total > 1 else ""
        self.iface.statusBarIface().showMessage(
            f"Quick Find: {snippet(match.value, self.query)} in {layer.name()} [{match.field}]{more}{note}",
            10000)

    def _warn(self, text):
        self.iface.messageBar().pushMessage("Quick Find", text, Qgis.MessageLevel.Warning, 4)

    # ----- menu ----------------------------------------------------------
    def _fill_menu(self):
        self.menu.clear()
        if self.matches:
            self.menu.addSection(f"Matches for '{self.query}'")
            for i, match in enumerate(self.matches):
                action = self.menu.addAction(
                    f"{snippet(match.value, self.query)}    {match.layer_name} · {match.field}")
                action.setCheckable(True)
                action.setChecked(i == self.current)
                action.triggered.connect(partial(self.show_match, i))
            if len(self.matches) == MAX_MATCHES:
                self.menu.addAction(f"Showing the first {MAX_MATCHES}; type more to narrow").setEnabled(False)
        if self.pin is not None:
            self.menu.addSection(f"Pin at {self.pin.text}")
            self.menu.addAction(self.save_action)
            self.menu.addAction("Copy coordinates").triggered.connect(self._copy_pin)
            self.menu.addAction(self.street_action)
            layers = self._point_layers()
            last = project_layer(self.last_layer_id) or self.pin_layer_memory.find(layers)
            if last in layers:
                self.menu.addAction(f"Add pin to '{last.name()}'").triggered.connect(
                    partial(self.add_pin_to_layer, last.id()))
            submenu = self.menu.addMenu("Add pin to layer")
            if not layers:
                submenu.addAction("No point layers in the project").setEnabled(False)
            for layer in layers:
                submenu.addAction(layer.name()).triggered.connect(
                    partial(self.add_pin_to_layer, layer.id()))
            self.menu.addAction("Remove pin").triggered.connect(self.remove_pin)
        self.menu.addSection("Options")
        pins_path = self._pins_path().replace(os.path.expanduser("~"), "~")
        self.menu.addAction(f"Keep saved pins in {pins_path}…").triggered.connect(self._choose_pins_file)
        for key, label, default in ((KEY_HIDDEN, "Search hidden layers too", False),
                                    (KEY_LARGE, f"Search large layers too (over {LARGE_LAYER:,} "
                                                "features; slow)", False),
                                    (KEY_SELECT, "Select the found feature", True)):
            action = self.menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self._option(key, default))
            action.toggled.connect(partial(self._set_option, key))
