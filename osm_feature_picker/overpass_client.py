"""Build Overpass QL queries and fetch results from the Overpass API.

For large areas, fetch_osm_xml_for_bbox / fetch_key_counts / fetch_value_counts
automatically split the bbox into a grid of smaller tiles (see split_bbox),
fetch each tile in turn with a pause between requests (see fetch_tiled), and
merge the results (deduplicating elements that show up in more than one
tile) - this keeps individual requests small enough to avoid Overpass
timeouts/rate limits instead of sending one huge query for a big area.
"""

import json
import math
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
# Tried in order; the public overpass-api.de instance frequently returns 502/503/504
# under load, so fall back to other known mirrors before giving up.
#
# overpass.osm.ch was tried here previously and removed: it answers with
# HTTP 200 and a structurally valid but essentially empty response (no
# error, no real data, a bogus non-timestamp osm_base) instead of a proper
# error - which silently produced incomplete results with no indication
# anything had gone wrong. lz4.overpass-api.de is the same backend cluster
# as the primary (just load-balanced separately), so it's a safe fallback.
DEFAULT_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
)
TRANSIENT_HTTP_CODES = {429, 502, 503, 504}
# Overpass responses carry an "as of" timestamp - osm_base="..." in XML,
# "timestamp_osm_base": "..." in JSON - reflecting the database state, even
# for a query that legitimately matches nothing. A response missing a
# properly formatted one is treated as untrustworthy rather than as a
# genuine empty result, since a broken/misconfigured mirror can return a
# structurally valid response like this instead of a real HTTP error (see
# the osm.ch note above).
_VALID_OSM_BASE_RE = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
# When Overpass's own [timeout:N] budget for a query runs out partway
# through, it still answers HTTP 200 with a valid osm_base, but the response
# is truncated: it can contain way/relation elements (from the "out body"
# phase, which runs first) with NONE of the <node> coordinate data that
# would normally follow from the ">; out skel qt;" recursion. GDAL then has
# no coordinates to build geometry from, so every such way/relation is
# silently dropped - the fetch reports success but nothing loads. A
# <remark> element (XML) / "remark" key (JSON) is how Overpass flags this,
# so treat its presence as a failure worth retrying (with a larger timeout)
# rather than accepting the response as complete.
_REMARK_RE = re.compile(rb"<remark>|\"remark\"\s*:")
USER_AGENT = "QGIS-OSM-Feature-Picker/0.1 (+https://qgis.org)"

# A tile edge of ~0.05 degrees (roughly 5km, less east-west away from the
# equator) keeps a single Overpass request comfortably within its default
# timeout/rate-limit budget for typical tag density.
DEFAULT_MAX_TILE_DEGREES = 0.05
# Pause between tile requests so we don't hammer the (often already
# congested) public Overpass servers.
DEFAULT_TILE_DELAY_SECONDS = 3.0
# A single tile that fails (both endpoints exhausted) is retried this many
# times, waiting longer each time, before it's given up on - so a large,
# long-running multi-tile fetch survives an occasional transient failure
# instead of throwing away everything fetched so far over one bad tile.
DEFAULT_TILE_RETRIES = 3
DEFAULT_TILE_RETRY_BACKOFF_SECONDS = 15.0
_SLEEP_STEP_SECONDS = 0.2


class OverpassError(Exception):
    pass


def _escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_query(south, west, north, east, tag_filters, elements=("node", "way", "relation"), timeout=60):
    """tag_filters: list of (key, value) tuples; value may be '' / None for "key exists"."""
    if not tag_filters:
        raise OverpassError("タグフィルタを1つ以上指定してください。")
    if not elements:
        raise OverpassError("取得する要素の種類(ノード/ウェイ/リレーション)を1つ以上選択してください。")

    bbox = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    clauses = []
    for key, value in tag_filters:
        key = (key or "").strip()
        if not key:
            continue
        if value:
            tag_expr = f'["{_escape(key)}"="{_escape(value)}"]'
        else:
            tag_expr = f'["{_escape(key)}"]'
        for element in elements:
            clauses.append(f"  {element}{tag_expr}({bbox});")

    if not clauses:
        raise OverpassError("有効なタグフィルタがありません。キーを入力してください。")

    query = (
        f"[out:xml][timeout:{int(timeout)}];\n"
        "(\n" + "\n".join(clauses) + "\n);\n"
        "out body;\n"
        ">;\n"
        "out skel qt;\n"
    )
    return query


