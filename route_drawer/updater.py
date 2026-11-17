"""Keeps this plugin up to date from MarkT's plugin repository on GitHub.

The same file is in every plugin folder of the repository; keep the copies
identical (tools/build_repo.py check compares them).

What it does:
  * Adds the repository to Plugins > Manage and Install Plugins > Settings
    the first time the plugin loads, so the Plugin Manager also lists the
    upgrades. Unticking the repository there turns the update messages off;
    a repository the user deleted is not added again.
  * A few seconds after QGIS starts, reads the repository's plugins.xml. If
    it lists a newer version of this plugin that runs on this QGIS, a message
    bar offers "Update now", which installs it with QGIS's own plugin
    installer (the same as Upgrade in the Plugin Manager) and restarts the
    plugin.
  * Adds "Check for updates…" to the plugin's menu, which also turns the
    repository back on.

Only plugins.xml and the release zip are downloaded, from GitHub; nothing is
sent. A copy loaded from outside QGIS's plugin folder (a developer's checkout
or a link to one) is never replaced and gets no automatic messages.
"""
import configparser
import os
import re
import xml.etree.ElementTree as ET

from qgis.PyQt.QtCore import QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import QPushButton
try:  # Qt6 (QGIS 4) keeps QAction in QtGui; Qt5 (QGIS 3) in QtWidgets
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction
from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsNetworkAccessManager, QgsSettings
from qgis.gui import QgsMessageBar

REPO_NAME = "MarkT plugins"
REPO_URL = "https://raw.githubusercontent.com/marktamsedesign-oss/qgis-plugins/main/plugins.xml"
REPOS_GROUP = "app/plugin_repositories"  # where the Plugin Manager keeps its repositories
KEY_ADDED = "markt_plugins/repository_added"  # set once this code has added the repository
CHECK_DELAY_MS = 5000  # after the plugin loads, so QGIS finishes starting first
STATE_LOADED = 2  # pyplugin_installer's Repositories.STATE_LOADED


# --------------------------------------------------------------------------- versions
def _parts(text):
    """'1.11.0' -> ([1, 11, 0], ''); '1.2-beta' -> ([1, 2], 'beta')."""
    m = re.match(r"\s*[vV]?(\d+(?:\.\d+)*)(.*)", text or "")
    if not m:
        return [], (text or "").strip()
    return [int(n) for n in m.group(1).split(".")], m.group(2).strip(" .-_")


def compare_versions(a, b):
    """-1, 0 or 1 as version a is older than, the same as, or newer than b.
    1.2 equals 1.2.0, and 1.2.0-beta comes before 1.2.0."""
    (na, sa), (nb, sb) = _parts(a), _parts(b)
    size = max(len(na), len(nb))
    na, nb = na + [0] * (size - len(na)), nb + [0] * (size - len(nb))
    if na != nb:
        return -1 if na < nb else 1
    if sa == sb:
        return 0
    if not sa or not sb:  # a plain release is newer than its pre-release
        return 1 if not sa else -1
    return -1 if sa < sb else 1


def runs_on(qgis_version, minimum, maximum):
    """True if QGIS qgis_version (e.g. '3.34.4-Prizren') is within minimum..maximum,
    read the way QGIS reads them: a missing maximum means '<minimum major>.99',
    and a two-part maximum such as 4.99 includes every 4.99.x."""
    current = (_parts(qgis_version)[0] + [0, 0, 0])[:3]
    low = (_parts(minimum)[0] + [0, 0, 0])[:3]
    high_parts = _parts(maximum)[0] or [low[0], 99]
    high = (high_parts + [99, 99])[:3] if len(high_parts) < 3 else high_parts[:3]
    return low <= current <= high


def qgis_version():
    version = getattr(Qgis, "version", None)
    return version() if callable(version) else Qgis.QGIS_VERSION


# --------------------------------------------------------------------------- plugins.xml
def _is_true(text):
    return (text or "").strip().lower() in ("true", "yes", "1")


