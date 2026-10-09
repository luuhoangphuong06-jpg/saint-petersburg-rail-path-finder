from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PITCH_DB_PATH", BASE_DIR / "pitch_manager.db"))
BOOKING_STATUSES = {"pending", "confirmed", "completed", "cancelled"}
DEFAULT_ADMIN_MIGRATION = "20261009_remove_seeded_admin"
CUSTOMER_AUTH_MIGRATION = "20261009_customer_auth_fields"


class BookingConflictError(ValueError):
    """Raised when a pitch already has a non-cancelled booking in this time slot."""


class PitchUnavailableError(ValueError):
    """Raised when a booking is requested for an inactive pitch."""


class BookingNotFoundError(ValueError):
    """Raised when a requested booking does not exist."""


class PitchNotFoundError(ValueError):
    """Raised when a requested pitch does not exist."""


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


def _install_booking_triggers(conn: sqlite3.Connection) -> None:
    for trigger in (
        "bookings_validate_insert",
        "bookings_validate_update",
        "bookings_history_insert",
        "bookings_history_status_update",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")

    validation = """
        SELECT CASE WHEN NEW.status NOT IN ('pending', 'confirmed', 'completed', 'cancelled')
            THEN RAISE(ABORT, 'invalid booking status') END;
        SELECT CASE WHEN date(NEW.booking_date) IS NOT NEW.booking_date
            THEN RAISE(ABORT, 'invalid booking date') END;
        SELECT CASE WHEN strftime('%H:%M', NEW.start_time) IS NOT NEW.start_time
            OR strftime('%H:%M', NEW.end_time) IS NOT NEW.end_time
            OR NEW.end_time <= NEW.start_time
            THEN RAISE(ABORT, 'invalid booking time') END;
        SELECT CASE WHEN NEW.total_price < 0 OR NEW.deposit_amount < 0
            OR NEW.remaining_amount < 0 OR NEW.deposit_amount > NEW.total_price
            OR ABS(NEW.total_price - NEW.deposit_amount - NEW.remaining_amount) > 0.000001
            THEN RAISE(ABORT, 'invalid booking amounts') END;
    """
    conn.execute(
        f"""
        CREATE TRIGGER bookings_validate_insert
        BEFORE INSERT ON bookings
        BEGIN
            {validation}
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM pitches WHERE id = NEW.pitch_id AND is_available = 1
            ) THEN RAISE(ABORT, 'pitch unavailable') END;
            SELECT CASE WHEN NEW.status != 'cancelled' AND EXISTS (
                SELECT 1 FROM bookings
                WHERE pitch_id = NEW.pitch_id AND booking_date = NEW.booking_date
                  AND status != 'cancelled'
                  AND start_time < NEW.end_time AND end_time > NEW.start_time
            ) THEN RAISE(ABORT, 'booking conflict') END;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER bookings_validate_update
        BEFORE UPDATE OF pitch_id, customer_id, booking_date, start_time, end_time,
                         status, total_price, deposit_amount, remaining_amount
        ON bookings
        BEGIN
            {validation}
            SELECT CASE WHEN NEW.status != 'cancelled' AND NOT EXISTS (
                SELECT 1 FROM pitches WHERE id = NEW.pitch_id AND is_available = 1
            ) THEN RAISE(ABORT, 'pitch unavailable') END;
            SELECT CASE WHEN NEW.status != 'cancelled' AND EXISTS (
                SELECT 1 FROM bookings
                WHERE id != OLD.id AND pitch_id = NEW.pitch_id
                  AND booking_date = NEW.booking_date AND status != 'cancelled'
                  AND start_time < NEW.end_time AND end_time > NEW.start_time
            ) THEN RAISE(ABORT, 'booking conflict') END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER bookings_history_insert
        AFTER INSERT ON bookings
        BEGIN
            INSERT INTO booking_status_history (booking_id, status_from, status_to)
            VALUES (NEW.id, NULL, NEW.status);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER bookings_history_status_update
        AFTER UPDATE OF status ON bookings
        WHEN OLD.status != NEW.status
        BEGIN
            INSERT INTO booking_status_history (booking_id, status_from, status_to)
            VALUES (NEW.id, OLD.status, NEW.status);
        END
        """
    )


def init_db(db_path: str | Path | None = None) -> None:
    if db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with database_connection(db_path) as conn:
        had_admin_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'admins'"
        ).fetchone() is not None
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        seeded_admin_migration = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (DEFAULT_ADMIN_MIGRATION,),
        ).fetchone()
        if seeded_admin_migration is None:
            if had_admin_table:
                conn.execute("DELETE FROM admins WHERE username = 'admin'")
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (DEFAULT_ADMIN_MIGRATION,),
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pitches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                pitch_type TEXT NOT NULL,
                hourly_rate INTEGER NOT NULL CHECK (hourly_rate >= 0),
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
                password_hash TEXT,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'inactive')),
                last_login_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        customer_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(customers)").fetchall()
        }
        if "password_hash" not in customer_columns:
            conn.execute("ALTER TABLE customers ADD COLUMN password_hash TEXT")
        if "status" not in customer_columns:
            conn.execute(
                "ALTER TABLE customers ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
        if "last_login_at" not in customer_columns:
            conn.execute("ALTER TABLE customers ADD COLUMN last_login_at TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status)"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (version)
            VALUES (?)
            """,
            (CUSTOMER_AUTH_MIGRATION,),
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
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'confirmed', 'completed', 'cancelled')),
                notes TEXT,
                total_price INTEGER NOT NULL CHECK (total_price >= 0),
                deposit_amount INTEGER NOT NULL DEFAULT 0 CHECK (deposit_amount >= 0),
                remaining_amount INTEGER NOT NULL DEFAULT 0 CHECK (remaining_amount >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (end_time > start_time),
                CHECK (deposit_amount <= total_price),
                CHECK (total_price = deposit_amount + remaining_amount),
                FOREIGN KEY (pitch_id) REFERENCES pitches(id),
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            )
            """
        )
        booking_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(bookings)").fetchall()
        }
        if "deposit_amount" not in booking_columns:
            conn.execute(
                "ALTER TABLE bookings ADD COLUMN deposit_amount INTEGER NOT NULL DEFAULT 0 "
                "CHECK (deposit_amount >= 0)"
            )
        if "remaining_amount" not in booking_columns:
            conn.execute(
                "ALTER TABLE bookings ADD COLUMN remaining_amount INTEGER NOT NULL DEFAULT 0 "
                "CHECK (remaining_amount >= 0)"
            )
        conn.execute(
            "UPDATE bookings SET remaining_amount = total_price - deposit_amount "
            "WHERE remaining_amount = 0 AND total_price > deposit_amount"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS booking_status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_id INTEGER NOT NULL,
                status_from TEXT,
                status_to TEXT NOT NULL,
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_booking_history_booking "
            "ON booking_status_history(booking_id, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_pitch_date_time "
            "ON bookings(pitch_id, booking_date, start_time, end_time)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_status_date "
            "ON bookings(status, booking_date)"
        )
        _install_booking_triggers(conn)
        conn.execute(
            """
            INSERT INTO booking_status_history (booking_id, status_from, status_to, changed_at)
            SELECT b.id, NULL, b.status, COALESCE(b.created_at, CURRENT_TIMESTAMP)
            FROM bookings b
            WHERE NOT EXISTS (
                SELECT 1 FROM booking_status_history h WHERE h.booking_id = b.id
            )
            """
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
                    (pitch_id, customer_id, booking_date, start_time, end_time, status, notes,
                     total_price, deposit_amount, remaining_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, 1, today, "08:00", "10:00", "confirmed", "Đặt sân buổi sáng", 360000, 0, 360000),
                    (2, 2, today, "18:00", "20:00", "pending", "Đội tập luyện", 440000, 0, 440000),
                    (3, 3, today, "19:00", "21:00", "completed", "Đá giao hữu", 700000, 0, 700000),
                ],
            )


