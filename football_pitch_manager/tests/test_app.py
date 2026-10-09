from __future__ import annotations

import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

_MODULE_DATABASE_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault(
    "PITCH_DB_PATH", str(Path(_MODULE_DATABASE_DIR.name) / "module-import.sqlite3")
)

from app import create_app
from database import (
    BookingConflictError,
    add_pitch,
    create_admin_account,
    create_booking as save_booking,
    database_connection,
    get_bookings,
    get_pitches,
)


class PitchManagerAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_pitch_manager.db"
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": str(self.db_path),
            }
        )
        create_admin_account("test-admin", "test-admin-password-2026", self.db_path)
        self.client = self.app.test_client()
        login_page = self.client.get("/login")
        token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            login_page.get_data(as_text=True),
        ).group(1)
        login = self.client.post(
            "/login",
            data={
                "username": "test-admin",
                "password": "test-admin-password-2026",
                "csrf_token": token,
            },
            follow_redirects=True,
        )
        self.assertEqual(login.status_code, 200)

        with database_connection(self.db_path) as conn:
            conn.execute("DELETE FROM bookings")
            conn.execute("DELETE FROM customers")
            conn.execute("DELETE FROM pitches")
        self.pitch_id = add_pitch("Sân thử", "5v5", 180000, 1, self.db_path)
        self.closed_pitch_id = add_pitch("Sân bảo trì", "7v7", 220000, 0, self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def booking_form(self, **overrides: str) -> dict[str, str]:
        form = {
            "customer_name": "Nguyễn Văn Test",
            "phone": "0900000000",
            "pitch_id": str(self.pitch_id),
            "booking_date": date.today().isoformat(),
            "start_time": "09:00",
            "end_time": "10:30",
            "status": "confirmed",
        }
        form.update(overrides)
        return form

    def create_booking(self, **overrides: str):
        return self.client.post(
            "/bookings",
            data={**self.booking_form(**overrides), "csrf_token": self.get_csrf_token()},
            follow_redirects=True,
        )

    def get_csrf_token(self) -> str:
        response = self.client.get("/bookings")
        token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            response.get_data(as_text=True),
        )
        self.assertIsNotNone(token)
        return token.group(1)

    def test_csrf_rejects_mutation_without_token_and_logout_is_post_only(self) -> None:
        pitches_before = get_pitches(db_path=self.db_path)
        response = self.client.post(
            "/customers",
            data={"name": "CSRF blocked", "phone": "0900000001"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            [pitch["id"] for pitch in get_pitches(db_path=self.db_path)],
            [pitch["id"] for pitch in pitches_before],
        )
        self.assertEqual(self.client.get("/logout").status_code, 405)

        token = self.get_csrf_token()
        response = self.client.post("/logout", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

    def test_template_escapes_customer_supplied_html(self) -> None:
        response = self.client.post(
            "/bookings",
            data={
                "customer_name": "<script>alert(1)</script>",
                "phone": "0909999999",
                "pitch_id": str(self.pitch_id),
                "booking_date": "2026-10-24",
                "start_time": "14:00",
                "end_time": "15:00",
                "csrf_token": self.get_csrf_token(),
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", response.data)
        self.assertNotIn(b"<script>alert(1)</script>", response.data)

    def test_main_pages_render(self) -> None:
        for path in ("/", "/pitches", "/bookings", "/customers", "/reports"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_customer_page_adds_and_updates_customer_by_phone(self) -> None:
        first = self.client.post(
            "/customers",
            data={
                "name": "Khách Mới",
                "phone": "0901111111",
                "email": "new@example.com",
                "csrf_token": self.get_csrf_token(),
            },
            follow_redirects=True,
        )
        self.assertIn("Lưu khách hàng thành công.".encode(), first.data)

        updated = self.client.post(
            "/customers",
            data={
                "name": "Khách Cập Nhật",
                "phone": "0901111111",
                "csrf_token": self.get_csrf_token(),
            },
            follow_redirects=True,
        )
        self.assertIn("Khách Cập Nhật".encode(), updated.data)
        self.assertEqual(updated.data.count(b"0901111111"), 1)

    def test_booking_calculates_partial_hour_and_records_customer(self) -> None:
        response = self.create_booking()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Đặt sân thành công.".encode(), response.data)

        bookings = get_bookings(db_path=self.db_path)
        self.assertEqual(len(bookings), 1)
        self.assertEqual(bookings[0]["total_price"], 270000)
        self.assertEqual(bookings[0]["customer_name"], "Nguyễn Văn Test")

    def test_rejects_overlapping_booking_and_allows_adjacent_slot(self) -> None:
        self.create_booking(start_time="09:00", end_time="10:00")

        conflict = self.create_booking(start_time="09:30", end_time="10:30")
        self.assertIn("Sân đã có lịch đặt trùng".encode(), conflict.data)
        self.assertEqual(len(get_bookings(db_path=self.db_path)), 1)

        adjacent = self.create_booking(start_time="10:00", end_time="11:00")
        self.assertIn("Đặt sân thành công.".encode(), adjacent.data)
        self.assertEqual(len(get_bookings(db_path=self.db_path)), 2)

    def test_concurrent_requests_cannot_book_the_same_slot(self) -> None:
        def reserve(phone: str):
            try:
                save_booking(
                    pitch_id=self.pitch_id,
                    customer_name=f"Khách {phone}",
                    phone=phone,
                    booking_date=date.today().isoformat(),
                    start_time="12:00",
                    end_time="13:00",
                    notes="",
                    db_path=self.db_path,
                )
                return "created"
            except BookingConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, ("0900000001", "0900000002")))

        self.assertCountEqual(results, ["created", "conflict"])
        self.assertEqual(len(get_bookings(db_path=self.db_path)), 1)

    def test_cancelled_booking_does_not_block_slot(self) -> None:
        self.create_booking(status="cancelled")
        response = self.create_booking(status="confirmed")

        self.assertIn("Đặt sân thành công.".encode(), response.data)
        self.assertEqual(len(get_bookings(db_path=self.db_path)), 2)

    def test_rejects_invalid_time_and_unavailable_pitch(self) -> None:
        invalid_time = self.create_booking(start_time="11:00", end_time="10:00")
        self.assertIn("Thời gian kết thúc phải lớn hơn".encode(), invalid_time.data)

        unavailable = self.create_booking(pitch_id=str(self.closed_pitch_id))
        self.assertIn("đang tạm ngưng".encode(), unavailable.data)
        self.assertEqual(get_bookings(db_path=self.db_path), [])

    def test_status_change_rechecks_slot_conflicts(self) -> None:
        self.create_booking(start_time="09:00", end_time="10:00", status="cancelled")
        self.create_booking(start_time="09:30", end_time="10:30", status="confirmed")

        bookings = get_bookings(db_path=self.db_path)
        cancelled_booking = next(row for row in bookings if row["status"] == "cancelled")
        response = self.client.post(
            f"/bookings/{cancelled_booking['id']}/status",
            data={"status": "pending", "csrf_token": self.get_csrf_token()},
            follow_redirects=True,
        )
        self.assertIn("Sân đã có lịch đặt trùng".encode(), response.data)

        updated = get_bookings(status="cancelled", db_path=self.db_path)
        self.assertEqual(len(updated), 1)

    def test_rejects_unknown_status(self) -> None:
        response = self.create_booking(status="invalid")
        self.assertIn("Trạng thái đặt sân không hợp lệ.".encode(), response.data)
        self.assertEqual(get_bookings(db_path=self.db_path), [])

    def test_booking_query_rejects_sql_injection_like_status(self) -> None:
        response = self.client.get(
            "/api/bookings", query_string={"status": "' OR 1=1 --"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Trạng thái đặt sân không hợp lệ.")

    def test_reports_exclude_cancelled_bookings(self) -> None:
        self.create_booking(start_time="08:00", end_time="09:00", status="confirmed")
        self.create_booking(start_time="09:00", end_time="10:00", status="completed")
        self.create_booking(start_time="10:00", end_time="11:00", status="cancelled")

        response = self.client.get("/reports")
        self.assertEqual(response.status_code, 200)
        self.assertIn("360,000đ".encode(), response.data)
        self.assertNotIn("540,000đ".encode(), response.data)


if __name__ == "__main__":
    unittest.main()
