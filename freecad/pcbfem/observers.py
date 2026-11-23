# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Document observers installed by the workbench at FreeCAD start-up:

* when a document has been opened, plain Part::Feature terminals made by the old single-file
  macro are upgraded to PCB FEM terminal objects, and PCB FEM meshes that still carry
  FreeCAD's thousands of automatic groups are prepared for ElmerGrid;
* whenever the mesh of a PCB FEM analysis is regenerated - by our Build, or by FreeCAD's own
  Gmsh dialog - its mesh groups are pruned for ElmerGrid (see fem.prepare_mesh_for_elmer).
  Without this, ElmerGrid's 4000-boundary limit is exceeded and the solve returns all zeros.

Nothing is touched while a document is still being restored (doing so can crash FreeCAD);
all work is deferred to the Qt event loop.
"""
import re

_AUTO = re.compile(r"^(Vertex|Edge|Face)\d+$")
_busy = set()
_installed = None


def _defer(fn, ms=0):
    try:
        from PySide import QtCore
        QtCore.QTimer.singleShot(ms, fn)
    except Exception:
        fn()


def _log(s):
    import FreeCAD
    s = s.strip()
    if "removed 0 unused groups" in s and "MISSING" not in s:
        return                                    # nothing happened - stay quiet
    if not s.startswith("PCB FEM"):
        s = "PCB FEM: " + s
    FreeCAD.Console.PrintMessage(s + "\n")


def _doc_alive(name):
    import FreeCAD
    return name in FreeCAD.listDocuments()


def _pcb_analysis_of(mesh):
    for p in mesh.InList:
        if p.TypeId == "Fem::FemAnalysis" and getattr(p, "PcbNet", None):
            return p
    return None


def _needs_prep(mesh) -> bool:
    fm = mesh.FemMesh
    return bool(fm.NodeCount) and any(_AUTO.match(fm.getGroupName(g)) for g in fm.Groups)


def _prepare(doc_name, mesh_name):
    import FreeCAD
    from .fem import prepare_mesh_for_elmer
    if not _doc_alive(doc_name):
        return
    doc = FreeCAD.getDocument(doc_name)
    if doc.Restoring:
        _defer(lambda: _prepare(doc_name, mesh_name), 200)
        return
    mesh = doc.getObject(mesh_name)
    if mesh is None:
        return
    an = _pcb_analysis_of(mesh)
    if an is None or not _needs_prep(mesh):
        return
    key = (doc_name, mesh_name)
    _busy.add(key)
    try:
        prepare_mesh_for_elmer(an, log=_log)
    except Exception as e:
        FreeCAD.Console.PrintWarning("PCB FEM: mesh preparation failed: %s\n" % e)
    finally:
        _busy.discard(key)


def _after_open(doc_name, tries=0):
    import FreeCAD
    if not _doc_alive(doc_name):
        return
    doc = FreeCAD.getDocument(doc_name)
    if doc.Restoring:
        if tries < 100:
            _defer(lambda: _after_open(doc_name, tries + 1), 200)
        return
    try:
        from .objects import upgrade_legacy_terminals
        upgrade_legacy_terminals(doc, log=_log)
    except Exception as e:
        FreeCAD.Console.PrintWarning("PCB FEM: terminal upgrade failed: %s\n" % e)
    for o in list(doc.Objects):
        if o.TypeId.startswith("Fem::FemMesh"):
            _prepare(doc_name, o.Name)


class _DocObserver:
    # FreeCAD has no "document restored" slot for Python observers; an opened document
    # becomes active right after loading, so check it there (once per document).
    _seen = set()

    def slotActivateDocument(self, doc):           # noqa: N802
        if doc.Name in self._seen:
            return
        self._seen.add(doc.Name)
        name = doc.Name
        _defer(lambda: _after_open(name))

    def slotDeletedDocument(self, doc):            # noqa: N802
        self._seen.discard(doc.Name)

    def slotChangedObject(self, obj, prop):        # noqa: N802
        if prop != "FemMesh":
            return
        try:
            if obj.Document.Restoring or not obj.TypeId.startswith("Fem::FemMesh"):
                return
            key = (obj.Document.Name, obj.Name)
        except Exception:
            return
        if key in _busy:
            return
        # Gmsh assigns FemMesh first and adds its groups afterwards in place: decide later.
        _defer(lambda: _prepare(*key))


def install():
    global _installed
    if _installed is None:
        import FreeCAD
        _installed = _DocObserver()
        FreeCAD.addDocumentObserver(_installed)
    return _installed
