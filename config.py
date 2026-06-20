import os
import sys
import math
from pathlib import Path


class Config:
    if getattr(sys, "frozen", False):
        APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AI_Railway_Pathfinding"
        BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        APP_DIR = Path(__file__).resolve().parent
        BUNDLE_DIR = APP_DIR

    TRANSFER_PENALTY = 200.0
    CITY = "Sankt-Peterburg, Russia"
    CENTER_LAT = 59.9311
    CENTER_LON = 30.3609
    MAP_RADIUS = 6000
    WALK_SPEED = 5.0 / 3.6
    TRAIN_SPEED = 80.0 / 3.6
    WAITING_PENALTY_SEC = 120.0

    MAX_NODES_SEARCH = 60000
    SEARCH_TIMEOUT_SECONDS = 8.0
    IDS_MAX_DEPTH = 45
    MAX_WALK_TO_STATION = 3000.0
    PREFER_TRAIN_MIN_DIRECT_WALK = 800.0
    MAX_DESTINATION_WALK_RATIO = 0.35
    MAX_STATION_CANDIDATES = 8
    MAX_WALK_GRAPH_STATION_CANDIDATES = 40
    MAX_CANDIDATE_PAIRS = 160
    MAX_STATION_RAIL_SNAP_M = 750.0
    MAX_STATION_WALK_SNAP_M = 250.0
    EXTENDED_STATION_CANDIDATES = 150
    MAX_ASSIST_DESTINATION_WALK = 3000.0
    MAX_ASSIST_ALIGHT_WALK_RATIO = 0.65
    MAX_ASSIST_ALIGHT_CANDIDATES = 25
    TRANSFER_RADIUS_M = 500.0
    OBSTACLE_RADIUS_M = 150.0
    HEURISTIC_CACHE_MAX = 100000
    WALK_DISTANCE_CACHE_MAX = 64
    SEED_CACHE_DIR = BUNDLE_DIR / "cache"
    CACHE_DIR = APP_DIR / "cache"
    RAIL_GRAPHML_CACHE = CACHE_DIR / "spb_rail.graphml"
    RAIL_PICKLE_CACHE = CACHE_DIR / "spb_rail.pkl"
    WALK_GRAPHML_CACHE = CACHE_DIR / "spb_walk.graphml"
    WALK_PICKLE_CACHE = CACHE_DIR / "spb_walk.pkl"
    STATION_NODE_CACHE = CACHE_DIR / "spb_station_nodes.json"
    STATION_RECORD_CACHE = CACHE_DIR / "spb_station_records.json"
    SEARCH_HISTORY_JSONL = CACHE_DIR / "search_history.jsonl"
    SEARCH_HISTORY_CSV = CACHE_DIR / "search_history.csv"
    SEARCH_HISTORY_DB = CACHE_DIR / "search_history.sqlite3"


def fast_haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))
