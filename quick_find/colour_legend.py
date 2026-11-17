"""One Layers-panel entry per colour used, labelled with the features' names.

Saved pins and Drawn routes store each feature's own colour in a "color"
column. This turns that into a categorized style, so the Layers panel shows
e.g. a red pin "Pin 1" and a green pin "Pin 2" instead of one symbol for all.
It is rebuilt after every save and every edit (a colour changed in the
attribute table, a feature deleted). A style the user made is never replaced:
only layers carrying this module's mark are rebuilt. (The same file is in
Quick Find and Route Drawer, so each plugin installs on its own.)
"""
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsCategorizedSymbolRenderer, QgsFeatureRequest, QgsProject, QgsRendererCategory

FIELD = "color"
MARK = "colour_legend"  # custom layer property: this legend was made here
MAX_NAMES = 3

_watched = set()


def is_colour_legend(layer):
    renderer = layer.renderer()
    return (renderer is not None and renderer.type() == "categorizedSymbol"
            and renderer.classAttribute() == FIELD and bool(layer.customProperty(MARK, False)))


def _fallback(layer, default):
    """The colour for features without one: kept from the current style if set."""
    renderer = layer.renderer()
    if renderer is not None and renderer.type() == "categorizedSymbol":
        for category in renderer.categories():
            if category.value() in (None, "") or str(category.value()) == "NULL":
                return category.symbol().color().name()
    elif renderer is not None and renderer.type() == "singleSymbol":
        return renderer.symbol().color().name()
    return default


def build(layer, make_symbol, default_color, name_field="name"):
    """Rebuild the colour list; make_symbol(colour) gives the symbol for one colour."""
    fallback = _fallback(layer, default_color)
    name_index = layer.fields().lookupField(name_field)
    colour_index = layer.fields().lookupField(FIELD)
    names = {}  # colour text (None: no colour of its own) -> feature names
    request = QgsFeatureRequest().setSubsetOfAttributes([i for i in (name_index, colour_index) if i >= 0])
    for feature in layer.getFeatures(request):
        value = feature.attribute(colour_index) if colour_index >= 0 else None
        colour = str(value).strip() if value is not None and str(value).strip() not in ("", "NULL") else None
        if colour is not None and not QColor(colour).isValid():
            colour = None  # not a colour: drawn like features without one
        name = feature.attribute(name_index) if name_index >= 0 else None
        names.setdefault(colour, []).append(str(name) if name not in (None, "") and str(name) != "NULL"
                                            else f"#{feature.id()}")

    def label(found):
        shown = ", ".join(found[:MAX_NAMES])
        return shown + (f" +{len(found) - MAX_NAMES} more" if len(found) > MAX_NAMES else "")

    categories = [QgsRendererCategory(colour, make_symbol(colour), label(found))
                  for colour, found in sorted(((c, n) for c, n in names.items() if c), key=lambda item: item[0].lower())]
    if None in names:  # last, and only when used: features without a colour of their own
        categories.append(QgsRendererCategory("", make_symbol(fallback), label(names[None])))
    layer.setRenderer(QgsCategorizedSymbolRenderer(FIELD, categories))
    layer.setCustomProperty(MARK, True)
    layer.emitStyleChanged()
    layer.triggerRepaint()


def rebuild_file(layer, rebuild):
    """After writing straight to the file (no edit signal): rebuild the list of this layer and
    of any other layer showing the same file, so a new colour is never left undrawn."""
    for other in [layer] + [l for l in QgsProject.instance().mapLayers().values()
                            if l is not layer and l.source() == layer.source()]:
        if is_colour_legend(other):
            rebuild(other)


def watch(layer, rebuild):
    """Rebuild after edits; rebuild(layer) is called only while the layer keeps this legend."""
    layer_id = layer.id()
    if layer_id in _watched:
        return
    _watched.add(layer_id)

    def again(*_):
        current = QgsProject.instance().mapLayer(layer_id)
        if current is not None and is_colour_legend(current):
            rebuild(current)

    layer.layerModified.connect(again)    # edits in the attribute table, deletions, rollback
    layer.editingStopped.connect(again)   # saved edits
    layer.willBeDeleted.connect(lambda: _watched.discard(layer_id))
