"""Route Drawer: click points on the map to draw a line, optionally along roads."""


def classFactory(iface):
    from .plugin import RouteDrawerPlugin
    return RouteDrawerPlugin(iface)
