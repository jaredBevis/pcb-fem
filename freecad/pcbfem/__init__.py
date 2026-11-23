# SPDX-License-Identifier: LGPL-2.1-or-later
"""PCB FEM - KiCad copper nets to FreeCAD solids for electrical FEM (Elmer static current)."""
import os

__version__ = "0.3.1"
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ICONPATH = os.path.join(PKG_DIR, "Resources", "icons")


def icon(name: str) -> str:
    return os.path.join(ICONPATH, name if name.endswith(".svg") else name + ".svg")