def find_release(xml_data, folder):
    """The newest usable <pyqgis_plugin> entry for plugin folder `folder` in plugins.xml,
    as a dict, or None. Experimental and deprecated entries are skipped, as QGIS does
    by default. Raises ET.ParseError if the data isn't a plain XML document."""
    if re.search(rb"<!(DOCTYPE|ENTITY)", xml_data, re.IGNORECASE):
        raise ET.ParseError("DOCTYPE and ENTITY declarations are not accepted")  # no entity tricks
    root = ET.fromstring(xml_data)
    best = None
    for node in root.iter("pyqgis_plugin"):
        download_url = (node.findtext("download_url") or "").strip()
        file_name = (node.findtext("file_name") or "").strip()
        if not file_name:
            file_name = download_url.split("?")[0].rsplit("/", 1)[-1]
        if file_name.partition(".")[0] != folder:  # QGIS's plugin ID: file name up to the first dot
            continue
        if _is_true(node.findtext("experimental")) or _is_true(node.findtext("deprecated")):
            continue
        release = {
            "name": node.get("name") or folder,
            "version": (node.get("version") or node.findtext("version") or "").strip(),
            "minimum": (node.findtext("qgis_minimum_version") or "").strip(),
            "maximum": (node.findtext("qgis_maximum_version") or "").strip(),
            "changelog": (node.findtext("changelog") or "").strip(),
            "download_url": download_url,
        }
        if best is None or compare_versions(release["version"], best["version"]) > 0:
            best = release
    return best


def _short(text, limit=160):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- repository
def _same_url(a, b):
    def norm(url):
        return (url or "").strip().split("#")[0].split("?")[0].rstrip("/").lower()
    return norm(a) == norm(b)


def find_repository():
    """(name, enabled) of the Plugin Manager repository for REPO_URL, or (None, False)."""
    settings = QgsSettings()
    settings.beginGroup(REPOS_GROUP)
    try:
        for name in settings.childGroups():
            if _same_url(settings.value(name + "/url", "", type=str), REPO_URL):
                return name, settings.value(name + "/enabled", True, type=bool)
    finally:
        settings.endGroup()
    return None, False


def ensure_repository(asked):
    """Make sure the Plugin Manager knows the repository. Returns (name, enabled, note).

    On its own (asked=False) the repository is added only if it was never added
    before, so one the user deleted stays deleted, and a disabled one stays
    disabled. When the user asked for an update check (asked=True) it is added
    or ticked again. `note` says what changed, for the message bar."""
    settings = QgsSettings()
    name, enabled = find_repository()
    if name and (enabled or not asked):
        settings.setValue(KEY_ADDED, True)
        return name, enabled, ""
    if name:  # disabled, and the user asked
        settings.setValue(f"{REPOS_GROUP}/{name}/enabled", True)
        return name, True, f"The '{name}' repository is turned on again in the Plugin Manager. "
    if settings.value(KEY_ADDED, False, type=bool) and not asked:
        return None, False, ""  # the user deleted it
    settings.beginGroup(REPOS_GROUP)
    taken = set(settings.childGroups())
    settings.endGroup()
    name, n = REPO_NAME, 2
    while name in taken:  # a different repository already uses the name
        name, n = f"{REPO_NAME} ({n})", n + 1
    settings.setValue(f"{REPOS_GROUP}/{name}/url", REPO_URL)
    settings.setValue(f"{REPOS_GROUP}/{name}/enabled", True)
    settings.setValue(KEY_ADDED, True)
    note = f"The '{name}' repository was added to the Plugin Manager again. " if asked else ""
    return name, True, note


def read_metadata(folder_path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        with open(os.path.join(folder_path, "metadata.txt"), encoding="utf-8-sig") as fh:
            parser.read_file(fh)
        return dict(parser.items("general"))
    except (OSError, configparser.Error):
        return {}


def home_plugin_folder():
    """The folder QGIS installs plugins into, for the current user profile."""
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "python", "plugins")


def installed_by_qgis(folder_path):
    """True if this copy is a normal install in QGIS's plugin folder, which the
    QGIS installer may replace; False for a link or a copy loaded from elsewhere."""
    if os.path.islink(folder_path):
        return False

    def norm(path):
        return os.path.normcase(os.path.realpath(path))
    return norm(os.path.dirname(folder_path)) == norm(home_plugin_folder())


