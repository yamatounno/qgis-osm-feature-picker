"""Load OSM XML into QGIS layers via the OGR OSM driver, and build tile-grid layers."""

import os
import re
import tempfile

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant

SUBLAYERS = ["points", "lines", "multilinestrings", "multipolygons", "other_relations"]

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "resources", "osmconf_template.ini")

_ATTR_LINE_RE = re.compile(r"^(attributes=)(.*)$", re.MULTILINE)


def _build_osmconf(tag_keys):
    """Return the path to a temp osmconf.ini that also reports tag_keys as columns."""
    with open(_TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    keys = [k.strip() for k in tag_keys if k and k.strip()]

    def _patch(match):
        prefix, existing_raw = match.group(1), match.group(2)
        existing = [a.strip() for a in existing_raw.split(",") if a.strip()]
        for key in keys:
            if key not in existing:
                existing.append(key)
        return prefix + ",".join(existing)

    patched = _ATTR_LINE_RE.sub(_patch, template)

    fd, path = tempfile.mkstemp(prefix="osm_feature_picker_conf_", suffix=".ini")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(patched)
    return path


def save_osm_xml(xml_bytes):
    """Write already-merged/sorted OSM XML bytes (see
    overpass_client.fetch_osm_xml_for_bbox) to a temp file for the OGR OSM
    driver to read."""
    fd, path = tempfile.mkstemp(prefix="osm_feature_picker_", suffix=".osm")
    with os.fdopen(fd, "wb") as f:
        f.write(xml_bytes)
    return path


def _has_features(layer):
    from qgis.core import QgsFeatureRequest
    request = QgsFeatureRequest().setLimit(1).setNoAttributes()
    for _ in layer.getFeatures(request):
        return True
    return False


def load_osm_layers(osm_path, tag_keys, layer_group_name="OSM"):
    """Load the OSM sublayers that contain data as QgsVectorLayers into the project.

    Returns the list of layers that were added.
    """
    conf_path = _build_osmconf(tag_keys)
    previous_conf = os.environ.get("OSM_CONFIG_FILE")
    os.environ["OSM_CONFIG_FILE"] = conf_path

    added = []
    try:
        for sublayer in SUBLAYERS:
            uri = f"{osm_path}|layername={sublayer}"
            layer = QgsVectorLayer(uri, f"{layer_group_name}_{sublayer}", "ogr")
            if not layer.isValid():
                continue
            if not _has_features(layer):
                continue
            QgsProject.instance().addMapLayer(layer)
            added.append(layer)
    finally:
        if previous_conf is None:
            os.environ.pop("OSM_CONFIG_FILE", None)
        else:
            os.environ["OSM_CONFIG_FILE"] = previous_conf
        try:
            os.remove(conf_path)
        except OSError:
            pass

    return added


def create_tile_grid_layer(tiles, group_name, crs_authid="EPSG:4326"):
    """Create a polygon layer with one feature per tile (fields: tile_id,
    status), so the user can see on the map which tiles of a large split
    download succeeded, failed, or are still pending, and easily identify
    which specific area to re-fetch later. tile_id matches the numbering
    used for that tile's own data layers (see load_osm_layers's
    layer_group_name)."""
    layer = QgsVectorLayer(f"Polygon?crs={crs_authid}", f"{group_name}_tiles", "memory")
    provider = layer.dataProvider()
    provider.addAttributes([
        QgsField("tile_id", QVariant.Int),
        QgsField("status", QVariant.String, len=20),
    ])
    layer.updateFields()

    features = []
    for tile_id, (south, west, north, east) in enumerate(tiles, start=1):
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromRect(QgsRectangle(west, south, east, north)))
        feature.setAttributes([tile_id, "pending"])
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()

    QgsProject.instance().addMapLayer(layer)
    return layer


def set_tile_status(tile_grid_layer, tile_id, status):
    """Update the status field of the tile_grid_layer feature with this tile_id."""
    status_index = tile_grid_layer.fields().indexOf("status")
    if status_index < 0:
        return
    for feature in tile_grid_layer.getFeatures(f'"tile_id" = {tile_id}'):
        tile_grid_layer.dataProvider().changeAttributeValues({feature.id(): {status_index: status}})
    tile_grid_layer.triggerRepaint()


def filter_tiles_by_geometry(tiles, geometry):
    """Keep only the (south, west, north, east) tiles whose rectangle
    intersects geometry (a QgsGeometry in the same CRS as the tile
    coordinates, i.e. WGS84). Used when the download area was defined from
    an arbitrary-shaped polygon rather than a plain bbox, so tiles that
    fall entirely outside the polygon aren't fetched from Overpass."""
    kept = []
    for tile in tiles:
        south, west, north, east = tile
        rect_geometry = QgsGeometry.fromRect(QgsRectangle(west, south, east, north))
        if rect_geometry.intersects(geometry):
            kept.append(tile)
    return kept
