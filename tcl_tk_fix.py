import glob
import os
import sys
from pathlib import Path


def _first_parent(patterns):
    for pattern in patterns:
        matches = glob.glob(str(pattern))
        if matches:
            return str(Path(matches[0]).parent)
    return None


def fix_tcl_tk_paths():
    """Help Tkinter find Tcl/Tk when Python is embedded or packaged."""
    try:
        roots = [
            Path(sys.base_prefix),
            Path(sys.prefix),
            Path(sys.executable).resolve().parent,
        ]

        if getattr(sys, "frozen", False):
            roots.extend([
                Path(getattr(sys, "_MEIPASS", "")),
                Path(sys.executable).resolve().parent / "_internal",
            ])

        tcl_patterns = []
        tk_patterns = []
        for root in roots:
            if not root:
                continue
            tcl_patterns.extend([
                root / "tcl" / "tcl*" / "init.tcl",
                root / "_internal" / "tcl" / "tcl*" / "init.tcl",
            ])
            tk_patterns.extend([
                root / "tcl" / "tk*" / "pkgIndex.tcl",
                root / "_internal" / "tcl" / "tk*" / "pkgIndex.tcl",
            ])

        tcl_dir = _first_parent(tcl_patterns)
        tk_dir = _first_parent(tk_patterns)

        if tcl_dir:
            os.environ.setdefault("TCL_LIBRARY", tcl_dir)
        if tk_dir:
            os.environ.setdefault("TK_LIBRARY", tk_dir)
    except Exception:
        pass


fix_tcl_tk_paths()