# --------------------------------------------------------------------------- updater
class Updater:
    """One per plugin: Updater(iface, MENU).start() in initGui, .stop() in unload."""

    def __init__(self, iface, menu):
        self.iface = iface
        self.menu = menu
        self.path = os.path.dirname(os.path.abspath(__file__))
        self.folder = os.path.basename(self.path)  # the plugin ID
        metadata = read_metadata(self.path)
        self.title = metadata.get("name", self.folder)
        self.version = metadata.get("version", "")
        self.repo_name = None
        self.action = None
        self.item = None  # the message bar item this updater shows, if any
        self.reply = None  # plugins.xml download in progress
        self.asked = False  # the user picked Check for updates, so answer even if nothing is new
        self.note = ""  # what ensure_repository changed, said with the answer
        self.release = None  # the newer release on offer
        self.listening = False  # waiting for QGIS's installer to read the repository
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.check(asked=False))

    # ----- plugin life -------------------------------------------------
    def start(self):
        self.action = QAction("Check for updates…", self.iface.mainWindow())
        self.action.setObjectName(f"{self.folder}_check_updates")
        self.action.setToolTip(f"Look for a newer version of {self.title} and install it")
        self.action.triggered.connect(lambda: self.check(asked=True))
        self.iface.addPluginToMenu(self.menu, self.action)
        try:
            self.repo_name, enabled, _ = ensure_repository(asked=False)
        except Exception as e:  # never stop the plugin from loading
            self.log(f"Could not add the repository to the Plugin Manager: {e}")
            return
        if enabled and installed_by_qgis(self.path):
            self.timer.start(CHECK_DELAY_MS)

    def stop(self):
        """Called from the plugin's unload(), also when QGIS's installer replaces it."""
        self.timer.stop()
        if self.reply is not None:
            reply, self.reply = self.reply, None
            try:
                reply.finished.disconnect()
                reply.abort()
                reply.deleteLater()
            except (RuntimeError, TypeError):
                pass
        self._stop_listening()
        self._pop()
        if self.action is not None:
            self.iface.removePluginMenu(self.menu, self.action)
            self.action.deleteLater()
            self.action = None

    # ----- messages ------------------------------------------------------
    def log(self, text):
        QgsMessageLog.logMessage(text, self.title, Qgis.MessageLevel.Info)

    def _pop(self):
        item, self.item = self.item, None
        if item is not None:
            try:
                self.iface.messageBar().popWidget(item)
            except RuntimeError:  # already closed by the user
                pass

    def _show(self, text, level=Qgis.MessageLevel.Info, duration=8, button=None, slot=None):
        self._pop()
        item = QgsMessageBar.createMessage(self.title, text)
        if button:
            push = QPushButton(button)
            push.clicked.connect(slot)
            item.layout().addWidget(push)
        self.item = item
        self.iface.messageBar().pushWidget(item, level, duration)

    def _plugin_manager_button(self):
        return {"button": "Plugin Manager", "slot": self._open_plugin_manager}

    def _open_plugin_manager(self):
        self._pop()
        action = getattr(self.iface, "actionManagePlugins", None)
        if action is not None and action() is not None:
            action().trigger()

    # ----- checking ------------------------------------------------------
    def check(self, asked):
        """Read plugins.xml in the background; asked=True when the user picked the menu item."""
        if asked:
            self.asked = True
            try:
                self.repo_name, _, self.note = ensure_repository(asked=True)
            except Exception as e:
                self.note = ""
                self.log(f"Could not add the repository to the Plugin Manager: {e}")
            self._show("Checking for updates…", duration=0)
        if self.reply is not None:
            return  # already checking; the answer comes when that finishes
        if not asked:
            self.asked, self.note = False, ""
        request = QNetworkRequest(QUrl(REPO_URL))
        request.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                             QNetworkRequest.CacheLoadControl.AlwaysNetwork)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self.reply = QgsNetworkAccessManager.instance().get(request)
        self.reply.finished.connect(self._checked)

    def _checked(self):
        reply, self.reply = self.reply, None
        if reply is None:  # stopped meanwhile
            return
        asked, self.asked = self.asked, False
        try:
            failed = reply.error() != QNetworkReply.NetworkError.NoError
            error = reply.errorString()
            data = bytes(reply.readAll())
        finally:
            reply.deleteLater()
        if failed:
            self.log(f"Update check failed: {error}")
            if asked:
                self._show(f"Could not check for updates: {error}", Qgis.MessageLevel.Warning)
            return
        try:
            release = find_release(data, self.folder)
        except ET.ParseError as e:
            self.log(f"Update check: {REPO_URL} is not a plugin list ({e})")
            if asked:
                self._show("Could not check for updates: the plugin list could not be read.",
                           Qgis.MessageLevel.Warning)
            return
        self.result(release, asked)

    def result(self, release, asked):
        """Act on the newest release in plugins.xml (None if the plugin isn't listed)."""
        note, self.note = self.note, ""
        if release is None:
            self.log(f"Update check: {self.folder} is not listed in {REPO_URL}")
            if asked:
                self._show(note + f"{self.title} is not in the plugin list yet.", Qgis.MessageLevel.Warning)
            return
        new, have = release["version"], self.version
        if compare_versions(new, have) <= 0:
            self.log(f"Update check: {have} is the latest version")
            if asked:
                self._show(note + f"You have the latest version ({have}).", Qgis.MessageLevel.Success, 5)
            return
        if not runs_on(qgis_version(), release["minimum"], release["maximum"]):
            text = (f"{self.title} {new} needs QGIS {release['minimum']} to {release['maximum']}; "
                    f"this is QGIS {qgis_version().split('-')[0]}.")
            self.log("Update check: " + text)
            if asked:
                self._show(note + text, Qgis.MessageLevel.Warning)
            return
        self.log(f"Update check: {new} is available (installed: {have})")
        text = f"{new} is available (you have {have})."
        if release["changelog"]:
            text += " What's new: " + _short(release["changelog"])
        if not installed_by_qgis(self.path):
            if asked:
                self._show(note + text + f" This copy is loaded from {self.path}, so update it "
                           "there (it is not replaced automatically).", Qgis.MessageLevel.Info, 0)
            return
        self.release = release
        self._show(note + text, Qgis.MessageLevel.Info, 0, "Update now", self.update)

    # ----- installing ----------------------------------------------------
    def update(self):
        """Have QGIS's installer read the repository again, then install from it."""
        self._pop()
        try:
            import pyplugin_installer
            from pyplugin_installer.installer_data import plugins, repositories
            pyplugin_installer.instance()  # make sure the installer exists
            if not self.repo_name:
                self.repo_name, _, _ = ensure_repository(asked=True)
            if self.repo_name not in repositories.all():
                # added while QGIS was running: reload the list, keeping the others' state
                before = {key: (repo.get("state"), repo.get("error"))
                          for key, repo in repositories.all().items()}
                repositories.load()
                for key, (state, error) in before.items():
                    if key in repositories.all():
                        repositories.setRepositoryData(key, "state", state)
                        repositories.setRepositoryData(key, "error", error)
            plugins.removeRepository(self.repo_name)
            repositories.repositoryFetched.connect(self._fetched)
            self.listening = True
            repositories.requestFetching(self.repo_name, force_reload=True)
        except Exception as e:
            self._stop_listening()
            self._failed(f"QGIS's plugin installer could not start ({e}).")
            return
        self._show(f"Getting {self.title} {self.release['version']}…", duration=0)

    def _stop_listening(self):
        if not self.listening:
            return
        self.listening = False
        try:
            from pyplugin_installer.installer_data import repositories
            repositories.repositoryFetched.disconnect(self._fetched)
        except (ImportError, RuntimeError, TypeError):
            pass

    def _fetched(self, name):
        if name != self.repo_name:
            return
        self._stop_listening()
        try:
            from pyplugin_installer.installer_data import plugins, repositories
            repo = repositories.all()[name]
            if repo.get("state") != STATE_LOADED:
                self._failed(f"QGIS could not read the plugin list: {repo.get('error') or 'unknown error'}")
                return
            plugins.getAllInstalled()
            plugins.rebuild()
            info = plugins.all().get(self.folder) or {}
        except Exception as e:
            self._failed(f"QGIS's plugin installer failed ({e}).")
            return
        if info.get("zip_repository") != name:
            self._failed(f"QGIS takes {self.title} from the '{info.get('zip_repository')}' repository, "
                         f"not '{name}'.")
            return
        if info.get("status") != "upgradeable":
            self._failed(f"QGIS doesn't see a newer {self.title} yet (status: {info.get('status')}, "
                         f"available: {info.get('version_available')}). GitHub can take a few minutes "
                         "to publish a new version; try again shortly.")
            return
        # leave the network signal before the installer opens its dialog
        QTimer.singleShot(0, lambda: self._install(info.get("version_available")))

    def _install(self, version):
        self._pop()
        # The installer unloads this plugin (calling stop()) and starts the new
        # version, so after this call only locals and iface are used.
        iface, title, folder = self.iface, self.title, self.folder
        try:
            import pyplugin_installer
            from pyplugin_installer.installer_data import plugins
            pyplugin_installer.instance().installPlugin(folder, quiet=True)
            now = (plugins.all().get(folder) or {}).get("version_installed") or ""
        except Exception as e:
            self._failed(f"QGIS's plugin installer failed ({e}).")
            return
        if now and compare_versions(now, version) >= 0:
            iface.messageBar().pushMessage(title, f"Updated to {now}.", Qgis.MessageLevel.Success, 8)
            QgsMessageLog.logMessage(f"Updated to {now}", title, Qgis.MessageLevel.Info)
        else:
            text = (f"The update to {version} did not complete (installed: {now or 'unknown'}). "
                    "Try Plugins > Manage and Install Plugins > Upgradeable.")
            iface.messageBar().pushMessage(title, text, Qgis.MessageLevel.Warning, 0)
            QgsMessageLog.logMessage(text, title, Qgis.MessageLevel.Warning)

    def _failed(self, text):
        self.log("Update failed: " + text)
        self._show(text + " You can also update from Plugins > Manage and Install Plugins.",
                   Qgis.MessageLevel.Warning, 0, **self._plugin_manager_button())
