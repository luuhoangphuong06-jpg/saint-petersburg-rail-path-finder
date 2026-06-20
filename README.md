# Saint Petersburg Rail Path Finder

Ứng dụng desktop Python mô phỏng tìm đường đa phương thức tại Saint Petersburg bằng dữ liệu OpenStreetMap. App kết hợp mạng đường sắt và mạng đi bộ, hỗ trợ nhiều thuật toán tìm kiếm AI và lưu lịch sử chạy vào SQLite.

## Tính năng chính

- Tìm đường kết hợp đi bộ và tàu/tram/rail.
- Hỗ trợ A*, Dijkstra, UCS, Greedy, BFS, DFS và IDS.
- So sánh thuật toán theo thời gian chạy, số node duyệt và loại kết quả.
- Hiển thị ga/trạm từ OpenStreetMap.
- Đặt vật cản để mô phỏng sự cố và tìm tuyến thay thế.
- Hiển thị chỉ dẫn hành trình từng bước.
- Lưu lịch sử tìm kiếm, ga đi qua, vật cản và dữ liệu bản đồ vào SQLite/cache.
- Có script đóng gói thành ứng dụng Windows `.exe`.

## Cài đặt

Khuyến nghị dùng Python 3.12 trên Windows.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Chạy ứng dụng

```powershell
python app.py
```

Lần chạy đầu có thể cần Internet để tải dữ liệu OpenStreetMap và ảnh nền bản đồ. Các lần sau app dùng cache cục bộ để mở nhanh hơn.

## Kiểm thử

```powershell
python test_all_cases.py
```

## Đóng gói EXE

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build_exe.ps1
```

Sau khi build, file phát hành nằm trong:

```text
dist\Saint Petersburg Rail Path Finder
```

Khi gửi cho người khác, gửi cả thư mục hoặc file ZIP trong `dist`, không gửi riêng mỗi file `.exe`.

## Ghi chú dữ liệu

Repo không đưa lên GitHub các file nặng như cache bản đồ, database SQLite, môi trường ảo, file build và ZIP phát hành. Những file này được tạo lại khi chạy hoặc build app.

