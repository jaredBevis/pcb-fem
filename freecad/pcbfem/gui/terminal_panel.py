# SPDX-License-Identifier: LGPL-2.1-or-later
"""Terminal manager task panel (non-modal: the 3D view stays pickable)."""
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

from ..kicad import TermCandidate
from .. import prefs
from ..fem import (TERM_TYPES, TERM_DIST, net_objects, net_candidates, TerminalBuilder, _opts_from,
                   terminals_for, create_terminal, build_fem_model, prepare_mesh_for_elmer,
                   find_result_vtu, terminal_report)

# ----------------------------------------------------------------------------------
# GUI: FEM terminal manager (task panel, non-modal so the 3D view stays pickable)
# ----------------------------------------------------------------------------------


def _qt():
    from PySide import QtGui, QtCore
    try:
        from PySide import QtWidgets as QtW
    except ImportError:
        QtW = QtGui
    return QtW, QtCore


def _std_buttons(QtW, *names):
    v = 0
    for n in names:
        b = getattr(QtW.QDialogButtonBox, n, None)
        if b is None:
            b = getattr(QtW.QDialogButtonBox.StandardButton, n)
        v |= int(b.value) if hasattr(b, "value") else int(b)
    return v


class _PickObserver:
    def __init__(self, panel):
        self.panel = panel

    def addSelection(self, doc, obj, sub, pnt):    # noqa: N802 (FreeCAD API name)
        try:
            self.panel.on_pick(doc, obj, sub, pnt)
        except Exception as e:     # never let an observer exception escape into FreeCAD
            self.panel.status("Pick error: %s" % e)


