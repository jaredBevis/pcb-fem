# SPDX-License-Identifier: LGPL-2.1-or-later
"""Import dialog: pick a board, filter/check nets, set options."""
from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Tuple

from .. import __version__
from ..kicad import Board
from .. import prefs
from ..importer import convert

def run_import_dialog():
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtGui, QtCore   # FreeCAD's PySide shim (Qt5/Qt6)
    try:
        from PySide import QtWidgets as QtW
    except ImportError:
        QtW = QtGui

    class Dlg(QtW.QDialog):
        def __init__(self):
            super().__init__(Gui.getMainWindow())
            self.setWindowTitle("KiCad PCB -> FreeCAD FEM copper (v%s)" % __version__)
            self.resize(560, 640)
            lay = QtW.QVBoxLayout(self)
            row = QtW.QHBoxLayout()
            self.path = QtW.QLineEdit()
            b = QtW.QPushButton("Browse...")
            b.clicked.connect(self.browse)
            row.addWidget(self.path)
            row.addWidget(b)
            lay.addWidget(QtW.QLabel("Board file (.kicad_pcb):"))
            lay.addLayout(row)
            self.info = QtW.QLabel("")
            self.info.setWordWrap(True)
            lay.addWidget(self.info)
            self.filt = QtW.QLineEdit()
            self.filt.setPlaceholderText("filter nets (wildcards ok, e.g. *GND*)")
            self.filt.textChanged.connect(self.apply_filter)
            lay.addWidget(self.filt)
            self.lst = QtW.QListWidget()
            lay.addWidget(self.lst, 1)
            form = QtW.QFormLayout()
            self.plating = QtW.QDoubleSpinBox()
            self.plating.setDecimals(4)
            self.plating.setRange(0.001, 0.2)
            self.plating.setSingleStep(0.005)
            self.plating.setValue(prefs.get("Plating"))
            self.plating.setSuffix(" mm")
            form.addRow("Barrel plating thickness:", self.plating)
            self.micro = QtW.QCheckBox("Microvias solid")
            self.micro.setChecked(prefs.get("MicroviaSolid"))
            self.filled = QtW.QCheckBox("Filled vias (IPC-4761 filling) solid")
            self.filled.setChecked(prefs.get("FilledViaSolid"))
            self.body = QtW.QCheckBox("Create PartDesign Bodies (else Part features)")
            self.body.setChecked(prefs.get("AsBody"))
            self.newdoc = QtW.QCheckBox("New document")
            self.newdoc.setChecked(App.ActiveDocument is None)
            self.step = QtW.QCheckBox("Also export one STEP per net")
            for w in (self.micro, self.filled, self.body, self.newdoc, self.step):
                form.addRow(w)
            lay.addLayout(form)
            bb = QtW.QDialogButtonBox(QtW.QDialogButtonBox.Ok | QtW.QDialogButtonBox.Cancel)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)

        def browse(self):
            p, _ = QtW.QFileDialog.getOpenFileName(self, "KiCad board", "", "KiCad PCB (*.kicad_pcb)")
            if p:
                self.path.setText(p)
                self.load(p)

        def load(self, p):
            try:
                b = Board(p, prefs.options())
            except Exception as e:
                self.info.setText("Error: %s" % e)
                return
            self.info.setText(b.stackup.table().replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
                              + ("<br><b>" + "<br>".join(b.warnings) + "</b>" if b.warnings else ""))
            self.lst.clear()
            for n in b.net_names():
                it = QtW.QListWidgetItem(n)
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(QtCore.Qt.Unchecked)
                self.lst.addItem(it)

        def apply_filter(self, t):
            pat = t if any(c in t for c in "*?[") else "*%s*" % t
            for i in range(self.lst.count()):
                it = self.lst.item(i)
                it.setHidden(bool(t) and not fnmatch.fnmatch(it.text().lower(), pat.lower()))

        def nets(self):
            return [self.lst.item(i).text() for i in range(self.lst.count())
                    if self.lst.item(i).checkState() == QtCore.Qt.Checked]

    d = Dlg()
    run = getattr(d, "exec", None) or d.exec_
    if not run():
        return
    nets = d.nets()
    if not nets:
        App.Console.PrintError("PCB FEM: no nets selected\n")
        return
    o = prefs.options()
    o.plating, o.microvia_solid = d.plating.value(), d.micro.isChecked()
    o.filled_vias_solid, o.as_body = d.filled.isChecked(), d.body.isChecked()
    p = d.path.text()
    step_dir = os.path.join(os.path.dirname(p), "fem_step") if d.step.isChecked() else None
    doc = None if d.newdoc.isChecked() or App.ActiveDocument is None else App.ActiveDocument
    QtW.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
    try:
        convert(p, nets, doc=doc, opts=o, step_dir=step_dir,
                log=lambda s: App.Console.PrintMessage(s + "\n"))
    finally:
        QtW.QApplication.restoreOverrideCursor()
    Gui.SendMsgToActiveView("ViewFit")
    try:
        Gui.activeDocument().activeView().viewIsometric()
    except Exception:
        pass


