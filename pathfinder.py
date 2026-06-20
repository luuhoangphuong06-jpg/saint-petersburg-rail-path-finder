import heapq
import itertools
import time
from collections import deque

from config import Config, fast_haversine
from spatial_index import SpatialIndex


class PathFinder:
    def __init__(self, graph, obstacles):
        self.G = graph
        self.nodes_data = {node: (data["y"], data["x"]) for node, data in graph.nodes(data=True)}
        self.obstacles = obstacles

        self.station_nodes = [
            (node, data["y"], data["x"])
            for node, data in graph.nodes(data=True)
            if data.get("is_station") and node not in obstacles
        ]
        self.station_index = SpatialIndex(self.station_nodes)
        self.node_index = SpatialIndex(list(
            (node, lat, lon)
            for node, (lat, lon) in self.nodes_data.items()
            if node not in obstacles
        ))

        self.h_cache = {}
        self.last_path_lines = []

        self.adj = {node: [] for node in graph.nodes()}

        for u, v, data in graph.edges(data=True):
            if u in obstacles or v in obstacles:
                continue

            length = float(data.get("length", 1.0))
            mode = data.get("mode")
            cost = length + Config.TRANSFER_PENALTY if mode == "transfer" else length
            line = data.get("ref") or data.get("name") or "Unknown"
            if isinstance(line, list):
                line = line[0]

            self.adj[u].append((v, cost, line))
            self.adj[v].append((u, cost, line))

        self.component_id = {}
        self._next_component_id = 0

    def _index_component_from(self, start):
        if start in self.obstacles or start in self.component_id:
            return

        current_id = self._next_component_id
        self._next_component_id += 1
        queue = deque([start])
        self.component_id[start] = current_id

        while queue:
            node = queue.popleft()
            for neighbor, _, _ in self.adj[node]:
                if neighbor not in self.component_id:
                    self.component_id[neighbor] = current_id
                    queue.append(neighbor)

    def same_component(self, n1, n2):
        if n1 in self.obstacles or n2 in self.obstacles:
            return False
        if n1 not in self.component_id:
            self._index_component_from(n1)
        if n2 not in self.component_id:
            self._index_component_from(n2)
        id1 = self.component_id.get(n1)
        id2 = self.component_id.get(n2)
        return id1 is not None and id1 == id2

    def reconstruct_path_simple(self, came_from, current):
        path = [current]
        while current in came_from:
            entry = came_from[current]
            current = entry[0] if isinstance(entry, tuple) else entry
            path.append(current)
        return path[::-1]

    def reconstruct_path_lines(self, came_from, current):
        lines = []
        while current in came_from:
            previous, line = came_from[current]
            lines.append(line)
            current = previous
        return lines[::-1]

    def heuristic(self, n1, n2):
        key = (n1, n2)
        if key not in self.h_cache:
            if len(self.h_cache) >= Config.HEURISTIC_CACHE_MAX:
                self.h_cache.clear()
            c1 = self.nodes_data[n1]
            c2 = self.nodes_data[n2]
            self.h_cache[key] = fast_haversine(c1[0], c1[1], c2[0], c2[1])
        return self.h_cache[key]

    def _timed_out(self, deadline):
        return deadline is not None and time.monotonic() >= deadline

    def astar(self, start, goal, deadline=None):
        self.last_path_lines = []
        counter = itertools.count()
        pq = [(self.heuristic(start, goal), next(counter), start)]
        came_from = {}
        cost_so_far = {start: 0.0}
        nodes_searched = 0

        while pq:
            _, _, current = heapq.heappop(pq)
            current_g = cost_so_far.get(current, float("inf"))

            nodes_searched += 1
            if nodes_searched > Config.MAX_NODES_SEARCH or self._timed_out(deadline):
                return nodes_searched, None

            if current == goal:
                self.last_path_lines = self.reconstruct_path_lines(came_from, current)
                return nodes_searched, self.reconstruct_path_simple(came_from, current)

            for neighbor, cost, line in self.adj[current]:
                new_cost = current_g + cost
                if new_cost < cost_so_far.get(neighbor, float("inf")):
                    cost_so_far[neighbor] = new_cost
                    came_from[neighbor] = (current, line)
                    f_score = new_cost + self.heuristic(neighbor, goal)
                    heapq.heappush(pq, (f_score, next(counter), neighbor))

        return nodes_searched, None

    def greedy(self, start, goal, deadline=None):
        self.last_path_lines = []
        counter = itertools.count()
        pq = [(self.heuristic(start, goal), next(counter), start)]
        came_from = {}
        visited = {start}
        nodes_searched = 0

        while pq:
            _, _, current = heapq.heappop(pq)
            nodes_searched += 1
            if nodes_searched > Config.MAX_NODES_SEARCH or self._timed_out(deadline):
                return nodes_searched, None
            if current == goal:
                self.last_path_lines = self.reconstruct_path_lines(came_from, current)
                return nodes_searched, self.reconstruct_path_simple(came_from, current)

            for neighbor, _, line in self.adj[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    came_from[neighbor] = (current, line)
                    heapq.heappush(pq, (self.heuristic(neighbor, goal), next(counter), neighbor))
        return nodes_searched, None

    def ucs(self, start, goal, deadline=None):
        self.last_path_lines = []
        counter = itertools.count()
        pq = [(0.0, next(counter), start)]
        came_from = {}
        cost_so_far = {start: 0.0}
        nodes_searched = 0

        while pq:
            current_cost, _, current = heapq.heappop(pq)

            if current_cost > cost_so_far.get(current, float("inf")):
                continue

            nodes_searched += 1
            if nodes_searched > Config.MAX_NODES_SEARCH or self._timed_out(deadline):
                return nodes_searched, None
            if current == goal:
                self.last_path_lines = self.reconstruct_path_lines(came_from, current)
                return nodes_searched, self.reconstruct_path_simple(came_from, current)

            for neighbor, cost, line in self.adj[current]:
                new_cost = current_cost + cost
                if new_cost < cost_so_far.get(neighbor, float("inf")):
                    cost_so_far[neighbor] = new_cost
                    came_from[neighbor] = (current, line)
                    heapq.heappush(pq, (new_cost, next(counter), neighbor))
        return nodes_searched, None

    def bfs(self, start, goal, deadline=None):
        self.last_path_lines = []
        queue = deque([start])
        came_from = {}
        visited = {start}
        nodes_searched = 0

        while queue:
            current = queue.popleft()
            nodes_searched += 1
            if nodes_searched > Config.MAX_NODES_SEARCH or self._timed_out(deadline):
                return nodes_searched, None
            if current == goal:
                self.last_path_lines = self.reconstruct_path_lines(came_from, current)
                return nodes_searched, self.reconstruct_path_simple(came_from, current)

            for neighbor, _, line in self.adj[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    came_from[neighbor] = (current, line)
                    queue.append(neighbor)
        return nodes_searched, None

    def dfs(self, start, goal, deadline=None):
        self.last_path_lines = []
        stack = [start]
        came_from = {}
        visited = {start}
        nodes_searched = 0

        while stack:
            current = stack.pop()
            nodes_searched += 1
            if nodes_searched > Config.MAX_NODES_SEARCH or self._timed_out(deadline):
                return nodes_searched, None
            if current == goal:
                self.last_path_lines = self.reconstruct_path_lines(came_from, current)
                return nodes_searched, self.reconstruct_path_simple(came_from, current)

            for neighbor, _, line in self.adj[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    came_from[neighbor] = (current, line)
                    stack.append(neighbor)
        return nodes_searched, None

    def ids(self, start, goal, deadline=None):
        self.last_path_lines = []
        nodes_searched = 0
        max_depth = Config.IDS_MAX_DEPTH

        def dfs_limit_iterative(depth_limit):
            local_nodes = 1
            stack = [[start, 0, 0]]
            path = [start]
            path_lines = []
            path_nodes = {start}

            while stack:
                if nodes_searched + local_nodes > Config.MAX_NODES_SEARCH or self._timed_out(deadline):
                    return None, local_nodes

                current, depth, next_index = stack[-1]
                if current == goal:
                    return (list(path), list(path_lines)), local_nodes

                if depth >= depth_limit or next_index >= len(self.adj[current]):
                    stack.pop()
                    popped = path.pop()
                    path_nodes.remove(popped)
                    if path_lines:
                        path_lines.pop()
                    continue

                neighbor, _, line = self.adj[current][next_index]
                stack[-1][2] += 1
                if neighbor in path_nodes:
                    continue

                local_nodes += 1
                path.append(neighbor)
                path_lines.append(line)
                path_nodes.add(neighbor)
                stack.append([neighbor, depth + 1, 0])
            return None, local_nodes

        for depth_limit in range(1, max_depth + 1):
            result, visited_this_depth = dfs_limit_iterative(depth_limit)
            nodes_searched += visited_this_depth
            if result is not None:
                path, lines = result
                self.last_path_lines = lines
                return nodes_searched, path
            if nodes_searched >= Config.MAX_NODES_SEARCH or self._timed_out(deadline):
                break

        return nodes_searched, None