class TerminalPanel:
    """Define pad/via terminals and build the Elmer static-current analysis."""

    def __init__(self, doc, terminal=None, preselect=None):
        import FreeCADGui as Gui
        QtW, QtCore = _qt()
        self.Gui, self.QtW, self.QtCore = Gui, QtW, QtCore
        self.doc = doc
        self.board = None
        self.cands: List[TermCandidate] = []
        self.items: Dict[str, object] = {}     # key -> QTreeWidgetItem
        self.faces: Dict[str, object] = {}     # key -> pad face for hit testing
        self.tb = None

        w = self.form = QtW.QWidget()
        w.setWindowTitle("PCB FEM terminals")
        lay = QtW.QVBoxLayout(w)

        row = QtW.QHBoxLayout()
        row.addWidget(QtW.QLabel("Net:"))
        self.net = QtW.QComboBox()
        self.nets = net_objects(doc)
        for o in self.nets:
            self.net.addItem("%s  (%s)" % (o.NetName, o.Name))
        self.net.currentIndexChanged.connect(self.load_net)
        row.addWidget(self.net, 1)
        lay.addLayout(row)

        # --- candidates -------------------------------------------------------------
        box = QtW.QGroupBox("1. Pads / vias (tick, or click them in the 3D view)")
        bl = QtW.QVBoxLayout(box)
        r2 = QtW.QHBoxLayout()
        self.filt = QtW.QLineEdit()
        self.filt.setPlaceholderText("filter: U1.*, *VIA*, J1.2 ...")
        self.filt.textChanged.connect(self.apply_filter)
        r2.addWidget(self.filt, 1)
        self.pick = QtW.QCheckBox("3D pick")
        self.pick.setChecked(True)
        self.pick.setToolTip("Clicking a pad/via face in the 3D view toggles it here.\n"
                             "Side (top/bottom) follows where you click.")
        r2.addWidget(self.pick)
        b = QtW.QPushButton("Clear")
        b.clicked.connect(lambda: self.set_checked([]))
        r2.addWidget(b)
        bl.addLayout(r2)
        self.tree = QtW.QTreeWidget()
        self.tree.setHeaderLabels(["Pad / via", "Side", "Details"])
        self.tree.setRootIsDecorated(False)
        self.tree.setMinimumHeight(170)
        bl.addWidget(self.tree)
        lay.addWidget(box)

        # --- terminal definition --------------------------------------------------------
        box = QtW.QGroupBox("2. Terminal")
        fl = QtW.QFormLayout(box)
        self.name = QtW.QLineEdit()
        fl.addRow("Name:", self.name)
        self.ttype = QtW.QComboBox()
        self.ttype.addItems(["Current (A into net)", "Ground (0 V)", "Voltage (V)"])
        self.ttype.currentIndexChanged.connect(self.type_changed)
        fl.addRow("Type:", self.ttype)
        self.value = QtW.QDoubleSpinBox()
        self.value.setRange(-1e5, 1e5)
        self.value.setDecimals(6)
        self.value.setValue(1.0)
        self.value.setSuffix(" A")
        fl.addRow("Value:", self.value)
        self.dist = QtW.QComboBox()
        self.dist.addItems(TERM_DIST)
        self.dist.setToolTip("Uniform J: one current density over all items (current shared by area)\n"
                             "Equal per item: every pad/via carries I/N")
        fl.addRow("Distribution:", self.dist)
        self.scale = QtW.QDoubleSpinBox()
        self.scale.setRange(0.05, 1.0)
        self.scale.setSingleStep(0.05)
        self.scale.setValue(prefs.get("ContactScale"))
        self.scale.setToolTip("Linear scale of the pad outline used for the contact face "
                              "(0.8 -> 64 % of the pad area)")
        fl.addRow("Contact scale:", self.scale)
        self.height = QtW.QDoubleSpinBox()
        self.height.setRange(0.001, 5.0)
        self.height.setDecimals(3)
        self.height.setSingleStep(0.01)
        self.height.setValue(prefs.get("StubHeight"))
        self.height.setSuffix(" mm")
        fl.addRow("Stub height:", self.height)
        self.fill = QtW.QCheckBox("Fill drilled holes (soldered lead)")
        self.fill.setChecked(prefs.get("FillHoles"))
        fl.addRow(self.fill)
        r3 = QtW.QHBoxLayout()
        b = QtW.QPushButton("Add / update terminal")
        b.clicked.connect(self.add_terminal)
        r3.addWidget(b)
        fl.addRow(r3)
        lay.addWidget(box)

        # --- existing terminals ----------------------------------------------------------
        box = QtW.QGroupBox("Terminals of this net")
        tl = QtW.QVBoxLayout(box)
        self.terms = QtW.QTreeWidget()
        self.terms.setHeaderLabels(["Name", "Type", "Value", "Items"])
        self.terms.setRootIsDecorated(False)
        self.terms.setMinimumHeight(90)
        self.terms.itemDoubleClicked.connect(lambda *a: self.edit_terminal())
        tl.addWidget(self.terms)
        r4 = QtW.QHBoxLayout()
        b = QtW.QPushButton("Edit")
        b.clicked.connect(self.edit_terminal)
        r4.addWidget(b)
        b = QtW.QPushButton("Delete")
        b.clicked.connect(self.delete_terminal)
        r4.addWidget(b)
        tl.addLayout(r4)
        lay.addWidget(box)

        # --- build ---------------------------------------------------------------------------
        box = QtW.QGroupBox("3. Analysis (Elmer, Static Current)")
        gl = QtW.QFormLayout(box)
        self.mmax = QtW.QDoubleSpinBox()
        self.mmax.setRange(0.001, 50)
        self.mmax.setDecimals(3)
        self.mmax.setValue(prefs.get("MeshMax"))
        self.mmax.setSuffix(" mm")
        gl.addRow("Mesh max size:", self.mmax)
        self.mmin = QtW.QDoubleSpinBox()
        self.mmin.setRange(0.0005, 5)
        self.mmin.setDecimals(4)
        self.mmin.setValue(prefs.get("MeshMin"))
        self.mmin.setSuffix(" mm")
        gl.addRow("Mesh min size:", self.mmin)
        self.joule = QtW.QCheckBox("Calculate Joule heating")
        self.joule.setChecked(prefs.get("JouleHeating"))
        gl.addRow(self.joule)
        self.meshnow = QtW.QCheckBox("Run Gmsh after building (recommended)")
        self.meshnow.setChecked(prefs.get("MeshAfterBuild"))
        gl.addRow(self.meshnow)
        self.islands = QtW.QCheckBox("Drop copper islands without terminals")
        self.islands.setChecked(prefs.get("DropIslands"))
        self.islands.setToolTip("Floating copper carries no current; dropping it keeps the mesh small")
        gl.addRow(self.islands)
        self.algo3d = QtW.QComboBox()
        self.algo3d.addItems(["HXT (multicore)", "Automatic", "Delaunay", "New Delaunay", "Frontal"])
        self.algo3d.setToolTip("Gmsh 3D algorithm, applied to the mesh on every build")
        a3 = prefs.get("Algorithm3D")
        self.algo3d.setCurrentIndex(prefs.ALGORITHMS.index(a3) if a3 in prefs.ALGORITHMS else 0)
        gl.addRow("3D mesher:", self.algo3d)
        self.lsolver = QtW.QComboBox()
        self.lsolver.addItems(["Direct (UMFPACK) - reliable", "Iterative (CG+ILU2) - very large models"])
        self.lsolver.setToolTip("FreeCAD's default iterative settings do not converge on thin PCB "
                                "copper and Elmer then silently returns wrong results.")
        self.lsolver.setCurrentIndex(1 if prefs.get("LinearSolver") == "iterative" else 0)
        gl.addRow("Linear solver:", self.lsolver)
        r5 = QtW.QHBoxLayout()
        b = QtW.QPushButton("Build / update FEM analysis")
        b.clicked.connect(self.build)
        r5.addWidget(b)
        b = QtW.QPushButton("Prepare mesh for Elmer")
        b.setToolTip("Run after meshing from FreeCAD's own mesh dialog: removes the thousands of\n"
                     "unused mesh groups that make ElmerGrid fail (all-zero results).")
        b.clicked.connect(self.prep_mesh)
        r5.addWidget(b)
        b = QtW.QPushButton("Run Elmer")
        b.setToolTip("Run the solver now; when it finishes only the result is shown")
        b.clicked.connect(self.solve)
        r5.addWidget(b)
        b = QtW.QPushButton("Report results")
        b.setToolTip("After running Elmer: terminal voltages, V/I and dissipated power")
        b.clicked.connect(self.report)
        r5.addWidget(b)
        gl.addRow(r5)
        lay.addWidget(box)

        self.log = QtW.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        lay.addWidget(self.log)

        self._bulk = False
        self.tree.itemChanged.connect(lambda *a: None if self._bulk else self.update_preview())
        for o in list(doc.Objects):                 # stale preview from an aborted session
            if hasattr(o, "PcbPreview"):
                doc.removeObject(o.Name)
        self.obs = _PickObserver(self)
        Gui.Selection.addObserver(self.obs)
        if terminal is not None and getattr(terminal, "NetObject", None) in self.nets:
            self.net.blockSignals(True)
            self.net.setCurrentIndex(self.nets.index(terminal.NetObject))
            self.net.blockSignals(False)
        self.load_net()
        if terminal is not None:
            self.load_terminal(terminal)
        elif preselect:
            self.pick_from_selection(preselect)

    # -- task panel API -----------------------------------------------------------------
    def getStandardButtons(self):   # noqa: N802
        return _std_buttons(self.QtW, "Close")

    def _close(self):
        self.remove_preview()
        try:
            self.Gui.Selection.removeObserver(self.obs)
        except Exception:
            pass
        self.Gui.Control.closeDialog()
        return True

    def reject(self):
        return self._close()

    def accept(self):
        return self._close()

    # -- helpers -----------------------------------------------------------------------------
    def status(self, msg):
        self.log.appendPlainText(msg)

    def net_obj(self):
        i = self.net.currentIndex()
        return self.nets[i] if 0 <= i < len(self.nets) else None

    def load_net(self, *_):
        self.tree.clear()
        self.items.clear()
        self.faces.clear()
        self.keymap = {}
        o = self.net_obj()
        if o is None:
            return
        try:
            self.board, self.cands = net_candidates(o)
        except Exception as e:
            self.status("Cannot read board: %s" % e)
            self.cands = []
            return
        self.tb = TerminalBuilder(self.board, _opts_from(o))
        QtW, QtCore = self.QtW, self.QtCore
        for c in sorted(self.cands, key=lambda c: (c.kind != "pad", _natkey(c.label))):
            for sd in ("top", "bottom"):
                if sd not in c.sides:
                    continue
                it = QtW.QTreeWidgetItem([c.label, sd + (" *" if sd == c.default_side else ""), c.info])
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(0, QtCore.Qt.Unchecked)
                it.setData(0, QtCore.Qt.UserRole, c.key(sd))
                self.tree.addTopLevelItem(it)
                self.items[c.key(sd)] = it
                self.keymap[c.key(sd)] = (c, sd)
        for i in range(3):
            self.tree.resizeColumnToContents(i)
        self.refresh_terms()
        self.status("%s: %d pad/via contact positions (* = default side)" % (o.NetName, len(self.items)))

    def apply_filter(self, t):
        pat = t if any(ch in t for ch in "*?[") else "*%s*" % t
        for key, it in self.items.items():
            it.setHidden(bool(t) and not fnmatch.fnmatch(key.lower(), pat.lower()))

    def checked(self) -> List[str]:
        return [k for k, it in self.items.items() if it.checkState(0) == self.QtCore.Qt.Checked]

    def set_checked(self, keys):
        ks = set(keys)
        self._bulk = True
        try:
            for k, it in self.items.items():
                it.setCheckState(0, self.QtCore.Qt.Checked if k in ks else self.QtCore.Qt.Unchecked)
        finally:
            self._bulk = False
        self.update_preview()

    # -- 3D highlight of ticked pads/vias ------------------------------------------------
    def remove_preview(self):
        o = self.doc.getObject("PcbFem_Preview")
        if o is not None:
            self.doc.removeObject(o.Name)

    def update_preview(self):
        keys = self.checked()
        if not keys or self.tb is None:
            self.remove_preview()
            return
        import Part
        V = self.tb.V
        st = self.board.stackup
        faces = []
        for k in keys:
            c, sd = self.keymap[k]
            f = self.faces.get(k)
            if f is None:
                f = self.faces[k] = self.tb.pad_face(c, sd)
            if f is None:
                continue
            L = st.by_name[c.layers[sd]]
            g = f.copy()
            g.translate(V(0, 0, L.z_top + 0.004 if sd == "top" else L.z_bot - 0.004))
            faces.append(g)
        if not faces:
            self.remove_preview()
            return
        o = self.doc.getObject("PcbFem_Preview")
        if o is None:
            o = self.doc.addObject("Part::Feature", "PcbFem_Preview")
            o.Label = "PCB FEM selection (temporary)"
            o.addProperty("App::PropertyBool", "PcbPreview", "PCB", "temporary highlight")
            try:
                vo = o.ViewObject
                vo.ShapeColor = (1.0, 0.95, 0.0)
                vo.LineColor = (1.0, 0.6, 0.0)
                vo.LineWidth = 3.0
                vo.Selectable = False
            except Exception:
                pass
        o.Shape = Part.makeCompound(faces)
        self.doc.recompute()

    def pick_from_selection(self, sel_ex):
        """Tick the pads/vias under the picked points of a Gui.Selection.getSelectionEx() list."""
        n = 0
        for s in sel_ex:
            for p in getattr(s, "PickedPoints", []) or []:
                if self.on_pick(self.doc.Name, s.ObjectName, "", (p.x, p.y, p.z), toggle=False):
                    n += 1
        if n:
            self.status("Ticked %d pad/via(s) from the 3D selection" % n)

    def load_terminal(self, t):
        self.name.setText(t.Label)
        self.ttype.setCurrentIndex(TERM_TYPES.index(t.TermType))
        self.value.setValue(t.Current_A if t.TermType == "Current" else t.Potential_V)
        self.dist.setCurrentText(t.Distribution)
        self.scale.setValue(t.Scale)
        self.height.setValue(t.StubHeight)
        self.fill.setChecked(t.FillHoles)
        self.set_checked(list(t.Items))
        self.status("Editing '%s': change ticks/values, then Add / update." % t.Label)

    def type_changed(self, i):
        self.value.setEnabled(i != 1)
        self.value.setSuffix(" A" if i == 0 else " V")
        self.dist.setEnabled(i == 0)

    def refresh_terms(self):
        self.terms.clear()
        o = self.net_obj()
        if o is None:
            return
        for t in terminals_for(self.doc, o.NetName):
            v = ("%g A" % t.Current_A) if t.TermType == "Current" else ("%g V" % t.Potential_V)
            it = self.QtW.QTreeWidgetItem([t.Label, t.TermType, v, ", ".join(t.Items)])
            it.setData(0, self.QtCore.Qt.UserRole, t.Name)
            self.terms.addTopLevelItem(it)
        n = len(terminals_for(self.doc, o.NetName))
        if not self.name.text():
            self.name.setText("T%d" % (n + 1))

    # -- 3D picking --------------------------------------------------------------------------
    def on_pick(self, doc, obj, sub, pnt, toggle=True):
        if not self.pick.isChecked() or self.board is None or doc != self.doc.Name:
            return False
        x, y, z = pnt
        side = "top" if z > self.board.stackup.total / 2.0 else "bottom"
        V = self.tb.V
        import Part
        v = Part.Vertex(V(x, y, 0))
        hits = []
        for c in self.cands:
            if side not in c.sides:
                continue
            k = c.key(side)
            # quick reject
            if math.hypot(c.center[0] - x, c.center[1] - y) > 10.0:
                continue
            f = self.faces.get(k)
            if f is None:
                f = self.faces[k] = self.tb.pad_face(c, side)
            if f is not None and f.distToShape(v)[0] < 1e-3:
                hits.append((f.Area, k))
        if not hits:
            self.status("No %s pad/via of this net at (%.3f, %.3f)" % (side, x, -y))
            return False
        k = min(hits)[1]
        it = self.items[k]
        on = (it.checkState(0) != self.QtCore.Qt.Checked) if toggle else True
        it.setCheckState(0, self.QtCore.Qt.Checked if on else self.QtCore.Qt.Unchecked)
        self.tree.scrollToItem(it)
        self.status("%s %s" % ("+" if on else "-", k))
        self.QtCore.QTimer.singleShot(0, self.Gui.Selection.clearSelection)
        return True

    # -- actions ------------------------------------------------------------------------------
    def add_terminal(self):
        o = self.net_obj()
        keys = self.checked()
        name = self.name.text().strip()
        if not (o and keys and name):
            self.status("Need a net, a terminal name and at least one ticked pad/via.")
            return
        ttype = TERM_TYPES[self.ttype.currentIndex()]
        existing = {t.Label: t for t in terminals_for(self.doc, o.NetName)}
        try:
            t = create_terminal(self.doc, o, name, keys, ttype, self.value.value(),
                                self.dist.currentText(), self.scale.value(), self.height.value(),
                                self.fill.isChecked(), obj=existing.get(name))
        except Exception as e:
            self.status("Error: %s" % e)
            return
        self.doc.recompute()
        infos = json.loads(t.StubFaces)
        area = sum(i["area"] for i in infos)
        msg = "%s '%s': %d item(s), contact area %.5f mm2" % (
            "Updated" if name in existing else "Created", name, len(keys), area)
        if ttype == "Current" and area > 0:
            msg += ", J = %.6g A/m2 (uniform)" % (self.value.value() / (area * 1e-6))
        self.status(msg)
        self.set_checked([])
        self.name.setText("")
        self.refresh_terms()

    def _sel_term(self):
        it = self.terms.currentItem()
        return self.doc.getObject(it.data(0, self.QtCore.Qt.UserRole)) if it else None

    def edit_terminal(self):
        t = self._sel_term()
        if t is not None:
            self.load_terminal(t)

    def delete_terminal(self):
        t = self._sel_term()
        if t is None:
            return
        name = t.Label
        self.doc.removeObject(t.Name)
        self.doc.recompute()
        self.refresh_terms()
        self.status("Deleted terminal '%s' (rebuild the analysis to update constraints)" % name)

    def build(self):
        o = self.net_obj()
        if o is None:
            return
        QtW, QtCore = self.QtW, self.QtCore
        QtW.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self.remove_preview()
            build_fem_model(self.doc, o, self.mmax.value(), self.mmin.value(), prefs.get("Curvature"),
                            self.joule.isChecked(), self.meshnow.isChecked(), log=self.status,
                            linear_solver="iterative" if self.lsolver.currentIndex() == 1 else "direct",
                            algorithm3d=self.algo3d.currentText().split(" (")[0],
                            drop_islands=self.islands.isChecked())
        except Exception as e:
            self.status("Error: %s" % e)
        finally:
            QtW.QApplication.restoreOverrideCursor()
        self.refresh_terms()


