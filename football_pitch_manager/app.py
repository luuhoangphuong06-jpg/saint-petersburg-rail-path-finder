from __future__ import annotations

import os
import secrets
from datetime import date
from functools import wraps

import click
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for

from database import (
    BOOKING_STATUSES,
    BookingNotFoundError,
    DB_PATH,
    BookingConflictError,
    PitchNotFoundError,
    PitchUnavailableError,
    add_pitch,
    cancel_booking,
    create_admin_account,
    create_booking,
    create_customer_account,
    get_booking,
    get_booking_status_history,
    get_bookings,
    get_customer_bookings,
    get_customer,
    get_customers,
    get_dashboard_stats,
    get_pitches,
    get_report_data,
    is_pitch_available,
    init_db,
    toggle_pitch_status,
    upsert_customer,
    update_booking,
    update_booking_status,
    verify_admin_login,
    verify_customer_login,
)


STATUS_LABELS = {
    "pending": "Chờ xác nhận",
    "confirmed": "Đã xác nhận",
    "completed": "Hoàn tất",
    "cancelled": "Đã hủy",
}

BOOKING_API_FIELDS = {
    "pitch_id",
    "customer_id",
    "customer_name",
    "phone",
    "email",
    "booking_date",
    "start_time",
    "end_time",
    "status",
    "notes",
    "deposit_amount",
}


def admin_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            flash("Vui lòng đăng nhập để tiếp tục.", "error")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped_view


