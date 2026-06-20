CACH DONG GOI APP THANH FILE .EXE
=================================

1. Nen copy toan bo project ra duong dan ngan:

   C:\RailwayApp

2. Mo PowerShell tai thu muc do.

3. Kiem tra Python:

   python --version

   Neu PowerShell bao khong tim thay python, hay cai Python ban Windows 64-bit tu python.org.
   Khi cai dat bat buoc tich:

   Add python.exe to PATH

4. Chay lenh:

   Set-ExecutionPolicy -Scope Process Bypass
   .\build_exe.ps1

5. Sau khi build xong, file mo app nam o:

   dist\Saint Petersburg Rail Path Finder\Saint Petersburg Rail Path Finder.exe

6. Khi gui cho may khac, gui ca thu muc:

   dist\Saint Petersburg Rail Path Finder

   Khong chi gui rieng file .exe, vi build dang dung che do --onedir.

Luu y neu gap loi Tkinter/Tcl:
- Nen dung Python cai tu python.org, khong dung ban Python rut gon/embedded.
- Nen de project o C:\RailwayApp hoac D:\RailwayApp.
- File tcl_tk_fix.py da duoc them de tu dong gan TCL_LIBRARY va TK_LIBRARY truoc khi import tkinter.
