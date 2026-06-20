import time

from config import Config, fast_haversine


def _safe_edge_length(graph, u, v):
    def fallback_distance():
        try:
            return fast_haversine(
                graph.nodes[u]["y"], graph.nodes[u]["x"],
                graph.nodes[v]["y"], graph.nodes[v]["x"]
            )
        except Exception:
            return 1.0

    def clean_length(value):
        try:
            length = float(value)
        except (TypeError, ValueError):
            return None
        return length if length > 0 else None

    edge_data = graph.get_edge_data(u, v)
    if not edge_data:
        edge_data = graph.get_edge_data(v, u)
    if not edge_data:
        return fallback_distance()
    if isinstance(edge_data, dict) and edge_data:
        if all(isinstance(value, dict) for value in edge_data.values()):
            lengths = [
                clean_length(value.get("length"))
                for value in edge_data.values()
                if isinstance(value, dict) and clean_length(value.get("length")) is not None
            ]
            return min(lengths) if lengths else fallback_distance()
        length = clean_length(edge_data.get("length"))
        return length if length is not None else fallback_distance()
    return fallback_distance()


def _path_length(graph, path):
    return sum(
        _safe_edge_length(graph, path[i], path[i + 1])
        for i in range(len(path) - 1)
    )


def _trim_train_path_near_destination(graph, train_path, destination_coord):
    if len(train_path) < 2:
        return train_path, float("inf")

    candidate_indexes = [
        i for i, node in enumerate(train_path[1:], start=1)
        if graph.nodes[node].get("is_station")
    ]
    if not candidate_indexes:
        stop_node = train_path[-1]
        stop_coord = (graph.nodes[stop_node]["y"], graph.nodes[stop_node]["x"])
        walk_to_destination = fast_haversine(
            destination_coord[0],
            destination_coord[1],
            stop_coord[0],
            stop_coord[1],
        )
        return train_path, walk_to_destination

    best_i = min(
        candidate_indexes,
        key=lambda i: fast_haversine(
            destination_coord[0],
            destination_coord[1],
            graph.nodes[train_path[i]]["y"],
            graph.nodes[train_path[i]]["x"],
        )
    )
    stop_node = train_path[best_i]
    stop_coord = (graph.nodes[stop_node]["y"], graph.nodes[stop_node]["x"])
    walk_to_destination = fast_haversine(
        destination_coord[0],
        destination_coord[1],
        stop_coord[0],
        stop_coord[1],
    )
    return train_path[:best_i + 1], walk_to_destination


