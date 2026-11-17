"""Remember a chosen layer between QGIS sessions.

A layer's id changes when it is added to another project, so the choice is
kept by the layer's file (path + layer name inside the file), with the id and
display name as extra clues. Only file-based sources are stored: database
connection strings can hold passwords and are never written to the settings.
"""
import os

from qgis.core import QgsProject, QgsProviderRegistry, QgsSettings, QgsVectorLayer


def _file_key(provider, source):
    """'normalised path|layer name' for a file-based layer, else None."""
    try:
        parts = QgsProviderRegistry.instance().decodeUri(provider, source)
    except Exception:  # unknown provider
        return None
    path = parts.get("path") or ""
    if not path:
        return None
    sub = parts.get("layerName") or parts.get("layerId") or ""
    # normcase: on Windows "C:/Users/..." and "c:\users\..." are the same file
    return os.path.normcase(os.path.realpath(path)) + "|" + str(sub)


class RememberedLayer:
    def __init__(self, prefix):
        self.prefix = prefix
        self.settings = QgsSettings()

    def _get(self, key):
        return self.settings.value(f"{self.prefix}/{key}", "") or ""

    def known(self):
        return bool(self._get("name"))

    def name(self):
        return self._get("name")

    def save(self, layer):
        key = _file_key(layer.providerType(), layer.source())
        self.settings.setValue(f"{self.prefix}/name", layer.name())
        self.settings.setValue(f"{self.prefix}/id", layer.id())
        self.settings.setValue(f"{self.prefix}/provider", layer.providerType())
        self.settings.setValue(f"{self.prefix}/source", layer.source() if key else "")

    def forget(self):
        for key in ("name", "id", "provider", "source"):
            self.settings.remove(f"{self.prefix}/{key}")

    def is_this(self, layer):
        if not self.known():
            return False
        source = self._get("source")
        if source:
            wanted = _file_key(self._get("provider"), source)
            return wanted is not None and _file_key(layer.providerType(), layer.source()) == wanted
        return layer.id() == self._get("id")

    def find(self, layers):
        """The remembered layer among layers (same file, else same id, else same name)."""
        if not self.known():
            return None
        for test in (self.is_this,
                     lambda l: l.id() == self._get("id"),
                     lambda l: l.name() == self._get("name")):
            match = next((l for l in layers if test(l)), None)
            if match is not None:
                return match
        return None

    def open_file(self):
        """Open the remembered file without adding it to the project (None if gone)."""
        source, provider = self._get("source"), self._get("provider")
        key = _file_key(provider, source) if source else None
        if not key or not os.path.exists(key.split("|", 1)[0]):
            return None
        layer = QgsVectorLayer(source, self.name(), provider)
        return layer if layer.isValid() else None


def project_layer(layer_id):
    return QgsProject.instance().mapLayer(layer_id) if layer_id else None
