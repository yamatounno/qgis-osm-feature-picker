def classFactory(iface):
    from .osm_feature_picker import OsmFeaturePicker
    return OsmFeaturePicker(iface)
