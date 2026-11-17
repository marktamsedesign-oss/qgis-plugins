"""Quick Find: type a value, press Enter, the map zooms to the feature."""


def classFactory(iface):
    from .plugin import QuickFindPlugin
    return QuickFindPlugin(iface)
