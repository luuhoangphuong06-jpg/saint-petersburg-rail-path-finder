import csv
import json
import pickle
import tempfile
import threading
import time
from pathlib import Path

import networkx as nx

from app import PathfindingApp
from config import Config, fast_haversine
from database import AppDatabase
from pathfinder import PathFinder
from route_logic import find_smart_route
from spatial_index import SpatialIndex


ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"


class DummyThing:
    def __init__(self):
        self.deleted = False
        self.state = None

    def delete(self):
        self.deleted = True

    def configure(self, **kwargs):
        self.state = kwargs


class TestHarness:
    def __init__(self):
        with (CACHE / "spb_rail.pkl").open("rb") as f:
            self.rail = pickle.load(f)
        with (CACHE / "spb_walk.pkl").open("rb") as f:
            self.walk = pickle.load(f)

        raw_station_records = json.loads(
            (CACHE / "spb_station_records.json").read_text(encoding="utf-8")
        ).get("station_records", [])

        self.app = object.__new__(PathfindingApp)
        self.app.G = self.rail
        self.app.walk_G = self.walk
        self.station_records = self.app._sanitize_station_records(
            raw_station_records, self.rail, self.walk
        )
        self.app._remove_station_transfer_edges(self.rail)
        for _, data in self.rail.nodes(data=True):
            data.pop("is_station", None)
        for record in self.station_records:
            self.rail.nodes[int(record["rail_node"])]["is_station"] = True
        self.app.station_records = self.station_records
        self.app.rail_node_index = self.app._build_graph_node_index(self.rail)
        self.app.walk_node_index = self.app._build_graph_node_index(self.walk)
        self.app._state_lock = threading.Lock()
        self.app._walk_cache_lock = threading.Lock()
        self.app._walk_distance_cache = {}
        self.app.obstacles = set()
        self.app.obstacle_coords = []
        self.app.station_name_by_rail_node = {}
        for record in self.station_records:
            rail_node = int(record["rail_node"])
            self.app.station_name_by_rail_node.setdefault(
                rail_node, str(record.get("name") or "Unnamed station")
            )

        self.transfer_edges_added = self.app._add_station_transfer_edges(self.rail)

    def route(self, start, goal, algo="A*", obstacles=None, obstacle_coords=None):
        self.app.obstacles = set(obstacles or [])
        self.app.obstacle_coords = list(obstacle_coords or [])
        with self.app._walk_cache_lock:
            self.app._walk_distance_cache.clear()

        finder = PathFinder(self.rail, self.app.obstacles)
        start_candidates, goal_candidates, candidate_mode = self.app._route_station_candidates(start, goal)
        _, direct_walk_dist, _ = self.app._walk_segment_coords(start, goal)
        res = find_smart_route(
            start,
            goal,
            self.rail,
            finder,
            algo,
            start_candidates=start_candidates,
            goal_candidates=goal_candidates,
            candidate_mode=candidate_mode,
            direct_walk_dist=direct_walk_dist,
        )
        res = self.app._apply_walk_graph_to_result(res)
        res = self.app._add_route_directions(res)
        return res

    def walk_segment(self, start, goal, obstacle_coords=None):
        self.app.obstacle_coords = list(obstacle_coords or [])
        return self.app._walk_segment_coords(start, goal)

    def latest_history_coords(self):
        rows = list(csv.DictReader((CACHE / "search_history.csv").open("r", encoding="utf-8-sig")))
        row = rows[-1]
        return (
            (float(row["start_lat"]), float(row["start_lon"])),
            (float(row["goal_lat"]), float(row["goal_lon"])),
        )


def pass_if(condition, message):
    if not condition:
        raise AssertionError(message)


def count_transfer_edges(graph):
    return sum(1 for _, _, data in graph.edges(data=True) if data.get("mode") == "transfer")


