from __future__ import annotations

import os
import secrets
from datetime import date

from flask import Flask, flash, redirect, render_template, request, url_for

from database import (
    BOOKING_STATUSES,
    DB_PATH,
    BookingConflictError,
    PitchUnavailableError,
    add_pitch,
    create_booking,
    get_bookings,
    get_customers,
    get_dashboard_stats,
    get_pitches,
    get_report_data,
    init_db,
    toggle_pitch_status,
    upsert_customer,
    update_booking_status,
)


STATUS_LABELS = {
    "pending": "Chờ xác nhận",
    "confirmed": "Đã xác nhận",
    "completed": "Hoàn tất",
    "cancelled": "Đã hủy",
}


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32),
        DATABASE=str(DB_PATH),
    )
    if test_config:
        app.config.update(test_config)
    init_db(app.config["DATABASE"])

    @app.context_processor
    def inject_status_labels() -> dict[str, dict[str, str]]:
        return {"status_labels": STATUS_LABELS}

    @app.route("/")
    def dashboard():
        db_path = app.config["DATABASE"]
        stats = get_dashboard_stats(db_path)
        recent_bookings = get_bookings(limit=5, db_path=db_path)
        return render_template(
            "dashboard.html", stats=stats, recent_bookings=recent_bookings
        )

    @app.route("/pitches", methods=["GET", "POST"])
    def pitches():
        db_path = app.config["DATABASE"]
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            pitch_type = request.form.get("pitch_type", "").strip()
            hourly_rate = request.form.get("hourly_rate", "").strip()
            is_available = 1 if request.form.get("is_available") == "on" else 0

            try:
                add_pitch(name, pitch_type, hourly_rate, is_available, db_path)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("pitches"))

            flash("Thêm sân mới thành công.", "success")
            return redirect(url_for("pitches"))

        return render_template("pitches.html", pitches=get_pitches(db_path=db_path))

    @app.route("/pitches/<int:pitch_id>/toggle", methods=["POST"])
    def toggle_pitch(pitch_id: int):
        if not toggle_pitch_status(pitch_id, app.config["DATABASE"]):
            flash("Không tìm thấy sân cần cập nhật.", "error")
        else:
            flash("Cập nhật trạng thái sân thành công.", "success")
        return redirect(url_for("pitches"))

    @app.route("/bookings", methods=["GET", "POST"])
    def bookings():
        db_path = app.config["DATABASE"]
        if request.method == "POST":
            customer_name = request.form.get("customer_name", "").strip()
            phone = request.form.get("phone", "").strip()
            email = request.form.get("email", "").strip() or None
            pitch_id_raw = request.form.get("pitch_id", "")
            booking_date = request.form.get("booking_date", "").strip()
            start_time = request.form.get("start_time", "").strip()
            end_time = request.form.get("end_time", "").strip()
            notes = request.form.get("notes", "").strip()
            status = request.form.get("status", "pending")

            if not all(
                [customer_name, phone, pitch_id_raw, booking_date, start_time, end_time]
            ):
                flash("Vui lòng điền đầy đủ thông tin khách hàng, sân và thời gian.", "error")
                return redirect(url_for("bookings"))
            try:
                pitch_id = int(pitch_id_raw)
                if status not in BOOKING_STATUSES:
                    raise ValueError("Trạng thái đặt sân không hợp lệ.")
                create_booking(
                    pitch_id=pitch_id,
                    customer_name=customer_name,
                    phone=phone,
                    booking_date=booking_date,
                    start_time=start_time,
                    end_time=end_time,
                    notes=notes,
                    status=status,
                    email=email,
                    db_path=db_path,
                )
            except BookingConflictError as exc:
                flash(str(exc), "error")
                return redirect(url_for("bookings"))
            except PitchUnavailableError as exc:
                flash(str(exc), "error")
                return redirect(url_for("bookings"))
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("bookings"))

            flash("Đặt sân thành công.", "success")
            return redirect(url_for("bookings"))

        return render_template(
            "bookings.html",
            pitches=get_pitches(available_only=True, db_path=db_path),
            bookings=get_bookings(db_path=db_path),
            today=date.today().isoformat(),
        )

    @app.route("/bookings/<int:booking_id>/status", methods=["POST"])
    def booking_status(booking_id: int):
        status = request.form.get("status", "")
        try:
            updated = update_booking_status(booking_id, status, app.config["DATABASE"])
        except BookingConflictError as exc:
            flash(str(exc), "error")
            return redirect(url_for("bookings"))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("bookings"))

        if not updated:
            flash("Không tìm thấy lịch đặt cần cập nhật.", "error")
        else:
            flash("Cập nhật trạng thái đặt sân thành công.", "success")
        return redirect(url_for("bookings"))

    @app.route("/customers", methods=["GET", "POST"])
    def customers():
        db_path = app.config["DATABASE"]
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            phone = request.form.get("phone", "").strip()
            email = request.form.get("email", "").strip() or None
            try:
                upsert_customer(name, phone, email, db_path)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("customers"))
            flash("Lưu khách hàng thành công.", "success")
            return redirect(url_for("customers"))

        return render_template(
            "customers.html", customers=get_customers(db_path)
        )

    @app.route("/reports")
    def reports():
        report_data = get_report_data(app.config["DATABASE"])
        total_revenue = sum(float(item["revenue"] or 0) for item in report_data["by_pitch"])
        return render_template(
            "reports.html", reports=report_data, total_revenue=total_revenue
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(
        debug=os.environ.get("FLASK_DEBUG") == "1",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
    )