def _panel_report(self):
    o = self.net_obj()
    if o is None:
        return
    vtu = find_result_vtu(self.doc, o)
    if vtu is None:
        QtW = self.QtW
        vtu, _ = QtW.QFileDialog.getOpenFileName(self.form, "Elmer result", "", "VTU (*.vtu)")
        if not vtu:
            return
    try:
        terminal_report(self.doc, o, vtu, log=self.status)
    except Exception as e:
        self.status("Error: %s" % e)


TerminalPanel.report = _panel_report


def _panel_prep_mesh(self):
    o = self.net_obj()
    if o is None:
        return
    for an in self.doc.Objects:
        if an.TypeId == "Fem::FemAnalysis" and getattr(an, "PcbNet", None) == o.NetName:
            try:
                prepare_mesh_for_elmer(an, log=self.status)
                self.doc.recompute()
            except Exception as e:
                self.status("Error: %s" % e)
            return
    self.status("No analysis for %s yet - Build first." % o.NetName)


TerminalPanel.prep_mesh = _panel_prep_mesh


def _panel_solve(self):
    from ..solve import run_solver, analysis_for
    o = self.net_obj()
    if o is None:
        return
    an = analysis_for(self.doc, o)
    if an is None:
        self.status("No analysis for %s yet - Build first." % o.NetName)
        return
    try:
        run_solver(an, log=self.status)
    except Exception as e:
        self.status("Error: %s" % e)


TerminalPanel.solve = _panel_solve


def _natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def run_terminal_panel(doc=None, terminal=None, preselect=None):
    """Open the terminal manager. terminal: edit this terminal; preselect: a
    Gui.Selection.getSelectionEx() list whose picked points tick pads/vias."""
    import FreeCAD as App
    import FreeCADGui as Gui
    doc = doc or App.ActiveDocument
    if doc is None or not net_objects(doc):
        App.Console.PrintError("PCB FEM: open a document with imported PCB nets first\n")
        return None
    if Gui.Control.activeDialog():
        Gui.Control.closeDialog()
    p = TerminalPanel(doc, terminal=terminal, preselect=preselect)
    Gui.Control.showDialog(p)
    return p


