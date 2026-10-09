import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class FootballPitchManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["PITCH_DB_PATH"] = str(Path(self.temp_dir) / "pitch_manager.db")

        import app as app_module
        import database as database_module

        self.database = importlib.reload(database_module)
        self.app_module = importlib.reload(app_module)
        with self.database.get_connection() as conn:
            conn.execute("DELETE FROM bookings")
            conn.execute("DELETE FROM pitches")
            conn.execute("DELETE FROM customers")
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        os.environ.pop("PITCH_DB_PATH", None)

    def test_routes_return_ok(self):
        for path in ["/", "/pitches", "/bookings", "/customers", "/reports"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)

    def test_overlap_booking_is_rejected(self):
        pitch_id = self.database.add_pitch("Sân Test", "5v5", 150000, 1)
        self.database.create_booking(
            pitch_id=pitch_id,
            customer_name="Khách 1",
            phone="0901111222",
            booking_date="2026-10-15",
            start_time="09:00",
            end_time="10:30",
            notes="Lần đầu",
            status="confirmed",
        )

        with self.assertRaises((self.database.BookingConflictError, ValueError)):
            self.database.create_booking(
                pitch_id=pitch_id,
                customer_name="Khách 2",
                phone="0903333444",
                booking_date="2026-10-15",
                start_time="09:45",
                end_time="11:00",
                notes="Lần hai",
                status="pending",
            )

    def test_reports_exclude_cancelled_booking(self):
        pitch_id = self.database.add_pitch("Sân Báo Cáo", "7v7", 200000, 1)
        self.database.create_booking(
            pitch_id=pitch_id,
            customer_name="Khách A",
            phone="0905555666",
            booking_date="2026-10-16",
            start_time="08:00",
            end_time="10:00",
            notes="Hoàn tất",
            status="completed",
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
        )

        report = self.database.get_report_data()
        total = sum(float(item["revenue"] or 0) for item in report["by_pitch"])
        self.assertEqual(total, 400000.0)


if __name__ == "__main__":
    unittest.main()