def build_keys_query(south, west, north, east, elements=("node", "way", "relation"), timeout=60):
    """Query for the distinct tag keys present on tagged elements within the bbox."""
    if not elements:
        raise OverpassError("取得する要素の種類(ノード/ウェイ/リレーション)を1つ以上選択してください。")

    bbox = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    clauses = [f"  {element}({bbox})(if: count_tags() > 0);" for element in elements]

    query = (
        f"[out:json][timeout:{int(timeout)}];\n"
        "(\n" + "\n".join(clauses) + "\n);\n"
        "out tags;\n"
    )
    return query


def build_values_query(south, west, north, east, key, elements=("node", "way", "relation"), timeout=60):
    """Query for the distinct values of a specific tag key within the bbox."""
    key = (key or "").strip()
    if not key:
        raise OverpassError("キーを入力してください。")
    if not elements:
        raise OverpassError("取得する要素の種類(ノード/ウェイ/リレーション)を1つ以上選択してください。")

    bbox = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    esc_key = _escape(key)
    clauses = [f'  {element}["{esc_key}"]({bbox});' for element in elements]

    query = (
        f"[out:json][timeout:{int(timeout)}];\n"
        "(\n" + "\n".join(clauses) + "\n);\n"
        "out tags;\n"
    )
    return query