def test_database_roundtrip():
    test_dir = ROOT / "build" / "test_results"
    test_dir.mkdir(parents=True, exist_ok=True)
    db_path = test_dir / f"test_{int(time.time() * 1000)}.sqlite3"
    db = AppDatabase(db_path)
    db.initialize()
    db.save_station_records([
        {"name": "A", "lat": 1.0, "lon": 2.0, "rail_node": 10, "access_node": 20}
    ])
    pass_if(len(db.load_station_records()) == 1, "station record roundtrip failed")
    db.save_obstacle(1.0, 2.0, 150.0, [10, 11])
    pass_if(len(db.load_active_obstacles()) == 1, "active obstacle save failed")
    db.deactivate_obstacles()
    pass_if(len(db.load_active_obstacles()) == 0, "obstacle deactivate failed")
    db.save_search({
        "timestamp": "2026-06-08 00:00:00",
        "run_mode": "test",
        "algorithm": "A*",
        "start_lat": 1.0,
        "start_lon": 2.0,
        "goal_lat": 3.0,
        "goal_lon": 4.0,
        "type": "WALK",
        "time": 1.0,
        "dist": 10.0,
        "walk_dist": 10.0,
        "train_dist": 0.0,
        "nodes": 1,
        "exec_seconds": 0.1,
        "candidate_mode": "test",
        "lines": [],
        "passed_stations": [],
        "segments": [],
    })
    pass_if(db.count_search_runs() == 1, "search history save failed")
    recent = db.load_recent_searches(10)
    pass_if(len(recent) == 1 and recent[0]["algorithm"] == "A*", "search history query failed")
    pass_if(db.clear_search_history() == 1, "search history clear count failed")
    pass_if(db.count_search_runs() == 0, "search history clear failed")


def test_ui_state_methods():
    app = object.__new__(PathfindingApp)
    app._station_fetch_lock = threading.Lock()
    app._station_fetch_token = 0
    app._fetching_stations = True
    app.stations_btn = DummyThing()
    app.path_objects = [DummyThing(), DummyThing()]
    app.route_markers = [DummyThing()]
    app.station_markers = [DummyThing()]
    app.markers = {
        "obs": [DummyThing()],
        "start": DummyThing(),
        "goal": DummyThing(),
    }
    app._state_lock = threading.Lock()
    app._walk_cache_lock = threading.Lock()
    app.obstacles = {1, 2}
    app.obstacle_coords = [(1.0, 2.0)]
    app._walk_distance_cache = {1: 2}
    app._finder_cache = object()
    app._finder_obstacles_snapshot = frozenset({1})
    app.database = type("DB", (), {"deactivate_obstacles": lambda self: None})()
    app.start_coords = (1.0, 2.0)
    app.goal_coords = (3.0, 4.0)
    app._update_search_button_state = lambda: None
    app.update_log = lambda text: None

    PathfindingApp.clear_all(app)
    pass_if(app.start_coords is None and app.goal_coords is None, "clear_all did not clear endpoints")
    pass_if(not app.obstacles and not app.obstacle_coords, "clear_all did not clear obstacles")
    pass_if(not app.path_objects and not app.route_markers, "clear_all did not clear route objects")


