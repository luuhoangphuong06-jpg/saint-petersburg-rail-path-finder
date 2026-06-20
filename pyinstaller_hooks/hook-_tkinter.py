import sys
from pathlib import Path


python_root = Path(sys.base_prefix)
tcl_root = python_root / "tcl"
dll_root = python_root / "DLLs"
project_python = Path.cwd() / "python_full" / "Python312"

if not (tcl_root / "tcl8.6" / "init.tcl").exists():
    tcl_root = project_python / "tcl"
if not (dll_root / "tcl86t.dll").exists():
    dll_root = project_python / "DLLs"

datas = [
    (str(tcl_root / "tcl8.6"), "_tcl_data"),
    (str(tcl_root / "tk8.6"), "_tk_data"),
]

binaries = [
    (str(dll_root / "tcl86t.dll"), "."),
    (str(dll_root / "tk86t.dll"), "."),
]
