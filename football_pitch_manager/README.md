# Hệ thống quản lý sân bóng đá

Ứng dụng web quản lý sân bóng đá bằng Flask + SQLite. Ứng dụng chạy độc lập trong thư mục `football_pitch_manager/` và không thay đổi ứng dụng tìm đường Tkinter ở repo gốc.

## Tính năng

- Dashboard: số sân, số lịch hôm nay, doanh thu hôm nay và lịch đặt gần đây.
- Quản lý sân: thêm sân, giá theo giờ và bật/tắt trạng thái hoạt động.
- Đặt sân: lưu thông tin khách hàng, tính tiền theo thời lượng đến phút và cập nhật trạng thái.
- Chặn lịch giao nhau trên cùng sân; lịch đã hủy không giữ khung giờ. Sân tạm ngưng không nhận lịch mới.
- Quản lý khách hàng: xem danh sách và thêm/cập nhật theo số điện thoại.
- Lịch sử đặt sân: xem toàn bộ lịch, giờ, giá và trạng thái.
- Báo cáo doanh thu theo sân và ngày. Doanh thu chỉ tính lịch đã xác nhận hoặc hoàn tất, loại trừ lịch đã hủy.
- Tạo SQLite database và dữ liệu mẫu tự động khi chạy lần đầu.

## Cài đặt và chạy

Từ thư mục gốc repo, chạy:

```powershell
Set-Location football_pitch_manager
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
flask --app app create-admin
python app.py
```

Mở http://127.0.0.1:5000 sau khi server khởi động. Dừng server bằng `Ctrl+C`.

Lệnh `create-admin` yêu cầu nhập tên đăng nhập và mật khẩu mới (tối thiểu 12 ký tự) trực tiếp trong terminal. Không có tài khoản hay mật khẩu mặc định; chỉ tạo tài khoản đầu tiên khi lệnh được chạy. Khi nâng cấp database cũ, migration một lần sẽ xóa tài khoản `admin` được phiên bản trước tự tạo để không giữ lại credential cũ.

Có thể cấu hình khóa phiên, cổng và đường dẫn SQLite bằng các biến môi trường `FLASK_SECRET_KEY`, `PORT` và `PITCH_DB_PATH`. Mặc định ứng dụng chỉ lắng nghe trên máy cục bộ; chỉ bật debug trong môi trường phát triển bằng `FLASK_DEBUG=1`.

## Kiểm thử

Từ thư mục `football_pitch_manager/`, chạy:

```powershell
python -m unittest -v test_app.py
python -m unittest discover -s tests -v
```

Các test dùng SQLite tạm, không sửa database của ứng dụng.

## Booking API

Các API dùng JSON cho thao tác ghi và giữ cơ chế truy cập hiện có của ứng dụng. Các yêu cầu ghi từ phiên cookie phải gửi token trong header `X-CSRFToken`; giao diện cung cấp token tại thẻ `<meta name="csrf-token">`. Các trường thời gian dùng `YYYY-MM-DD` và `HH:MM`; các khoản tiền là số nguyên đồng. Tổng tiền được tính theo giá sân và số phút đặt, làm tròn đến đồng gần nhất.

| Method | Endpoint | Mục đích |
|---|---|---|
| `GET` | `/api/bookings?date=2026-10-15&pitch_id=1&status=confirmed` | Tra cứu lịch đặt; bộ lọc là tùy chọn |
| `POST` | `/api/bookings` | Tạo lịch đặt; truyền `pitch_id`, ngày/giờ và `customer_id` hoặc `customer_name` + `phone`; `deposit_amount` tùy chọn |
| `GET` | `/api/bookings/<id>` | Chi tiết lịch và lịch sử trạng thái |
| `PUT`, `PATCH` | `/api/bookings/<id>` | Cập nhật trường lịch đặt; đổi sân hoặc giờ sẽ tính lại tiền |
| `POST` | `/api/bookings/<id>/cancel` | Hủy lịch; ghi trạng thái vào lịch sử trong cùng transaction |
| `GET` | `/api/pitches/<id>/availability?date=2026-10-15&start_time=09:00&end_time=10:00` | Kiểm tra sân còn trống trong khoảng giờ |

SQLite tự bổ sung các cột đặt cọc/còn phải thu và lịch sử trạng thái cho database cũ. Trigger trong database bảo vệ khoảng giờ, sân đang hoạt động, trạng thái và cân đối `total_price = deposit_amount + remaining_amount`; transaction khóa ghi ngăn hai yêu cầu đồng thời đặt cùng khung giờ.

## Giao diện khách hàng

Khách hàng đăng ký tại `/customer/register`, đăng nhập bằng số điện thoại và mật khẩu tại `/customer/login`, sau đó sử dụng `/customer`. Schema customer lưu password hash, trạng thái active/inactive, thời điểm tạo và lần đăng nhập gần nhất; không lưu password plaintext. Trang khách đọc danh sách sân thật từ SQLite, lọc availability theo ngày/giờ, gửi yêu cầu đặt sân qua `POST /customer/bookings`, tự lấy danh tính từ customer session và chỉ cho xem booking của chính tài khoản. Tra cứu tại `/customer/lookup` có thể lọc thêm mã booking. API đọc availability công khai là `GET /customer/api/pitches`; các thao tác ghi vẫn được bảo vệ bằng CSRF token của session.

## Cấu trúc

- `app.py`: ứng dụng Flask, routes và factory `create_app`.
- `database.py`: schema, dữ liệu mẫu và nghiệp vụ SQLite.
- `templates/`: các trang dashboard, sân, lịch đặt, khách hàng và báo cáo.
- `templates/customer_*.html`: landing, xác nhận và tra cứu dành cho khách hàng.
- `static/style.css`: giao diện.
- `tests/test_app.py`: test routes và quy tắc đặt sân.
- `pitch_manager.db`: database cục bộ được tạo tự động khi chạy ứng dụng.