def find_smart_route(A_coord, B_coord, graph, finder, algo_name,
                     start_candidates=None, goal_candidates=None,
                     candidate_mode="straight", direct_walk_dist=None):
    deadline = time.monotonic() + Config.SEARCH_TIMEOUT_SECONDS
    straight_dist = fast_haversine(A_coord[0], A_coord[1], B_coord[0], B_coord[1])
    direct_dist = float(direct_walk_dist) if direct_walk_dist else straight_dist
    direct_time = direct_dist / Config.WALK_SPEED / 60

    if straight_dist < 400:
        return {
            "type": "WALK", "time": direct_time, "dist": direct_dist, "nodes": 0,
            "train_dist": 0.0,
            "segments": [{"mode": "walk", "coords": [A_coord, B_coord]}],
            "candidate_mode": candidate_mode,
            "lines": []
        }

    def get_smart_candidates(coord, direct_dist, max_k=Config.MAX_STATION_CANDIDATES):
        dynamic_max_radius = min(Config.MAX_WALK_TO_STATION, max(1200.0, straight_dist * 0.35))
        candidates = finder.station_index.query_radius(
            coord[0], coord[1], dynamic_max_radius, max_k
        )
        if candidates:
            return candidates

        return finder.node_index.query_radius(
            coord[0], coord[1], Config.MAX_WALK_TO_STATION, max_k
        )

    if start_candidates is None:
        start_candidates = get_smart_candidates(A_coord, direct_dist)
    if goal_candidates is None:
        goal_candidates = get_smart_candidates(B_coord, direct_dist)

    if not start_candidates or not goal_candidates:
        return {
            "type": "WALK",
            "time": direct_time,
            "dist": direct_dist,
            "nodes": 0,
            "train_dist": 0.0,
            "segments": [{"mode": "walk", "coords": [A_coord, B_coord]}],
            "lines": [],
            "candidate_mode": candidate_mode,
            "fallback_reason": (
                "Khong co ga tau den duoc bang duong bo trong gioi han "
                f"{Config.MAX_WALK_TO_STATION / 1000:.0f} km tai diem di hoac diem den."
            ),
            "error": "Không tìm thấy ga tàu gần điểm đi/đến"
        }

    best_mixed_route = None
    min_combo_time = float("inf")
    total_nodes_searched = 0

    algo_map = {
        "A*": finder.astar, "Dijkstra": finder.ucs, "Greedy": finder.greedy, "UCS": finder.ucs,
        "BFS": finder.bfs, "DFS": finder.dfs, "IDS": finder.ids,
    }

    if algo_name not in algo_map:
        raise ValueError(f"Unknown algorithm: {algo_name}")

    run_algo = algo_map[algo_name]

    def merge_candidates(primary, extra):
        merged = []
        best_distance_by_node = {}
        for distance, node in list(primary or []) + list(extra or []):
            if distance > Config.MAX_WALK_TO_STATION:
                continue
            previous = best_distance_by_node.get(node)
            if previous is None or distance < previous:
                best_distance_by_node[node] = distance
        for node, distance in best_distance_by_node.items():
            merged.append((distance, node))
        merged.sort(key=lambda item: item[0])
        return merged

    def build_candidate_pairs(starts, goals,
                              max_start_walk=Config.MAX_WALK_TO_STATION,
                              max_goal_walk=Config.MAX_WALK_TO_STATION):
        pairs = []
        for d_walk_a, s_station in starts:
            for d_walk_b, g_station in goals:
                if d_walk_a > max_start_walk or d_walk_b > max_goal_walk:
                    continue
                if s_station != g_station and finder.same_component(s_station, g_station):
                    priority = d_walk_a * 2.0 + d_walk_b
                    pairs.append((priority, d_walk_a, s_station, d_walk_b, g_station))
        pairs.sort(key=lambda item: item[0])
        return pairs[:Config.MAX_CANDIDATE_PAIRS]

    def nearest_destination_station_candidates(max_walk, max_k):
        candidates = finder.station_index.query_radius(
            B_coord[0], B_coord[1], max_walk, max_k
        )
        if candidates:
            return candidates

        fallback = []
        for node, lat, lon in finder.station_nodes:
            distance = fast_haversine(B_coord[0], B_coord[1], lat, lon)
            if distance <= max_walk:
                fallback.append((distance, node))
        fallback.sort(key=lambda item: item[0])
        return fallback[:max_k]

    def evaluate_candidate_pairs(pairs, max_destination_walk):
        nonlocal best_mixed_route, min_combo_time, total_nodes_searched

        for _, d_walk_a, s_station, d_walk_b, g_station in pairs:
            if time.monotonic() >= deadline:
                break

            nodes, train_path = run_algo(s_station, g_station, deadline=deadline)
            route_lines = list(finder.last_path_lines)
            total_nodes_searched += nodes

            if train_path and len(train_path) >= 2:
                # The selected destination station already has a measured walking
                # distance. Do not trim to another station using straight-line
                # distance because that can create a very long real walk.
                actual_walk_b = d_walk_b
                train_dist = _path_length(graph, train_path)

                combo_time = (
                    (d_walk_a / Config.WALK_SPEED)
                    + (train_dist / Config.TRAIN_SPEED)
                    + (actual_walk_b / Config.WALK_SPEED)
                    + Config.WAITING_PENALTY_SEC
                ) / 60

                total_distance_mixed = d_walk_a + train_dist + actual_walk_b
                if (
                    total_distance_mixed > direct_dist * 6.0
                    or d_walk_a > Config.MAX_WALK_TO_STATION
                    or actual_walk_b > max_destination_walk
                ):
                    continue

                if combo_time < min_combo_time:
                    min_combo_time = combo_time
                    train_lines = []
                    for line in route_lines:
                        if line and line != "Unknown" and line not in train_lines:
                            train_lines.append(line)

                    best_mixed_route = {
                        "path": train_path,
                        "board_node": train_path[0],
                        "alight_node": train_path[-1],
                        "d_walk_a": d_walk_a,
                        "d_walk_b": actual_walk_b,
                        "train_dist": train_dist,
                        "total_dist": total_distance_mixed,
                        "max_destination_walk": max_destination_walk,
                        "lines": train_lines
                    }

    candidate_pairs = build_candidate_pairs(start_candidates, goal_candidates)

    if not candidate_pairs and not candidate_mode.startswith("walk_graph"):
        extended_start = finder.station_index.query_radius(
            A_coord[0],
            A_coord[1],
            Config.MAX_WALK_TO_STATION,
            Config.EXTENDED_STATION_CANDIDATES,
        )
        extended_goal = finder.station_index.query_radius(
            B_coord[0],
            B_coord[1],
            Config.MAX_WALK_TO_STATION,
            Config.EXTENDED_STATION_CANDIDATES,
        )
        start_candidates = merge_candidates(start_candidates, extended_start)
        goal_candidates = merge_candidates(goal_candidates, extended_goal)
        candidate_pairs = build_candidate_pairs(start_candidates, goal_candidates)
        if candidate_pairs and candidate_mode == "walk_graph":
            candidate_mode = "walk_graph+extended"

    evaluate_candidate_pairs(candidate_pairs, Config.MAX_WALK_TO_STATION)

    assist_pairs = []
    if (
        not best_mixed_route
        and direct_dist >= Config.PREFER_TRAIN_MIN_DIRECT_WALK
        and time.monotonic() < deadline
        and not candidate_mode.startswith("walk_graph")
    ):
        assist_destination_walk = min(
            Config.MAX_ASSIST_DESTINATION_WALK,
            direct_dist * Config.MAX_ASSIST_ALIGHT_WALK_RATIO,
        )
        assist_goal_candidates = nearest_destination_station_candidates(
            assist_destination_walk,
            Config.MAX_ASSIST_ALIGHT_CANDIDATES,
        )
        assist_pairs = build_candidate_pairs(
            start_candidates,
            assist_goal_candidates,
            max_start_walk=Config.MAX_WALK_TO_STATION,
            max_goal_walk=assist_destination_walk,
        )
        if assist_pairs:
            if candidate_mode == "walk_graph":
                candidate_mode = "walk_graph+rail_assist"
            else:
                candidate_mode = f"{candidate_mode}+rail_assist"
            evaluate_candidate_pairs(assist_pairs, assist_destination_walk)

    if not best_mixed_route:
        reason = (
            "Không có cặp ga trong bán kính đi bộ tối đa nằm cùng mạng rail."
            if not candidate_pairs and not assist_pairs
            else "Có cặp ga kết nối nhưng không tìm được tuyến tàu phù hợp trong giới hạn hiện tại."
        )
        return {
            "type": "WALK", "time": direct_time, "dist": direct_dist,
            "nodes": total_nodes_searched,
            "train_dist": 0.0,
            "segments": [{"mode": "walk", "coords": [A_coord, B_coord]}],
            "candidate_mode": candidate_mode,
            "lines": [],
            "direct_walk_dist": direct_dist,
            "fallback_reason": reason,
        }

    train_is_faster = min_combo_time < direct_time

    if not train_is_faster:
        return {
            "type": "WALK", "time": direct_time, "dist": direct_dist,
            "nodes": total_nodes_searched,
            "train_dist": 0.0,
            "segments": [{"mode": "walk", "coords": [A_coord, B_coord]}],
            "candidate_mode": candidate_mode,
            "lines": [],
            "direct_walk_dist": direct_dist,
            "fallback_reason": "Có tuyến tàu nhưng thời gian ước lượng không tốt hơn đi bộ đủ nhiều.",
        }

    t_coords = [(graph.nodes[n]["y"], graph.nodes[n]["x"]) for n in best_mixed_route["path"]]

    return {
        "type": "MIXED",
        "time": min_combo_time,
        "dist": best_mixed_route["total_dist"],
        "nodes": total_nodes_searched,
        "train_dist": best_mixed_route["train_dist"],
        "lines": best_mixed_route["lines"],
        "candidate_mode": candidate_mode,
        "direct_walk_dist": direct_dist,
        "board_node": best_mixed_route["board_node"],
        "alight_node": best_mixed_route["alight_node"],
        "board_coord": t_coords[0],
        "alight_coord": t_coords[-1],
        "max_destination_walk": best_mixed_route["max_destination_walk"],
        "segments": [
            {"mode": "walk", "coords": [A_coord, t_coords[0]]},
            {"mode": "train", "coords": t_coords, "nodes": best_mixed_route["path"]},
            {"mode": "walk", "coords": [t_coords[-1], B_coord]}
        ]
    }
