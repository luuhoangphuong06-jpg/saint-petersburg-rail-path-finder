from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PITCH_DB_PATH", BASE_DIR / "pitch_manager.db"))
BOOKING_STATUSES = {"pending", "confirmed", "completed", "cancelled"}


class BookingConflictError(ValueError):
    """Raised when a pitch already has a non-cancelled booking in this time slot."""


class PitchUnavailableError(ValueError):
    """Raised when a booking is requested for an inactive pitch."""


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def database_connection(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(db_path: str | Path | None = None) -> None:
    if db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with database_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pitches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                pitch_type TEXT NOT NULL,
                hourly_rate REAL NOT NULL CHECK (hourly_rate >= 0),
                is_available INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT,
                email TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pitch_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                booking_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                notes TEXT,
                total_price REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (pitch_id) REFERENCES pitches(id),
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_pitch_date_time "
            "ON bookings(pitch_id, booking_date, start_time, end_time)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_status_date "
            "ON bookings(status, booking_date)"
        )

    seed_data(db_path)


def seed_data(db_path: str | Path | None = None) -> None:
    with database_connection(db_path) as conn:
        pitch_count = conn.execute("SELECT COUNT(*) AS count FROM pitches").fetchone()["count"]
        if pitch_count == 0:
            conn.executemany(
                "INSERT INTO pitches (name, pitch_type, hourly_rate, is_available) VALUES (?, ?, ?, ?)",
                [
                    ("Sân A1", "5v5", 180000, 1),
                    ("Sân A2", "7v7", 220000, 1),
                    ("Sân B1", "11v11", 350000, 1),
                    ("Sân B2", "11v11", 380000, 0),
                ],
            )

        customer_count = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]
        if customer_count == 0:
            conn.executemany(
                "INSERT INTO customers (name, phone, email) VALUES (?, ?, ?)",
                [
                    ("Nguyễn Văn An", "0901234567", "an@example.com"),
                    ("Trần Thị Bích", "0912345678", "bich@example.com"),
                    ("Lê Hoàng Cường", "0923456789", "cuong@example.com"),
                ],
            )

        booking_count = conn.execute("SELECT COUNT(*) AS count FROM bookings").fetchone()["count"]
        if booking_count == 0:
            today = date.today().isoformat()
            conn.executemany(
                """
                INSERT INTO bookings
                    (pitch_id, customer_id, booking_date, start_time, end_time, status, notes, total_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, 1, today, "08:00", "10:00", "confirmed", "Đặt sân buổi sáng", 360000),
                    (2, 2, today, "18:00", "20:00", "pending", "Đội tập luyện", 440000),
                    (3, 3, today, "19:00", "21:00", "completed", "Đá giao hữu", 700000),
                ],
            )


def add_pitch(
    name: str,
    pitch_type: str,
    hourly_rate: float,
    is_available: int,
    db_path: str | Path | None = None,
) -> int:
    name = name.strip()
    pitch_type = pitch_type.strip()
    try:
        rate = float(hourly_rate)
    except (TypeError, ValueError) as exc:
        raise ValueError("Giá thuê phải là một số hợp lệ.") from exc
    if not name or not pitch_type:
        raise ValueError("Tên sân và loại sân không được để trống.")
    if rate < 0 or rate != rate or rate in (float("inf"), float("-inf")):
        raise ValueError("Giá thuê phải là số không âm.")
    if is_available not in (0, 1):
        raise ValueError("Trạng thái sân không hợp lệ.")

    with database_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO pitches (name, pitch_type, hourly_rate, is_available) VALUES (?, ?, ?, ?)",
            (name, pitch_type, rate, is_available),
        )
        return int(cursor.lastrowid)


def get_pitches(
    available_only: bool = False, db_path: str | Path | None = None
) -> list[sqlite3.Row]:
    query = "SELECT * FROM pitches"
    if available_only:
        query += " WHERE is_available = 1"
    query += " ORDER BY id"
    with database_connection(db_path) as conn:
        return conn.execute(query).fetchall()


def toggle_pitch_status(pitch_id: int, db_path: str | Path | None = None) -> bool:
    with database_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE pitches SET is_available = CASE WHEN is_available = 1 THEN 0 ELSE 1 END WHERE id = ?",
            (pitch_id,),
        )
        return cursor.rowcount > 0


def get_customers(db_path: str | Path | None = None) -> list[sqlite3.Row]:
    with database_connection(db_path) as conn:
        return conn.execute(
            """
            SELECT c.*, COUNT(b.id) AS booking_count
            FROM customers c
            LEFT JOIN bookings b ON b.customer_id = c.id
            GROUP BY c.id
            ORDER BY c.id DESC
            """
        ).fetchall()


def _upsert_customer(
    conn: sqlite3.Connection, name: str, phone: str, email: str | None
) -> int:
    name = name.strip()
    phone = phone.strip()
    email = email.strip() if email else None
    existing = conn.execute(
        "SELECT id FROM customers WHERE phone = ? OR LOWER(name) = LOWER(?) ORDER BY id LIMIT 1",
        (phone, name),
    ).fetchone()
    if existing:
        customer_id = int(existing["id"])
        conn.execute(
            "UPDATE customers SET name = ?, phone = ?, email = ? WHERE id = ?",
            (name, phone, email, customer_id),
        )
        return customer_id

    cursor = conn.execute(
        "INSERT INTO customers (name, phone, email) VALUES (?, ?, ?)",
        (name, phone, email),
    )
    return int(cursor.lastrowid)


def upsert_customer(
    name: str,
    phone: str,
    email: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    if not name.strip() or not phone.strip():
        raise ValueError("Tên khách hàng và số điện thoại không được để trống.")
    with database_connection(db_path) as conn:
        return _upsert_customer(conn, name, phone, email)


def _parse_booking_times(start_time: str, end_time: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(start_time, "%H:%M")
        end = datetime.strptime(end_time, "%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError("Giờ bắt đầu và kết thúc phải theo định dạng HH:MM.") from exc
    if start.strftime("%H:%M") != start_time or end.strftime("%H:%M") != end_time:
        raise ValueError("Giờ bắt đầu và kết thúc phải theo định dạng HH:MM.")
    if start >= end:
        raise ValueError("Thời gian kết thúc phải lớn hơn thời gian bắt đầu.")
    return start, end


def calculate_total_price(hourly_rate: float, start_time: str, end_time: str) -> float:
    start, end = _parse_booking_times(start_time, end_time)
    duration_hours = (end - start).total_seconds() / 3600
    return round(float(hourly_rate) * duration_hours, 2)


def _validate_booking(
    booking_date: str, start_time: str, end_time: str, status: str
) -> tuple[datetime, datetime]:
    try:
        if date.fromisoformat(booking_date).isoformat() != booking_date:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Ngày đặt sân không hợp lệ.") from exc
    if status not in BOOKING_STATUSES:
        raise ValueError("Trạng thái đặt sân không hợp lệ.")
    return _parse_booking_times(start_time, end_time)


def _ensure_slot_available(
    conn: sqlite3.Connection,
    pitch_id: int,
    booking_date: str,
    start_time: str,
    end_time: str,
    exclude_booking_id: int | None = None,
) -> None:
    query = """
        SELECT 1 FROM bookings
        WHERE pitch_id = ? AND booking_date = ? AND status != 'cancelled'
          AND start_time < ? AND end_time > ?
    """
    params: list[Any] = [pitch_id, booking_date, end_time, start_time]
    if exclude_booking_id is not None:
        query += " AND id != ?"
        params.append(exclude_booking_id)
    if conn.execute(query + " LIMIT 1", params).fetchone():
        raise BookingConflictError("Sân đã có lịch đặt trùng khung giờ này.")


def create_booking(
    pitch_id: int,
    customer_name: str,
    phone: str,
    booking_date: str,
    start_time: str,
    end_time: str,
    notes: str,
    status: str = "pending",
    email: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    _validate_booking(booking_date, start_time, end_time, status)
    if not customer_name.strip() or not phone.strip():
        raise ValueError("Tên khách hàng và số điện thoại không được để trống.")

    with database_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        pitch = conn.execute(
            "SELECT hourly_rate, is_available FROM pitches WHERE id = ?", (pitch_id,)
        ).fetchone()
        if pitch is None:
            raise ValueError("Sân được chọn không tồn tại.")
        if not pitch["is_available"]:
            raise PitchUnavailableError("Sân này đang tạm ngưng và không thể nhận đặt lịch.")
        if status != "cancelled":
            _ensure_slot_available(conn, pitch_id, booking_date, start_time, end_time)

        customer_id = _upsert_customer(conn, customer_name, phone, email)
        total_price = calculate_total_price(
            float(pitch["hourly_rate"]), start_time, end_time
        )
        cursor = conn.execute(
            """
            INSERT INTO bookings
                (pitch_id, customer_id, booking_date, start_time, end_time, status, notes, total_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pitch_id,
                customer_id,
                booking_date,
                start_time,
                end_time,
                status,
                notes.strip(),
                total_price,
            ),
        )
        return int(cursor.lastrowid)


