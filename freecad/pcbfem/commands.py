# SPDX-License-Identifier: LGPL-2.1-or-later
"""Toolbar / menu commands of the PCB FEM workbench."""
import FreeCAD as App
import FreeCADGui as Gui

from . import icon

TOOLBAR = ["PcbFem_Import", "Separator", "PcbFem_AddTerminal", "PcbFem_TerminalManager",
           "Separator", "PcbFem_Build", "PcbFem_PrepareMesh", "PcbFem_Solve", "PcbFem_Report"]
MENU = TOOLBAR + ["Separator", "PcbFem_Preferences"]


def _log(s):
    App.Console.PrintMessage(s + "\n")


def _nets(doc):
    from .fem import net_objects
    return net_objects(doc) if doc else []


def _target_net(doc):
    """Net for a command: from the selection (net, terminal, FEM shape, analysis member), else
    the only net in the document, else ask."""
    nets = _nets(doc)
    if not nets:
        return None
    by_name = {n.NetName: n for n in nets}
    for o in Gui.Selection.getSelection():
        cands = [o] + list(getattr(o, "InList", []))
        for c in cands:
            for attr in ("NetName", "PcbNet"):
                v = getattr(c, attr, None)
                if v in by_name:
                    return by_name[v]
    if len(nets) == 1:
        return nets[0]
    from .gui.terminal_panel import _qt
    QtW, _ = _qt()
    item, ok = QtW.QInputDialog.getItem(Gui.getMainWindow(), "PCB FEM", "Net:",
                                        [n.NetName for n in nets], 0, False)
    return by_name.get(item) if ok else None


class _Base:
    def IsActive(self):                # noqa: N802
        return bool(_nets(App.ActiveDocument))


class Import:
    def GetResources(self):            # noqa: N802
        return {"Pixmap": icon("Import"), "MenuText": "Import KiCad nets...",
                "ToolTip": "Import selected nets of a .kicad_pcb as copper bodies (one per net), "
                           "using the board stackup"}

    def Activated(self):               # noqa: N802
        from .gui.import_dialog import run_import_dialog
        run_import_dialog()

    def IsActive(self):                # noqa: N802
        return True


class AddTerminal(_Base):
    def GetResources(self):            # noqa: N802
        return {"Pixmap": icon("AddTerminal"), "MenuText": "Add terminal from selection",
                "ToolTip": "Click pads/vias in the 3D view (Ctrl for several), then run this to "
                           "make them a Current / Ground / Voltage terminal"}

    def Activated(self):               # noqa: N802
        from .gui.terminal_panel import run_terminal_panel
        from .objects import is_terminal
        sel = Gui.Selection.getSelectionEx()
        terms = [s.Object for s in sel if is_terminal(s.Object)]
        if terms:
            run_terminal_panel(App.ActiveDocument, terminal=terms[0])
        else:
            run_terminal_panel(App.ActiveDocument, preselect=sel)


class TerminalManager(_Base):
    def GetResources(self):            # noqa: N802
        return {"Pixmap": icon("TerminalManager"), "MenuText": "Terminal manager",
                "ToolTip": "Define, edit and delete terminals; build the analysis"}

    def Activated(self):               # noqa: N802
        from .gui.terminal_panel import run_terminal_panel
        run_terminal_panel(App.ActiveDocument)


class Build(_Base):
    def GetResources(self):            # noqa: N802
        return {"Pixmap": icon("Build"), "MenuText": "Build FEM analysis",
                "ToolTip": "Fuse net copper + terminal stubs, mesh, and create/update the Elmer "
                           "Static Current analysis (settings from Preferences > PCB FEM)"}

    def Activated(self):               # noqa: N802
        from .fem import build_fem_model
        doc = App.ActiveDocument
        net = _target_net(doc)
        if net is None:
            return
        from .gui.terminal_panel import _qt
        QtW, QtCore = _qt()
        QtW.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            build_fem_model(doc, net, log=_log)
        except Exception as e:
            QtW.QMessageBox.warning(Gui.getMainWindow(), "PCB FEM", str(e))
        finally:
            QtW.QApplication.restoreOverrideCursor()