def create_admin_account(
    username: str, password: str, db_path: str | Path | None = None
) -> None:
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Tên đăng nhập không được để trống.")
    username = username.strip()
    if len(username) > 64:
        raise ValueError("Tên đăng nhập không được vượt quá 64 ký tự.")
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("Mật khẩu quản trị phải có ít nhất 12 ký tự.")
    with database_connection(db_path) as conn:
        admin_count = conn.execute("SELECT COUNT(*) AS count FROM admins").fetchone()["count"]
        if admin_count:
            raise ValueError("Tài khoản quản trị đã được khởi tạo.")
        try:
            conn.execute(
                "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Tên đăng nhập đã tồn tại.") from exc


def add_pitch(
    name: str,
    pitch_type: str,
    hourly_rate: float,
    is_available: int,
    db_path: str | Path | None = None,
) -> int:
    name = name.strip()
    pitch_type = pitch_type.strip()
    if not name or not pitch_type:
        raise ValueError("Tên sân và loại sân không được để trống.")
    rate = _money_amount(hourly_rate, "Giá thuê")
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


def verify_admin_login(username: str, password: str, db_path: str | Path | None = None) -> bool:
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return False
    with database_connection(db_path) as conn:
        row = conn.execute(
            "SELECT password_hash FROM admins WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            return False
        return check_password_hash(row["password_hash"], password)


def create_customer_account(
    name: str,
    phone: str,
    password: str,
    email: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Họ tên không được để trống.")
    if not isinstance(phone, str) or not phone.strip():
        raise ValueError("Số điện thoại không được để trống.")
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("Mật khẩu phải có ít nhất 8 ký tự.")
    if email is not None and not isinstance(email, str):
        raise ValueError("Email không hợp lệ.")

    name = name.strip()
    phone = phone.strip()
    email = email.strip() if email else None
    with database_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT id, password_hash, status FROM customers WHERE phone = ?",
            (phone,),
        ).fetchone()
        password_hash = generate_password_hash(password)
        if existing:
            if existing["password_hash"]:
                raise ValueError("Số điện thoại đã được đăng ký.")
            conn.execute(
                """
                UPDATE customers
                SET name = ?, email = ?, password_hash = ?, status = 'active'
                WHERE id = ?
                """,
                (name, email, password_hash, existing["id"]),
            )
            return int(existing["id"])

        cursor = conn.execute(
            """
            INSERT INTO customers (name, phone, email, password_hash, status)
            VALUES (?, ?, ?, ?, 'active')
            """,
            (name, phone, email, password_hash),
        )
        return int(cursor.lastrowid)


def verify_customer_login(
    phone: str, password: str, db_path: str | Path | None = None
) -> sqlite3.Row | None:
    phone = phone.strip() if isinstance(phone, str) else ""
    if not phone or not isinstance(password, str) or not password:
        return None
    with database_connection(db_path) as conn:
        customer = conn.execute(
            """
            SELECT id, name, phone, email, status, password_hash
            FROM customers
            WHERE phone = ?
            """,
            (phone,),
        ).fetchone()
        if (
            customer is None
            or customer["status"] != "active"
            or not customer["password_hash"]
            or not check_password_hash(customer["password_hash"], password)
        ):
            return None
        conn.execute(
            "UPDATE customers SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?",
            (customer["id"],),
        )
        return customer


def get_customer(
    customer_id: int, db_path: str | Path | None = None
) -> sqlite3.Row | None:
    with database_connection(db_path) as conn:
        return conn.execute(
            """
            SELECT id, name, phone, email, status, created_at, last_login_at
            FROM customers WHERE id = ?
            """,
            (customer_id,),
        ).fetchone()


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
    if not isinstance(name, str) or not isinstance(phone, str):
        raise ValueError("Tên khách hàng và số điện thoại phải là chuỗi.")
    if email is not None and not isinstance(email, str):
        raise ValueError("Email phải là chuỗi.")
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
    if not isinstance(name, str) or not isinstance(phone, str) or not name.strip() or not phone.strip():
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


def calculate_total_price(hourly_rate: float, start_time: str, end_time: str) -> int:
    return _price_for_duration(hourly_rate, start_time, end_time)


def _validate_booking(
    booking_date: str | None,
    start_time: str | None,
    end_time: str | None,
    status: str,
) -> tuple[datetime, datetime]:
    try:
        if date.fromisoformat(booking_date).isoformat() != booking_date:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Ngày đặt sân không hợp lệ.") from exc
    if not isinstance(status, str) or status not in BOOKING_STATUSES:
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


def _money_amount(value: Any, field_name: str) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} phải là số tiền hợp lệ.") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field_name} phải là số không âm.")
    try:
        rounded = amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} vượt quá giới hạn cho phép.") from exc
    if amount != rounded:
        raise ValueError(f"{field_name} phải là số nguyên đồng.")
    if rounded > Decimal(2**63 - 1):
        raise ValueError(f"{field_name} vượt quá giới hạn cho phép.")
    return int(rounded)