def get_pitch(pitch_id: int, db_path: str | Path | None = None) -> sqlite3.Row | None:
    with database_connection(db_path) as conn:
        return conn.execute("SELECT * FROM pitches WHERE id = ?", (pitch_id,)).fetchone()


def get_bookings(
    limit: int | None = None,
    status: str | None = None,
    db_path: str | Path | None = None,
) -> list[sqlite3.Row]:
    query = """
        SELECT b.*, p.name AS pitch_name, p.pitch_type, p.hourly_rate,
               c.name AS customer_name, c.phone, c.email
        FROM bookings b
        JOIN pitches p ON p.id = b.pitch_id
        JOIN customers c ON c.id = b.customer_id
    """
    params: list[Any] = []
    if status:
        if status not in BOOKING_STATUSES:
            raise ValueError("Trạng thái đặt sân không hợp lệ.")
        query += " WHERE b.status = ?"
        params.append(status)
    query += " ORDER BY b.booking_date DESC, b.start_time DESC, b.id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(0, int(limit)))
    with database_connection(db_path) as conn:
        return conn.execute(query, params).fetchall()


def update_booking_status(
    booking_id: int, status: str, db_path: str | Path | None = None
) -> bool:
    if status not in BOOKING_STATUSES:
        raise ValueError("Trạng thái đặt sân không hợp lệ.")
    with database_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        booking = conn.execute(
            """
            SELECT pitch_id, booking_date, start_time, end_time
            FROM bookings WHERE id = ?
            """,
            (booking_id,),
        ).fetchone()
        if booking is None:
            return False
        if status != "cancelled":
            _ensure_slot_available(
                conn,
                int(booking["pitch_id"]),
                booking["booking_date"],
                booking["start_time"],
                booking["end_time"],
                exclude_booking_id=booking_id,
            )
        conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
        return True