def _post_once(query, endpoint, timeout):
    request = urllib.request.Request(
        endpoint,
        data=query.encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _post(query, endpoints=None, timeout=90, status_callback=None):
    """POST the Overpass QL query, trying each endpoint in turn on transient
    server errors (rate limiting / gateway timeouts), and return the raw
    response bytes from the first endpoint that succeeds.

    If the caller passes its own `endpoints` list (rather than leaving it to
    default), that list is reordered in place to put a working fallback
    first once one is found. This makes endpoint choice "sticky" across a
    series of calls sharing the same list - e.g. the tiles of one
    fetch_tiled() run - so a single large fetch doesn't end up mixing
    responses from different Overpass mirrors (which can be very slightly
    out of sync with each other) for different tiles."""
    sticky = endpoints is not None
    if endpoints is None:
        endpoints = list(DEFAULT_ENDPOINTS)
    errors = []
    for index, endpoint in enumerate(endpoints):
        if status_callback:
            if index == 0:
                status_callback(f"Overpass APIに問い合わせています ({endpoint})...")
            else:
                status_callback(f"別のOverpassサーバーに切り替えて再試行しています ({endpoint})...")
        try:
            result = _post_once(query, endpoint, timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300].strip()
            errors.append(f"{endpoint}: HTTP {exc.code}\n{detail}")
            if exc.code not in TRANSIENT_HTTP_CODES:
                raise OverpassError(
                    f"Overpass APIがエラーを返しました (HTTP {exc.code}, {endpoint}):\n{detail}"
                ) from exc
            continue
        except urllib.error.URLError as exc:
            errors.append(f"{endpoint}: 接続失敗 ({exc.reason})")
            continue

        if not _VALID_OSM_BASE_RE.search(result):
            errors.append(f"{endpoint}: 応答の形式が異常です（信頼できないミラーの可能性があります）")
            continue

        if _REMARK_RE.search(result):
            errors.append(
                f"{endpoint}: サーバー側のクエリタイムアウト等により応答が不完全です"
                "（地物のタグはあるが座標データが欠落）"
            )
            continue

        if sticky and index > 0:
            endpoints.remove(endpoint)
            endpoints.insert(0, endpoint)
        return result

    raise OverpassError(
        "Overpass API（複数サーバーで試行）への問い合わせに失敗しました。"
        "サーバーが混雑している可能性があります。時間をおくか、範囲/条件を狭めて再試行してください。\n\n"
        + "\n\n".join(errors)
    )


def fetch_osm_xml(query, endpoints=None, timeout=90, status_callback=None):
    """POST a single Overpass QL query and return the raw OSM XML response bytes."""
    return _post(query, endpoints=endpoints, timeout=timeout, status_callback=status_callback)


def split_bbox(south, west, north, east, max_tile_degrees=DEFAULT_MAX_TILE_DEGREES):
    """Split a bbox into a grid of tiles, each no larger than max_tile_degrees
    on a side, as evenly sized as possible. Returns [(south, west, north, east), ...]
    covering the original bbox with no gaps or overlaps."""
    if max_tile_degrees <= 0:
        raise OverpassError("タイルサイズは正の値にしてください。")

    height = north - south
    width = east - west
    # A tiny epsilon guards against float rounding (e.g. north - south
    # landing a hair above an exact multiple of max_tile_degrees) spuriously
    # adding an extra row/column of tiles.
    epsilon = 1e-9
    rows = max(1, math.ceil(height / max_tile_degrees - epsilon))
    cols = max(1, math.ceil(width / max_tile_degrees - epsilon))
    tile_height = height / rows
    tile_width = width / cols

    tiles = []
    for r in range(rows):
        tile_south = south + r * tile_height
        tile_north = north if r == rows - 1 else south + (r + 1) * tile_height
        for c in range(cols):
            tile_west = west + c * tile_width
            tile_east = east if c == cols - 1 else west + (c + 1) * tile_width
            tiles.append((tile_south, tile_west, tile_north, tile_east))
    return tiles


def _paced_sleep(delay_seconds, status_callback):
    """Sleep in small steps, calling status_callback periodically, so a
    caller that pumps the UI event loop from status_callback stays responsive
    during the pause instead of a single long blocking time.sleep()."""
    remaining = delay_seconds
    while remaining > 0:
        if status_callback:
            status_callback(f"サーバー負荷軽減のため待機しています... (残り{remaining:.1f}秒)")
        step = min(_SLEEP_STEP_SECONDS, remaining)
        time.sleep(step)
        remaining -= step


def fetch_tiled(
    south,
    west,
    north,
    east,
    build_tile_query,
    merge_results,
    max_tile_degrees=DEFAULT_MAX_TILE_DEGREES,
    delay_seconds=DEFAULT_TILE_DELAY_SECONDS,
    timeout=60,
    request_timeout=90,
    status_callback=None,
    tile_retries=DEFAULT_TILE_RETRIES,
    retry_backoff_seconds=DEFAULT_TILE_RETRY_BACKOFF_SECONDS,
):
    """Split the bbox into tiles (see split_bbox), fetch each one in turn
    (pausing between requests when there's more than one tile), and pass the
    list of raw per-tile response bytes to merge_results.

    A tile that fails outright (every endpoint exhausted) is retried up to
    tile_retries times with growing backoff; if it still fails, that tile is
    skipped rather than aborting the whole fetch, so one bad tile in a large,
    long-running multi-tile fetch doesn't throw away everything already
    fetched. Raises OverpassError only if every tile ultimately fails.

    build_tile_query(south, west, north, east, timeout) -> Overpass QL string
    merge_results(list_of_raw_bytes) -> final result

    Returns (merge_results(...), failed_tile_count).
    """
    tiles = split_bbox(south, west, north, east, max_tile_degrees)
    total = len(tiles)
    raw_responses = []
    failed_tiles = 0
    # Shared (and mutated in place by _post) across every tile in this run,
    # so all tiles prefer the same Overpass mirror once one is found to work
    # - see the stickiness note on _post.
    endpoints = list(DEFAULT_ENDPOINTS)

    for index, (t_south, t_west, t_north, t_east) in enumerate(tiles, start=1):
        if total > 1 and status_callback:
            status_callback(f"範囲を{total}分割して取得しています ({index}/{total})...")

        response = None
        last_error = None
        for attempt in range(1, tile_retries + 1):
            # Each retry asks Overpass for a longer [timeout:N] budget: if a
            # tile failed because the query was too complex to finish within
            # the previous budget (Overpass then returns an HTTP-200 but
            # truncated response - see the _REMARK_RE note on _post), simply
            # retrying with the same budget would likely hit the same wall.
            attempt_timeout = timeout * attempt
            attempt_request_timeout = max(request_timeout, attempt_timeout + 30)
            query = build_tile_query(t_south, t_west, t_north, t_east, attempt_timeout)
            try:
                response = _post(
                    query, endpoints=endpoints, timeout=attempt_request_timeout, status_callback=status_callback
                )
                break
            except OverpassError as exc:
                last_error = exc
                if attempt < tile_retries:
                    backoff = retry_backoff_seconds * attempt
                    if status_callback:
                        status_callback(
                            f"タイル{index}/{total}の取得に失敗しました。{backoff:.0f}秒待ってから"
                            f"より長いタイムアウト({timeout * (attempt + 1)}秒)で再試行します ({attempt}/{tile_retries})..."
                        )
                    _paced_sleep(backoff, status_callback)

        if response is None:
            failed_tiles += 1
            if status_callback:
                status_callback(f"タイル{index}/{total}の取得を{tile_retries}回試しましたが失敗したためスキップします。")
        else:
            raw_responses.append(response)

        if index < total:
            _paced_sleep(delay_seconds, status_callback)

    if not raw_responses:
        raise last_error or OverpassError("すべてのタイルの取得に失敗しました。時間をおいて再試行してください。")

    return merge_results(raw_responses), failed_tiles


def _merge_and_sort_osm_xml(raw_responses):
    """Merge one or more raw OSM XML responses into one, deduplicating
    elements by (tag, id) - since adjacent tiles can both return the same
    way/relation - and sorting nodes/ways/relations by ascending id.

    Sorting matters because GDAL's OSM driver assumes ids are monotonically
    increasing; Overpass doesn't guarantee that (it returns directly-matched
    elements first, then recursively-fetched supplementary nodes), and
    without sorting, way/relation geometries can silently fail to resolve
    and be dropped entirely."""
    seen = set()
    nodes, ways, relations, other = [], [], [], []
    buckets = {"node": nodes, "way": ways, "relation": relations}

    for raw in raw_responses:
        root = ET.fromstring(raw)
        for child in list(root):
            bucket = buckets.get(child.tag)
            if bucket is None:
                if not other:
                    other.append(child)
                continue
            key = (child.tag, child.get("id"))
            if key in seen:
                continue
            seen.add(key)
            bucket.append(child)

    nodes.sort(key=lambda e: int(e.get("id")))
    ways.sort(key=lambda e: int(e.get("id")))
    relations.sort(key=lambda e: int(e.get("id")))

    root = ET.Element("osm", version="0.6", generator="osm_feature_picker")
    for element in other + nodes + ways + relations:
        root.append(element)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def fetch_osm_xml_for_bbox(
    south,
    west,
    north,
    east,
    tag_filters,
    elements=("node", "way", "relation"),
    timeout=60,
    request_timeout=90,
    status_callback=None,
    max_tile_degrees=DEFAULT_MAX_TILE_DEGREES,
    delay_seconds=DEFAULT_TILE_DELAY_SECONDS,
    tile_retries=DEFAULT_TILE_RETRIES,
    retry_backoff_seconds=DEFAULT_TILE_RETRY_BACKOFF_SECONDS,
):
    """Fetch the OSM XML for tag_filters within bbox, automatically tiling
    and pacing requests for large areas. Returns (xml_bytes, failed_tile_count);
    xml_bytes is merged, deduplicated, id-sorted OSM XML ready for
    save_osm_xml / the OGR OSM driver."""
    return fetch_tiled(
        south,
        west,
        north,
        east,
        build_tile_query=lambda s, w, n, e, t: build_query(s, w, n, e, tag_filters, elements=elements, timeout=t),
        merge_results=_merge_and_sort_osm_xml,
        max_tile_degrees=max_tile_degrees,
        delay_seconds=delay_seconds,
        timeout=timeout,
        request_timeout=request_timeout,
        status_callback=status_callback,
        tile_retries=tile_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )


def _dedupe_and_count(raw_json_responses, count_key=None):
    """Merge one or more raw Overpass JSON ('out tags') responses,
    deduplicating elements by (type, id) across tiles, then count either
    all tag keys (count_key=None) or the values of a specific key."""
    seen = set()
    counts = {}
    for raw in raw_json_responses:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OverpassError(f"Overpass APIの応答を解析できませんでした: {exc}") from exc

        for element in data.get("elements", []):
            element_key = (element.get("type"), element.get("id"))
            if element_key in seen:
                continue
            seen.add(element_key)

            tags = element.get("tags", {})
            if count_key is None:
                for key in tags:
                    counts[key] = counts.get(key, 0) + 1
            else:
                value = tags.get(count_key)
                if value is not None:
                    counts[value] = counts.get(value, 0) + 1

    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def fetch_key_counts(
    south,
    west,
    north,
    east,
    elements=("node", "way", "relation"),
    timeout=60,
    request_timeout=90,
    status_callback=None,
    max_tile_degrees=DEFAULT_MAX_TILE_DEGREES,
    delay_seconds=DEFAULT_TILE_DELAY_SECONDS,
    tile_retries=DEFAULT_TILE_RETRIES,
    retry_backoff_seconds=DEFAULT_TILE_RETRY_BACKOFF_SECONDS,
):
    """Return ([(key, count), ...], failed_tile_count) for tag keys found
    within the bbox, sorted by count desc. Automatically tiles/paces
    requests for large areas."""
    return fetch_tiled(
        south,
        west,
        north,
        east,
        build_tile_query=lambda s, w, n, e, t: build_keys_query(s, w, n, e, elements=elements, timeout=t),
        merge_results=_dedupe_and_count,
        max_tile_degrees=max_tile_degrees,
        delay_seconds=delay_seconds,
        timeout=timeout,
        request_timeout=request_timeout,
        status_callback=status_callback,
        tile_retries=tile_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )


def fetch_value_counts(
    south,
    west,
    north,
    east,
    key,
    elements=("node", "way", "relation"),
    timeout=60,
    request_timeout=90,
    status_callback=None,
    max_tile_degrees=DEFAULT_MAX_TILE_DEGREES,
    delay_seconds=DEFAULT_TILE_DELAY_SECONDS,
    tile_retries=DEFAULT_TILE_RETRIES,
    retry_backoff_seconds=DEFAULT_TILE_RETRY_BACKOFF_SECONDS,
):
    """Return ([(value, count), ...], failed_tile_count) for the values of
    `key` found within the bbox, sorted by count desc. Automatically
    tiles/paces requests for large areas."""
    return fetch_tiled(
        south,
        west,
        north,
        east,
        build_tile_query=lambda s, w, n, e, t: build_values_query(s, w, n, e, key, elements=elements, timeout=t),
        merge_results=lambda raws: _dedupe_and_count(raws, count_key=key),
        max_tile_degrees=max_tile_degrees,
        delay_seconds=delay_seconds,
        timeout=timeout,
        request_timeout=request_timeout,
        status_callback=status_callback,
        tile_retries=tile_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
