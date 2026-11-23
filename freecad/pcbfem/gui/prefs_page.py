# SPDX-License-Identifier: LGPL-2.1-or-later
"""Edit > Preferences > PCB FEM."""
from .. import prefs


def _qt():
    from PySide import QtGui, QtCore
    try:
        from PySide import QtWidgets as QtW
    except ImportError:
        QtW = QtGui
    return QtW, QtCore


class PrefsPage:
    SECTIONS = [
        ("Import", ["Plating", "PolyTol", "MicroviaSolid", "FilledViaSolid", "AsBody"]),
        ("Terminals", ["ContactScale", "StubHeight", "FillHoles"]),
        ("Analysis", ["MeshMax", "MeshMin", "Curvature", "Algorithm3D", "LinearSolver",
                      "JouleHeating", "DropIslands", "MeshAfterBuild"]),
    ]

    def __init__(self, parent=None):
        QtW, _ = _qt()
        self.form = QtW.QWidget(parent)
        self.form.setWindowTitle("PCB FEM")
        lay = QtW.QVBoxLayout(self.form)
        self.w = {}
        for title, keys in self.SECTIONS:
            box = QtW.QGroupBox(title)
            fl = QtW.QFormLayout(box)
            for k in keys:
                typ, default, label, tip = prefs.SPEC[k]
                if typ is bool:
                    w = QtW.QCheckBox(label)
                    fl.addRow(w)
                elif k in ("Algorithm3D", "LinearSolver"):
                    w = QtW.QComboBox()
                    w.addItems(prefs.ALGORITHMS if k == "Algorithm3D" else prefs.SOLVERS)
                    fl.addRow(label + ":", w)
                elif typ is int:
                    w = QtW.QSpinBox()
                    w.setRange(0, 10000)
                    fl.addRow(label + ":", w)
                else:
                    w = QtW.QDoubleSpinBox()
                    w.setDecimals(4)
                    w.setRange(0.0, 100.0)
                    w.setSingleStep(0.005 if default < 0.1 else 0.05)
                    fl.addRow(label + ":", w)
                if tip:
                    w.setToolTip(tip)
                self.w[k] = w
            lay.addWidget(box)
        lay.addStretch(1)

    def loadSettings(self):          # noqa: N802
        for k, w in self.w.items():
            v = prefs.get(k)
            if hasattr(w, "setChecked"):
                w.setChecked(bool(v))
            elif hasattr(w, "setCurrentText"):
                w.setCurrentText(str(v))
            else:
                w.setValue(v)

    def saveSettings(self):          # noqa: N802
        for k, w in self.w.items():
            if hasattr(w, "isChecked"):
                prefs.set(k, w.isChecked())
            elif hasattr(w, "currentText"):
                prefs.set(k, w.currentText())
            else:
                prefs.set(k, w.value())
