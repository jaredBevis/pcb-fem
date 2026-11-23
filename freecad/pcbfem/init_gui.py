# SPDX-License-Identifier: LGPL-2.1-or-later
"""PCB FEM workbench registration (loaded by FreeCAD at start-up)."""
import FreeCADGui as Gui

from . import icon, __version__


class PcbFemWorkbench(Gui.Workbench):
    MenuText = "PCB FEM"
    ToolTip = "KiCad copper nets -> FreeCAD solids, pad/via terminals and Elmer current-flow analysis"
    Icon = icon("PcbFem")

    def Initialize(self):            # noqa: N802
        from . import commands
        commands.register()
        self.appendToolbar("PCB FEM", commands.TOOLBAR)
        self.appendMenu("PCB FEM", commands.MENU)

    def Activated(self):             # noqa: N802
        pass

    def Deactivated(self):           # noqa: N802
        pass

    def GetClassName(self):          # noqa: N802
        return "Gui::PythonWorkbench"


Gui.addWorkbench(PcbFemWorkbench())

# Things that must work even before the workbench is first activated:
try:
    from .gui.prefs_page import PrefsPage
    Gui.addPreferencePage(PrefsPage, "PCB FEM")
except Exception as e:   # pragma: no cover
    import FreeCAD
    FreeCAD.Console.PrintWarning("PCB FEM: preference page not available: %s\n" % e)
try:
    from . import observers
    observers.install()
except Exception as e:   # pragma: no cover
    import FreeCAD
    FreeCAD.Console.PrintWarning("PCB FEM: document observer not installed: %s\n" % e)

import FreeCAD  # noqa: E402
FreeCAD.Console.PrintLog("PCB FEM %s loaded\n" % __version__)
