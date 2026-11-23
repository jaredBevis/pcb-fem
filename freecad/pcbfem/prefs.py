# SPDX-License-Identifier: LGPL-2.1-or-later
"""User preferences (Edit > Preferences > PCB FEM), stored in FreeCAD's parameter tree."""

PARAM = "User parameter:BaseApp/Preferences/Mod/PcbFem"

# key: (type, default, label, tooltip)
SPEC = {
    # import
    "Plating":        (float, 0.025, "Barrel plating thickness [mm]", "Wall of drilled vias / PTH barrels"),
    "PolyTol":        (float, 0.001, "Polygon cleanup tolerance [mm]",
                       "Merge vertices / drop collinear points closer than this. KiCad fills contain "
                       "1 nm edges that produce degenerate mesh elements."),
    "MicroviaSolid":  (bool, True, "Microvias solid", "Model microvias as solid copper"),
    "FilledViaSolid": (bool, True, "Filled vias solid", "Vias with IPC-4761 filling become solid"),
    "AsBody":         (bool, True, "Create PartDesign Bodies", "Else plain Part features"),
    # terminals
    "ContactScale":   (float, 0.8, "Contact scale (linear)", "Pad outline scale for the contact stub"),
    "StubHeight":     (float, 0.05, "Stub height [mm]", "Height of the contact stub above the copper"),
    "FillHoles":      (bool, True, "Fill drilled holes of terminals", "Soldered lead in PTH / via barrels"),
    # analysis
    "MeshMax":        (float, 0.3, "Mesh max size [mm]", ""),
    "MeshMin":        (float, 0.005, "Mesh min size [mm]", ""),
    "Curvature":      (int, 12, "Mesh elements per 2*pi (curvature)", ""),
    "Algorithm3D":    (str, "HXT", "Gmsh 3D algorithm", "HXT (multithreaded, + Netgen optimiser) or Automatic"),
    "LinearSolver":   (str, "direct", "Linear solver", "direct (UMFPACK) or iterative (CG+ILU2)"),
    "JouleHeating":   (bool, True, "Calculate Joule heating", ""),
    "DropIslands":    (bool, True, "Drop copper islands without terminals",
                       "Floating copper carries no current; removing it keeps meshes small and "
                       "below ElmerGrid's body limit"),
    "MeshAfterBuild": (bool, True, "Run Gmsh after building", "Keeps the mesh in sync with the model"),
}

ALGORITHMS = ["HXT", "Automatic", "Delaunay", "New Delaunay", "Frontal"]
SOLVERS = ["direct", "iterative"]


def _grp():
    import FreeCAD
    return FreeCAD.ParamGet(PARAM)


def get(key):
    typ, default = SPEC[key][0], SPEC[key][1]
    try:
        g = _grp()
    except Exception:
        return default
    if typ is bool:
        return g.GetBool(key, default)
    if typ is int:
        return g.GetInt(key, default)
    if typ is float:
        return g.GetFloat(key, default)
    return g.GetString(key, default) or default


def set(key, value):  # noqa: A001
    typ = SPEC[key][0]
    g = _grp()
    if typ is bool:
        g.SetBool(key, bool(value))
    elif typ is int:
        g.SetInt(key, int(value))
    elif typ is float:
        g.SetFloat(key, float(value))
    else:
        g.SetString(key, str(value))


def options():
    """kicad.Options initialised from the preferences."""
    from .kicad import Options
    return Options(plating=get("Plating"), poly_tol=get("PolyTol"), microvia_solid=get("MicroviaSolid"),
                   filled_vias_solid=get("FilledViaSolid"), as_body=get("AsBody"))