def _target_analysis(doc):
    """Active PCB FEM analysis, else the analysis of the net from the selection / the only net."""
    from .solve import analysis_for
    try:
        import FemGui
        an = FemGui.getActiveAnalysis()
        if an is not None and an.Document == doc and getattr(an, "PcbNet", None):
            return an
    except Exception:
        pass
    for o in Gui.Selection.getSelection():
        if o.TypeId == "Fem::FemAnalysis" and getattr(o, "PcbNet", None):
            return o
    net = _target_net(doc)
    return analysis_for(doc, net) if net is not None else None


class Solve(_Base):
    def GetResources(self):            # noqa: N802
        return {"Pixmap": icon("Solve"), "MenuText": "Run Elmer",
                "ToolTip": "Run the Elmer solver of the active PCB FEM analysis. When it finishes, "
                           "only the result is shown and the terminal report goes to the Report view."}

    def IsActive(self):                # noqa: N802
        doc = App.ActiveDocument
        return bool(doc) and any(o.TypeId == "Fem::FemAnalysis" and getattr(o, "PcbNet", None)
                                 for o in doc.Objects)

    def Activated(self):               # noqa: N802
        from .solve import run_solver
        doc = App.ActiveDocument
        an = _target_analysis(doc)
        if an is None:
            return
        try:
            run_solver(an)
        except Exception as e:
            from .gui.terminal_panel import _qt
            QtW, _ = _qt()
            QtW.QMessageBox.warning(Gui.getMainWindow(), "PCB FEM", str(e))


class PrepareMesh(_Base):
    def GetResources(self):            # noqa: N802
        return {"Pixmap": icon("PrepareMesh"), "MenuText": "Prepare mesh for Elmer",
                "ToolTip": "Prune unused mesh groups (ElmerGrid 4000-boundary limit). Runs "
                           "automatically after meshing; use this for meshes made elsewhere."}

    def Activated(self):               # noqa: N802
        from .fem import prepare_mesh_for_elmer
        doc = App.ActiveDocument
        for an in doc.Objects:
            if an.TypeId == "Fem::FemAnalysis" and getattr(an, "PcbNet", None):
                prepare_mesh_for_elmer(an, log=_log)
        doc.recompute()


class Report(_Base):
    def GetResources(self):            # noqa: N802
        return {"Pixmap": icon("Report"), "MenuText": "Report results",
                "ToolTip": "Terminal voltages, V/I and dissipated power from the last Elmer run"}

    def Activated(self):               # noqa: N802
        from .fem import terminal_report, find_result_vtu
        from .gui.terminal_panel import _qt
        QtW, _ = _qt()
        doc = App.ActiveDocument
        an = _target_analysis(doc)
        nets = [n for n in _nets(doc) if an is not None and n.NetName == an.PcbNet]
        net = nets[0] if nets else _target_net(doc)
        if net is None:
            return
        vtu = find_result_vtu(doc, net)
        if vtu is None:
            vtu, _f = QtW.QFileDialog.getOpenFileName(Gui.getMainWindow(), "Elmer result", "", "VTU (*.vtu)")
            if not vtu:
                return
        lines = []
        try:
            terminal_report(doc, net, vtu, log=lambda s: (lines.append(s), _log(s)))
            QtW.QMessageBox.information(Gui.getMainWindow(), "PCB FEM results: " + net.NetName,
                                        "\n".join(lines))
        except Exception as e:
            QtW.QMessageBox.warning(Gui.getMainWindow(), "PCB FEM", str(e))


class Preferences:
    def GetResources(self):            # noqa: N802
        return {"Pixmap": icon("PcbFem"), "MenuText": "Preferences...",
                "ToolTip": "PCB FEM defaults (plating, contact stubs, mesh, solver)"}

    def Activated(self):               # noqa: N802
        Gui.showPreferences("PCB FEM")

    def IsActive(self):                # noqa: N802
        return True


def register():
    for name, cls in (("PcbFem_Import", Import), ("PcbFem_AddTerminal", AddTerminal),
                      ("PcbFem_TerminalManager", TerminalManager), ("PcbFem_Build", Build),
                      ("PcbFem_Solve", Solve), ("PcbFem_PrepareMesh", PrepareMesh),
                      ("PcbFem_Report", Report),
                      ("PcbFem_Preferences", Preferences)):
        Gui.addCommand(name, cls())
