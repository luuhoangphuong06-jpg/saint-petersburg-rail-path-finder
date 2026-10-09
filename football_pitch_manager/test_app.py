import importlib
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class FootballPitchManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pitch_manager.sqlite3"
        os.environ["PITCH_DB_PATH"] = str(self.db_path)

        import app as app_module
        import database as database_module

        self.database = importlib.reload(database_module)
        self.app_module = importlib.reload(app_module)
        self.database.create_admin_account(
            "test-admin", "test-admin-password-2026", self.db_path
        )
        with self.database.database_connection(self.db_path) as conn:
            conn.execute("DELETE FROM bookings")
            conn.execute("DELETE FROM customers")
            conn.execute("DELETE FROM pitches")

        self.client = self.app_module.app.test_client()
        login_page = self.client.get("/login")
        csrf_token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            login_page.get_data(as_text=True),
        ).group(1)
        login_response = self.client.post(
            "/login",
            data={
                "username": "test-admin",
                "password": "test-admin-password-2026",
                "csrf_token": csrf_token,
            },
            follow_redirects=True,
        )
        self.assertEqual(login_response.status_code, 200)
        self.pitch_id = self.database.add_pitch(
            "Sân thử", "5v5", 180000, 1, self.db_path
        )
        self.closed_pitch_id = self.database.add_pitch(
            "Sân bảo trì", "7v7", 220000, 0, self.db_path
        )

    def csrf_headers(self):
        response = self.client.get("/")
        token = re.search(
            r'<meta name="csrf-token" content="([^"]+)"',
            response.get_data(as_text=True),
        )
        self.assertIsNotNone(token)
        return {"X-CSRFToken": token.group(1)}

    def api_post(self, path, **kwargs):
        return self.client.post(path, headers=self.csrf_headers(), **kwargs)

    def api_patch(self, path, **kwargs):
        return self.client.patch(path, headers=self.csrf_headers(), **kwargs)

    def tearDown(self):
        os.environ.pop("PITCH_DB_PATH", None)
        self.temp_dir.cleanup()

    def create_pitch(self, name="Sân Test", rate=150000, available=1):
        return self.database.add_pitch(name, "5v5", rate, available, self.db_path)

    def booking_payload(self, pitch_id, **overrides):
        payload = {
            "pitch_id": pitch_id,
            "customer_name": "Khách API",
            "phone": "0901111222",
            "booking_date": "2026-10-15",
            "start_time": "09:00",
            "end_time": "10:30",
        }
        payload.update(overrides)
        return payload

    def test_current_pages_load_when_logged_in(self):
        for path in ["/", "/pitches", "/bookings", "/customers", "/reports"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_customer_landing_filters_pitches_and_exposes_public_api(self):
        public_client = self.app_module.app.test_client()
        landing = public_client.get("/customer")
        self.assertEqual(landing.status_code, 200)
        self.assertIn("Đặt sân dễ dàng".encode(), landing.data)

        available = public_client.get(
            "/customer/api/pitches",
            query_string={
                "date": "2026-10-15",
                "start_time": "09:00",
                "end_time": "10:00",
            },
        )
        self.assertEqual(available.status_code, 200)
        options = {item["id"]: item for item in available.get_json()}
        self.assertFalse(options[self.closed_pitch_id]["available"])
        self.assertTrue(options[self.pitch_id]["available"])

    def test_customer_auth_register_login_wrong_password_and_booking_guard(self):
        public_client = self.app_module.app.test_client()
        self.assertEqual(public_client.get("/customer/lookup").status_code, 302)
        self.assertIn("/customer/login", public_client.get("/customer/lookup").headers["Location"])

        register_page = public_client.get("/customer/register")
        token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            register_page.get_data(as_text=True),
        ).group(1)
        registered = public_client.post(
            "/customer/register",
            data={
                "csrf_token": token,
                "name": "Khách Đăng Ký",
                "phone": "0901234555",
                "password": "customer-password",
            },
        )
        self.assertEqual(registered.status_code, 302)
        with self.database.database_connection(self.db_path) as connection:
            customer = connection.execute(
                "SELECT password_hash, status FROM customers WHERE phone = ?",
                ("0901234555",),
            ).fetchone()
        self.assertTrue(customer["password_hash"])
        self.assertNotEqual(customer["password_hash"], "customer-password")
        self.assertEqual(customer["status"], "active")

        logged_in_page = public_client.get("/customer")
        logout_token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            logged_in_page.get_data(as_text=True),
        ).group(1)
        self.assertEqual(
            public_client.post(
                "/customer/logout", data={"csrf_token": logout_token}
            ).status_code,
            302,
        )
        login_page = public_client.get("/customer/login")
        login_token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            login_page.get_data(as_text=True),
        ).group(1)
        wrong = public_client.post(
            "/customer/login",
            data={
                "csrf_token": login_token,
                "phone": "0901234555",
                "password": "wrong-password",
            },
        )
        self.assertEqual(wrong.status_code, 200)
        self.assertIn("không đúng".encode(), wrong.data)

        valid = public_client.post(
            "/customer/login",
            data={
                "csrf_token": login_token,
                "phone": "0901234555",
                "password": "customer-password",
            },
        )
        self.assertEqual(valid.status_code, 302)

        booking_page = public_client.get("/customer")
        booking_token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            booking_page.get_data(as_text=True),
        ).group(1)
        created = public_client.post(
            "/customer/bookings",
            data={
                "csrf_token": booking_token,
                "pitch_id": str(self.pitch_id),
                "booking_date": "2026-10-25",
                "start_time": "12:00",
                "end_time": "13:00",
            },
        )
        self.assertEqual(created.status_code, 302)

        other_client = self.app_module.app.test_client()
        self.customer_register(other_client, name="Khách Khác", phone="0901234666")
        booking_id = self.database.get_bookings(db_path=self.db_path)[0]["id"]
        other_lookup = other_client.get(
            "/customer/lookup", query_string={"booking_id": booking_id}
        )
        self.assertIn("Không tìm thấy lịch đặt phù hợp.".encode(), other_lookup.data)

    def customer_register(self, client, name="Khách Website", phone="0901234000"):
        page = client.get("/customer/register")
        token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            page.get_data(as_text=True),
        ).group(1)
        response = client.post(
            "/customer/register",
            data={
                "csrf_token": token,
                "name": name,
                "phone": phone,
                "email": "web@example.com",
                "password": "customer-password",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

    def test_customer_booking_confirmation_and_lookup_use_real_booking(self):
        public_client = self.app_module.app.test_client()
        self.customer_register(public_client)
        landing = public_client.get("/customer")
        csrf_token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            landing.get_data(as_text=True),
        ).group(1)
        response = public_client.post(
            "/customer/bookings",
            data={
                "csrf_token": csrf_token,
                "pitch_id": str(self.pitch_id),
                "booking_date": "2026-10-15",
                "start_time": "09:00",
                "end_time": "10:00",
                "notes": "Đặt từ trang khách",
            },
        )
        self.assertEqual(response.status_code, 302)
        confirmation_url = response.headers["Location"]
        confirmation = public_client.get(confirmation_url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn("Yêu cầu đặt sân thành công".encode(), confirmation.data)

        booking_id = self.database.get_bookings(db_path=self.db_path)[0]["id"]
        lookup = public_client.get(
            "/customer/lookup",
            query_string={"phone": "0901234000", "booking_id": booking_id},
        )
        self.assertEqual(lookup.status_code, 200)
        self.assertIn(f"#{booking_id}".encode(), lookup.data)
        self.assertIn("Chờ xác nhận".encode(), lookup.data)

        unavailable = public_client.get(
            "/customer/api/pitches",
            query_string={
                "date": "2026-10-15",
                "start_time": "09:30",
                "end_time": "10:30",
            },
        )
        self.assertEqual(unavailable.status_code, 200)
        self.assertFalse(
            next(item for item in unavailable.get_json() if item["id"] == self.pitch_id)[
                "available"
            ]
        )

    def test_customer_booking_requires_csrf_and_shows_error_for_maintenance(self):
        public_client = self.app_module.app.test_client()
        self.customer_register(public_client, name="Khách Bảo Trì", phone="0901234002")
        missing_token = public_client.post(
            "/customer/bookings",
            data={
                "pitch_id": str(self.pitch_id),
                "booking_date": "2026-10-16",
                "start_time": "10:00",
                "end_time": "11:00",
            },
            follow_redirects=True,
        )
        self.assertEqual(missing_token.status_code, 400)

        landing = public_client.get("/customer")
        csrf_token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            landing.get_data(as_text=True),
        ).group(1)
        maintenance = public_client.post(
            "/customer/bookings",
            data={
                "csrf_token": csrf_token,
                "pitch_id": str(self.closed_pitch_id),
                "booking_date": "2026-10-16",
                "start_time": "10:00",
                "end_time": "11:00",
            },
            follow_redirects=True,
        )
        self.assertEqual(maintenance.status_code, 200)
        self.assertIn("đang tạm ngưng".encode(), maintenance.data)

    def test_admin_bootstrap_is_explicit_and_login_requires_csrf(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = Path(directory) / "legacy-admin.sqlite3"
            connection = sqlite3.connect(legacy_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE admins (
                        id INTEGER PRIMARY KEY,
                        username TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
                    (
                        "admin",
                        self.database.generate_password_hash(
                            "legacy-seeded-password-for-migration"
                        ),
                    ),
                )
            finally:
                connection.close()
            self.app_module.create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "migration-test",
                    "DATABASE": str(legacy_path),
                }
            )
            with self.database.database_connection(legacy_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM admins").fetchone()[0],
                    0,
                )

            db_path = Path(directory) / "bootstrap.sqlite3"
            app = self.app_module.create_app(
                {"TESTING": True, "SECRET_KEY": "cli-test", "DATABASE": str(db_path)}
            )
            with self.assertRaises(ValueError):
                self.database.create_admin_account("admin", "short", db_path)

            runner = app.test_cli_runner()
            result = runner.invoke(
                args=["create-admin"],
                input="bootstrap-admin\nbootstrap-admin-password\nbootstrap-admin-password\n",
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(
                self.database.verify_admin_login(
                    "bootstrap-admin", "bootstrap-admin-password", db_path
                )
            )

        login_client = self.app_module.app.test_client()
        login_page = login_client.get("/login")
        self.assertNotIn(b'type="password" name="password" value=', login_page.data)
        self.assertNotIn(b'name="username" value=', login_page.data)
        csrf_token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            login_page.get_data(as_text=True),
        ).group(1)
        missing_token = login_client.post(
            "/login", data={"username": "test-admin", "password": "test-admin-password-2026"}
        )
        self.assertEqual(missing_token.status_code, 400)
        valid_login = login_client.post(
            "/login",
            data={
                "username": "test-admin",
                "password": "test-admin-password-2026",
                "csrf_token": csrf_token,
            },
        )
        self.assertEqual(valid_login.status_code, 302)

    def test_csrf_protects_form_json_and_logout_mutations(self):
        pitches_before = self.database.get_pitches(db_path=self.db_path)
        no_token_form = self.client.post(
            "/pitches",
            data={
                "name": "Không được tạo",
                "pitch_type": "5v5",
                "hourly_rate": "100000",
            },
        )
        self.assertEqual(no_token_form.status_code, 400)
        self.assertEqual(
            [pitch["id"] for pitch in self.database.get_pitches(db_path=self.db_path)],
            [pitch["id"] for pitch in pitches_before],
        )

        no_token_api = self.client.post(
            "/api/bookings", json={"pitch_id": 1}
        )
        self.assertEqual(no_token_api.status_code, 400)
        self.assertEqual(self.client.get("/logout").status_code, 405)

        csrf_token = self.csrf_headers()["X-CSRFToken"]
        logout = self.client.post(
            "/logout", data={"csrf_token": csrf_token}, follow_redirects=True
        )
        self.assertEqual(logout.status_code, 200)
        self.assertIn(b"login-box", logout.data)

    def test_api_creates_updates_and_queries_booking_with_consistent_money(self):
        pitch_id = self.create_pitch(rate=120000)
        created = self.api_post(
            "/api/bookings",
            json=self.booking_payload(pitch_id, deposit_amount=50000),
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        booking = created.get_json()
        self.assertEqual(
            (booking["total_price"], booking["deposit_amount"], booking["remaining_amount"]),
            (180000, 50000, 130000),
        )
        self.assertEqual(
            [(event["status_from"], event["status_to"]) for event in booking["status_history"]],
            [(None, "pending")],
        )

        updated = self.api_patch(
            f"/api/bookings/{booking['id']}",
            json={"end_time": "11:00", "deposit_amount": 60000, "status": "confirmed"},
        )
        self.assertEqual(updated.status_code, 200, updated.get_json())
        changed = updated.get_json()
        self.assertEqual(
            (changed["total_price"], changed["deposit_amount"], changed["remaining_amount"]),
            (240000, 60000, 180000),
        )
        self.assertEqual(
            [(event["status_from"], event["status_to"]) for event in changed["status_history"]],
            [(None, "pending"), ("pending", "confirmed")],
        )

        queried = self.client.get(
            "/api/bookings",
            query_string={"date": "2026-10-15", "pitch_id": pitch_id, "status": "confirmed"},
        )
        self.assertEqual(queried.status_code, 200)
        self.assertEqual([item["id"] for item in queried.get_json()], [booking["id"]])

        bad_deposit = self.api_patch(
            f"/api/bookings/{booking['id']}", json={"deposit_amount": 250000}
        )
        self.assertEqual(bad_deposit.status_code, 400)
        persisted = self.client.get(f"/api/bookings/{booking['id']}").get_json()
        self.assertEqual(
            persisted["total_price"],
            persisted["deposit_amount"] + persisted["remaining_amount"],
        )

    def test_rejects_overlapping_booking_and_database_trigger_enforces_it(self):
        pitch_id = self.create_pitch()
        created = self.api_post(
            "/api/bookings", json=self.booking_payload(pitch_id)
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        conflict = self.api_post(
            "/api/bookings",
            json=self.booking_payload(
                pitch_id, phone="0901111333", start_time="10:00", end_time="11:00"
            ),
        )
        self.assertEqual(conflict.status_code, 409)
        adjacent = self.api_post(
            "/api/bookings",
            json=self.booking_payload(
                pitch_id, phone="0901111555", start_time="10:30", end_time="11:30"
            ),
        )
        self.assertEqual(adjacent.status_code, 201)
        moved_overlap = self.api_patch(
            f"/api/bookings/{created.get_json()['id']}",
            json={"end_time": "10:45"},
        )
        self.assertEqual(moved_overlap.status_code, 409)

        booking = self.database.get_booking(created.get_json()["id"], self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.database_connection(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO bookings (
                        pitch_id, customer_id, booking_date, start_time, end_time,
                        status, notes, total_price, deposit_amount, remaining_amount
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pitch_id,
                        booking["customer_id"],
                        "2026-10-15",
                        "10:00",
                        "11:00",
                        "pending",
                        "",
                        150000,
                        0,
                        150000,
                    ),
                )

    def test_rejects_invalid_time_and_maintenance_pitch(self):
        active_pitch = self.create_pitch("Sân Hoạt Động")
        closed_pitch = self.create_pitch("Sân Bảo Trì", available=0)
        invalid = self.api_post(
            "/api/bookings",
            json=self.booking_payload(active_pitch, start_time="10:00", end_time="09:00"),
        )
        self.assertEqual(invalid.status_code, 400)
        malformed = self.api_post(
            "/api/bookings",
            json=self.booking_payload(active_pitch, start_time="9:00"),
        )
        self.assertEqual(malformed.status_code, 400)
        maintenance = self.api_post(
            "/api/bookings", json=self.booking_payload(closed_pitch)
        )
        self.assertEqual(maintenance.status_code, 409)

    def test_availability_accounts_for_overlap_cancel_and_exclusion(self):
        pitch_id = self.create_pitch()
        booking = self.api_post(
            "/api/bookings", json=self.booking_payload(pitch_id)
        ).get_json()
        with self.database.database_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE pitches SET hourly_rate = 300000 WHERE id = ?", (pitch_id,)
            )
        query = {
            "date": "2026-10-15",
            "start_time": "09:30",
            "end_time": "10:00",
        }
        unavailable = self.client.get(
            f"/api/pitches/{pitch_id}/availability", query_string=query
        )
        self.assertFalse(unavailable.get_json()["available"])

        query["exclude_booking_id"] = booking["id"]
        self.assertTrue(
            self.client.get(
                f"/api/pitches/{pitch_id}/availability", query_string=query
            ).get_json()["available"]
        )

        cancelled = self.api_post(f"/api/bookings/{booking['id']}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()["status"], "cancelled")
        self.assertEqual(cancelled.get_json()["total_price"], 225000)
        self.assertEqual(cancelled.get_json()["remaining_amount"], 225000)
        history = cancelled.get_json()["status_history"]
        self.assertEqual(
            [(event["status_from"], event["status_to"]) for event in history],
            [(None, "pending"), ("pending", "cancelled")],
        )
        self.api_post(f"/api/bookings/{booking['id']}/cancel")
        self.assertEqual(
            len(self.database.get_booking_status_history(booking["id"], self.db_path)),
            2,
        )
        self.assertTrue(
            self.client.get(
                f"/api/pitches/{pitch_id}/availability", query_string=query
            ).get_json()["available"]
        )

    def test_database_migrates_existing_bookings_and_initial_history(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.executescript(
                """
                CREATE TABLE pitches (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, pitch_type TEXT NOT NULL,
                    hourly_rate REAL NOT NULL, is_available INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE customers (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, email TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE bookings (
                    id INTEGER PRIMARY KEY, pitch_id INTEGER NOT NULL, customer_id INTEGER NOT NULL,
                    booking_date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', notes TEXT, total_price REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO pitches VALUES (1, 'Sân cũ', '5v5', 100000, 1, CURRENT_TIMESTAMP);
                INSERT INTO customers VALUES (1, 'Khách cũ', '0901111444', NULL, CURRENT_TIMESTAMP);
                INSERT INTO bookings
                    (id, pitch_id, customer_id, booking_date, start_time, end_time, status, total_price)
                    VALUES (1, 1, 1, '2026-10-15', '08:00', '09:00', 'confirmed', 100000);
                """
            )
        finally:
            conn.close()

        self.database.init_db(legacy_path)
        migrated = self.database.get_booking(1, legacy_path)
        self.assertEqual(
            (migrated["total_price"], migrated["deposit_amount"], migrated["remaining_amount"]),
            (100000, 0, 100000),
        )
        history = self.database.get_booking_status_history(1, legacy_path)
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["status_from"], history[0]["status_to"]), (None, "confirmed"))

    def test_reports_exclude_cancelled_booking(self):
        pitch_id = self.create_pitch("Sân Báo Cáo", rate=200000)
        self.database.create_booking(
            pitch_id=pitch_id,
            customer_name="Khách A",
            phone="0905555666",
            booking_date="2026-10-16",
            start_time="08:00",
            end_time="10:00",
            notes="Hoàn tất",
            status="completed",
            db_path=self.db_path,
        )
        self.database.create_booking(
            pitch_id=pitch_id,
            customer_name="Khách B",
            phone="0907777888",
            booking_date="2026-10-16",
            start_time="10:00",
            end_time="12:00",
            notes="Đã hủy",
            status="cancelled",
            db_path=self.db_path,
        )
        report = self.database.get_report_data(self.db_path)
        total = sum(float(item["revenue"] or 0) for item in report["by_pitch"])
        self.assertEqual(total, 400000)


if __name__ == "__main__":
    unittest.main()