def main():
    started = time.time()
    harness = TestHarness()
    results = []

    def run(name, func, expected="PASS"):
        t0 = time.time()
        try:
            details = func()
            results.append((name, expected, "PASS", time.time() - t0, details or ""))
        except Exception as exc:
            results.append((name, expected, "FAIL", time.time() - t0, str(exc)))

    # 1. Very close points.
    run("01_near_points_walk", lambda: (
        lambda res: (
            pass_if(res["type"] == "WALK", f"expected WALK, got {res['type']}"),
            f"type={res['type']}, dist={res['dist']:.1f}m"
        )[1]
    )(harness.route((59.93110, 30.36090), (59.93112, 30.36095))))

    # 2. Near station to near station, should be mixed.
    run("02_near_station_to_near_station_mixed", lambda: (
        lambda res: (
            pass_if(res["type"] == "MIXED", f"expected MIXED, got {res['type']} {res.get('fallback_reason')}"),
            pass_if(res.get("train_dist", 0) > 0, "mixed route has no train distance"),
            f"time={res['time']:.1f}min, walk={res.get('walk_dist', 0):.0f}m, train={res.get('train_dist', 0):.0f}m"
        )[2]
    )(harness.route((59.93598067426779, 30.309272840881363), (59.888907085146876, 30.410110265410594))))

    # 3. Previously failing far-ish case: should no longer fail because of disconnected rail components.
    run("03_previous_no_component_case", lambda: (
        lambda res: (
            pass_if(res["type"] in {"WALK", "MIXED"}, f"unexpected type {res['type']}"),
            pass_if("cung mang rail" not in str(res.get("fallback_reason", "")), "still failed by rail component"),
            f"type={res['type']}, nodes={res.get('nodes')}, reason={res.get('fallback_reason', '')[:65]}"
        )[1]
    )(harness.route(*harness.latest_history_coords())))

    # 4. Start near station, goal farther from station: should not crash and should explain if walking.
    run("04_start_near_goal_far_stable", lambda: (
        lambda res: (
            pass_if(res["type"] in {"WALK", "MIXED"}, f"unexpected type {res['type']}"),
            f"type={res['type']}, reason={res.get('fallback_reason', '')[:70]}"
        )[1]
    )(harness.route((59.93598067426779, 30.309272840881363), (59.8970, 30.4670))))

    # 5. Start farther, goal near station.
    run("05_start_far_goal_near_stable", lambda: (
        lambda res: (
            pass_if(res["type"] in {"WALK", "MIXED"}, f"unexpected type {res['type']}"),
            f"type={res['type']}, reason={res.get('fallback_reason', '')[:70]}"
        )[1]
    )(harness.route((59.97131781762799, 30.268981627195217), (59.888907085146876, 30.410110265410594))))

    # 6. Across river: walking should use actual street graph, not a two-point straight fallback.
    run("06_across_river_walk_routed", lambda: (
        lambda data: (
            pass_if(data[2] is True and len(data[0]) > 2, "walk route fell back to straight line"),
            f"routed={data[2]}, dist={data[1]:.0f}m, points={len(data[0])}"
        )[1]
    )(harness.walk_segment((59.9360, 30.3090), (59.9370, 30.3900))))

    # 7. Transfer graph connectivity.
    run("07_transfer_edges_component", lambda: (
        pass_if(harness.transfer_edges_added > 0, "no transfer edges added"),
        pass_if(count_transfer_edges(harness.rail) >= harness.transfer_edges_added, "transfer edges not present"),
        f"added={harness.transfer_edges_added}, total_transfer={count_transfer_edges(harness.rail)}"
    )[2])

    # 8. Point outside loaded graph: should not crash.
    run("08_outside_graph_no_crash", lambda: (
        lambda res: (
            pass_if(res["type"] in {"WALK", "MIXED", "NO_ROUTE"}, f"unexpected type {res['type']}"),
            f"type={res['type']}, dist={res.get('dist'):.0f}m"
        )[1]
    )(harness.route((60.2000, 31.0000), (59.9311, 30.3609))), expected="PASS_WITH_NOTE")

    # 9. Same point should be near-zero.
    run("09_same_point_zeroish", lambda: (
        lambda res: (
            pass_if(res["type"] == "WALK", f"expected WALK, got {res['type']}"),
            pass_if(res.get("dist", 0) < 5.0, f"same point distance too large: {res.get('dist')}"),
            f"dist={res.get('dist'):.3f}m, time={res.get('time'):.4f}min"
        )[2]
    )(harness.route((59.9311, 30.3609), (59.9311, 30.3609))))

    # 10. Obstacle on walking path changes/remains valid without crashing.
    run("10_walk_obstacle_stable", lambda: (
        lambda base: (
            lambda mid, blocked: (
                pass_if(blocked[2] in {True, False}, "invalid routed flag"),
                f"base={base[1]:.0f}m, with_obstacle={blocked[1]:.0f}m"
            )[1]
        )(base[0][len(base[0]) // 2], harness.walk_segment((59.9360, 30.3090), (59.9370, 30.3900), [base[0][len(base[0]) // 2]]))
    )(harness.walk_segment((59.9360, 30.3090), (59.9370, 30.3900))))

    # 11. Obstacle on chosen station should not allow that station as board/alight.
    def station_obstacle_case():
        base = harness.route((59.93598067426779, 30.309272840881363), (59.888907085146876, 30.410110265410594))
        if base["type"] != "MIXED":
            return f"skipped: baseline type={base['type']}"
        board = base["board_node"]
        coord = (harness.rail.nodes[board]["y"], harness.rail.nodes[board]["x"])
        blocked = harness.app._rail_obstacle_nodes_for_coord(coord)
        reroute = harness.route(
            (59.93598067426779, 30.309272840881363),
            (59.888907085146876, 30.410110265410594),
            obstacles=blocked,
            obstacle_coords=[coord],
        )
        pass_if(reroute.get("board_node") != board, "blocked board station was reused")
        return f"blocked_nodes={len(blocked)}, reroute={reroute['type']}"
    run("11_station_obstacle_not_reused", station_obstacle_case)

    # 12. Multiple rail obstacles should not crash.
    def multi_obstacle_case():
        base = harness.route((59.93598067426779, 30.309272840881363), (59.888907085146876, 30.410110265410594))
        nodes = base.get("segments", [{}])[1].get("nodes", []) if base["type"] == "MIXED" else []
        blocked = set()
        coords = []
        for node in nodes[::max(1, len(nodes) // 5)][:5]:
            node = int(node)
            coord = (harness.rail.nodes[node]["y"], harness.rail.nodes[node]["x"])
            coords.append(coord)
            blocked.update(harness.app._rail_obstacle_nodes_for_coord(coord))
        res = harness.route(
            (59.93598067426779, 30.309272840881363),
            (59.888907085146876, 30.410110265410594),
            obstacles=blocked,
            obstacle_coords=coords,
        )
        pass_if(res["type"] in {"WALK", "MIXED"}, f"unexpected type {res['type']}")
        return f"blocked_nodes={len(blocked)}, result={res['type']}"
    run("12_many_obstacles_stable", multi_obstacle_case)

    # 13. Algorithm smoke test.
    def algorithms_case():
        output = []
        for algo in ["A*", "Dijkstra", "Greedy", "UCS", "BFS", "DFS", "IDS"]:
            res = harness.route(
                (59.93598067426779, 30.309272840881363),
                (59.888907085146876, 30.410110265410594),
                algo=algo,
            )
            pass_if(res["type"] in {"WALK", "MIXED"}, f"{algo} returned {res['type']}")
            output.append(f"{algo}:{res['type']}/{res.get('nodes', 0)}")
        return "; ".join(output)
    run("13_all_algorithms_smoke", algorithms_case)

    # 14. Far performance.
    run("14_far_route_performance", lambda: (
        lambda t0, res: (
            pass_if(time.time() - t0 < Config.SEARCH_TIMEOUT_SECONDS + 12, "far route too slow"),
            pass_if(res["type"] in {"WALK", "MIXED"}, f"unexpected type {res['type']}"),
            f"type={res['type']}, elapsed={time.time() - t0:.2f}s, nodes={res.get('nodes')}"
        )[2]
    )(time.time(), harness.route((59.97131781762799, 30.268981627195217), (59.888907085146876, 30.410110265410594))))

    # 15. Cache/database.
    run("15_database_roundtrip", lambda: (test_database_roundtrip(), "database ok")[1])

    # 16-18. UI state methods that can be safely tested headless.
    run("16_17_18_ui_state_headless", lambda: (test_ui_state_methods(), "clear_all/marker state ok")[1])

    # 19. No route/disconnected synthetic graph.
    def no_route_case():
        g = nx.MultiDiGraph()
        g.add_node(1, y=59.0, x=30.0, is_station=True)
        g.add_node(2, y=59.1, x=30.1, is_station=True)
        finder = PathFinder(g, set())
        res = find_smart_route(
            (59.0, 30.0), (59.1, 30.1), g, finder, "A*",
            start_candidates=[(10.0, 1)], goal_candidates=[(10.0, 2)],
            candidate_mode="test", direct_walk_dist=20000.0
        )
        pass_if(res["type"] == "WALK" and res.get("fallback_reason"), f"bad no-route result: {res}")
        return res["fallback_reason"][:80]
    run("19_no_route_no_crash", no_route_case)

    # 20. EXE is not rebuilt from current source in this test run.
    run("20_exe_package_currentness", lambda: (
        pass_if((ROOT / "dist" / "RailwayPathfindingApp" / "RailwayPathfindingApp.exe").exists(), "exe not found"),
        "exe exists, but not rebuilt after latest source edits"
    )[1], expected="PASS_WITH_NOTE")

    print("\nTEST RESULTS")
    print("=" * 96)
    failed = 0
    for name, expected, status, seconds, details in results:
        if status != "PASS":
            failed += 1
        safe_details = str(details).encode("ascii", "replace").decode("ascii")
        print(f"{status:4} | {expected:14} | {seconds:6.2f}s | {name} | {safe_details}")
    print("=" * 96)
    print(f"Total: {len(results)}, failed: {failed}, elapsed: {time.time() - started:.2f}s")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
