# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Document objects.

PcbFem Terminal (Part::FeaturePython): a named group of pads/vias of one net acting as one
electrical connection. Its Shape is the compound of contact stubs; it rebuilds itself on
recompute whenever one of its properties changes. Double-click to edit.

Documents saved with terminals need this workbench installed to stay editable (the geometry
still opens without it). Terminals made by the old single-file macro (plain Part::Feature with
the same properties) are upgraded automatically when a document is opened.
"""
import json

TYPE = "PcbFem::Terminal"
GROUP = "PCB Terminal"
TERM_TYPES = ["Current", "Ground", "Voltage"]
TERM_DIST = ["Uniform J", "Equal per item"]
COLORS = {"Current": (0.90, 0.10, 0.10), "Ground": (0.10, 0.30, 0.90), "Voltage": (0.95, 0.60, 0.00)}

_PROPS = [
    # type, name, tooltip
    ("App::PropertyString", "PcbNet", "KiCad net name"),
    ("App::PropertyLink", "NetObject", "Net copper object (Body / Part feature)"),
    ("App::PropertyEnumeration", "TermType", "Current (A into the net) / Ground (0 V) / Voltage (V)"),
    ("App::PropertyFloat", "Current_A", "Total current INTO the net [A] (Current type; negative = load)"),
    ("App::PropertyFloat", "Potential_V", "Potential [V] (Voltage type)"),
    ("App::PropertyEnumeration", "Distribution",
     "Uniform J: one current density over all items (current shared by area); "
     "Equal per item: every pad/via carries I/N"),
    ("App::PropertyStringList", "Items", "Pads/vias: label[@top|@bottom], wildcards allowed"),
    ("App::PropertyFloat", "Scale", "Linear scale of the pad outline used for the contact stub"),
    ("App::PropertyFloat", "StubHeight", "Contact stub height [mm]"),
    ("App::PropertyBool", "FillHoles", "Fill drilled barrels with copper (soldered lead)"),
    ("App::PropertyString", "StubFaces", "internal: stub end faces (JSON)"),
]


def is_terminal(obj) -> bool:
    return hasattr(obj, "TermType") and hasattr(obj, "Items")


def is_legacy_terminal(obj) -> bool:
    return is_terminal(obj) and obj.TypeId == "Part::Feature"


class TerminalProxy:
    Type = TYPE

    def __init__(self, obj):
        obj.Proxy = self
        self.setup(obj)

    @staticmethod
    def setup(obj):
        for typ, name, tip in _PROPS:
            if not hasattr(obj, name):
                obj.addProperty(typ, name, GROUP, tip)
                if name == "TermType":
                    obj.TermType = TERM_TYPES
                elif name == "Distribution":
                    obj.Distribution = TERM_DIST
                elif name == "Scale":
                    obj.Scale = 0.8
                elif name == "StubHeight":
                    obj.StubHeight = 0.05
                elif name == "FillHoles":
                    obj.FillHoles = True
        try:
            obj.setEditorMode("StubFaces", 2)     # hidden
            obj.setEditorMode("PcbNet", 1)        # read-only (follows NetObject)
        except Exception:
            pass

    def onDocumentRestored(self, obj):
        self.setup(obj)
        attach_view_provider(obj)

    def rebuild(self, obj):
        """Rebuild stubs now; raises on error (used by create/edit for immediate feedback)."""
        from .fem import compute_stubs
        shape, infos, keys = compute_stubs(obj)
        obj.Shape = shape
        obj.StubFaces = json.dumps(infos)
        if hasattr(obj, "NetObject") and obj.NetObject is not None:
            if obj.PcbNet != obj.NetObject.NetName:
                obj.PcbNet = obj.NetObject.NetName

    def execute(self, obj):
        try:
            self.rebuild(obj)
        except Exception as e:
            import FreeCAD
            FreeCAD.Console.PrintError("PCB FEM: terminal '%s': %s\n" % (obj.Label, e))

    def onChanged(self, obj, prop):
        pass

    # FreeCAD >= 0.21 / 1.x
    def dumps(self):
        return None

    def loads(self, state):
        return None

    # older FreeCAD
    def __getstate__(self):
        return None

    def __setstate__(self, state):
        return None


class TerminalViewProvider:
    def __init__(self, vobj):
        vobj.Proxy = self
        self.Object = vobj.Object

    def attach(self, vobj):
        self.Object = vobj.Object
        self._color(vobj.Object)

    def getIcon(self):
        from . import icon
        t = getattr(getattr(self, "Object", None), "TermType", "Current")
        return icon("Terminal" + t)

    def _color(self, obj):
        try:
            obj.ViewObject.ShapeColor = COLORS.get(obj.TermType, COLORS["Current"])
        except Exception:
            pass

    def updateData(self, obj, prop):
        self.Object = obj
        if prop == "TermType":
            self._color(obj)

    def onChanged(self, vobj, prop):
        pass

    def doubleClicked(self, vobj):
        self._edit(vobj.Object)
        return True

    def setupContextMenu(self, vobj, menu):
        from .gui.terminal_panel import _qt
        QtW, QtCore = _qt()
        act = QtW.QAction("Edit terminal...", menu)
        act.triggered.connect(lambda: self._edit(vobj.Object))
        menu.addAction(act)

    def _edit(self, obj):
        from .gui.terminal_panel import run_terminal_panel
        run_terminal_panel(obj.Document, terminal=obj)

    def dumps(self):
        return None

    def loads(self, state):
        return None

    def __getstate__(self):
        return None

    def __setstate__(self, state):
        return None


def attach_view_provider(obj):
    """Give terminals made headless (CLI) their view provider when opened in the GUI."""
    import FreeCAD
    if not FreeCAD.GuiUp:
        return
    try:
        vo = obj.ViewObject
        if vo is not None and not isinstance(getattr(vo, "Proxy", None), TerminalViewProvider):
            TerminalViewProvider(vo)
    except Exception:
        pass


def make_terminal(doc, name: str):
    import FreeCAD
    from .shapes import _safe_name
    obj = doc.addObject("Part::FeaturePython", "PcbTerm_" + _safe_name(name))
    TerminalProxy(obj)
    obj.Label = name
    if FreeCAD.GuiUp:
        TerminalViewProvider(obj.ViewObject)
    return obj


def upgrade_legacy_terminals(doc, log=None) -> int:
    """Replace plain Part::Feature terminals (old macro) by PcbFem Terminal objects."""
    legacy = [o for o in doc.Objects if is_legacy_terminal(o)]
    for old in legacy:
        label = old.Label
        new = make_terminal(doc, label + "_tmp")
        for _typ, name, _tip in _PROPS:
            if hasattr(old, name):
                try:
                    setattr(new, name, getattr(old, name))
                except Exception:
                    pass
        new.Shape = old.Shape.copy()
        parents = list(old.InList)
        for p in parents:            # keep group membership
            if hasattr(p, "Group") and old in p.Group:
                g = list(p.Group)
                g[g.index(old)] = new
                p.Group = g
        doc.removeObject(old.Name)
        new.Label = label
        new.purgeTouched()
    if legacy and log:
        log("PCB FEM: upgraded %d terminal(s) in %s to PCB FEM terminal objects" % (len(legacy), doc.Label))
    return len(legacy)