def _price_for_duration(hourly_rate: Any, start_time: str, end_time: str) -> int:
    start, end = _parse_booking_times(start_time, end_time)
    try:
        rate = Decimal(str(hourly_rate))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Giá thuê sân không hợp lệ.") from exc
    if not rate.is_finite() or rate < 0:
        raise ValueError("Giá thuê sân phải là số không âm.")
    duration_minutes = int((end - start).total_seconds() // 60)
    try:
        price = (
            (rate * Decimal(duration_minutes) / Decimal(60)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    except InvalidOperation as exc:
        raise ValueError("Tổng tiền sân vượt quá giới hạn cho phép.") from exc
    if price > Decimal(2**63 - 1):
        raise ValueError("Tổng tiền sân vượt quá giới hạn cho phép.")
    return int(price)


def create_booking(
    pitch_id: int,
    customer_name: str | None = None,
    phone: str | None = None,
    booking_date: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    notes: str = "",
    status: str = "pending",
    email: str | None = None,
    db_path: str | Path | None = None,
    customer_id: int | None = None,
    deposit_amount: Any = 0,
) -> int:
    _validate_booking(booking_date, start_time, end_time, status)
    deposit = _money_amount(deposit_amount, "Tiền đặt cọc")
    if customer_id is None and (
        not isinstance(customer_name, str)
        or not customer_name.strip()
        or not isinstance(phone, str)
        or not phone.strip()
    ):
        raise ValueError("Cần chọn khách hàng hoặc nhập tên và số điện thoại.")
    if customer_id is not None and any(
        value is not None for value in (customer_name, phone, email)
    ):
        raise ValueError("Chỉ chọn khách hàng có sẵn hoặc nhập thông tin khách mới.")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("Ghi chú phải là chuỗi.")

    with database_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        pitch = conn.execute(
            "SELECT hourly_rate, is_available FROM pitches WHERE id = ?", (pitch_id,)
        ).fetchone()
        if pitch is None:
            raise PitchNotFoundError("Sân được chọn không tồn tại.")
        if not pitch["is_available"]:
            raise PitchUnavailableError("Sân này đang tạm ngưng và không thể nhận đặt lịch.")
        if status != "cancelled":
            _ensure_slot_available(conn, pitch_id, booking_date, start_time, end_time)

        if customer_id is None:
            customer_id = _upsert_customer(conn, customer_name or "", phone or "", email)
        elif conn.execute(
            "SELECT 1 FROM customers WHERE id = ?", (customer_id,)
        ).fetchone() is None:
            raise ValueError("Khách hàng được chọn không tồn tại.")
        total_price = _price_for_duration(pitch["hourly_rate"], start_time, end_time)
        if deposit > total_price:
            raise ValueError("Tiền đặt cọc không được lớn hơn tổng tiền sân.")
        cursor = conn.execute(
            """
            INSERT INTO bookings
                (pitch_id, customer_id, booking_date, start_time, end_time, status, notes,
                 total_price, deposit_amount, remaining_amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pitch_id,
                customer_id,
                booking_date,
                start_time,
                end_time,
                status,
                (notes or "").strip(),
                total_price,
                deposit,
                total_price - deposit,
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
    booking_date: str | None = None,
    pitch_id: int | None = None,
) -> list[sqlite3.Row]:
    query = """
        SELECT b.*, p.name AS pitch_name, p.pitch_type, p.hourly_rate,
               c.name AS customer_name, c.phone, c.email
        FROM bookings b
        JOIN pitches p ON p.id = b.pitch_id
        JOIN customers c ON c.id = b.customer_id
    """
    params: list[Any] = []
    filters: list[str] = []
    if status is not None:
        if not isinstance(status, str) or status not in BOOKING_STATUSES:
            raise ValueError("Trạng thái đặt sân không hợp lệ.")
        filters.append("b.status = ?")
        params.append(status)
    if booking_date:
        _validate_booking(booking_date, "00:00", "00:01", "pending")
        filters.append("b.booking_date = ?")
        params.append(booking_date)
    if pitch_id is not None:
        if int(pitch_id) <= 0:
            raise ValueError("Sân được chọn không hợp lệ.")
        filters.append("b.pitch_id = ?")
        params.append(int(pitch_id))
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY b.booking_date DESC, b.start_time ASC, b.id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(0, int(limit)))
    with database_connection(db_path) as conn:
        return conn.execute(query, params).fetchall()


def get_booking(
    booking_id: int, db_path: str | Path | None = None
) -> sqlite3.Row | None:
    with database_connection(db_path) as conn:
        return conn.execute(
            """
            SELECT b.*, p.name AS pitch_name, p.pitch_type, p.hourly_rate,
                   c.name AS customer_name, c.phone, c.email
            FROM bookings b
            JOIN pitches p ON p.id = b.pitch_id
            JOIN customers c ON c.id = b.customer_id
            WHERE b.id = ?
            """,
            (booking_id,),
        ).fetchone()


def get_customer_bookings(
    phone: str | None = None,
    booking_id: int | None = None,
    db_path: str | Path | None = None,
    customer_id: int | None = None,
) -> list[sqlite3.Row]:
    if customer_id is None:
        phone = phone.strip() if isinstance(phone, str) else ""
        if not phone:
            raise ValueError("Số điện thoại không được để trống.")
    elif customer_id <= 0:
        raise ValueError("Mã khách hàng không hợp lệ.")
    query = """
        SELECT b.*, p.name AS pitch_name, p.pitch_type, p.hourly_rate,
               c.name AS customer_name, c.phone, c.email
        FROM bookings b
        JOIN pitches p ON p.id = b.pitch_id
        JOIN customers c ON c.id = b.customer_id
        WHERE c.phone = ?
    """
    params: list[Any] = [phone]
    if customer_id is not None:
        query = query.replace("WHERE c.phone = ?", "WHERE c.id = ?")
        params = [customer_id]
    if booking_id is not None:
        if booking_id <= 0:
            raise ValueError("Mã booking không hợp lệ.")
        query += " AND b.id = ?"
        params.append(booking_id)
    query += " ORDER BY b.booking_date DESC, b.start_time DESC, b.id DESC LIMIT 20"
    with database_connection(db_path) as conn:
        return conn.execute(query, params).fetchall()


def get_booking_status_history(
    booking_id: int, db_path: str | Path | None = None
) -> list[sqlite3.Row]:
    with database_connection(db_path) as conn:
        return conn.execute(
            """
            SELECT id, booking_id, status_from, status_to, changed_at
            FROM booking_status_history
            WHERE booking_id = ?
            ORDER BY id
            """,
            (booking_id,),
        ).fetchall()


def update_booking(
    booking_id: int,
    *,
    pitch_id: int | None = None,
    customer_id: int | None = None,
    customer_name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    booking_date: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    status: str | None = None,
    notes: str | None = None,
    deposit_amount: Any = None,
    db_path: str | Path | None = None,
) -> bool:
    with database_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        booking = conn.execute(
            "SELECT * FROM bookings WHERE id = ?", (booking_id,)
        ).fetchone()
        if booking is None:
            raise BookingNotFoundError("Không tìm thấy lịch đặt sân.")

        new_pitch_id = int(pitch_id if pitch_id is not None else booking["pitch_id"])
        new_date = booking_date if booking_date is not None else booking["booking_date"]
        new_start = start_time if start_time is not None else booking["start_time"]
        new_end = end_time if end_time is not None else booking["end_time"]
        new_status = status if status is not None else booking["status"]
        _validate_booking(new_date, new_start, new_end, new_status)

        pitch = conn.execute(
            "SELECT hourly_rate, is_available FROM pitches WHERE id = ?", (new_pitch_id,)
        ).fetchone()
        if pitch is None:
            raise PitchNotFoundError("Sân được chọn không tồn tại.")
        if new_status != "cancelled" and not pitch["is_available"]:
            raise PitchUnavailableError("Sân này đang tạm ngưng và không thể nhận đặt lịch.")
        if new_status != "cancelled":
            _ensure_slot_available(
                conn, new_pitch_id, new_date, new_start, new_end, booking_id
            )

        if customer_id is not None and any(
            value is not None for value in (customer_name, phone, email)
        ):
            raise ValueError("Chỉ chọn khách hàng có sẵn hoặc cập nhật thông tin khách.")
        if customer_id is not None:
            if conn.execute(
                "SELECT 1 FROM customers WHERE id = ?", (customer_id,)
            ).fetchone() is None:
                raise ValueError("Khách hàng được chọn không tồn tại.")
            new_customer_id = customer_id
        elif customer_name is not None or phone is not None or email is not None:
            if (
                not isinstance(customer_name, str)
                or not customer_name.strip()
                or not isinstance(phone, str)
                or not phone.strip()
            ):
                raise ValueError("Cần nhập cả tên khách hàng và số điện thoại.")
            new_customer_id = _upsert_customer(conn, customer_name, phone, email)
        else:
            new_customer_id = int(booking["customer_id"])

        price_changed = (
            new_pitch_id != int(booking["pitch_id"])
            or new_start != booking["start_time"]
            or new_end != booking["end_time"]
        )
        new_total = (
            _price_for_duration(pitch["hourly_rate"], new_start, new_end)
            if price_changed
            else _money_amount(booking["total_price"], "Tổng tiền sân")
        )
        new_deposit = (
            _money_amount(deposit_amount, "Tiền đặt cọc")
            if deposit_amount is not None
            else _money_amount(booking["deposit_amount"], "Tiền đặt cọc")
        )
        if new_deposit > new_total:
            raise ValueError("Tiền đặt cọc không được lớn hơn tổng tiền sân.")
        if notes is not None and not isinstance(notes, str):
            raise ValueError("Ghi chú phải là chuỗi.")
        new_notes = notes.strip() if notes is not None else booking["notes"]

        conn.execute(
            """
            UPDATE bookings
            SET pitch_id = ?, customer_id = ?, booking_date = ?, start_time = ?,
                end_time = ?, status = ?, notes = ?, total_price = ?,
                deposit_amount = ?, remaining_amount = ?
            WHERE id = ?
            """,
            (
                new_pitch_id,
                new_customer_id,
                new_date,
                new_start,
                new_end,
                new_status,
                new_notes,
                new_total,
                new_deposit,
                new_total - new_deposit,
                booking_id,
            ),
        )
        return True


def cancel_booking(
    booking_id: int, db_path: str | Path | None = None
) -> bool:
    return update_booking(booking_id, status="cancelled", db_path=db_path)


def is_pitch_available(
    pitch_id: int,
    booking_date: str,
    start_time: str,
    end_time: str,
    *,
    exclude_booking_id: int | None = None,
    db_path: str | Path | None = None,
) -> bool:
    _validate_booking(booking_date, start_time, end_time, "pending")
    if exclude_booking_id is not None and exclude_booking_id <= 0:
        raise ValueError("ID lịch đặt cần loại trừ phải là số nguyên dương.")
    with database_connection(db_path) as conn:
        pitch = conn.execute(
            "SELECT is_available FROM pitches WHERE id = ?", (pitch_id,)
        ).fetchone()
        if pitch is None:
            raise PitchNotFoundError("Sân được chọn không tồn tại.")
        if not pitch["is_available"]:
            raise PitchUnavailableError("Sân này đang tạm ngưng.")
        query = """
            SELECT 1 FROM bookings
            WHERE pitch_id = ? AND booking_date = ? AND status != 'cancelled'
              AND start_time < ? AND end_time > ?
        """
        params: list[Any] = [pitch_id, booking_date, end_time, start_time]
        if exclude_booking_id is not None:
            query += " AND id != ?"
            params.append(exclude_booking_id)
        return conn.execute(query + " LIMIT 1", params).fetchone() is None


def update_booking_status(
    booking_id: int, status: str, db_path: str | Path | None = None
) -> bool:
    try:
        return update_booking(booking_id, status=status, db_path=db_path)
    except BookingNotFoundError:
        return False


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
