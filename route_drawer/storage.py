"""Small helpers shared by Route Drawer's import/export code."""
from qgis.PyQt.QtCore import QMetaType
from qgis.core import QgsField


def new_field(name, kind):
    """A QgsField of kind "String" or "Double" on QGIS 3.22+ and QGIS 4."""
    try:  # QGIS >= 3.38 and QGIS 4 take QMetaType
        return QgsField(name, getattr(QMetaType.Type, "QString" if kind == "String" else kind))
    except TypeError:  # older QGIS 3 takes QVariant
        from qgis.PyQt.QtCore import QVariant
        return QgsField(name, getattr(QVariant, kind))
