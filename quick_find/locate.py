"""Find this computer's current location.

* Windows (and Linux): Qt's positioning module, i.e. Windows Location.
* Mac: QGIS is not allowed to ask macOS for the location (QGIS.app does not
  declare location use), so a Shortcuts-app shortcut named SHORTCUT_NAME is
  run instead; it must output "latitude, longitude" as text.

Nothing is sent anywhere by Quick Find: only the operating system's own
location service is asked.
"""
import os
import subprocess
import sys
import tempfile

from qgis.PyQt.QtCore import QT_VERSION_STR, QObject, QProcess, QTimer, pyqtSignal

from .coords import parse_lat_lon

IS_MAC = sys.platform == "darwin"
SHORTCUT_NAME = "QGIS My Location"
SHORTCUTS_CMD = "/usr/bin/shortcuts"
TIMEOUT_MS = 30000

try:  # same Qt as QGIS: Qt6 for QGIS 4, Qt5 for QGIS 3
    if QT_VERSION_STR.startswith("6"):
        from PyQt6.QtPositioning import QGeoPositionInfo, QGeoPositionInfoSource
    else:
        from PyQt5.QtPositioning import QGeoPositionInfo, QGeoPositionInfoSource
except ImportError:
    QGeoPositionInfo = QGeoPositionInfoSource = None

MAC_SETUP = (
    "QGIS on a Mac is not allowed to ask macOS for your location, so Quick Find asks the "
    "Shortcuts app instead. Make this shortcut once:\n\n"
    "1. Open the Shortcuts app and click + to make a new shortcut.\n"
    f"2. Name it exactly:  {SHORTCUT_NAME}\n"
    "3. Add the action \"Get Current Location\".\n"
    "4. Add the action \"Text\". In it, insert the Current Location variable, click it and "
    "choose Latitude; type a comma and a space; insert Current Location again and choose "
    "Longitude. It should read:  Latitude, Longitude\n"
    "5. Add the action \"Stop and Output\" and set it to output the Text.\n"
    "6. Run the shortcut once in Shortcuts and allow location access when macOS asks.\n\n"
    "Then click My location again.")

WINDOWS_SETUP = (
    "Windows did not give a location. Open Settings > Privacy & security > Location, turn on "
    "Location services and \"Let desktop apps access your location\", then try again. Indoors "
    "or without Wi-Fi it can take a while or fail.")


class Locator(QObject):
    found = pyqtSignal(float, float, float)  # latitude, longitude, accuracy in metres (-1 unknown)
    failed = pyqtSignal(str, bool)           # message, setup instructions should be shown

    def __init__(self, parent=None):
        super().__init__(parent)
        self.create_source = (QGeoPositionInfoSource.createDefaultSource
                              if QGeoPositionInfoSource is not None else None)
        self._busy = False
        self._source = None
        self._process = None
        self._output = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timed_out)

    def busy(self):
        return self._busy

    def locate(self):
        if self._busy:
            return
        self._busy = True
        self._timer.start(TIMEOUT_MS)
        if IS_MAC:
            self._run_shortcut()
        else:
            self._ask_qt()

    # ----- Mac: Shortcuts app ---------------------------------------------
    def shortcut_exists(self):
        try:
            names = subprocess.run([SHORTCUTS_CMD, "list"], stdin=subprocess.DEVNULL,
                                   capture_output=True, encoding="utf-8", errors="replace",
                                   timeout=10).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            return False
        return SHORTCUT_NAME in (n.strip() for n in names)

    def _run_shortcut(self):
        if not self.shortcut_exists():
            self._fail(f"The shortcut \"{SHORTCUT_NAME}\" is not set up yet.", True)
            return
        self._output = os.path.join(tempfile.gettempdir(), f"qgis_my_location_{os.getpid()}.txt")
        if os.path.exists(self._output):
            os.remove(self._output)
        self._process = QProcess(self)
        # "shortcuts run" reads its input from stdin when that is a pipe; QProcess leaves an
        # open pipe there, so without this the shortcut waits forever for input
        self._process.setStandardInputFile(QProcess.nullDevice())
        self._process.finished.connect(self._shortcut_done)
        self._process.start(SHORTCUTS_CMD, ["run", SHORTCUT_NAME, "--output-path", self._output,
                                            "--output-type", "public.plain-text"])

    def _shortcut_done(self, *_):
        if not self._busy:
            return
        error = bytes(self._process.readAllStandardError()).decode("utf-8", "replace").strip()
        text = ""
        if self._output and os.path.exists(self._output):
            with open(self._output, encoding="utf-8", errors="replace") as handle:
                text = handle.read().strip()
            os.remove(self._output)
        if not text:  # some shortcuts print instead of writing the output file
            text = bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace").strip()
        coordinate = parse_lat_lon(text) if text else None
        if coordinate:
            self._done(coordinate[0], coordinate[1], -1.0)
        elif "find shortcut" in error.lower():
            self._fail(f"The shortcut \"{SHORTCUT_NAME}\" is not set up yet.", True)
        elif text:
            self._fail(f"The shortcut returned \"{text[:80]}\", not a latitude, longitude.", True)
        else:
            self._fail("The shortcut gave no location"
                       + (f" ({error[:120]})" if error else "")
                       + ". Run it once in the Shortcuts app and allow location access.", True)

    # ----- Windows / Linux: Qt positioning --------------------------------
    def _ask_qt(self):
        source = self.create_source(self) if self.create_source else None
        if source is None:
            self._fail("This computer has no location service QGIS can use. " + WINDOWS_SETUP, False)
            return
        self._source = source
        source.positionUpdated.connect(self._position)
        error_signal = getattr(source, "errorOccurred", None)  # Qt6; Qt5 calls it "error"
        if error_signal is None:
            error_signal = getattr(source, "error", None)
        try:
            error_signal.connect(self._qt_error)
        except (AttributeError, TypeError):
            pass
        if hasattr(source, "updateTimeout"):  # Qt5
            source.updateTimeout.connect(lambda: self._fail(
                "No location found in time. " + WINDOWS_SETUP, False))
        source.requestUpdate(TIMEOUT_MS - 5000)

    def _position(self, info):
        coordinate = info.coordinate()
        accuracy = -1.0
        attribute = QGeoPositionInfo.Attribute.HorizontalAccuracy
        if info.hasAttribute(attribute):
            accuracy = float(info.attribute(attribute))
        self._done(coordinate.latitude(), coordinate.longitude(), accuracy)

    def _qt_error(self, *_):
        self._fail(WINDOWS_SETUP, False)

    # ----- common ----------------------------------------------------------
    def _timed_out(self):
        if self._process is not None:
            self._process.kill()
        self._fail("No location after 30 seconds. " + (
            "Check the shortcut in the Shortcuts app." if IS_MAC else WINDOWS_SETUP), IS_MAC)

    def _finish(self):
        self._busy = False
        self._timer.stop()
        if self._source is not None:
            self._source.stopUpdates()
            self._source.deleteLater()
            self._source = None

    def _done(self, lat, lon, accuracy):
        if self._busy:
            self._finish()
            self.found.emit(lat, lon, accuracy)

    def _fail(self, message, setup):
        if self._busy:
            self._finish()
            self.failed.emit(message, setup)
