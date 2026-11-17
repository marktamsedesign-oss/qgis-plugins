"""Where Save puts a pin or route: the group selected in the Layers panel.

That is the selected group itself, or the group of the selected layer; the
top level when a top-level layer (or nothing) is selected. Each group keeps
its own Saved pins / Drawn routes layer, in its own file. (The same file is in
Quick Find and Route Drawer, so each plugin installs on its own.)
"""
import os
import re

from qgis.core import QgsLayerTree, QgsProject


def selected_group(iface):
    root = QgsProject.instance().layerTreeRoot()
    view = iface.layerTreeView() if hasattr(iface, "layerTreeView") else None
    node = view.currentNode() if view is not None else None
    if node is None:
        return root
    if QgsLayerTree.isGroup(node):
        return node
    parent = node.parent()
    return parent if parent is not None and QgsLayerTree.isGroup(parent) else root


def is_top(group):
    return group.parent() is None


def where(group):
    """' in 'sub-group1'' for messages; nothing for the top level."""
    return "" if is_top(group) else f" in '{group.name()}'"


def layer_in(group, is_ours):
    """The first layer directly in the group for which is_ours(layer) holds, else None."""
    for node in group.children():
        if QgsLayerTree.isLayer(node):
            layer = node.layer()
            if layer is not None and layer.isValid() and is_ours(layer):
                return layer
    return None


def group_file(folder, prefix, group):
    """A new file for the group's layer: saved_pins_sub-group1.gpkg, ..._2, ..."""
    name = re.sub(r"[^\w-]+", "_", group.name()).strip("_")[:60] or "group"
    stem = os.path.join(folder, f"{prefix}_{name}")
    candidate, n = stem + ".gpkg", 2
    while os.path.exists(candidate):
        candidate, n = f"{stem}_{n}.gpkg", n + 1
    return candidate


def add_to(group, layer):
    """Add the layer to the project at the top of the group, so a basemap cannot cover it."""
    QgsProject.instance().addMapLayer(layer, False)
    group.insertLayer(0, layer)
