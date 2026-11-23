# SPDX-License-Identifier: LGPL-2.1-or-later
"""Run Elmer for a PCB FEM analysis without leaving the workbench, and tidy the view."""
import FreeCAD as App

_running = {}      # analysis name -> ElmerTools (keeps the QProcess alive)


def _log(s):
    App.Console.PrintMessage(s.rstrip() + "\n")


def _cursor(busy):
    if not App.GuiUp:
        return
    try:
        from .gui.terminal_panel import _qt
        QtW, QtCore = _qt()
        if busy:
            QtW.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        else:
            QtW.QApplication.restoreOverrideCursor()
    except Exception:
        pass


def analysis_for(doc, net_obj):
    for o in doc.Objects:
        if o.TypeId == "Fem::FemAnalysis" and getattr(o, "PcbNet", None) == net_obj.NetName:
            return o
    return None


def solver_of(an):
    for m in an.Group:
        if getattr(getattr(m, "Proxy", None), "Type", "") == "Fem::SolverElmer":
            return m
    return None


def set_active(an):
    """Make `an` the active analysis, so FreeCAD's own FEM commands act on it too."""
    if not App.GuiUp:
        return
    try:
        import FemGui
        FemGui.setActiveAnalysis(an)
    except Exception:
        pass


def result_objects(an):
    """Result pipelines of the analysis plus everything inside them (filters, ...)."""
    keep = set()
    solver = solver_of(an)
    pipes = [r for r in (getattr(solver, "Results", []) or []) if r.isDerivedFrom("Fem::FemPostPipeline")] \
        if solver else []
    pipes += [m for m in an.Group if m.isDerivedFrom("Fem::FemPostPipeline")]
    stack = list(pipes)
    while stack:
        o = stack.pop()
        if o.Name in keep:
            continue
        keep.add(o.Name)
        stack.extend(getattr(o, "Group", []) or [])
        stack.extend(getattr(o, "OutList", []) or [])
    return [an.Document.getObject(n) for n in keep if an.Document.getObject(n) is not None]


def show_only_results(an):
    """Hide every object of the document except the analysis' solver result(s)."""
    if not App.GuiUp:
        return
    doc = an.Document
    keep = result_objects(an)
    keep_names = {o.Name for o in keep}
    for o in doc.Objects:
        if o.Name in keep_names:
            continue
        try:
            if o.ViewObject is not None and o.ViewObject.Visibility:
                o.ViewObject.Visibility = False
        except Exception:
            pass
    for o in keep:
        if o.isDerivedFrom("Fem::FemPostPipeline"):
            try:
                o.ViewObject.Visibility = True
            except Exception:
                pass


def _show_field(an, name="potential"):
    for o in result_objects(an):
        if o.isDerivedFrom("Fem::FemPostPipeline"):
            try:
                fields = o.ViewObject.getEnumerationsOfProperty("Field")
                for f in fields:
                    if f.lower().startswith(name):
                        o.ViewObject.Field = f
                        break
            except Exception:
                pass


def run_solver(an, blocking=False, log=_log, report=True, on_finished=None):
    """
    Prepare the mesh and run Elmer for analysis `an` (asynchronously unless blocking).
    When it finishes successfully: only the result is shown (potential field), the
    analysis stays active and the terminal report is written to the Report view.
    """
    from femsolver.elmer import elmertools
    from .fem import prepare_mesh_for_elmer, terminal_report, find_result_vtu
    if an.Name in _running:
        log("PCB FEM: Elmer is already running for %s" % an.Label)
        return None
    solver = solver_of(an)
    if solver is None:
        raise ValueError("%s has no Elmer solver - Build first" % an.Label)
    mesh = [m for m in an.Group if m.TypeId.startswith("Fem::FemMesh")]
    if not mesh or not mesh[0].FemMesh.NodeCount:
        raise ValueError("%s has no mesh yet - Build (with 'Run Gmsh after building') first" % an.Label)
    if "Touched" in (getattr(mesh[0], "State", []) or []):
        log("PCB FEM: WARNING - the mesh is out of date; Build again to remesh")
    prepare_mesh_for_elmer(an, log=lambda s: None)
    set_active(an)
    doc = an.Document
    net = [o for o in doc.Objects if getattr(o, "NetName", None) == an.PcbNet and hasattr(o, "SourceBoard")]

    tool = elmertools.ElmerTools(solver)
    _running[an.Name] = tool
    err = []

    def stderr():
        try:
            err.append(tool.process.readAllStandardError().data().decode("utf-8", "replace"))
        except Exception:
            pass

    def finished(code, status):           # runs after FreeCAD has loaded the result
        _running.pop(an.Name, None)
        _cursor(False)
        doc.recompute()
        ok = (code == 0)
        if not ok:
            msg = ("Elmer finished with errors (exit code %s) - result not updated.\n"
                   "Open the solver output for details. Typical causes: 'Degenerate 3D element' "
                   "(bad mesh: Build again or choose the Automatic mesher) or a mesh that was "
                   "not prepared for Elmer." % code)
            App.Console.PrintError("PCB FEM: " + msg + "\n" + "".join(err)[-2000:] + "\n")
            if App.GuiUp and not blocking:
                try:
                    import FreeCADGui as Gui
                    from .gui.terminal_panel import _qt
                    QtW, _ = _qt()
                    QtW.QMessageBox.warning(Gui.getMainWindow(), "PCB FEM", msg)
                except Exception:
                    pass
        else:
            show_only_results(an)
            _show_field(an)

            if report and net:
                try:
                    terminal_report(doc, net[0], find_result_vtu(doc, net[0]), log=log)
                except Exception as e:
                    log("PCB FEM: report: %s" % e)
        if on_finished:
            on_finished(ok)

    tool.process.readyReadStandardError.connect(stderr)
    tool.process.finished.connect(finished)
    _cursor(True)
    log("PCB FEM: running Elmer for %s in %s" % (an.Label, solver.WorkingDirectory))
    try:
        tool.run(blocking)
    except Exception:
        _running.pop(an.Name, None)
        _cursor(False)
        raise
    return tool
