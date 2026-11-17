from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsProject,
    QgsRectangle,
)
from qgis.gui import QgsMapLayerComboBox

from . import osm_layers, overpass_client
from .candidate_list_dialog import CandidateListDialog
from .osm_keys import KeyCompleterDelegate, fetch_key_description, load_osm_keys
from .overpass_client import OverpassError
from .tiled_download_worker import TiledDownloadWorker

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

KEY_COLUMN_WIDTH = 170
VALUE_COLUMN_WIDTH = 220

# Qt aborts the process if a QThread is destroyed while still running. A
# worker that doesn't stop within WORKER_STOP_TIMEOUT_MS of being asked to
# is detached from the dialog and kept alive here instead, so it can finish
# on its own without crashing anything (mirrors candidate_list_dialog.py).
WORKER_STOP_TIMEOUT_MS = 10000
_ORPHANED_WORKERS = []


class OsmFeaturePickerDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._download_counter = 0
        self._key_query_cache = None
        self._key_description_cache = {}
        self._download_worker = None
        self._tile_grid_layer = None
        self._download_tag_keys = None
        self._download_group_name = None
        self._download_bbox = None
        self._download_tile_counts = {"success": 0, "empty": 0, "failed": 0}
        self._target_polygon_geometry = None
        self.setWindowTitle("OSM Feature Picker")
        self.setMinimumWidth(480)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        bbox_group = QGroupBox("範囲 (緯度経度 / WGS84)")
        bbox_form = QFormLayout(bbox_group)

        self.south_spin = self._make_coord_spin(-90, 90, 35.65)
        self.west_spin = self._make_coord_spin(-180, 180, 139.74)
        self.north_spin = self._make_coord_spin(-90, 90, 35.70)
        self.east_spin = self._make_coord_spin(-180, 180, 139.80)

        bbox_form.addRow("南端の緯度 (south)", self.south_spin)
        bbox_form.addRow("西端の経度 (west)", self.west_spin)
        bbox_form.addRow("北端の緯度 (north)", self.north_spin)
        bbox_form.addRow("東端の経度 (east)", self.east_spin)

        for spin in (self.south_spin, self.west_spin, self.north_spin, self.east_spin):
            spin.valueChanged.connect(self._clear_polygon_geometry)

        self.use_canvas_button = QPushButton("現在の地図表示範囲を使用")
        self.use_canvas_button.clicked.connect(self._use_canvas_extent)
        bbox_form.addRow(self.use_canvas_button)

        self.polygon_layer_combo = QgsMapLayerComboBox()
        self.polygon_layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.polygon_layer_combo.setAllowEmptyLayer(True)
        self.set_range_from_polygon_button = QPushButton("このポリゴンから範囲を設定")
        self.set_range_from_polygon_button.clicked.connect(self._on_set_range_from_polygon_clicked)
        polygon_row = QHBoxLayout()
        polygon_row.addWidget(self.polygon_layer_combo)
        polygon_row.addWidget(self.set_range_from_polygon_button)
        bbox_form.addRow("任意形状のポリゴンから範囲指定", polygon_row)

        polygon_hint = QLabel(
            "(選択したレイヤの選択地物、未選択なら全地物の形状を範囲として使用します。\n"
            "取得時、このポリゴンと重ならないタイルは自動的に除外されます)"
        )
        polygon_hint.setWordWrap(True)
        bbox_form.addRow(polygon_hint)

        layout.addWidget(bbox_group)

        tag_group = QGroupBox("タグフィルタ (例: key=building, value=空欄なら任意 / キー入力中に候補を表示)")
        tag_layout = QVBoxLayout(tag_group)

        self.tag_table = QTableWidget(0, 2)
        self.tag_table.setHorizontalHeaderLabels(["キー", "値 (空欄で任意)"])
        self.tag_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.tag_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.tag_table.setColumnWidth(0, KEY_COLUMN_WIDTH)
        self.tag_table.setColumnWidth(1, VALUE_COLUMN_WIDTH)
        self.tag_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.tag_table.setEditTriggers(QTableWidget.AllEditTriggers)
        self._osm_keys = load_osm_keys()
        self.tag_table.setItemDelegateForColumn(0, KeyCompleterDelegate(self._osm_keys, self.tag_table))
        tag_layout.addWidget(self.tag_table)

        row_buttons = QHBoxLayout()
        add_row_button = QPushButton("行を追加")
        add_row_button.clicked.connect(lambda: self._add_tag_row())
        remove_row_button = QPushButton("選択行を削除")
        remove_row_button.clicked.connect(self._remove_selected_rows)
        row_buttons.addWidget(add_row_button)
        row_buttons.addWidget(remove_row_button)
        row_buttons.addStretch(1)
        tag_layout.addLayout(row_buttons)

        # Lookup buttons are aligned under the key column, since both act on
        # (and populate) the key that's typed into the currently edited row.
        lookup_buttons = QHBoxLayout()
        key_lookup_column = QVBoxLayout()
        self.fetch_keys_button = QPushButton("この範囲のキー候補を取得...")
        self.fetch_keys_button.setFixedWidth(KEY_COLUMN_WIDTH)
        self.fetch_keys_button.clicked.connect(self._on_fetch_keys_clicked)
        self.fetch_values_button = QPushButton("選択行の値候補を取得...")
        self.fetch_values_button.setFixedWidth(KEY_COLUMN_WIDTH)
        self.fetch_values_button.clicked.connect(self._on_fetch_values_clicked)
        key_lookup_column.addWidget(self.fetch_keys_button)
        key_lookup_column.addWidget(self.fetch_values_button)
        lookup_buttons.addLayout(key_lookup_column)
        lookup_buttons.addStretch(1)
        tag_layout.addLayout(lookup_buttons)

        layout.addWidget(tag_group)
        self._add_tag_row("building", "")
        self.tag_table.setCurrentCell(0, 0)

        element_group = QGroupBox("取得する要素")
        element_layout = QHBoxLayout(element_group)
        self.node_check = QCheckBox("ノード")
        self.way_check = QCheckBox("ウェイ")
        self.relation_check = QCheckBox("リレーション")
        for cb in (self.node_check, self.way_check, self.relation_check):
            cb.setChecked(True)
            element_layout.addWidget(cb)
        layout.addWidget(element_group)

        split_group = QGroupBox("範囲分割時の設定 (広い範囲を自動分割して取得する場合)")
        split_form = QFormLayout(split_group)
        self.tile_delay_spin = QSpinBox()
        self.tile_delay_spin.setRange(1, 24 * 60 * 60)
        self.tile_delay_spin.setValue(3600)
        self.tile_delay_spin.setSuffix(" 秒")
        split_form.addRow("タイル間の待機時間", self.tile_delay_spin)
        layout.addWidget(split_group)

        self.status_label = QLabel(
            "範囲とタグフィルタを指定して「ダウンロード」を押してください。"
        )
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        action_layout = QHBoxLayout()
        self.download_button = QPushButton("ダウンロード")
        self.download_button.clicked.connect(self._on_download_clicked)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self._on_cancel_download_clicked)
        self.cancel_button.setVisible(False)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.close)
        action_layout.addWidget(self.download_button)
        action_layout.addWidget(self.cancel_button)
        action_layout.addStretch(1)
        action_layout.addWidget(close_button)
        layout.addLayout(action_layout)

    def _make_coord_spin(self, minimum, maximum, value):
        spin = QDoubleSpinBox()
        spin.setDecimals(7)
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        return spin

    def _add_tag_row(self, key="", value=""):
        row = self.tag_table.rowCount()
        self.tag_table.insertRow(row)
        self.tag_table.setItem(row, 0, QTableWidgetItem(key))
        self.tag_table.setItem(row, 1, QTableWidgetItem(value))

    def _remove_selected_rows(self):
        rows = sorted({index.row() for index in self.tag_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.tag_table.removeRow(row)

    def _collect_tag_filters(self):
        filters = []
        for row in range(self.tag_table.rowCount()):
            key_item = self.tag_table.item(row, 0)
            value_item = self.tag_table.item(row, 1)
            key = key_item.text().strip() if key_item else ""
            value = value_item.text().strip() if value_item else ""
            if key:
                filters.append((key, value))
        return filters

    def _selected_elements(self):
        elements = []
        if self.node_check.isChecked():
            elements.append("node")
        if self.way_check.isChecked():
            elements.append("way")
        if self.relation_check.isChecked():
            elements.append("relation")
        return elements

    def _use_canvas_extent(self):
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        src_crs = canvas.mapSettings().destinationCrs()
        if src_crs != WGS84:
            transform = QgsCoordinateTransform(src_crs, WGS84, QgsProject.instance())
            extent = transform.transformBoundingBox(extent)
        self.south_spin.setValue(extent.yMinimum())
        self.north_spin.setValue(extent.yMaximum())
        self.west_spin.setValue(extent.xMinimum())
        self.east_spin.setValue(extent.xMaximum())

    def _clear_polygon_geometry(self, _value=None):
        self._target_polygon_geometry = None

    def _on_set_range_from_polygon_clicked(self):
        layer = self.polygon_layer_combo.currentLayer()
        if layer is None:
            QMessageBox.information(self, "レイヤ未選択", "ポリゴンレイヤを選択してください。")
            return

        features = list(layer.selectedFeatures())
        used_selection = bool(features)
        if not features:
            features = list(layer.getFeatures())
        geometries = [f.geometry() for f in features if f.geometry() and not f.geometry().isEmpty()]
        if not geometries:
            QMessageBox.information(self, "地物なし", "選択したレイヤに有効な形状を持つ地物がありません。")
            return

        geometry = QgsGeometry.unaryUnion(geometries)
        if geometry is None or geometry.isEmpty():
            QMessageBox.warning(self, "ポリゴンエラー", "ポリゴンの形状を取得できませんでした。")
            return

        src_crs = layer.crs()
        if src_crs != WGS84:
            transform = QgsCoordinateTransform(src_crs, WGS84, QgsProject.instance())
            geometry.transform(transform)

        bbox = geometry.boundingBox()
        # Set the spin boxes first: their valueChanged handlers clear
        # _target_polygon_geometry, so it must be assigned last.
        self.south_spin.setValue(bbox.yMinimum())
        self.north_spin.setValue(bbox.yMaximum())
        self.west_spin.setValue(bbox.xMinimum())
        self.east_spin.setValue(bbox.xMaximum())
        self._target_polygon_geometry = geometry

        source = "選択地物" if used_selection else "全地物"
        self.status_label.setText(
            f"{layer.name()}の{source}({len(features)}件)からポリゴン範囲を設定しました。"
            "取得時、このポリゴンと重ならないタイルは自動的に除外されます。"
        )

    def _update_status(self, message):
        self.status_label.setText(message)
        QApplication.processEvents()

    def _set_busy(self, busy, cursor=True):
        self.download_button.setEnabled(not busy)
        self.use_canvas_button.setEnabled(not busy)
        self.fetch_keys_button.setEnabled(not busy)
        self.fetch_values_button.setEnabled(not busy)
        # A background tiled download can run for hours; an application-wide
        # wait cursor for that whole time would make the rest of QGIS feel
        # broken, so callers driving that path pass cursor=False.
        if cursor:
            if busy:
                QApplication.setOverrideCursor(Qt.WaitCursor)
            else:
                QApplication.restoreOverrideCursor()

    def _get_validated_bbox(self):
        south = self.south_spin.value()
        west = self.west_spin.value()
        north = self.north_spin.value()
        east = self.east_spin.value()
        if south >= north or west >= east:
            QMessageBox.warning(self, "範囲エラー", "south < north、west < east となるように範囲を指定してください。")
            return None
        return south, west, north, east

    @staticmethod
    def _failed_tiles_note(failed_tiles):
        if not failed_tiles:
            return ""
        return f"\n(注意: {failed_tiles}個のタイルは再試行しても取得できなかったため、その部分は含まれていません)"

    @staticmethod
    def _format_duration(seconds):
        if seconds < 90:
            return f"約{seconds:.0f}秒"
        minutes = seconds / 60
        if minutes < 90:
            return f"約{minutes:.0f}分"
        return f"約{minutes / 60:.1f}時間"

    def _confirm_tiled_fetch(self, tiles, delay_seconds=None):
        """If more than one tile is needed to cover the area, ask the user
        before starting (since pacing requests apart can take a while).
        Returns False if the user cancels."""
        if len(tiles) <= 1:
            return True
        if delay_seconds is None:
            delay_seconds = overpass_client.DEFAULT_TILE_DELAY_SECONDS

        min_wait = (len(tiles) - 1) * delay_seconds
        reply = QMessageBox.question(
            self,
            "範囲分割の確認",
            f"指定した範囲は広いため、{len(tiles)}個のタイルに自動分割し、サーバー負荷軽減のため\n"
            f"タイルごとに約{delay_seconds:.0f}秒間隔を空けて順に取得します。\n"
            f"(待機時間だけで最短{self._format_duration(min_wait)}。実際の通信時間は別途かかります。\n"
            "失敗したタイルは間隔を空けて自動的に再試行されます)\n\n"
            "続行しますか？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        return reply == QMessageBox.Yes

    def _on_fetch_keys_clicked(self):
        bbox = self._get_validated_bbox()
        if bbox is None:
            return
        south, west, north, east = bbox

        elements = self._selected_elements()
        if not elements:
            QMessageBox.warning(self, "入力エラー", "取得する要素の種類(ノード/ウェイ/リレーション)を1つ以上選択してください。")
            return

        elements_key = tuple(elements)
        cache = self._key_query_cache
        if cache is not None and cache["bbox"] == bbox and cache["elements"] == elements_key:
            key_counts = cache["key_counts"]
            self.status_label.setText(f"この範囲で{len(key_counts)}種類のタグキーが見つかりました。(前回の取得結果を再利用)")
        else:
            tiles = overpass_client.split_bbox(*bbox)
            if not self._confirm_tiled_fetch(tiles):
                self.status_label.setText("キャンセルしました。")
                return

            self.status_label.setText("この範囲のタグキーを調べています...")
            self._set_busy(True)
            QApplication.processEvents()

            try:
                key_counts, failed_tiles = overpass_client.fetch_key_counts(
                    south, west, north, east, elements=elements, status_callback=self._update_status
                )
            except OverpassError as exc:
                self._set_busy(False)
                self.status_label.setText("キー一覧の取得に失敗しました。")
                QMessageBox.critical(self, "Overpass APIエラー", str(exc))
                return

            self._set_busy(False)

            if not key_counts:
                message = "この範囲にはタグ付き地物が見つかりませんでした。" + self._failed_tiles_note(failed_tiles)
                self.status_label.setText(message)
                QMessageBox.information(self, "結果なし", message)
                return

            self._key_query_cache = {"bbox": bbox, "elements": elements_key, "key_counts": key_counts}
            self.status_label.setText(
                f"この範囲で{len(key_counts)}種類のタグキーが見つかりました。"
                + self._failed_tiles_note(failed_tiles)
            )

        dialog = CandidateListDialog(
            key_counts,
            "この範囲で見つかったタグキー",
            "キー",
            self,
            description_provider=fetch_key_description,
            description_cache=self._key_description_cache,
        )
        if dialog.exec_() and dialog.selected_item:
            self._add_tag_row(dialog.selected_item, "")

    def _on_fetch_values_clicked(self):
        bbox = self._get_validated_bbox()
        if bbox is None:
            return
        south, west, north, east = bbox

        row = self.tag_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "行が未選択", "値候補を取得したい行を選択してください。")
            return
        key_item = self.tag_table.item(row, 0)
        key = key_item.text().strip() if key_item else ""
        if not key:
            QMessageBox.information(self, "キー未入力", "選択した行の「キー」を先に入力してください。")
            return

        elements = self._selected_elements()
        if not elements:
            QMessageBox.warning(self, "入力エラー", "取得する要素の種類(ノード/ウェイ/リレーション)を1つ以上選択してください。")
            return

        tiles = overpass_client.split_bbox(*bbox)
        if not self._confirm_tiled_fetch(tiles):
            self.status_label.setText("キャンセルしました。")
            return

        self.status_label.setText(f'"{key}" の値を調べています...')
        self._set_busy(True)
        QApplication.processEvents()

        try:
            value_counts, failed_tiles = overpass_client.fetch_value_counts(
                south, west, north, east, key, elements=elements, status_callback=self._update_status
            )
        except OverpassError as exc:
            self._set_busy(False)
            self.status_label.setText("値候補の取得に失敗しました。")
            QMessageBox.critical(self, "Overpass APIエラー", str(exc))
            return

        self._set_busy(False)

        if not value_counts:
            message = f'この範囲には "{key}" を持つ地物が見つかりませんでした。' + self._failed_tiles_note(failed_tiles)
            self.status_label.setText(message)
            QMessageBox.information(self, "結果なし", message)
            return

        self.status_label.setText(
            f'"{key}" について{len(value_counts)}種類の値が見つかりました。' + self._failed_tiles_note(failed_tiles)
        )
        dialog = CandidateListDialog(value_counts, f'"{key}" の値候補', "値", self)
        if dialog.exec_() and dialog.selected_item is not None:
            self.tag_table.setItem(row, 1, QTableWidgetItem(dialog.selected_item))

    def _zoom_canvas_to(self, south, west, north, east):
        rect = QgsRectangle(west, south, east, north)
        canvas = self.iface.mapCanvas()
        dst_crs = canvas.mapSettings().destinationCrs()
        if dst_crs != WGS84:
            transform = QgsCoordinateTransform(WGS84, dst_crs, QgsProject.instance())
            rect = transform.transformBoundingBox(rect)
        canvas.setExtent(rect)
        canvas.refresh()

    def _on_download_clicked(self):
        bbox = self._get_validated_bbox()
        if bbox is None:
            return
        south, west, north, east = bbox

        tag_filters = self._collect_tag_filters()
        elements = self._selected_elements()

        try:
            # Validate the filters/elements up front (raises OverpassError on
            # bad input) without needing the per-tile query text itself.
            overpass_client.build_query(south, west, north, east, tag_filters, elements=elements)
        except OverpassError as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return

        tiles = overpass_client.split_bbox(*bbox)

        polygon_geometry = self._target_polygon_geometry
        if polygon_geometry is not None:
            original_count = len(tiles)
            tiles = osm_layers.filter_tiles_by_geometry(tiles, polygon_geometry)
            if not tiles:
                QMessageBox.warning(self, "範囲エラー", "指定したポリゴンと重なるタイルがありませんでした。")
                return
            if len(tiles) < original_count:
                self.status_label.setText(
                    f"ポリゴンと重ならない{original_count - len(tiles)}個のタイルを除外しました"
                    f"({len(tiles)}/{original_count}個を取得します)。"
                )
                QApplication.processEvents()

        if len(tiles) <= 1:
            # Either the whole area fits in one request, or polygon
            # filtering narrowed it down to a single tile - either way, use
            # that tile's own rectangle (not necessarily the full bbox) as
            # the query region.
            self._download_single(tiles[0], tag_filters, elements)
            return

        delay_seconds = self.tile_delay_spin.value()
        if not self._confirm_tiled_fetch(tiles, delay_seconds=delay_seconds):
            self.status_label.setText("キャンセルしました。")
            return

        self._start_tiled_download(bbox, tiles, tag_filters, elements, delay_seconds)

    def _download_single(self, bbox, tag_filters, elements):
        """Small enough to fit in one Overpass request - the plain,
        synchronous, non-tiled path."""
        south, west, north, east = bbox

        self.status_label.setText("Overpass APIに問い合わせています...")
        self._set_busy(True)
        QApplication.processEvents()

        try:
            xml_bytes, failed_tiles = overpass_client.fetch_osm_xml_for_bbox(
                south, west, north, east, tag_filters, elements=elements, status_callback=self._update_status
            )
        except OverpassError as exc:
            self._set_busy(False)
            self.status_label.setText("ダウンロードに失敗しました。")
            QMessageBox.critical(self, "Overpass APIエラー", str(exc))
            return

        try:
            osm_path = osm_layers.save_osm_xml(xml_bytes)
            tag_keys = [key for key, _ in tag_filters]
            self._download_counter += 1
            group_name = f"OSM_{self._download_counter}"
            added = osm_layers.load_osm_layers(osm_path, tag_keys, layer_group_name=group_name)
        except Exception as exc:
            self.status_label.setText("地物の読み込みに失敗しました。")
            QMessageBox.critical(self, "読み込みエラー", f"OSMデータの読み込み中にエラーが発生しました:\n{exc}")
            return
        finally:
            self._set_busy(False)

        if not added:
            message = (
                "該当する地物が見つかりませんでした。条件を変えて再実行してください。"
                + self._failed_tiles_note(failed_tiles)
            )
            self.status_label.setText(message)
            QMessageBox.information(self, "結果なし", message)
            return

        layer_names = ", ".join(layer.name() for layer in added)
        self.status_label.setText(
            f"{len(added)}件のレイヤを読み込みました: {layer_names}" + self._failed_tiles_note(failed_tiles)
        )
        self._zoom_canvas_to(south, west, north, east)

    def _start_tiled_download(self, bbox, tiles, tag_filters, elements, delay_seconds):
        """Large area: fetch one tile at a time in the background. Each
        tile's data becomes its own QGIS layers as soon as it arrives
        (rather than waiting for every tile and merging them all at the
        end), and a polygon layer showing every tile's boundary + status
        (pending/success/empty/failed) is created up front, so failed tiles
        are easy to see on the map and re-fetch later without redoing the
        whole area."""
        self._download_bbox = bbox
        self._download_tag_keys = [key for key, _ in tag_filters]
        self._download_counter += 1
        self._download_group_name = f"OSM_{self._download_counter}"
        self._download_tile_counts = {"success": 0, "empty": 0, "failed": 0}

        self._tile_grid_layer = osm_layers.create_tile_grid_layer(tiles, self._download_group_name)

        self._download_worker = TiledDownloadWorker(tiles, tag_filters, elements, delay_seconds)
        self._download_worker.tile_status.connect(self._on_tile_status)
        self._download_worker.tile_finished.connect(self._on_tile_finished)
        self._download_worker.all_finished.connect(self._on_tiled_download_finished)
        self._download_worker.start()

        self._set_busy(True, cursor=False)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.status_label.setText(
            f"{len(tiles)}個のタイルの取得を開始しました。取得済みのタイルは順次レイヤとして追加されます。\n"
            f"「{self._download_group_name}_tiles」レイヤでタイルごとの状況(pending/success/empty/failed)を確認できます。"
        )

    def _on_tile_status(self, tile_id, total, message):
        self.status_label.setText(f"[タイル{tile_id}/{total}] {message}")

    def _on_tile_finished(self, tile_id, total, success, xml_bytes, error_message):
        status = "failed"
        if success:
            try:
                osm_path = osm_layers.save_osm_xml(bytes(xml_bytes))
                layer_group_name = f"{self._download_group_name}_tile{tile_id}"
                added = osm_layers.load_osm_layers(osm_path, self._download_tag_keys, layer_group_name=layer_group_name)
            except Exception as exc:
                success = False
                error_message = str(exc)
                added = []

            if success:
                status = "success" if added else "empty"

        self._download_tile_counts[status] = self._download_tile_counts.get(status, 0) + 1
        if self._tile_grid_layer is not None:
            osm_layers.set_tile_status(self._tile_grid_layer, tile_id, status)

        counts = self._download_tile_counts
        detail = f" ({error_message})" if status == "failed" and error_message else ""
        self.status_label.setText(
            f"[タイル{tile_id}/{total}] 完了: {status}{detail}\n"
            f"(成功:{counts.get('success', 0)} 空:{counts.get('empty', 0)} 失敗:{counts.get('failed', 0)})"
        )

    def _on_tiled_download_finished(self):
        was_cancelled = self._download_worker is not None and self._download_worker.was_cancelled

        self._set_busy(False, cursor=False)
        self.cancel_button.setVisible(False)
        self._download_worker = None
        counts = self._download_tile_counts
        headline = "キャンセルしました。" if was_cancelled else "全タイルの取得が完了しました。"
        self.status_label.setText(
            f"{headline}成功:{counts.get('success', 0)} 空:{counts.get('empty', 0)} 失敗:{counts.get('failed', 0)}\n"
            + (
                f"「{self._download_group_name}_tiles」レイヤのstatus列で失敗・未処理(pending)のタイルを確認し、"
                "その範囲だけ範囲欄に入力して再実行してください。"
                if counts.get("failed", 0) or was_cancelled
                else ""
            )
        )
        if self._download_bbox is not None:
            self._zoom_canvas_to(*self._download_bbox)

    def _on_cancel_download_clicked(self):
        if self._download_worker is None:
            return
        self._download_worker.request_stop()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("キャンセルを要求しました。現在処理中のタイルが終わり次第停止します。")

    def _stop_download_worker(self):
        if self._download_worker is None:
            return
        worker = self._download_worker
        self._download_worker = None

        try:
            worker.tile_status.disconnect(self._on_tile_status)
            worker.tile_finished.disconnect(self._on_tile_finished)
            worker.all_finished.disconnect(self._on_tiled_download_finished)
        except TypeError:
            pass
        worker.request_stop()

        if worker.wait(WORKER_STOP_TIMEOUT_MS):
            return

        # Still running after the grace period. Detach it from this dialog
        # so Qt's parent-child cleanup on close doesn't destroy a live
        # QThread (which would abort the process), and keep a reference
        # until it finishes naturally in the background.
        worker.setParent(None)
        _ORPHANED_WORKERS.append(worker)

        def _forget():
            if worker in _ORPHANED_WORKERS:
                _ORPHANED_WORKERS.remove(worker)

        worker.finished.connect(_forget)

    def closeEvent(self, event):
        if self._download_worker is not None:
            reply = QMessageBox.question(
                self,
                "ダウンロード中",
                "タイル分割ダウンロードが実行中です。閉じると停止します。続行しますか？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        self._stop_download_worker()
        super().closeEvent(event)