def get_dashboard_stats(db_path: str | Path | None = None) -> dict[str, Any]:
    today = date.today().isoformat()
    with database_connection(db_path) as conn:
        total_pitches = conn.execute("SELECT COUNT(*) AS count FROM pitches").fetchone()["count"]
        available_pitches = conn.execute(
            "SELECT COUNT(*) AS count FROM pitches WHERE is_available = 1"
        ).fetchone()["count"]
        revenue_today = conn.execute(
            """
            SELECT COALESCE(SUM(total_price), 0) AS total
            FROM bookings
            WHERE status IN ('confirmed', 'completed') AND booking_date = ?
            """,
            (today,),
        ).fetchone()["total"]
        today_bookings = conn.execute(
            "SELECT COUNT(*) AS count FROM bookings WHERE booking_date = ? AND status != 'cancelled'",
            (today,),
        ).fetchone()["count"]
        pending_bookings = conn.execute(
            "SELECT COUNT(*) AS count FROM bookings WHERE status = 'pending'"
        ).fetchone()["count"]
        return {
            "total_pitches": total_pitches,
            "available_pitches": available_pitches,
            "revenue_today": revenue_today,
            "bookings_today": today_bookings,
            "pending_bookings": pending_bookings,
        }


def get_report_data(db_path: str | Path | None = None) -> dict[str, Any]:
    with database_connection(db_path) as conn:
        by_pitch = conn.execute(
            """
            SELECT p.name, SUM(b.total_price) AS revenue
            FROM bookings b
            JOIN pitches p ON p.id = b.pitch_id
            WHERE b.status IN ('confirmed', 'completed')
            GROUP BY p.id, p.name
            ORDER BY revenue DESC
            """
        ).fetchall()
        by_date = conn.execute(
            """
            SELECT booking_date, SUM(total_price) AS revenue
            FROM bookings
            WHERE status IN ('confirmed', 'completed')
            GROUP BY booking_date
            ORDER BY booking_date DESC
            LIMIT 7
            """
        ).fetchall()
        return {"by_pitch": by_pitch, "by_date": by_date}
