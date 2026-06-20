import tcl_tk_fix  # noqa: F401 - sets TCL_LIBRARY/TK_LIBRARY before Tkinter loads
import tkinter
from tkinter import messagebox, ttk
import customtkinter
import tkintermapview
import threading
import time
import json
import pickle
import csv
import math
import shutil
from pathlib import Path

import osmnx as ox
import networkx as nx
from PIL import Image, ImageDraw, ImageTk

from config import Config, fast_haversine
from database import AppDatabase
from pathfinder import PathFinder
from route_logic import _path_length, find_smart_route
from spatial_index import SpatialIndex


ox.settings.use_cache = True
ox.settings.log_console = False


class PathfindingApp(customtkinter.CTk):
    def __init__(self):
        super().__init__()
        self.title("Saint Petersburg Rail Path Finder")
        self.geometry("1200x800")

        self.G = None
        self.walk_G = None
        self.rail_node_index = None
        self.walk_node_index = None
        self.station_records = []
        self.station_name_by_rail_node = {}
        self.obstacles = set()
        self.obstacle_coords = []
        self.start_coords = self.goal_coords = None
        self.markers = {"start": None, "goal": None, "obs": []}

        self.station_markers = []
        self.path_objects = []
        self.route_markers = []

        self._finder_cache = None
        self._finder_obstacles_snapshot = None
        self._walk_distance_cache = {}
        self._state_lock = threading.Lock()
        self._walk_cache_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._db_persist_lock = threading.Lock()
        self._db_persisting_graphs = set()
        self._copy_seed_cache_if_needed()
        self.database = AppDatabase(Config.SEARCH_HISTORY_DB)
        self.database.initialize()
        self._migrate_existing_history_to_database()
        self._station_fetch_lock = threading.Lock()
        self._fetching_stations = False
        self._station_fetch_token = 0
        self.history_window = None
        self.history_tree = None
        self.history_detail = None
        self._history_rows_by_id = {}

        self.setup_ui()

        dot_img = Image.new("RGBA", (12, 12), (255, 255, 255, 0))
        draw = ImageDraw.Draw(dot_img)
        draw.ellipse((0, 0, 11, 11), fill="#F1C40F", outline="#D35400")
        self.tiny_dot = ImageTk.PhotoImage(dot_img)

        threading.Thread(target=self._load_map_worker, daemon=True).start()

    def _get_finder(self):
        while True:
            with self._state_lock:
                obs_snapshot = frozenset(self.obstacles)
                if self._finder_cache is not None and self._finder_obstacles_snapshot == obs_snapshot:
                    return self._finder_cache
                graph = self.G

            if graph is None:
                raise RuntimeError("Graph chưa được tải xong.")

            new_finder = PathFinder(graph, obs_snapshot)

            with self._state_lock:
                if frozenset(self.obstacles) != obs_snapshot:
                    continue
                self._finder_cache = new_finder
                self._finder_obstacles_snapshot = obs_snapshot
                return self._finder_cache

    def _migrate_existing_history_to_database(self):
        try:
            if self.database.count_search_runs() > 0:
                return
            history_path = Path(Config.SEARCH_HISTORY_JSONL)
            if not history_path.exists():
                return

            imported = 0
            with history_path.open("r", encoding="utf-8") as file:
                for line in file:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    record.setdefault("segments", [])
                    self.database.save_search(record)
                    imported += 1
            if imported:
                print(f"Imported {imported} old search history rows into SQLite.")
        except Exception as exc:
            print("Khong migrate duoc lich su cu vao database:", exc)

    def _copy_seed_cache_if_needed(self):
        source_dir = Path(Config.SEED_CACHE_DIR)
        target_dir = Path(Config.CACHE_DIR)
        if not source_dir.exists():
            return

        try:
            if source_dir.resolve() == target_dir.resolve():
                return
        except OSError:
            pass

        target_dir.mkdir(parents=True, exist_ok=True)
        seed_files = [
            "spb_rail.pkl",
            "spb_walk.pkl",
            "spb_station_nodes.json",
            "spb_station_records.json",
            "search_history.sqlite3",
        ]

        for file_name in seed_files:
            src = source_dir / file_name
            dst = target_dir / file_name
            if not src.exists() or dst.exists():
                continue
            try:
                shutil.copy2(src, dst)
            except OSError as exc:
                print(f"Khong copy duoc seed cache {file_name}:", exc)

    def setup_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = customtkinter.CTkFrame(self, width=300, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")

        customtkinter.CTkLabel(self.sidebar, text="HỆ THỐNG AI",
                               font=("Roboto", 24, "bold")).pack(pady=20)

        self.algo_var = customtkinter.StringVar(value="A*")
        algorithms_list = ["A*", "Dijkstra", "Greedy", "UCS", "BFS", "DFS", "IDS"]
        customtkinter.CTkOptionMenu(self.sidebar, values=algorithms_list,
                                    variable=self.algo_var).pack(pady=10)

        self.search_btn = customtkinter.CTkButton(
            self.sidebar, text="TÌM ĐƯỜNG", command=self.run_search,
            height=45, fg_color="#2ECC71", hover_color="#27AE60", state="disabled"
        )
        self.search_btn.pack(pady=20, padx=20)

        self.compare_btn = customtkinter.CTkButton(
            self.sidebar, text="SO SÁNH THUẬT TOÁN", command=self.run_compare,
            height=38, fg_color="#3498DB", hover_color="#2980B9", state="disabled"
        )
        self.compare_btn.pack(pady=(0, 15), padx=20)

        customtkinter.CTkButton(self.sidebar, text="LÀM MỚI",
                                command=self.clear_all, fg_color="#E74C3C").pack(pady=5)
        customtkinter.CTkButton(self.sidebar, text="XÓA VẬT CẢN",
                                command=self.clear_obstacles,
                                fg_color="#E67E22", hover_color="#D35400").pack(pady=5)
        self.stations_btn = customtkinter.CTkButton(
            self.sidebar, text="HIỆN TẤT CẢ GA TÀU",
            command=self.show_real_stations,
            fg_color="#F39C12", hover_color="#D68910"
        )
        self.stations_btn.pack(pady=5)

        customtkinter.CTkButton(
            self.sidebar, text="LỊCH SỬ TÌM KIẾM",
            command=self.show_search_history,
            fg_color="#566573", hover_color="#424949"
        ).pack(pady=5)

        self.res_box = customtkinter.CTkTextbox(self.sidebar, width=260, height=300,
                                                font=("Consolas", 13))
        self.res_box.pack(pady=20, padx=10)

        self.map_widget = tkintermapview.TkinterMapView(self, corner_radius=0)
        self.map_widget.grid(row=0, column=1, sticky="nsew")
        self.map_widget.set_position(Config.CENTER_LAT, Config.CENTER_LON)
        self.map_widget.set_zoom(14)

        self.map_widget.add_right_click_menu_command(label="Điểm đi",
                                                      command=self.set_start, pass_coords=True)
        self.map_widget.add_right_click_menu_command(label="Điểm đến",
                                                      command=self.set_goal, pass_coords=True)
        self.map_widget.add_right_click_menu_command(label="Vật cản",
                                                      command=self.set_obs, pass_coords=True)

    def show_search_history(self):
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.lift()
            self._refresh_search_history()
            return

        window = customtkinter.CTkToplevel(self)
        window.title("Lịch sử tìm kiếm SQLite")
        window.geometry("1100x650")
        window.minsize(850, 500)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(1, weight=3)
        window.grid_rowconfigure(2, weight=2)
        self.history_window = window

        toolbar = customtkinter.CTkFrame(window, corner_radius=0)
        toolbar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        customtkinter.CTkLabel(
            toolbar, text="LỊCH SỬ TÌM KIẾM", font=("Roboto", 18, "bold")
        ).pack(side="left", padx=12, pady=10)
        customtkinter.CTkButton(
            toolbar, text="LÀM MỚI", width=100,
            command=self._refresh_search_history
        ).pack(side="right", padx=5, pady=8)
        customtkinter.CTkButton(
            toolbar, text="XÓA LỊCH SỬ", width=110,
            fg_color="#C0392B", hover_color="#922B21",
            command=self._clear_search_history
        ).pack(side="right", padx=5, pady=8)

        table_frame = customtkinter.CTkFrame(window, corner_radius=0)
        table_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)

        columns = ("time", "algo", "type", "duration", "distance", "board", "alight")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "time": "Thời điểm", "algo": "Thuật toán", "type": "Loại",
            "duration": "Phút", "distance": "Quãng đường",
            "board": "Ga lên", "alight": "Ga xuống",
        }
        widths = {
            "time": 145, "algo": 85, "type": 75, "duration": 70,
            "distance": 100, "board": 210, "alight": 210,
        }
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], minwidth=60, anchor="w")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.bind("<<TreeviewSelect>>", self._show_selected_history_detail)
        self.history_tree = tree

        detail = customtkinter.CTkTextbox(window, font=("Consolas", 13))
        detail.grid(row=2, column=0, sticky="nsew", padx=10, pady=(5, 10))
        detail.configure(state="disabled")
        self.history_detail = detail
        self._refresh_search_history()

    def _refresh_search_history(self):
        if self.history_tree is None or not self.history_tree.winfo_exists():
            return
        rows = self.database.load_recent_searches(300)
        self._history_rows_by_id = {str(row["id"]): row for row in rows}
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for row in rows:
            distance = row.get("distance_m")
            self.history_tree.insert(
                "", "end", iid=str(row["id"]),
                values=(
                    row.get("timestamp") or "",
                    row.get("algorithm") or "",
                    row.get("route_type") or "",
                    f"{float(row.get('time_minutes') or 0):.1f}",
                    self._format_route_distance(distance),
                    row.get("board_station") or "",
                    row.get("alight_station") or "",
                )
            )
        self._set_history_detail("")

    def _show_selected_history_detail(self, _event=None):
        if self.history_tree is None:
            return
        selection = self.history_tree.selection()
        if not selection:
            return
        row = self._history_rows_by_id.get(selection[0])
        if not row:
            return
        station_lines = "\n".join(
            f"  {index}. {name}"
            for index, name in enumerate(row.get("passed_stations") or [], start=1)
        ) or "  Không có"
        detail = (
            f"Mã lần chạy: {row['id']}\n"
            f"Thời điểm: {row.get('timestamp')}\n"
            f"Chế độ: {row.get('run_mode')} | Thuật toán: {row.get('algorithm')}\n"
            f"Loại hành trình: {row.get('route_type')}\n"
            f"Thời gian dự kiến: {float(row.get('time_minutes') or 0):.1f} phút\n"
            f"Tổng quãng đường: {self._format_route_distance(row.get('distance_m'))}\n"
            f"Đi bộ: {self._format_route_distance(row.get('walk_distance_m'))}\n"
            f"Đi tàu: {self._format_route_distance(row.get('train_distance_m'))}\n"
            f"Số nút duyệt: {row.get('nodes_searched') or 0}\n"
            f"Thời gian xử lý: {float(row.get('exec_seconds') or 0):.4f}s\n"
            f"Ga lên: {row.get('board_station') or 'Không có'}\n"
            f"Ga xuống: {row.get('alight_station') or 'Không có'}\n"
            f"Tuyến: {', '.join(row.get('lines') or []) or 'Không có'}\n"
            f"Tọa độ: ({row.get('start_lat')}, {row.get('start_lon')})"
            f" -> ({row.get('goal_lat')}, {row.get('goal_lon')})\n\n"
            f"Các ga đi qua:\n{station_lines}"
        )
        self._set_history_detail(detail)

    def _set_history_detail(self, text):
        if self.history_detail is None or not self.history_detail.winfo_exists():
            return
        self.history_detail.configure(state="normal")
        self.history_detail.delete("1.0", "end")
        self.history_detail.insert("1.0", text)
        self.history_detail.configure(state="disabled")

    def _clear_search_history(self):
        if not messagebox.askyesno(
            "Xóa lịch sử", "Xóa toàn bộ lịch sử tìm kiếm trong SQLite?",
            parent=self.history_window
        ):
            return
        deleted = self.database.clear_search_history()
        self._refresh_search_history()
        self._set_history_detail(f"Đã xóa {deleted} lần tìm kiếm.")

    def update_log(self, txt):
        def _update():
            self.res_box.configure(state="normal")
            self.res_box.delete("1.0", "end")
            self.res_box.insert("1.0", txt)
            self.res_box.configure(state="disabled")
        self.after(0, _update)

    def _update_search_button_state(self):
        if not hasattr(self, "search_btn"):
            return
        enabled = (
            self.G is not None
            and self.walk_G is not None
            and self.start_coords is not None
            and self.goal_coords is not None
        )
        state = "normal" if enabled else "disabled"
        self.search_btn.configure(state=state, text="TÌM ĐƯỜNG")
        if hasattr(self, "compare_btn"):
            self.compare_btn.configure(state=state)

    def _algo_description(self, algo):
        descriptions = {
            "A*": "A*: dùng heuristic khoảng cách để tìm nhanh đường ngắn.",
            "Dijkstra": "Dijkstra: tương đương UCS trên graph trọng số dương.",
            "UCS": "UCS: đảm bảo tối ưu nếu trọng số không âm.",
            "Greedy": "Greedy: nhanh nhưng không đảm bảo tối ưu.",
            "BFS": "BFS: tối ưu theo số cạnh, không tối ưu theo khoảng cách.",
            "DFS": "DFS: duyệt sâu, không đảm bảo tối ưu.",
            "IDS": "IDS: lặp sâu dần, dùng để minh họa tìm kiếm.",
        }
        return descriptions.get(algo, "")

    def _build_graph_node_index(self, graph):
        return SpatialIndex(
            [
                (node, data["y"], data["x"])
                for node, data in graph.nodes(data=True)
                if "y" in data and "x" in data
            ]
        )

    def _nearest_graph_node(self, graph, node_index, lat, lon):
        if graph is None:
            raise RuntimeError("Graph chua duoc tai.")

        if node_index is not None:
            for radius in (80.0, 250.0, 1000.0, 5000.0, 20000.0):
                candidates = node_index.query_radius(lat, lon, radius, max_k=1)
                if candidates:
                    return candidates[0][1]

        best_node = None
        best_dist = float("inf")
        for node, data in graph.nodes(data=True):
            try:
                dist = fast_haversine(lat, lon, data["y"], data["x"])
            except KeyError:
                continue
            if dist < best_dist:
                best_node = node
                best_dist = dist

        if best_node is None:
            raise RuntimeError("Khong tim thay node gan nhat trong graph.")
        return best_node

    def _persist_graph_snapshot_async(self, graph_type, graph):
        if graph is None:
            return

        node_count = graph.number_of_nodes()
        edge_count = graph.number_of_edges()
        if self.database.graph_snapshot_is_current(graph_type, node_count, edge_count):
            return

        with self._db_persist_lock:
            if graph_type in self._db_persisting_graphs:
                return
            self._db_persisting_graphs.add(graph_type)

        def worker():
            try:
                saved = self.database.save_graph_snapshot(graph_type, graph)
                if saved:
                    print(f"Saved {graph_type} graph snapshot to database.")
            except Exception as exc:
                print(f"Khong luu duoc {graph_type} graph vao database:", exc)
            finally:
                with self._db_persist_lock:
                    self._db_persisting_graphs.discard(graph_type)

        threading.Thread(target=worker, daemon=True).start()

    def _restore_active_obstacles_from_database(self):
        if self.G is None or self.markers["obs"]:
            return

        try:
            active_obstacles = self.database.load_active_obstacles()
        except Exception as exc:
            print("Khong nap duoc vat can tu database:", exc)
            return

        restored_nodes = set()
        restored_coords = []
        for obstacle in active_obstacles:
            coords = (float(obstacle["lat"]), float(obstacle["lon"]))
            saved_nodes = [
                node for node in obstacle.get("blocked_nodes", [])
                if node in self.G.nodes
            ]
            nodes = list(self._rail_obstacle_nodes_for_coord(coords))
            if not nodes:
                nodes = saved_nodes
            restored_nodes.update(nodes)
            restored_coords.append(coords)
            self.markers["obs"].append(
                self.map_widget.set_marker(
                    coords[0], coords[1], text="X", marker_color_circle="red"
                )
            )

        if restored_coords:
            with self._state_lock:
                self.obstacles.update(restored_nodes)
                self.obstacle_coords.extend(restored_coords)
                self._finder_cache = None
                self._finder_obstacles_snapshot = None
            with self._walk_cache_lock:
                self._walk_distance_cache.clear()
            print(f"Restored {len(restored_coords)} active obstacles from database.")

    def _load_graph_pickle(self, file_path):
        with Path(file_path).open("rb") as file:
            return pickle.load(file)

    def _save_graph_pickle(self, graph, file_path):
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as file:
            pickle.dump(graph, file, protocol=pickle.HIGHEST_PROTOCOL)

    def _load_graph_with_cache(self, pickle_path, graphml_path, loader, label, graph_type=None):
        pickle_path = Path(pickle_path)
        graphml_path = Path(graphml_path)
        graphml_path.parent.mkdir(parents=True, exist_ok=True)

        if pickle_path.exists():
            try:
                print(f"Loading {label} from pickle cache...")
                return self._load_graph_pickle(pickle_path)
            except Exception as exc:
                print(f"Pickle cache loi, tai lai {label}:", exc)
                try:
                    pickle_path.unlink()
                except OSError:
                    pass

        if graphml_path.exists():
            try:
                print(f"Loading {label} from graphml cache...")
                graph = ox.load_graphml(graphml_path)
                self._save_graph_pickle(graph, pickle_path)
                return graph
            except Exception as exc:
                print(f"GraphML cache loi, tai lai {label}:", exc)
                try:
                    graphml_path.unlink()
                except OSError:
                    pass

        if graph_type:
            try:
                print(f"Loading {label} from SQLite database...")
                graph = self.database.load_graph_snapshot(graph_type)
                if graph is not None:
                    self._save_graph_pickle(graph, pickle_path)
                    return graph
            except Exception as exc:
                print(f"SQLite database cache loi, tai lai {label}:", exc)

        print(f"Downloading {label} from OpenStreetMap...")
        graph = loader()
        try:
            ox.save_graphml(graph, graphml_path)
            self._save_graph_pickle(graph, pickle_path)
        except Exception as exc:
            print(f"Could not save {label} cache:", exc)
        return graph

    def _load_rail_graph(self):
        return self._load_graph_with_cache(
            Config.RAIL_PICKLE_CACHE,
            Config.RAIL_GRAPHML_CACHE,
            lambda: ox.graph_from_point(
                (Config.CENTER_LAT, Config.CENTER_LON),
                dist=Config.MAP_RADIUS,
                custom_filter='["railway"~"rail|subway|tram"]',
                simplify=True
            ),
            "rail graph",
            graph_type="rail"
        )

    def _load_walk_graph(self):
        return self._load_graph_with_cache(
            Config.WALK_PICKLE_CACHE,
            Config.WALK_GRAPHML_CACHE,
            lambda: ox.graph_from_point(
                (Config.CENTER_LAT, Config.CENTER_LON),
                dist=Config.MAP_RADIUS,
                network_type="walk",
                simplify=True
            ),
            "walk graph",
            graph_type="walk"
        )

    def _remove_non_passenger_rail_edges(self, graph):
        blocked_services = {"yard", "siding", "spur", "crossover"}
        edges_to_remove = []

        if graph.is_multigraph():
            edge_iter = graph.edges(keys=True, data=True)
            for u, v, key, data in edge_iter:
                service = str(data.get("service") or "").strip().lower()
                name = str(data.get("name") or "").strip().lower()
                if service in blocked_services or "yard" in name:
                    edges_to_remove.append((u, v, key))
        else:
            edge_iter = graph.edges(data=True)
            for u, v, data in edge_iter:
                service = str(data.get("service") or "").strip().lower()
                name = str(data.get("name") or "").strip().lower()
                if service in blocked_services or "yard" in name:
                    edges_to_remove.append((u, v))

        graph.remove_edges_from(edges_to_remove)
        return len(edges_to_remove)

    def _has_edge_between(self, graph, n1, n2):
        return graph.has_edge(n1, n2) or graph.has_edge(n2, n1)

    def _add_station_transfer_edges(self, graph):
        station_nodes = [
            (node, data["y"], data["x"])
            for node, data in graph.nodes(data=True)
            if data.get("is_station")
        ]
        edges_added = 0

        for idx, (n1, y1, x1) in enumerate(station_nodes):
            for n2, y2, x2 in station_nodes[idx + 1:]:
                if self._has_edge_between(graph, n1, n2):
                    continue

                dist = fast_haversine(y1, x1, y2, x2)
                if dist > Config.TRANSFER_RADIUS_M:
                    continue

                graph.add_edge(
                    n1, n2, length=dist, mode="transfer",
                    name="walking transfer"
                )
                graph.add_edge(
                    n2, n1, length=dist, mode="transfer",
                    name="walking transfer"
                )
                edges_added += 2

        return edges_added

    def _remove_station_transfer_edges(self, graph):
        edges_to_remove = []
        if graph.is_multigraph():
            for u, v, key, data in graph.edges(keys=True, data=True):
                if data.get("mode") == "transfer":
                    edges_to_remove.append((u, v, key))
        else:
            for u, v, data in graph.edges(data=True):
                if data.get("mode") == "transfer":
                    edges_to_remove.append((u, v))
        graph.remove_edges_from(edges_to_remove)
        return len(edges_to_remove)

    def _load_cached_station_nodes(self):
        path = Path(Config.STATION_NODE_CACHE)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [int(node) for node in payload.get("station_nodes", [])]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    def _save_cached_station_nodes(self, station_nodes):
        path = Path(Config.STATION_NODE_CACHE)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"station_nodes": [int(node) for node in station_nodes]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _metro_station_tags(self):
        return {
            "railway": ["station", "halt", "stop", "tram_stop", "subway_entrance"],
        }

    def _is_metro_station_feature(self, row):
        railway = str(row.get("railway") or "").strip().lower()
        return railway in {"station", "halt", "stop", "tram_stop", "subway_entrance"}

    def _normalize_station_record(self, row, rail_graph, walk_graph):
        if not isinstance(row, dict):
            return None
        try:
            rail_node = int(row["rail_node"])
            access_node = int(row["access_node"])
            lat = float(row["lat"])
            lon = float(row["lon"])
            if rail_node not in rail_graph.nodes or access_node not in walk_graph.nodes:
                return None

            rail_data = rail_graph.nodes[rail_node]
            walk_data = walk_graph.nodes[access_node]
            rail_snap_m = fast_haversine(lat, lon, rail_data["y"], rail_data["x"])
            walk_snap_m = fast_haversine(lat, lon, walk_data["y"], walk_data["x"])
            if rail_snap_m > Config.MAX_STATION_RAIL_SNAP_M:
                return None
            if walk_snap_m > Config.MAX_STATION_WALK_SNAP_M:
                return None

            return {
                "name": str(row.get("name") or "Ga khong ten"),
                "lat": lat,
                "lon": lon,
                "rail_node": rail_node,
                "access_node": access_node,
            }
        except (KeyError, TypeError, ValueError):
            return None

    def _sanitize_station_records(self, station_records, rail_graph, walk_graph):
        valid_records = []
        seen = set()
        for row in station_records or []:
            record = self._normalize_station_record(row, rail_graph, walk_graph)
            if record is None:
                continue
            key = (record["rail_node"], record["access_node"], record["name"])
            if key in seen:
                continue
            seen.add(key)
            valid_records.append(record)
        return valid_records

    def _load_cached_station_records(self, rail_graph, walk_graph):
        path = Path(Config.STATION_RECORD_CACHE)
        if not path.exists():
            return []

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

        raw_records = payload.get("station_records", [])
        records = self._sanitize_station_records(raw_records, rail_graph, walk_graph)
        if len(records) != len(raw_records):
            print(f"Da loai {len(raw_records) - len(records)} ban ghi ga anh xa sai trong cache.")
            self._save_cached_station_records(records)
        return records

    def _save_cached_station_records(self, station_records):
        path = Path(Config.STATION_RECORD_CACHE)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"station_records": station_records}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _fetch_station_records_for_graphs(self, rail_graph, walk_graph):
        tags = self._metro_station_tags()
        stations = ox.features_from_point(
            (Config.CENTER_LAT, Config.CENTER_LON),
            dist=Config.MAP_RADIUS,
            tags=tags
        )

        rail_index = self._build_graph_node_index(rail_graph)
        walk_index = self._build_graph_node_index(walk_graph)
        records = []
        seen = set()
        for _, row in stations.iterrows():
            if not self._is_metro_station_feature(row):
                continue

            geometry = row.get("geometry")
            if geometry is None or geometry.is_empty:
                continue

            point = geometry if geometry.geom_type == "Point" else geometry.centroid
            lat, lon = float(point.y), float(point.x)
            name = str(row.get("name") or row.get("ref") or "Ga không tên").strip()

            try:
                rail_node = int(self._nearest_graph_node(rail_graph, rail_index, lat, lon))
                access_node = int(self._nearest_graph_node(walk_graph, walk_index, lat, lon))
            except Exception:
                continue

            key = (rail_node, access_node, name)
            if key in seen:
                continue
            seen.add(key)

            record = self._normalize_station_record({
                "name": name,
                "lat": lat,
                "lon": lon,
                "rail_node": rail_node,
                "access_node": access_node,
            }, rail_graph, walk_graph)
            if record is not None:
                records.append(record)

        return records

    def _load_or_build_station_records(self, rail_graph, walk_graph):
        records = self._load_cached_station_records(rail_graph, walk_graph)
        if records:
            self.database.save_station_records(records)
            return records

        raw_records = self.database.load_station_records()
        records = self._sanitize_station_records(raw_records, rail_graph, walk_graph)
        if records:
            self._save_cached_station_records(records)
            self.database.save_station_records(records)
            return records

        records = self._fetch_station_records_for_graphs(rail_graph, walk_graph)
        self._save_cached_station_records(records)
        self.database.save_station_records(records)
        return records

    def _fetch_station_nodes_for_graph(self, graph):
        tags = self._metro_station_tags()
        stations = ox.features_from_point(
            (Config.CENTER_LAT, Config.CENTER_LON),
            dist=Config.MAP_RADIUS,
            tags=tags
        )

        station_coords = []
        for _, row in stations.iterrows():
            if not self._is_metro_station_feature(row):
                continue

            geometry = row.get("geometry")
            if geometry is None or geometry.is_empty:
                continue
            point = geometry if geometry.geom_type == "Point" else geometry.centroid
            station_coords.append((float(point.y), float(point.x)))

        if not station_coords:
            return []

        node_index = self._build_graph_node_index(graph)
        nearest_nodes = []
        for lat, lon in station_coords:
            node = self._nearest_graph_node(graph, node_index, lat, lon)
            node_data = graph.nodes[node]
            snap_m = fast_haversine(lat, lon, node_data["y"], node_data["x"])
            if snap_m <= Config.MAX_STATION_RAIL_SNAP_M:
                nearest_nodes.append(node)
        return sorted({int(node) for node in nearest_nodes})

    def _tag_station_nodes(self, graph):
        station_nodes = self._load_cached_station_nodes()
        station_nodes = [node for node in station_nodes if node in graph.nodes]

        if not station_nodes:
            station_nodes = self._fetch_station_nodes_for_graph(graph)
            self._save_cached_station_nodes(station_nodes)

        for node in station_nodes:
            if node in graph.nodes:
                graph.nodes[node]["is_station"] = True

        return len(station_nodes)

    def _load_map_worker(self):
        self.after(0, lambda: self.update_log("Dang tai mang duong ray..."))

        try:
            graph = self._load_rail_graph()
        except Exception as e:
            self.after(0, lambda err=e: self.update_log(
                "Khong tai duoc du lieu ban do OSM.\n"
                "Kiem tra ket noi mang hoac thu lai sau.\n\n"
                f"Loi: {err}"
            ))
            return

        removed_edges = self._remove_non_passenger_rail_edges(graph)
        self.after(0, lambda: self.update_log("Dang gan the cac nha ga hop le..."))

        try:
            station_count = self._tag_station_nodes(graph)
        except Exception as e:
            station_count = 0
            print("Khong tai duoc danh sach nha ga:", e)

        self.after(0, lambda: self.update_log("Dang xay dung mang chuyen tram..."))

        edges_added = self._add_station_transfer_edges(graph)

        final_msg = (
            f"Da nap mang tau: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges.\n"
            f"Da gan {station_count} ga, them {edges_added} canh chuyen tram.\n"
            f"Da loc {removed_edges} canh ray ky thuat.\n\n"
            "Mang tau da san sang.\n"
            "Dang tai mang di bo, vui long doi de ve duong pho that."
        )
        self.after(0, lambda g=graph, msg=final_msg: self._commit_loaded_graph(g, msg))
        threading.Thread(target=self._load_walk_graph_worker, daemon=True).start()

    def _commit_loaded_graph(self, graph, message):
        with self._state_lock:
            self.G = graph
            self.rail_node_index = self._build_graph_node_index(graph)
            self._finder_cache = None
            self._finder_obstacles_snapshot = None
        self._restore_active_obstacles_from_database()
        self._persist_graph_snapshot_async("rail", graph)
        self._update_search_button_state()
        self.update_log(message)

    def _load_walk_graph_worker(self):
        self.after(0, lambda: self.update_log(
            "Mang tau da san sang. Dang tai them mang di bo..."
        ))
        try:
            walk_graph = self._load_walk_graph()
        except Exception as e:
            print("Khong tai duoc walk graph:", e)
            self.after(0, lambda: self.update_log(
                "Mang tau da san sang.\n"
                "Khong tai duoc mang di bo, app se dung doan noi thang du phong."
            ))
            return

        try:
            with self._state_lock:
                rail_graph = self.G
            station_records = self._load_or_build_station_records(rail_graph, walk_graph)
        except Exception as e:
            print("Khong tao duoc station records:", e)
            station_records = []

        self.after(0, lambda wg=walk_graph, sr=station_records: self._commit_walk_graph(wg, sr))

    def _commit_walk_graph(self, walk_graph, station_records):
        station_records = self._sanitize_station_records(station_records, self.G, walk_graph)
        self._save_cached_station_records(station_records)
        self.walk_G = walk_graph
        self.walk_node_index = self._build_graph_node_index(walk_graph)
        self.station_records = station_records
        try:
            self.database.save_station_records(station_records)
        except Exception as exc:
            print("Khong luu duoc danh sach ga vao database:", exc)
        self._persist_graph_snapshot_async("walk", walk_graph)
        self.station_name_by_rail_node = {}
        for record in station_records:
            try:
                rail_node = int(record.get("rail_node"))
            except (TypeError, ValueError):
                continue
            if rail_node not in self.station_name_by_rail_node:
                self.station_name_by_rail_node[rail_node] = str(record.get("name") or "Ga không tên")
        with self._walk_cache_lock:
            self._walk_distance_cache.clear()
        extra_transfer_edges = 0
        with self._state_lock:
            if self.G is not None:
                self._remove_station_transfer_edges(self.G)
                for _, data in self.G.nodes(data=True):
                    data.pop("is_station", None)
                for record in station_records:
                    rail_node = int(record.get("rail_node", -1))
                    if rail_node in self.G.nodes:
                        self.G.nodes[rail_node]["is_station"] = True
                self._save_cached_station_nodes({
                    int(record["rail_node"]) for record in station_records
                })
                extra_transfer_edges = self._add_station_transfer_edges(self.G)
                self._finder_cache = None
                self._finder_obstacles_snapshot = None
        if extra_transfer_edges:
            self._persist_graph_snapshot_async("rail", self.G)
        self._update_search_button_state()
        if not self.path_objects:
            self.update_log(
                "Mang tau va mang di bo da san sang.\n"
                f"Da nap {len(station_records)} ban ghi ga.\n"
                f"Da bo sung {extra_transfer_edges} canh chuyen tram.\n"
                "Cac doan di bo va chon ga se uu tien duong pho neu co duong hop le."
            )

    def _clear_station_markers_if_current(self, token):
        if token != self._station_fetch_token:
            return
        for sm in self.station_markers:
            sm.delete()
        self.station_markers.clear()

    def _clear_route_markers(self):
        for marker in self.route_markers:
            marker.delete()
        self.route_markers.clear()

    def show_real_stations(self):
        with self._station_fetch_lock:
            if self._fetching_stations:
                self.update_log("Dang tai danh sach ga, vui long cho...")
                return
            self._fetching_stations = True
            self._station_fetch_token += 1
            token = self._station_fetch_token
        self.stations_btn.configure(state="disabled")
        self._clear_station_markers_if_current(token)
        self.after(0, lambda: self.update_log("Đang tìm vị trí các nhà ga thực tế..."))
        threading.Thread(target=self._fetch_stations, args=(token,), daemon=True).start()

    def _fetch_stations(self, token):
        try:
            count = 0
            if self.station_records:
                station_rows = [
                    (float(record["lat"]), float(record["lon"]), record.get("name", "Trạm Vô Danh"))
                    for record in self.station_records
                ]
            else:
                tags = self._metro_station_tags()
                stations = ox.features_from_point(
                    (Config.CENTER_LAT, Config.CENTER_LON), dist=Config.MAP_RADIUS, tags=tags)
                station_rows = []
                for _, row in stations.iterrows():
                    if not self._is_metro_station_feature(row):
                        continue

                    geometry = row.get("geometry")
                    if geometry is None or geometry.is_empty:
                        continue
                    point = geometry if geometry.geom_type == "Point" else geometry.centroid
                    station_rows.append((point.y, point.x, row.get("name", "Trạm Vô Danh")))

            for lat, lon, name in station_rows:
                def draw_and_save(l=lat, lo=lon, n=name, t=token):
                    if t != self._station_fetch_token:
                        return
                    m = self.map_widget.set_marker(
                        l, lo, icon=self.tiny_dot, text=None,
                        data=n, command=self.on_station_click)
                    if t != self._station_fetch_token:
                        m.delete()
                        return
                    self.station_markers.append(m)

                self.after(0, draw_and_save)
                count += 1

            self.after(0, lambda t=token, c=count: self.update_log(
                f"Đã tìm thấy và hiển thị {c} nhà ga trên bản đồ."
            ) if t == self._station_fetch_token else None)
        except Exception:
            self.after(0, lambda t=token: self.update_log(
                "Không thể tải nhà ga. Vui lòng thử lại."
            ) if t == self._station_fetch_token else None)

        self.after(0, lambda t=token: self._finish_fetch_stations(t))

    def _finish_fetch_stations(self, token):
        with self._station_fetch_lock:
            if token != self._station_fetch_token:
                return
            self._fetching_stations = False
        self.stations_btn.configure(state="normal")

    def on_station_click(self, marker):
        self.update_log(f"THÔNG TIN GA TÀU:\nTên ga: {marker.data}")

    def set_start(self, coords):
        if self.markers["start"]:
            self.markers["start"].delete()
        self.start_coords = coords
        self.markers["start"] = self.map_widget.set_marker(
            coords[0], coords[1], text="Điểm đi", marker_color_circle="green")
        self._update_search_button_state()

    def set_goal(self, coords):
        if self.markers["goal"]:
            self.markers["goal"].delete()
        self.goal_coords = coords
        self.markers["goal"] = self.map_widget.set_marker(
            coords[0], coords[1], text="Điểm đến", marker_color_circle="blue")
        self._update_search_button_state()

    def set_obs(self, coords):
        if not self.G:
            self.after(0, lambda: self.update_log("Vui lòng chờ bản đồ tải xong."))
            return
        try:
            blocked_nodes = self._rail_obstacle_nodes_for_coord(coords)
            if not blocked_nodes:
                raise RuntimeError("Không tìm thấy node/cạnh rail nào gần vật cản.")

            with self._state_lock:
                self.obstacles.update(blocked_nodes)
                self.obstacle_coords.append(coords)
                self._finder_cache = None
                self._finder_obstacles_snapshot = None
            with self._walk_cache_lock:
                self._walk_distance_cache.clear()
            try:
                self.database.save_obstacle(
                    coords[0], coords[1], Config.OBSTACLE_RADIUS_M, blocked_nodes
                )
            except Exception as exc:
                print("Khong luu duoc vat can vao database:", exc)

            for p in self.path_objects:
                p.delete()
            self.path_objects.clear()
            self._clear_route_markers()

            self.markers["obs"].append(
                self.map_widget.set_marker(coords[0], coords[1], text="X",
                                           marker_color_circle="red"))
            self.update_log(
                f"Đã đặt vật cản bán kính {Config.OBSTACLE_RADIUS_M:.0f}m.\n"
                f"Đã chặn {len(blocked_nodes)} node/cạnh rail gần điểm này.\n"
                f"Bấm 'Tìm đường' để tính lại tuyến tránh vật cản."
            )
        except Exception as e:
            self.after(0, lambda err=e: self.update_log(f"Không thể đặt vật cản tại vị trí này.\nLỗi: {err}"))

    def clear_obstacles(self):
        for m in self.markers["obs"]:
            m.delete()
        self.markers["obs"].clear()
        with self._state_lock:
            self.obstacles.clear()
            self.obstacle_coords.clear()
            self._finder_cache = None
            self._finder_obstacles_snapshot = None
        with self._walk_cache_lock:
            self._walk_distance_cache.clear()
        try:
            self.database.deactivate_obstacles()
        except Exception as exc:
            print("Khong cap nhat database vat can:", exc)

        for p in self.path_objects:
            p.delete()
        self.path_objects.clear()
        self._clear_route_markers()
        self.update_log("Đã gỡ bỏ toàn bộ vật cản.\nBạn có thể bấm 'Tìm đường' lại.")

    def clear_all(self):
        with self._station_fetch_lock:
            self._station_fetch_token += 1
            token = self._station_fetch_token
            self._fetching_stations = False
        if hasattr(self, "stations_btn"):
            self.stations_btn.configure(state="normal")
        for p in self.path_objects:
            p.delete()
        self._clear_route_markers()
        for m in self.markers["obs"]:
            m.delete()
        self.markers["obs"].clear()
        self._clear_station_markers_if_current(token)

        if self.markers["start"]:
            self.markers["start"].delete()
        if self.markers["goal"]:
            self.markers["goal"].delete()
        self.markers["start"] = None
        self.markers["goal"] = None

        with self._state_lock:
            self.obstacles.clear()
            self.obstacle_coords.clear()
            self._finder_cache = None
            self._finder_obstacles_snapshot = None
        with self._walk_cache_lock:
            self._walk_distance_cache.clear()
        try:
            self.database.deactivate_obstacles()
        except Exception as exc:
            print("Khong cap nhat database vat can:", exc)
        self.path_objects.clear()
        self.start_coords = None
        self.goal_coords = None
        self._update_search_button_state()
        self.update_log("Đã xóa sạch bản đồ.")

    def run_search(self):
        if not self.G:
            self.update_log("Bản đồ đang tải, vui lòng chờ...")
            return

        if not self.walk_G:
            self.update_log("Mạng đi bộ chưa tải xong, vui lòng chờ để app vẽ đường phố thật.")
            return

        if not self.start_coords or not self.goal_coords:
            self.update_log("Vui lòng chọn điểm đi và điểm đến trước.")
            return
        for p in self.path_objects:
            p.delete()
        self.path_objects.clear()
        self._clear_route_markers()
        with self._walk_cache_lock:
            self._walk_distance_cache.clear()
        algo = self.algo_var.get()
        self.search_btn.configure(state="disabled", text="Đang tìm...")
        if hasattr(self, "compare_btn"):
            self.compare_btn.configure(state="disabled")
        warning = ""
        if algo in {"DFS", "IDS"}:
            warning = "\nLưu ý: DFS/IDS dùng để minh họa duyệt đồ thị, không đảm bảo đường tối ưu."
        desc = self._algo_description(algo)
        self.update_log(f"Đang chạy {algo}...\n{desc}{warning}")
        start = self.start_coords
        goal = self.goal_coords
        threading.Thread(target=self._search_worker, args=(start, goal), daemon=True).start()

    def run_compare(self):
        if not self.G:
            self.update_log("Bản đồ đang tải, vui lòng chờ...")
            return

        if not self.walk_G:
            self.update_log("Mạng đi bộ chưa tải xong, vui lòng chờ để so sánh bằng đường phố thật.")
            return

        if not self.start_coords or not self.goal_coords:
            self.update_log("Vui lòng chọn điểm đi và điểm đến trước.")
            return

        for p in self.path_objects:
            p.delete()
        self.path_objects.clear()
        self._clear_route_markers()

        self.search_btn.configure(state="disabled")
        self.compare_btn.configure(state="disabled", text="Đang so sánh...")
        self.update_log("Đang so sánh tất cả thuật toán...\nQuá trình này có thể mất lâu với DFS/IDS.")

        start = self.start_coords
        goal = self.goal_coords
        threading.Thread(target=self._compare_worker, args=(start, goal), daemon=True).start()

    def _compare_worker(self, start_coords, goal_coords):
        algorithms = ["A*", "Dijkstra", "Greedy", "UCS", "BFS", "DFS", "IDS"]
        rows = []

        try:
            finder = self._get_finder()
            start_candidates, goal_candidates, candidate_mode = self._route_station_candidates(
                start_coords, goal_coords
            )
            _, direct_walk_dist, _ = self._walk_segment_coords(start_coords, goal_coords)

            for algo in algorithms:
                started = time.time()
                try:
                    res = find_smart_route(
                        start_coords,
                        goal_coords,
                        self.G,
                        finder,
                        algo,
                        start_candidates=list(start_candidates) if start_candidates else None,
                        goal_candidates=list(goal_candidates) if goal_candidates else None,
                        candidate_mode=candidate_mode,
                        direct_walk_dist=direct_walk_dist
                    )
                    res = self._decorate_route_stations(res)
                    res = self._apply_walk_graph_to_result(res)
                    res = self._add_route_directions(res)
                    elapsed = time.time() - started
                    self._save_search_history(
                        start_coords,
                        goal_coords,
                        res,
                        elapsed,
                        algorithm=algo,
                        run_mode="compare"
                    )
                    rows.append({
                        "algo": algo,
                        "seconds": elapsed,
                        "nodes": res.get("nodes", 0),
                        "dist": res.get("dist", float("inf")),
                        "type": res.get("type", "ERR"),
                        "optimal": "Có" if algo in {"A*", "Dijkstra", "UCS"} else "Không chắc",
                    })
                except Exception as exc:
                    rows.append({
                        "algo": algo,
                        "error": str(exc),
                    })
        except Exception as exc:
            self.after(0, lambda err=exc: self._show_search_error(err))
            return

        self.after(0, lambda r=rows: self._show_compare_result(r))

    def _show_compare_result(self, rows):
        self._update_search_button_state()
        if hasattr(self, "compare_btn"):
            self.compare_btn.configure(text="SO SÁNH THUẬT TOÁN")

        lines = ["SO SÁNH THUẬT TOÁN", "-" * 24]
        for row in rows:
            if "error" in row:
                lines.append(f"{row['algo']}: lỗi - {row['error']}")
                continue
            dist_text = "inf" if row["dist"] == float("inf") else f"{row['dist']:.0f}m"
            lines.append(
                f"{row['algo']}: {row['seconds']:.3f}s | "
                f"{row['nodes']} nút | {dist_text} | "
                f"{row['type']} | Tối ưu: {row['optimal']}"
            )

        lines.append("")
        lines.append("A*/Dijkstra/UCS tối ưu theo trọng số cạnh.")
        lines.append("Greedy/BFS/DFS/IDS dùng để so sánh, không luôn tối ưu quãng đường.")
        self.update_log("\n".join(lines))

    def _station_candidates_by_walk_graph(self, coord, reverse=False, max_k=None):
        if self.walk_G is None or not self.station_records:
            return None

        try:
            walk_graph = self.walk_G
            access_node = int(self._nearest_graph_node(
                walk_graph, self.walk_node_index, coord[0], coord[1]
            ))
        except Exception as e:
            print("Khong snap duoc diem vao mang di bo:", e)
            return None

        cache_key = (access_node, bool(reverse))
        with self._walk_cache_lock:
            distances = self._walk_distance_cache.get(cache_key)
        if distances is None:
            try:
                graph = self._walk_graph_avoiding_obstacles(access_node, access_node, undirected=True)
                distances = nx.single_source_dijkstra_path_length(
                    graph,
                    access_node,
                    cutoff=Config.MAX_WALK_TO_STATION,
                    weight="length"
                )
            except Exception as e:
                print("Khong tinh duoc ga gan bang mang di bo:", e)
                return None

            with self._walk_cache_lock:
                if len(self._walk_distance_cache) >= Config.WALK_DISTANCE_CACHE_MAX:
                    self._walk_distance_cache.clear()
                self._walk_distance_cache[cache_key] = distances

        best_by_rail_node = {}
        for record in self.station_records:
            try:
                rail_node = int(record["rail_node"])
            except (KeyError, TypeError, ValueError):
                continue

            if rail_node not in self.G.nodes:
                continue
            rail_data = self.G.nodes[rail_node]
            try:
                access_station = int(self._nearest_graph_node(
                    walk_graph,
                    self.walk_node_index,
                    rail_data["y"],
                    rail_data["x"],
                ))
            except Exception:
                continue

            distance = distances.get(access_station)
            if distance is None:
                continue

            current = best_by_rail_node.get(rail_node)
            if current is None or distance < current[0]:
                best_by_rail_node[rail_node] = (float(distance), rail_node)

        candidates = sorted(best_by_rail_node.values(), key=lambda item: item[0])
        if not candidates:
            return None
        limit = max_k or Config.MAX_WALK_GRAPH_STATION_CANDIDATES
        return candidates[:limit]

    def _route_station_candidates(self, start_coords, goal_coords):
        with self._state_lock:
            has_obstacles = bool(self.obstacles)
        candidate_limit = (
            Config.EXTENDED_STATION_CANDIDATES
            if has_obstacles
            else Config.MAX_WALK_GRAPH_STATION_CANDIDATES
        )
        start_candidates = self._station_candidates_by_walk_graph(
            start_coords, reverse=False, max_k=candidate_limit
        )
        goal_candidates = self._station_candidates_by_walk_graph(
            goal_coords, reverse=True, max_k=candidate_limit
        )

        # When the walking graph is available, an empty candidate list means
        # there is no station reachable within the walking limit. Falling back
        # to straight-line candidates would select stations that are close on
        # the map but very far by actual streets.
        if self.walk_G is not None:
            mode = "walk_graph+obstacle_recovery" if has_obstacles else "walk_graph"
            return start_candidates or [], goal_candidates or [], mode
        return None, None, "straight"

    def _station_name_for_rail_node(self, rail_node):
        if rail_node is None:
            return "Ga không xác định"

        try:
            rail_node = int(rail_node)
        except (TypeError, ValueError):
            return "Ga không xác định"

        station_name = self.station_name_by_rail_node.get(rail_node)
        if station_name:
            return station_name

        if self.G is not None and rail_node in self.G.nodes:
            node = self.G.nodes[rail_node]
            line = node.get("name") or node.get("ref")
            if line:
                return str(line)

        return f"Node {rail_node}"

    def _project_to_local_meters(self, lat, lon, ref_lat, ref_lon):
        radius = 6371000.0
        x = math.radians(lon - ref_lon) * radius * math.cos(math.radians(ref_lat))
        y = math.radians(lat - ref_lat) * radius
        return x, y

    def _point_to_segment_distance_m(self, point, start, end):
        px, py = self._project_to_local_meters(point[0], point[1], point[0], point[1])
        ax, ay = self._project_to_local_meters(start[0], start[1], point[0], point[1])
        bx, by = self._project_to_local_meters(end[0], end[1], point[0], point[1])
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        if denom <= 0:
            return fast_haversine(point[0], point[1], start[0], start[1])
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        nearest_x = ax + t * dx
        nearest_y = ay + t * dy
        return math.hypot(px - nearest_x, py - nearest_y)

    def _edge_coords_for_obstacle_check(self, graph, u, v, data):
        geometry = data.get("geometry") if isinstance(data, dict) else None
        if geometry is not None:
            try:
                return [(lat, lon) for lon, lat in geometry.coords]
            except Exception:
                pass
        return [(graph.nodes[u]["y"], graph.nodes[u]["x"]), (graph.nodes[v]["y"], graph.nodes[v]["x"])]

    def _polyline_distance_to_coord_m(self, point, coords):
        if not coords:
            return float("inf")
        if len(coords) == 1:
            return fast_haversine(point[0], point[1], coords[0][0], coords[0][1])
        return min(
            self._point_to_segment_distance_m(point, coords[i], coords[i + 1])
            for i in range(len(coords) - 1)
        )

    def _rail_obstacle_nodes_for_coord(self, coords):
        if self.G is None:
            return set()

        radius = Config.OBSTACLE_RADIUS_M
        blocked = set()

        for node, data in self.G.nodes(data=True):
            if fast_haversine(coords[0], coords[1], data["y"], data["x"]) <= radius:
                blocked.add(node)

        for u, v, data in self.G.edges(data=True):
            edge_coords = self._edge_coords_for_obstacle_check(self.G, u, v, data)
            if self._polyline_distance_to_coord_m(coords, edge_coords) <= radius:
                blocked.add(u)
                blocked.add(v)

        if not blocked:
            try:
                blocked.add(self._nearest_graph_node(
                    self.G, self.rail_node_index, coords[0], coords[1]
                ))
            except Exception:
                pass

        return blocked

    def _blocked_walk_nodes_for_obstacles(self, graph):
        with self._state_lock:
            obstacle_coords = list(self.obstacle_coords)

        if not obstacle_coords:
            return set()

        blocked = set()
        radius = Config.OBSTACLE_RADIUS_M
        for node, data in graph.nodes(data=True):
            node_coord = (data["y"], data["x"])
            if any(
                fast_haversine(obs[0], obs[1], node_coord[0], node_coord[1]) <= radius
                for obs in obstacle_coords
            ):
                blocked.add(node)
        return blocked

    def _walk_graph_avoiding_obstacles(self, start_node=None, end_node=None, undirected=False):
        if self.walk_G is None:
            return None

        graph = self.walk_G
        blocked = self._blocked_walk_nodes_for_obstacles(self.walk_G)
        if start_node is not None:
            blocked.discard(start_node)
        if end_node is not None:
            blocked.discard(end_node)

        if blocked:
            allowed_nodes = [node for node in self.walk_G.nodes if node not in blocked]
            graph = self.walk_G.subgraph(allowed_nodes)

        if undirected:
            return graph.to_undirected(as_view=True)
        return graph

    def _passed_station_names_for_result(self, res):
        if not res or res.get("type") != "MIXED" or self.G is None:
            return []

        train_nodes = []
        for seg in res.get("segments", []):
            if seg.get("mode") == "train":
                train_nodes = seg.get("nodes") or []
                break

        names = []
        seen = set()
        for node in train_nodes:
            try:
                node = int(node)
            except (TypeError, ValueError):
                continue

            if node not in self.G.nodes:
                continue

            if not self.G.nodes[node].get("is_station") and node not in self.station_name_by_rail_node:
                continue

            name = self._station_name_for_rail_node(node)
            if not name or name == "Ga không xác định" or name in seen:
                continue
            names.append(name)
            seen.add(name)

        board_name = self._station_name_for_rail_node(res.get("board_node"))
        alight_name = self._station_name_for_rail_node(res.get("alight_node"))

        if board_name and board_name != "Ga không xác định":
            names = [name for name in names if name != board_name]
            names.insert(0, board_name)

        if alight_name and alight_name != "Ga không xác định":
            names = [name for name in names if name != alight_name]
            names.append(alight_name)

        return names

    def _decorate_route_stations(self, res):
        if not res or res.get("type") != "MIXED":
            return res

        updated = dict(res)
        updated["board_name"] = self._station_name_for_rail_node(updated.get("board_node"))
        updated["alight_name"] = self._station_name_for_rail_node(updated.get("alight_node"))
        updated["passed_stations"] = self._passed_station_names_for_result(updated)
        return updated

    def _format_route_distance(self, distance_m):
        distance_m = max(0.0, float(distance_m or 0.0))
        if distance_m >= 1000.0:
            return f"{distance_m / 1000.0:.1f} km"
        return f"{distance_m:.0f} m"

    def _add_route_directions(self, res):
        if not res:
            return res

        updated = dict(res)
        walk_distances = list(updated.get("walk_segment_distances") or [])
        directions = []

        if updated.get("type") == "WALK":
            distance = walk_distances[0] if walk_distances else updated.get(
                "walk_dist", updated.get("dist", 0.0)
            )
            directions.append(
                f"Đi bộ theo đường phố {self._format_route_distance(distance)} tới điểm đến."
            )
        elif updated.get("type") == "MIXED":
            board_name = updated.get("board_name") or "ga lên tàu"
            alight_name = updated.get("alight_name") or "ga xuống tàu"
            first_walk = walk_distances[0] if walk_distances else 0.0
            last_walk = walk_distances[-1] if len(walk_distances) >= 2 else 0.0
            line_text = ", ".join(updated.get("lines") or []) or "tuyến phù hợp"
            passed_count = len(updated.get("passed_stations") or [])

            directions.append(
                f"Đi bộ {self._format_route_distance(first_walk)} tới ga {board_name}."
            )
            directions.append(f"Lên tàu {line_text} tại ga {board_name}.")
            train_step = f"Đi tàu {self._format_route_distance(updated.get('train_dist', 0.0))}"
            if passed_count:
                train_step += f", qua {passed_count} ga"
            directions.append(train_step + ".")
            directions.append(f"Xuống tàu tại ga {alight_name}.")
            directions.append(
                f"Đi bộ {self._format_route_distance(last_walk)} từ ga tới điểm đến."
            )

        updated["directions"] = directions
        return updated

    def _walk_segment_coords(self, start_coord, end_coord):
        straight_dist = fast_haversine(
            start_coord[0], start_coord[1],
            end_coord[0], end_coord[1]
        )
        if straight_dist < 1.0:
            return [start_coord, end_coord], 0.0, True
        if self.walk_G is None:
            return [start_coord, end_coord], straight_dist, False

        try:
            start_node = self._nearest_graph_node(
                self.walk_G, self.walk_node_index, start_coord[0], start_coord[1]
            )
            end_node = self._nearest_graph_node(
                self.walk_G, self.walk_node_index, end_coord[0], end_coord[1]
            )
            walk_graph = self._walk_graph_avoiding_obstacles(start_node, end_node, undirected=True)
            path_nodes = nx.shortest_path(walk_graph, start_node, end_node, weight="length")
            coords = self._path_nodes_to_map_coords(walk_graph, path_nodes)
            if not coords:
                return [start_coord, end_coord], straight_dist, False

            routed_dist = _path_length(walk_graph, path_nodes)
            start_connector = fast_haversine(start_coord[0], start_coord[1], coords[0][0], coords[0][1])
            end_connector = fast_haversine(end_coord[0], end_coord[1], coords[-1][0], coords[-1][1])

            if start_connector > 5:
                coords.insert(0, start_coord)
                routed_dist += start_connector
            if end_connector > 5:
                coords.append(end_coord)
                routed_dist += end_connector

            return coords, routed_dist, True
        except Exception as e:
            print("Khong tim duoc walk path, fallback duong thang:", e)
            return [start_coord, end_coord], straight_dist, False

    def _edge_to_map_coords(self, graph, u, v):
        fallback = [(graph.nodes[u]["y"], graph.nodes[u]["x"]), (graph.nodes[v]["y"], graph.nodes[v]["x"])]
        edge_data = graph.get_edge_data(u, v)
        reverse_coords = False
        if not edge_data:
            edge_data = graph.get_edge_data(v, u)
            reverse_coords = bool(edge_data)
        if not edge_data:
            return fallback

        if isinstance(edge_data, dict) and edge_data and all(isinstance(value, dict) for value in edge_data.values()):
            data = min(edge_data.values(), key=lambda item: float(item.get("length", 1.0)))
        else:
            data = edge_data

        geometry = data.get("geometry") if isinstance(data, dict) else None
        if geometry is None:
            return fallback

        try:
            coords = [(lat, lon) for lon, lat in geometry.coords]
            return coords[::-1] if reverse_coords else coords
        except Exception:
            return fallback

    def _path_nodes_to_map_coords(self, graph, path_nodes):
        if len(path_nodes) < 2:
            return [(graph.nodes[n]["y"], graph.nodes[n]["x"]) for n in path_nodes]

        coords = []
        for u, v in zip(path_nodes, path_nodes[1:]):
            segment = self._edge_to_map_coords(graph, u, v)
            if not segment:
                continue
            if coords and coords[-1] == segment[0]:
                coords.extend(segment[1:])
            else:
                coords.extend(segment)
        return coords

    def _apply_walk_graph_to_result(self, res):
        if not res or "segments" not in res:
            return res
        if res.get("type") == "NO_ROUTE":
            return res

        new_segments = []
        walk_dist = 0.0
        walk_segment_distances = []
        routed_walk = False

        for seg in res["segments"]:
            if seg.get("mode") != "walk" or len(seg.get("coords", [])) < 2:
                new_segments.append(seg)
                continue

            start_coord = seg["coords"][0]
            end_coord = seg["coords"][-1]
            coords, dist, routed = self._walk_segment_coords(start_coord, end_coord)
            walk_dist += dist
            walk_segment_distances.append(dist)
            routed_walk = routed_walk or routed
            new_segments.append({"mode": "walk", "coords": coords})

        updated = dict(res)
        updated["segments"] = new_segments
        updated["walk_dist"] = walk_dist
        updated["walk_segment_distances"] = walk_segment_distances
        updated["walk_routed"] = routed_walk

        train_dist = float(updated.get("train_dist", 0.0))
        updated["dist"] = walk_dist + train_dist
        if updated.get("type") == "MIXED":
            max_destination_walk = float(
                updated.get("max_destination_walk", Config.MAX_WALK_TO_STATION)
            )
            endpoint_limits = [Config.MAX_WALK_TO_STATION, max_destination_walk]
            endpoint_walk_too_long = any(
                distance > endpoint_limits[min(index, len(endpoint_limits) - 1)]
                for index, distance in enumerate(walk_segment_distances)
            )
            mixed_time = (
                walk_dist / Config.WALK_SPEED
                + train_dist / Config.TRAIN_SPEED
                + Config.WAITING_PENALTY_SEC
            ) / 60
            direct_walk_dist = updated.get("direct_walk_dist")
            try:
                direct_walk_time = float(direct_walk_dist) / Config.WALK_SPEED / 60
            except (TypeError, ValueError):
                direct_walk_time = None
            mixed_not_faster = (
                direct_walk_time is not None
                and mixed_time >= direct_walk_time
            )

            if endpoint_walk_too_long or mixed_not_faster:
                first_coord = res["segments"][0]["coords"][0]
                last_coord = res["segments"][-1]["coords"][-1]
                coords, direct_walk_dist, direct_routed = self._walk_segment_coords(
                    first_coord, last_coord
                )
                fallback_reason = (
                    "Tuyen tau bi loai vi co doan di bo toi hoac roi ga vuot gioi han."
                    if endpoint_walk_too_long
                    else "Co tuyen tau nhung tong thoi gian thuc te khong nhanh hon di bo."
                )
                updated.update({
                    "type": "WALK",
                    "time": direct_walk_dist / Config.WALK_SPEED / 60,
                    "dist": direct_walk_dist,
                    "walk_dist": direct_walk_dist,
                    "walk_segment_distances": [direct_walk_dist],
                    "train_dist": 0.0,
                    "segments": [{"mode": "walk", "coords": coords}],
                    "lines": [],
                    "passed_stations": [],
                    "board_node": None,
                    "alight_node": None,
                    "board_coord": None,
                    "alight_coord": None,
                    "board_name": "",
                    "alight_name": "",
                    "walk_routed": direct_routed,
                    "fallback_reason": fallback_reason,
                })
                return updated

            updated["time"] = mixed_time
        else:
            updated["time"] = walk_dist / Config.WALK_SPEED / 60

        return updated

    def _coords_distance(self, coords):
        total = 0.0
        for start, end in zip(coords, coords[1:]):
            total += fast_haversine(start[0], start[1], end[0], end[1])
        return total

    def _segments_for_history(self, segments):
        rows = []
        for segment in segments or []:
            coords = []
            for coord in segment.get("coords", []):
                try:
                    coords.append([float(coord[0]), float(coord[1])])
                except (TypeError, ValueError, IndexError):
                    continue

            nodes = []
            for node in segment.get("nodes", []):
                nodes.append(str(node))

            rows.append({
                "mode": segment.get("mode", ""),
                "distance_m": self._coords_distance(coords) if len(coords) >= 2 else 0.0,
                "coords": coords,
                "nodes": nodes,
            })
        return rows

    def _save_search_history(self, start_coords, goal_coords, res, exec_time,
                             algorithm=None, run_mode="search"):
        try:
            Config.CACHE_DIR.mkdir(parents=True, exist_ok=True)

            record = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "run_mode": run_mode,
                "algorithm": algorithm or self.algo_var.get(),
                "start_lat": float(start_coords[0]),
                "start_lon": float(start_coords[1]),
                "goal_lat": float(goal_coords[0]),
                "goal_lon": float(goal_coords[1]),
                "route_type": res.get("type"),
                "time_minutes": float(res.get("time", 0.0)) if res.get("time") != float("inf") else None,
                "distance_m": float(res.get("dist", 0.0)) if res.get("dist") != float("inf") else None,
                "walk_distance_m": float(res.get("walk_dist", 0.0)),
                "train_distance_m": float(res.get("train_dist", 0.0)),
                "nodes_searched": int(res.get("nodes", 0)),
                "exec_seconds": float(exec_time),
                "candidate_mode": res.get("candidate_mode", ""),
                "board_station": res.get("board_name", ""),
                "alight_station": res.get("alight_name", ""),
                "lines": res.get("lines", []),
                "passed_stations": res.get("passed_stations", []),
                "directions": res.get("directions", []),
                "segments": self._segments_for_history(res.get("segments", [])),
            }

            csv_row = dict(record)
            csv_row["lines"] = " -> ".join(record["lines"])
            csv_row["passed_stations"] = " -> ".join(record["passed_stations"])
            csv_row["directions"] = " | ".join(record["directions"])
            csv_row["segments"] = json.dumps(record["segments"], ensure_ascii=False)

            with self._history_lock:
                with open(Config.SEARCH_HISTORY_JSONL, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

                csv_path = Path(Config.SEARCH_HISTORY_CSV)
                write_header = not csv_path.exists() or csv_path.stat().st_size == 0
                with open(csv_path, "a", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(csv_row.keys()))
                    if write_header:
                        writer.writeheader()
                    writer.writerow(csv_row)

                self.database.save_search(record)

            return str(Config.SEARCH_HISTORY_DB)
        except Exception as exc:
            print("Khong luu duoc lich su tim duong:", exc)
            return None

    def _search_worker(self, start_coords, goal_coords):
        start_time = time.time()
        try:
            finder = self._get_finder()
            start_candidates, goal_candidates, candidate_mode = self._route_station_candidates(
                start_coords, goal_coords
            )
            _, direct_walk_dist, _ = self._walk_segment_coords(start_coords, goal_coords)
            res = find_smart_route(start_coords, goal_coords,
                                   self.G, finder, self.algo_var.get(),
                                   start_candidates=start_candidates,
                                   goal_candidates=goal_candidates,
                                   candidate_mode=candidate_mode,
                                   direct_walk_dist=direct_walk_dist)
            res = self._decorate_route_stations(res)
            res = self._apply_walk_graph_to_result(res)
            res = self._add_route_directions(res)
        except Exception as e:
            self.after(0, lambda err=e: self._show_search_error(err))
            return

        exec_time = time.time() - start_time
        history_file = self._save_search_history(start_coords, goal_coords, res, exec_time)
        if history_file:
            res = dict(res)
            res["history_file"] = history_file
        self.after(0, lambda: self.draw_result(res, exec_time))

    def _show_search_error(self, err):
        self._update_search_button_state()
        if hasattr(self, "compare_btn"):
            self.compare_btn.configure(text="SO SÁNH THUẬT TOÁN")
        self.update_log(f"Lỗi khi tìm đường:\n{err}")

    def _draw_route_station_markers(self, res):
        self._clear_route_markers()
        if res.get("type") != "MIXED":
            return

        board_coord = res.get("board_coord")
        alight_coord = res.get("alight_coord")
        if board_coord:
            marker = self.map_widget.set_marker(
                board_coord[0],
                board_coord[1],
                text="Ga lên tàu",
                marker_color_circle="green",
                data=res.get("board_name", "Ga lên tàu"),
                command=self.on_station_click
            )
            self.route_markers.append(marker)

        if alight_coord:
            marker = self.map_widget.set_marker(
                alight_coord[0],
                alight_coord[1],
                text="Ga xuống tàu",
                marker_color_circle="blue",
                data=res.get("alight_name", "Ga xuống tàu"),
                command=self.on_station_click
            )
            self.route_markers.append(marker)

    def draw_result(self, res, exec_time):
        self._update_search_button_state()

        if res["type"] == "NO_ROUTE":
            self.update_log(f"{res.get('error', 'Không tìm thấy đường')}\n"
                            f"Khoảng cách: {res['dist']:.0f}m\n"
                            f"Gợi ý: Thử điểm khác hoặc tăng bán kính tìm ga.")
            return

        if res["time"] == float("inf"):
            self.update_log(f"Không tìm thấy đường.\n"
                            f"Thuật toán bị ngắt sau khi đạt giới hạn "
                            f"{Config.MAX_NODES_SEARCH} nút.")
            return

        all_lats, all_lons = [], []
        for seg in res["segments"]:
            coords = seg["coords"]
            if seg.get("mode") == "train" and seg.get("nodes"):
                coords = self._path_nodes_to_map_coords(self.G, seg["nodes"])
            if len(coords) < 2:
                continue
            all_lats.extend([c[0] for c in coords])
            all_lons.extend([c[1] for c in coords])
            color = "#2ECC71" if seg["mode"] == "train" else "#95A5A6"
            width = 7 if seg["mode"] == "train" else 3
            path = self.map_widget.set_path(coords, color=color, width=width)
            self.path_objects.append(path)

        self._draw_route_station_markers(res)

        if all_lats and all_lons:
            self.map_widget.fit_bounding_box(
                (max(all_lats), min(all_lons)),
                (min(all_lats), max(all_lons))
            )

        lines_info = ""
        if res.get("lines"):
            lines_info = f"Tuyến: {', '.join(res['lines'][:3])}"
            if len(res["lines"]) > 3:
                lines_info += f" (+{len(res['lines']) - 3} khác)"

        if res["type"] == "WALK":
            route_label = "Đi bộ theo đường phố" if res.get("walk_routed") else "Đường thẳng dự phòng"
        else:
            route_label = "Kết hợp Tàu + đi bộ theo đường phố" if res.get("walk_routed") else "Kết hợp Tàu + đoạn nối thẳng"

        candidate_info = (
            "Chọn ga: theo mạng đi bộ"
            if res.get("candidate_mode") == "walk_graph"
            else "Chọn ga: theo khoảng cách thẳng"
        )
        candidate_mode = str(res.get("candidate_mode") or "")
        if "rail_assist" in candidate_mode:
            candidate_info = "Chon ga: theo mang di bo + ho tro di tau"
        elif candidate_mode.startswith("walk_graph"):
            candidate_info = "Chon ga: theo mang di bo"

        algo_warning = ""
        if self.algo_var.get() in {"DFS", "IDS"}:
            algo_warning = "\nCảnh báo: thuật toán này không tối ưu đường đi."

        walk_info = ""
        if "walk_dist" in res:
            walk_info = f"Đi bộ: {res['walk_dist']:.0f} m\n"

        train_info = ""
        if float(res.get("train_dist", 0.0)) > 0:
            train_info = f"Đi tàu: {res['train_dist']:.0f} m\n"

        station_info = ""
        if res.get("type") == "MIXED":
            station_info = (
                f"Ga lên: {res.get('board_name', 'Không rõ')}\n"
                f"Ga xuống: {res.get('alight_name', 'Không rõ')}\n"
            )

        passed_info = ""
        if res.get("passed_stations"):
            passed_lines = "\n".join(
                f"{idx}. {name}"
                for idx, name in enumerate(res["passed_stations"], start=1)
            )
            passed_info = f"Các ga đi qua ({len(res['passed_stations'])}):\n{passed_lines}\n"

        directions_info = ""
        if res.get("directions"):
            direction_lines = "\n".join(
                f"{idx}. {instruction}"
                for idx, instruction in enumerate(res["directions"], start=1)
            )
            directions_info = f"CHỈ DẪN HÀNH TRÌNH:\n{direction_lines}\n"

        history_info = ""
        if res.get("history_file"):
            history_info = f"Đã lưu dữ liệu: {Path(res['history_file']).name}\n"

        fallback_info = ""
        if res.get("fallback_reason"):
            fallback_info = f"Lý do không chọn tàu: {res['fallback_reason']}\n"

        info = (f"KẾT QUẢ: {self.algo_var.get()}\n{'-' * 20}\n"
                f"Thời gian: {res['time']:.1f} phút\n"
                f"Quãng đường: {res['dist']:.0f} m\n"
                f"{walk_info}"
                f"{train_info}"
                f"{station_info}"
                f"{directions_info}"
                f"{passed_info}"
                f"Số nút đã duyệt: {res['nodes']}\n"
                f"Tốc độ AI: {exec_time:.4f}s\n"
                f"{lines_info}\n"
                f"{candidate_info}\n"
                f"{fallback_info}"
                f"{route_label}{algo_warning}\n"
                f"{history_info}")
        self.update_log(info)


if __name__ == "__main__":
    app = PathfindingApp()
    app.mainloop()
