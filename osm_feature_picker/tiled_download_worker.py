"""Background worker that fetches one tile at a time for a large-area
download, so each tile's data can be turned into QGIS layers as soon as it
arrives (instead of waiting for every tile and merging them all at the
end), and so a very long inter-tile delay (the user may want up to an hour,
to let an Overpass-side rate limit / query-priority penalty cool down)
doesn't freeze the QGIS UI."""

import time

from qgis.PyQt.QtCore import QThread, pyqtSignal

from . import overpass_client
from .overpass_client import OverpassError


class TiledDownloadWorker(QThread):
    # (tile_id, total, message)
    tile_status = pyqtSignal(int, int, str)
    # (tile_id, total, success, xml_bytes, error_message)
    tile_finished = pyqtSignal(int, int, bool, bytes, str)
    all_finished = pyqtSignal()

    def __init__(
        self,
        tiles,
        tag_filters,
        elements,
        delay_seconds,
        timeout=60,
        request_timeout=90,
        tile_retries=overpass_client.DEFAULT_TILE_RETRIES,
        retry_backoff_seconds=overpass_client.DEFAULT_TILE_RETRY_BACKOFF_SECONDS,
        parent=None,
    ):
        super().__init__(parent)
        self._tiles = tiles
        self._tag_filters = tag_filters
        self._elements = elements
        self._delay_seconds = delay_seconds
        self._timeout = timeout
        self._request_timeout = request_timeout
        self._tile_retries = tile_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._stop = False
        self.was_cancelled = False

    def request_stop(self):
        self.was_cancelled = True
        self._stop = True

    def _paced_sleep(self, seconds, tile_id, total, label):
        remaining = seconds
        while remaining > 0 and not self._stop:
            self.tile_status.emit(tile_id, total, f"{label} (残り{remaining:.0f}秒)...")
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step

    def run(self):
        total = len(self._tiles)
        # Shared (and mutated in place by _post) across every tile in this
        # run, so all tiles prefer the same Overpass mirror once one is
        # found to work.
        endpoints = list(overpass_client.DEFAULT_ENDPOINTS)

        for tile_id, (south, west, north, east) in enumerate(self._tiles, start=1):
            if self._stop:
                break

            response = None
            last_error = None
            for attempt in range(1, self._tile_retries + 1):
                if self._stop:
                    break
                attempt_timeout = self._timeout * attempt
                attempt_request_timeout = max(self._request_timeout, attempt_timeout + 30)
                query = overpass_client.build_query(
                    south, west, north, east, self._tag_filters, elements=self._elements, timeout=attempt_timeout
                )
                try:
                    response = overpass_client._post(
                        query,
                        endpoints=endpoints,
                        timeout=attempt_request_timeout,
                        status_callback=lambda msg, tid=tile_id: self.tile_status.emit(tid, total, msg),
                    )
                    break
                except OverpassError as exc:
                    last_error = exc
                    if attempt < self._tile_retries and not self._stop:
                        backoff = self._retry_backoff_seconds * attempt
                        self._paced_sleep(
                            backoff,
                            tile_id,
                            total,
                            f"タイル{tile_id}/{total}の取得に失敗。"
                            f"タイムアウト{self._timeout * (attempt + 1)}秒で再試行するまで待機",
                        )

            if self._stop:
                break

            if response is None:
                self.tile_finished.emit(tile_id, total, False, b"", str(last_error) if last_error else "")
            else:
                # Sort this tile's own response so the OGR OSM driver can
                # resolve way/relation geometries correctly (see
                # _merge_and_sort_osm_xml's docstring).
                sorted_xml = overpass_client._merge_and_sort_osm_xml([response])
                self.tile_finished.emit(tile_id, total, True, sorted_xml, "")

            if tile_id < total and not self._stop:
                self._paced_sleep(self._delay_seconds, tile_id, total, "次のタイルまで待機")

        # Always emit, whether every tile was processed or we stopped early
        # (request_stop) - so the dialog can clean up (clear its worker
        # reference, re-enable buttons) in either case instead of only on
        # full natural completion.
        self.all_finished.emit()
