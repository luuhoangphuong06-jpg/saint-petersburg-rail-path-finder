import math

from config import fast_haversine


class SpatialIndex:
    def __init__(self, node_items, cell_deg=0.0015):
        self.cell_deg = cell_deg
        self.grid = {}
        for node, lat, lon in node_items:
            key = self._cell_key(lat, lon)
            self.grid.setdefault(key, []).append((node, lat, lon))

    def _cell_key(self, lat, lon):
        return int(lat / self.cell_deg), int(lon / self.cell_deg)

    def query_radius(self, lat, lon, radius_m, max_k):
        if not self.grid:
            return []

        meters_per_lat_cell = self.cell_deg * 111000
        lon_scale = max(math.cos(math.radians(lat)), 0.2)
        meters_per_lon_cell = self.cell_deg * 111000 * lon_scale
        margin_lat = int(radius_m / meters_per_lat_cell) + 1
        margin_lon = int(radius_m / meters_per_lon_cell) + 1
        ci, cj = self._cell_key(lat, lon)
        candidates = []

        for di in range(-margin_lat, margin_lat + 1):
            for dj in range(-margin_lon, margin_lon + 1):
                for node, nlat, nlon in self.grid.get((ci + di, cj + dj), []):
                    d = fast_haversine(lat, lon, nlat, nlon)
                    if d <= radius_m:
                        candidates.append((d, node))

        candidates.sort(key=lambda item: item[0])
        return candidates[:max_k]
