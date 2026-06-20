import json
import sqlite3
import time
from pathlib import Path


class AppDatabase:
    def __init__(self, db_path):
        self.db_path = Path(db_path)

    def initialize(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    run_mode TEXT NOT NULL,
                    algorithm TEXT NOT NULL,
                    start_lat REAL NOT NULL,
                    start_lon REAL NOT NULL,
                    goal_lat REAL NOT NULL,
                    goal_lon REAL NOT NULL,
                    route_type TEXT,
                    time_minutes REAL,
                    distance_m REAL,
                    walk_distance_m REAL,
                    train_distance_m REAL,
                    nodes_searched INTEGER,
                    exec_seconds REAL,
                    candidate_mode TEXT,
                    board_station TEXT,
                    alight_station TEXT,
                    lines_json TEXT,
                    passed_stations_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS route_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_id INTEGER NOT NULL,
                    line_order INTEGER NOT NULL,
                    line_name TEXT NOT NULL,
                    FOREIGN KEY (search_id) REFERENCES search_runs(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS passed_stations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_id INTEGER NOT NULL,
                    station_order INTEGER NOT NULL,
                    station_name TEXT NOT NULL,
                    FOREIGN KEY (search_id) REFERENCES search_runs(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_search_runs_timestamp ON search_runs(timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_search_runs_algorithm ON search_runs(algorithm)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_metadata (
                    graph_type TEXT PRIMARY KEY,
                    node_count INTEGER NOT NULL,
                    edge_count INTEGER NOT NULL,
                    saved_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS map_nodes (
                    graph_type TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    is_station INTEGER DEFAULT 0,
                    name TEXT,
                    ref TEXT,
                    PRIMARY KEY (graph_type, node_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS map_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    graph_type TEXT NOT NULL,
                    u TEXT NOT NULL,
                    v TEXT NOT NULL,
                    edge_key TEXT,
                    length_m REAL,
                    mode TEXT,
                    line_ref TEXT,
                    name TEXT,
                    geometry_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    rail_node TEXT,
                    access_node TEXT,
                    source TEXT DEFAULT 'osm',
                    UNIQUE(name, lat, lon, rail_node, access_node)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS route_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_id INTEGER NOT NULL,
                    segment_order INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    distance_m REAL,
                    coords_json TEXT,
                    nodes_json TEXT,
                    FOREIGN KEY (search_id) REFERENCES search_runs(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS obstacles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    radius_m REAL NOT NULL,
                    active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    cleared_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS obstacle_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    obstacle_id INTEGER NOT NULL,
                    graph_type TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    FOREIGN KEY (obstacle_id) REFERENCES obstacles(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_map_edges_graph_uv ON map_edges(graph_type, u, v)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_stations_nodes ON stations(rail_node, access_node)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_route_segments_search ON route_segments(search_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_obstacles_active ON obstacles(active)"
            )

    def graph_snapshot_is_current(self, graph_type, node_count, edge_count):
        self.initialize()
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            row = conn.execute(
                """
                SELECT node_count, edge_count
                FROM graph_metadata
                WHERE graph_type = ?
                """,
                (graph_type,),
            ).fetchone()
        return bool(row and row[0] == node_count and row[1] == edge_count)

    def save_graph_snapshot(self, graph_type, graph):
        self.initialize()
        node_count = graph.number_of_nodes()
        edge_count = graph.number_of_edges()
        if self.graph_snapshot_is_current(graph_type, node_count, edge_count):
            return False

        node_rows = []
        for node, data in graph.nodes(data=True):
            if "y" not in data or "x" not in data:
                continue
            node_rows.append((
                graph_type,
                str(node),
                float(data["y"]),
                float(data["x"]),
                1 if data.get("is_station") else 0,
                self._clean_text(data.get("name")),
                self._clean_text(data.get("ref")),
            ))

        edge_rows = []
        if graph.is_multigraph():
            edge_iter = graph.edges(keys=True, data=True)
            for u, v, key, data in edge_iter:
                edge_rows.append(self._edge_row(graph_type, u, v, key, data))
        else:
            edge_iter = graph.edges(data=True)
            for u, v, data in edge_iter:
                edge_rows.append(self._edge_row(graph_type, u, v, None, data))

        with sqlite3.connect(self.db_path, timeout=60) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM map_edges WHERE graph_type = ?", (graph_type,))
            conn.execute("DELETE FROM map_nodes WHERE graph_type = ?", (graph_type,))
            conn.executemany(
                """
                INSERT OR REPLACE INTO map_nodes (
                    graph_type, node_id, lat, lon, is_station, name, ref
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                node_rows,
            )
            conn.executemany(
                """
                INSERT INTO map_edges (
                    graph_type, u, v, edge_key, length_m,
                    mode, line_ref, name, geometry_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                edge_rows,
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO graph_metadata (
                    graph_type, node_count, edge_count, saved_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (graph_type, node_count, edge_count, time.strftime("%Y-%m-%d %H:%M:%S")),
            )
        return True

    def load_graph_snapshot(self, graph_type):
        self.initialize()
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            nodes = conn.execute(
                """
                SELECT node_id, lat, lon, is_station, name, ref
                FROM map_nodes
                WHERE graph_type = ?
                """,
                (graph_type,),
            ).fetchall()
            if not nodes:
                return None

            edges = conn.execute(
                """
                SELECT u, v, edge_key, length_m, mode, line_ref, name, geometry_json
                FROM map_edges
                WHERE graph_type = ?
                """,
                (graph_type,),
            ).fetchall()

        import networkx as nx

        graph = nx.MultiDiGraph()
        graph.graph["crs"] = "epsg:4326"
        for node_id, lat, lon, is_station, name, ref in nodes:
            attrs = {"y": float(lat), "x": float(lon)}
            if is_station:
                attrs["is_station"] = True
            if name:
                attrs["name"] = name
            if ref:
                attrs["ref"] = ref
            graph.add_node(self._maybe_int(node_id), **attrs)

        for u, v, edge_key, length_m, mode, line_ref, name, geometry_json in edges:
            attrs = {}
            if length_m is not None:
                attrs["length"] = float(length_m)
            if mode:
                attrs["mode"] = mode
            if line_ref:
                attrs["ref"] = line_ref
            if name:
                attrs["name"] = name

            geometry = self._json_to_geometry(geometry_json)
            if geometry is not None:
                attrs["geometry"] = geometry

            key = None if edge_key in (None, "") else self._maybe_int(edge_key)
            if key is None:
                graph.add_edge(self._maybe_int(u), self._maybe_int(v), **attrs)
            else:
                graph.add_edge(self._maybe_int(u), self._maybe_int(v), key=key, **attrs)

        return graph

    def _edge_row(self, graph_type, u, v, key, data):
        line_ref = data.get("ref") or data.get("route") or data.get("line")
        if isinstance(line_ref, list):
            line_ref = " | ".join(str(item) for item in line_ref)
        name = data.get("name")
        if isinstance(name, list):
            name = " | ".join(str(item) for item in name)

        return (
            graph_type,
            str(u),
            str(v),
            "" if key is None else str(key),
            self._clean_float(data.get("length")),
            self._clean_text(data.get("mode")),
            self._clean_text(line_ref),
            self._clean_text(name),
            self._geometry_to_json(data.get("geometry")),
        )

    def save_station_records(self, station_records):
        self.initialize()
        rows = []
        for record in station_records or []:
            try:
                rows.append((
                    str(record.get("name") or "Ga khong ten"),
                    float(record["lat"]),
                    float(record["lon"]),
                    str(record.get("rail_node", "")),
                    str(record.get("access_node", "")),
                    str(record.get("source") or "osm"),
                ))
            except (KeyError, TypeError, ValueError):
                continue

        with sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute("DELETE FROM stations")
            conn.executemany(
                """
                INSERT INTO stations (
                    name, lat, lon, rail_node, access_node, source
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def load_station_records(self):
        self.initialize()
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            rows = conn.execute(
                """
                SELECT name, lat, lon, rail_node, access_node
                FROM stations
                ORDER BY name
                """
            ).fetchall()
        return [
            {
                "name": row[0],
                "lat": row[1],
                "lon": row[2],
                "rail_node": self._maybe_int(row[3]),
                "access_node": self._maybe_int(row[4]),
            }
            for row in rows
        ]

    def save_obstacle(self, lat, lon, radius_m, blocked_nodes=None):
        self.initialize()
        blocked_nodes = list(blocked_nodes or [])
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            cursor = conn.execute(
                """
                INSERT INTO obstacles (lat, lon, radius_m, active)
                VALUES (?, ?, ?, 1)
                """,
                (float(lat), float(lon), float(radius_m)),
            )
            obstacle_id = cursor.lastrowid
            conn.executemany(
                """
                INSERT INTO obstacle_nodes (obstacle_id, graph_type, node_id)
                VALUES (?, 'rail', ?)
                """,
                [(obstacle_id, str(node)) for node in blocked_nodes],
            )
            return obstacle_id

    def load_active_obstacles(self):
        self.initialize()
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            obstacle_rows = conn.execute(
                """
                SELECT id, lat, lon, radius_m
                FROM obstacles
                WHERE active = 1
                ORDER BY id
                """
            ).fetchall()
            node_rows = conn.execute(
                """
                SELECT obstacle_id, node_id
                FROM obstacle_nodes
                WHERE graph_type = 'rail'
                """
            ).fetchall()

        nodes_by_obstacle = {}
        for obstacle_id, node_id in node_rows:
            nodes_by_obstacle.setdefault(obstacle_id, []).append(self._maybe_int(node_id))

        return [
            {
                "id": row[0],
                "lat": row[1],
                "lon": row[2],
                "radius_m": row[3],
                "blocked_nodes": nodes_by_obstacle.get(row[0], []),
            }
            for row in obstacle_rows
        ]

    def deactivate_obstacles(self):
        self.initialize()
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute(
                """
                UPDATE obstacles
                SET active = 0, cleared_at = CURRENT_TIMESTAMP
                WHERE active = 1
                """
            )

    def count_search_runs(self):
        self.initialize()
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            return conn.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0]

    def load_recent_searches(self, limit=200):
        self.initialize()
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            rows = conn.execute(
                """
                SELECT
                    id, timestamp, run_mode, algorithm, route_type,
                    time_minutes, distance_m, walk_distance_m, train_distance_m,
                    nodes_searched, exec_seconds, candidate_mode,
                    board_station, alight_station, lines_json, passed_stations_json,
                    start_lat, start_lon, goal_lat, goal_lon
                FROM search_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()

        searches = []
        for row in rows:
            try:
                lines = json.loads(row[14] or "[]")
            except (json.JSONDecodeError, TypeError):
                lines = []
            try:
                passed_stations = json.loads(row[15] or "[]")
            except (json.JSONDecodeError, TypeError):
                passed_stations = []
            searches.append({
                "id": row[0],
                "timestamp": row[1],
                "run_mode": row[2],
                "algorithm": row[3],
                "route_type": row[4],
                "time_minutes": row[5],
                "distance_m": row[6],
                "walk_distance_m": row[7],
                "train_distance_m": row[8],
                "nodes_searched": row[9],
                "exec_seconds": row[10],
                "candidate_mode": row[11],
                "board_station": row[12],
                "alight_station": row[13],
                "lines": lines,
                "passed_stations": passed_stations,
                "start_lat": row[16],
                "start_lon": row[17],
                "goal_lat": row[18],
                "goal_lon": row[19],
            })
        return searches

    def clear_search_history(self):
        self.initialize()
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            count = conn.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0]
            conn.execute("DELETE FROM search_runs")
        return count

    def _geometry_to_json(self, geometry):
        if geometry is None:
            return None
        try:
            return json.dumps(
                [(float(lat), float(lon)) for lon, lat in geometry.coords],
                ensure_ascii=False,
            )
        except Exception:
            return None

    def _json_to_geometry(self, value):
        if not value:
            return None
        try:
            from shapely.geometry import LineString

            coords = json.loads(value)
            return LineString([(float(lon), float(lat)) for lat, lon in coords])
        except Exception:
            return None

    def _clean_text(self, value):
        if value is None:
            return None
        return str(value)

    def _clean_float(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _maybe_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    def save_search(self, record):
        self.initialize()
        lines = list(record.get("lines") or [])
        passed_stations = list(record.get("passed_stations") or [])
        segments = list(record.get("segments") or [])

        with sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.execute(
                """
                INSERT INTO search_runs (
                    timestamp, run_mode, algorithm,
                    start_lat, start_lon, goal_lat, goal_lon,
                    route_type, time_minutes, distance_m,
                    walk_distance_m, train_distance_m,
                    nodes_searched, exec_seconds, candidate_mode,
                    board_station, alight_station,
                    lines_json, passed_stations_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("timestamp"),
                    record.get("run_mode", "search"),
                    record.get("algorithm", ""),
                    record.get("start_lat"),
                    record.get("start_lon"),
                    record.get("goal_lat"),
                    record.get("goal_lon"),
                    record.get("route_type"),
                    record.get("time_minutes"),
                    record.get("distance_m"),
                    record.get("walk_distance_m"),
                    record.get("train_distance_m"),
                    record.get("nodes_searched"),
                    record.get("exec_seconds"),
                    record.get("candidate_mode", ""),
                    record.get("board_station", ""),
                    record.get("alight_station", ""),
                    json.dumps(lines, ensure_ascii=False),
                    json.dumps(passed_stations, ensure_ascii=False),
                ),
            )
            search_id = cursor.lastrowid

            conn.executemany(
                """
                INSERT INTO route_lines (search_id, line_order, line_name)
                VALUES (?, ?, ?)
                """,
                [
                    (search_id, index, str(line_name))
                    for index, line_name in enumerate(lines, start=1)
                ],
            )
            conn.executemany(
                """
                INSERT INTO passed_stations (search_id, station_order, station_name)
                VALUES (?, ?, ?)
                """,
                [
                    (search_id, index, str(station_name))
                    for index, station_name in enumerate(passed_stations, start=1)
                ],
            )
            conn.executemany(
                """
                INSERT INTO route_segments (
                    search_id, segment_order, mode, distance_m, coords_json, nodes_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        search_id,
                        index,
                        str(segment.get("mode", "")),
                        self._clean_float(segment.get("distance_m")),
                        json.dumps(segment.get("coords", []), ensure_ascii=False),
                        json.dumps(segment.get("nodes", []), ensure_ascii=False),
                    )
                    for index, segment in enumerate(segments, start=1)
                ],
            )
            return search_id