def customer_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("customer_logged_in") or not session.get("customer_id"):
            flash("Vui lòng đăng nhập khách hàng để tiếp tục.", "error")
            return redirect(url_for("customer_login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped_view


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

    def get_csrf_token() -> str:
        token = session.get("csrf_token")
        if token is None:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    @app.context_processor
    def inject_csrf_token() -> dict[str, object]:
        return {"csrf_token": get_csrf_token}

    @app.context_processor
    def inject_customer_session() -> dict[str, object]:
        customer = None
        customer_id = session.get("customer_id")
        if customer_id:
            customer = get_customer(customer_id, app.config["DATABASE"])
        return {"current_customer": customer}

    @app.before_request
    def protect_cookie_authenticated_requests():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        expected_token = session.get("csrf_token")
        submitted_token = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
        if (
            not isinstance(expected_token, str)
            or not isinstance(submitted_token, str)
            or not secrets.compare_digest(expected_token, submitted_token)
        ):
            if request.path.startswith("/api/") or request.is_json:
                return jsonify(error="CSRF token không hợp lệ hoặc bị thiếu."), 400
            abort(400, description="CSRF token không hợp lệ hoặc bị thiếu.")
        return None

    @app.cli.command("create-admin")
    def create_admin_command() -> None:
        """Create the first administrator account interactively."""
        username = click.prompt("Tên đăng nhập quản trị")
        password = click.prompt(
            "Mật khẩu quản trị (ít nhất 12 ký tự)",
            hide_input=True,
            confirmation_prompt=True,
        )
        try:
            create_admin_account(username, password, app.config["DATABASE"])
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Đã tạo tài khoản quản trị '{username}'.")

    def booking_api_record(booking_id: int):
        booking = get_booking(booking_id, app.config["DATABASE"])
        if booking is None:
            return None
        result = dict(booking)
        result["status_history"] = [
            dict(row)
            for row in get_booking_status_history(booking_id, app.config["DATABASE"])
        ]
        return result

    def parse_optional_int(payload: dict, key: str) -> int | None:
        value = payload.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"{key} phải là số nguyên hợp lệ.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} phải là số nguyên hợp lệ.") from exc
        if isinstance(value, str) and str(parsed) != value.strip():
            raise ValueError(f"{key} phải là số nguyên hợp lệ.")
        if parsed <= 0:
            raise ValueError(f"{key} phải là số nguyên dương.")
        return parsed

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("admin_logged_in"):
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if verify_admin_login(username, password, app.config["DATABASE"]):
                session.pop("csrf_token", None)
                session["admin_logged_in"] = True
                session["admin_username"] = username
                flash("Đăng nhập thành công.", "success")
                return redirect(url_for("dashboard"))
            flash("Tên đăng nhập hoặc mật khẩu không đúng.", "error")

        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    @admin_required
    def logout():
        session.clear()
        flash("Bạn đã đăng xuất.", "success")
        return redirect(url_for("login"))

    @app.route("/customer/register", methods=["GET", "POST"])
    def customer_register():
        if session.get("customer_logged_in"):
            return redirect(url_for("customer_home"))
        if request.method == "POST":
            try:
                customer_id = create_customer_account(
                    request.form.get("name", ""),
                    request.form.get("phone", ""),
                    request.form.get("password", ""),
                    request.form.get("email") or None,
                    app.config["DATABASE"],
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template("customer_register.html"), 400
            session["customer_logged_in"] = True
            session["customer_id"] = customer_id
            session.pop("csrf_token", None)
            flash("Đăng ký thành công.", "success")
            return redirect(url_for("customer_home"))
        return render_template("customer_register.html")

    @app.route("/customer/login", methods=["GET", "POST"])
    def customer_login():
        if session.get("customer_logged_in"):
            return redirect(url_for("customer_home"))
        next_url = request.args.get("next") or request.form.get("next")
        if request.method == "POST":
            customer = verify_customer_login(
                request.form.get("phone", ""),
                request.form.get("password", ""),
                app.config["DATABASE"],
            )
            if customer:
                session.pop("csrf_token", None)
                session["customer_logged_in"] = True
                session["customer_id"] = int(customer["id"])
                flash("Đăng nhập thành công.", "success")
                if next_url and next_url.startswith("/"):
                    return redirect(next_url)
                return redirect(url_for("customer_home"))
            flash("Số điện thoại hoặc mật khẩu không đúng.", "error")
        return render_template("customer_login.html", next_url=next_url or "")

    @app.route("/customer/logout", methods=["POST"])
    @customer_required
    def customer_logout():
        session.pop("customer_logged_in", None)
        session.pop("customer_id", None)
        session.pop("csrf_token", None)
        flash("Bạn đã đăng xuất.", "success")
        return redirect(url_for("customer_home"))

    def customer_pitch_options(
        booking_date: str | None,
        start_time: str | None,
        end_time: str | None,
    ) -> tuple[list[dict], str | None]:
        pitches = get_pitches(db_path=app.config["DATABASE"])
        has_time_filter = all([booking_date, start_time, end_time])
        options = []
        availability_error = None
        for pitch in pitches:
            available = None
            if has_time_filter:
                try:
                    available = is_pitch_available(
                        pitch["id"],
                        booking_date,
                        start_time,
                        end_time,
                        db_path=app.config["DATABASE"],
                    )
                except (PitchNotFoundError, PitchUnavailableError):
                    available = False
                except ValueError as exc:
                    availability_error = str(exc)
            options.append({"pitch": pitch, "available": available})
        return options, availability_error

    @app.route("/customer", methods=["GET"])
    def customer_home():
        booking_date = request.args.get("date") or date.today().isoformat()
        start_time = request.args.get("start_time", "")
        end_time = request.args.get("end_time", "")
        availability_error = None
        if any([start_time, end_time]) and not all([start_time, end_time]):
            availability_error = "Vui lòng chọn cả giờ bắt đầu và giờ kết thúc."
        pitch_options, filter_error = customer_pitch_options(
            booking_date, start_time, end_time
        )
        availability_error = availability_error or filter_error
        return render_template(
            "customer_home.html",
            pitch_options=pitch_options,
            booking_date=booking_date,
            start_time=start_time,
            end_time=end_time,
            availability_error=availability_error,
        )

    @app.route("/customer/api/pitches")
    def customer_pitches_api():
        booking_date = request.args.get("date")
        start_time = request.args.get("start_time")
        end_time = request.args.get("end_time")
        if any([booking_date, start_time, end_time]) and not all(
            [booking_date, start_time, end_time]
        ):
            return jsonify(error="Cần truyền đủ date, start_time và end_time."), 400
        pitch_options, availability_error = customer_pitch_options(
            booking_date, start_time, end_time
        )
        if availability_error:
            return jsonify(error=availability_error), 400
        return jsonify(
            [
                {
                    "id": option["pitch"]["id"],
                    "name": option["pitch"]["name"],
                    "pitch_type": option["pitch"]["pitch_type"],
                    "hourly_rate": option["pitch"]["hourly_rate"],
                    "is_available": bool(option["pitch"]["is_available"]),
                    "available": option["available"],
                }
                for option in pitch_options
            ]
        )

    @app.route("/customer/bookings", methods=["POST"])
    @customer_required
    def customer_create_booking():
        form = request.form
        try:
            pitch_id = int(form.get("pitch_id", ""))
            booking_id = create_booking(
                pitch_id=pitch_id,
                customer_id=int(session["customer_id"]),
                booking_date=form.get("booking_date", ""),
                start_time=form.get("start_time", ""),
                end_time=form.get("end_time", ""),
                notes=form.get("notes", ""),
                status="pending",
                db_path=app.config["DATABASE"],
            )
        except (BookingConflictError, PitchUnavailableError) as exc:
            flash(str(exc), "error")
            return redirect(
                url_for(
                    "customer_home",
                    date=form.get("booking_date"),
                    start_time=form.get("start_time"),
                    end_time=form.get("end_time"),
                )
            )
        except (TypeError, ValueError) as exc:
            flash(str(exc), "error")
            return redirect(
                url_for(
                    "customer_home",
                    date=form.get("booking_date"),
                    start_time=form.get("start_time"),
                    end_time=form.get("end_time"),
                )
            )
        return redirect(
            url_for(
                "customer_confirmation",
                booking_id=booking_id,
            )
        )

    @app.route("/customer/confirmation/<int:booking_id>")
    @customer_required
    def customer_confirmation(booking_id: int):
        matches = get_customer_bookings(
            booking_id=booking_id,
            customer_id=int(session["customer_id"]),
            db_path=app.config["DATABASE"],
        )
        if not matches:
            return render_template(
                "customer_lookup.html",
                bookings=[],
                lookup_error="Không tìm thấy booking thuộc tài khoản của bạn.",
                phone="",
                booking_id=booking_id,
            ), 404
        booking = matches[0]
        return render_template(
            "customer_confirmation.html",
            booking=booking,
            history=[
                dict(row)
                for row in get_booking_status_history(booking_id, app.config["DATABASE"])
            ],
        )

    @app.route("/customer/lookup", methods=["GET"])
    @customer_required
    def customer_lookup():
        booking_id_raw = request.args.get("booking_id", "").strip()
        booking_id = None
        lookup_error = None
        bookings = []
        if request.args or not bookings:
            try:
                if booking_id_raw:
                    booking_id = int(booking_id_raw)
                bookings = get_customer_bookings(
                    booking_id=booking_id,
                    customer_id=int(session["customer_id"]),
                    db_path=app.config["DATABASE"],
                )
                if not bookings:
                    lookup_error = "Không tìm thấy lịch đặt phù hợp."
            except ValueError as exc:
                lookup_error = str(exc)
        return render_template(
            "customer_lookup.html",
            bookings=bookings,
            lookup_error=lookup_error,
            phone="",
            booking_id=booking_id_raw,
        )

    @app.route("/")
    @admin_required
    def dashboard():
        db_path = app.config["DATABASE"]
        stats = get_dashboard_stats(db_path)
        recent_bookings = get_bookings(limit=5, db_path=db_path)
        return render_template(
            "dashboard.html", stats=stats, recent_bookings=recent_bookings
        )

    @app.route("/pitches", methods=["GET", "POST"])
    @admin_required
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
    @admin_required
    def toggle_pitch(pitch_id: int):
        if not toggle_pitch_status(pitch_id, app.config["DATABASE"]):
            flash("Không tìm thấy sân cần cập nhật.", "error")
        else:
            flash("Cập nhật trạng thái sân thành công.", "success")
        return redirect(url_for("pitches"))

    @app.route("/bookings", methods=["GET", "POST"])
    @admin_required
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
    @admin_required
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

    @app.route("/api/bookings", methods=["GET", "POST"])
    @admin_required
    def bookings_api():
        db_path = app.config["DATABASE"]
        if request.method == "GET":
            try:
                pitch_id_raw = request.args.get("pitch_id")
                pitch_id = (
                    parse_optional_int({"pitch_id": pitch_id_raw}, "pitch_id")
                    if pitch_id_raw is not None
                    else None
                )
                bookings = get_bookings(
                    status=request.args.get("status") or None,
                    db_path=db_path,
                    booking_date=request.args.get("date") or None,
                    pitch_id=pitch_id,
                )
                return jsonify([dict(booking) for booking in bookings])
            except ValueError as exc:
                return jsonify(error=str(exc)), 400

        if not request.is_json:
            return jsonify(error="Yêu cầu nội dung JSON."), 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Dữ liệu đặt sân JSON không hợp lệ."), 400
        unknown_fields = set(payload) - BOOKING_API_FIELDS
        if unknown_fields:
            return jsonify(error="Trường không hỗ trợ: " + ", ".join(sorted(unknown_fields))), 400
        try:
            pitch_id = parse_optional_int(payload, "pitch_id")
            if pitch_id is None:
                raise ValueError("pitch_id là trường bắt buộc.")
            customer_id = parse_optional_int(payload, "customer_id")
            status = payload.get("status", "pending")
            if not isinstance(status, str) or status not in BOOKING_STATUSES:
                raise ValueError("Trạng thái đặt sân không hợp lệ.")
            booking_id = create_booking(
                pitch_id=pitch_id,
                customer_id=customer_id,
                customer_name=payload.get("customer_name"),
                phone=payload.get("phone"),
                email=payload.get("email"),
                booking_date=payload.get("booking_date"),
                start_time=payload.get("start_time"),
                end_time=payload.get("end_time"),
                status=status,
                notes=payload.get("notes", ""),
                deposit_amount=payload.get("deposit_amount", 0),
                db_path=db_path,
            )
        except (BookingConflictError, PitchUnavailableError) as exc:
            return jsonify(error=str(exc)), 409
        except PitchNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(booking_api_record(booking_id)), 201

    @app.route("/api/bookings/<int:booking_id>", methods=["GET", "PUT", "PATCH"])
    @admin_required
    def booking_api_detail(booking_id: int):
        db_path = app.config["DATABASE"]
        if request.method == "GET":
            booking = booking_api_record(booking_id)
            if booking is None:
                return jsonify(error="Không tìm thấy lịch đặt sân."), 404
            return jsonify(booking)

        if not request.is_json:
            return jsonify(error="Yêu cầu nội dung JSON."), 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Dữ liệu cập nhật JSON không hợp lệ."), 400
        null_fields = sorted(key for key, value in payload.items() if value is None)
        if null_fields:
            return jsonify(error="Trường không được để null: " + ", ".join(null_fields)), 400
        unknown_fields = set(payload) - BOOKING_API_FIELDS
        if unknown_fields:
            return jsonify(error="Trường không hỗ trợ: " + ", ".join(sorted(unknown_fields))), 400
        try:
            if not payload:
                raise ValueError("Cần cung cấp ít nhất một trường để cập nhật.")
            for key in ("pitch_id", "customer_id"):
                if key in payload:
                    payload[key] = parse_optional_int(payload, key)
            update_booking(booking_id, db_path=db_path, **payload)
        except BookingNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        except (BookingConflictError, PitchUnavailableError, PitchNotFoundError) as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(booking_api_record(booking_id))

    @app.route("/api/bookings/<int:booking_id>/cancel", methods=["POST"])
    @admin_required
    def cancel_booking_api(booking_id: int):
        try:
            cancel_booking(booking_id, app.config["DATABASE"])
        except BookingNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        return jsonify(booking_api_record(booking_id))

    @app.route("/api/pitches/<int:pitch_id>/availability")
    @admin_required
    def pitch_availability_api(pitch_id: int):
        booking_date = request.args.get("date")
        start_time = request.args.get("start_time")
        end_time = request.args.get("end_time")
        if not all([booking_date, start_time, end_time]):
            return jsonify(error="Cần truyền date, start_time và end_time."), 400
        try:
            exclude_booking_id = request.args.get("exclude_booking_id")
            exclude_id = int(exclude_booking_id) if exclude_booking_id else None
            if exclude_id is not None and exclude_id <= 0:
                raise ValueError("exclude_booking_id phải là số nguyên dương.")
            available = is_pitch_available(
                pitch_id,
                booking_date,
                start_time,
                end_time,
                exclude_booking_id=exclude_id,
                db_path=app.config["DATABASE"],
            )
        except PitchNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        except PitchUnavailableError as exc:
            return jsonify(error=str(exc), available=False), 409
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(
            {
                "pitch_id": pitch_id,
                "date": booking_date,
                "start_time": start_time,
                "end_time": end_time,
                "available": available,
            }
        )

    @app.route("/customers", methods=["GET", "POST"])
    @admin_required
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
    @admin_required
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
