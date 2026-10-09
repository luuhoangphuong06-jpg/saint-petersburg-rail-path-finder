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
python app.py
```

Mở http://127.0.0.1:5000 sau khi server khởi động. Dừng server bằng `Ctrl+C`.

Có thể cấu hình khóa phiên, cổng và đường dẫn SQLite bằng các biến môi trường `FLASK_SECRET_KEY`, `PORT` và `PITCH_DB_PATH`. Mặc định ứng dụng chỉ lắng nghe trên máy cục bộ; chỉ bật debug trong môi trường phát triển bằng `FLASK_DEBUG=1`.

## Kiểm thử

Từ thư mục `football_pitch_manager/`, chạy:

```powershell
python -m unittest discover -s tests -v
```

Các test dùng SQLite tạm, không sửa database của ứng dụng. Có thể chạy thêm bộ smoke test tương thích ở `python test_app.py`.

## Cấu trúc

- `app.py`: ứng dụng Flask, routes và factory `create_app`.
- `database.py`: schema, dữ liệu mẫu và nghiệp vụ SQLite.
- `templates/`: các trang dashboard, sân, lịch đặt, khách hàng và báo cáo.
- `static/style.css`: giao diện.
- `tests/test_app.py`: test routes và quy tắc đặt sân.
- `pitch_manager.db`: database cục bộ được tạo tự động khi chạy ứng dụng.
