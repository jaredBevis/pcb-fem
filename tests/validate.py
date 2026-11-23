# SPDX-License-Identifier: LGPL-2.1-or-later
"""
End-to-end validation: import -> terminals -> mesh -> Elmer -> compare with expected values.

Run with FreeCAD's Python, pointing at your Elmer installation, e.g. on Windows:

  "C:\\Program Files\\FreeCAD 1.0\\bin\\python.exe" tests\\validate.py ^
        --elmer "C:\\Program Files\\Elmer 9.0-Release\\bin"

Checks (values from the reference run, Elmer built from source, FreeCAD 26.3 dev):
  trace  10 mm x 0.5 mm x 35 um, +1 A  ->  V_in = 8.888 mV  (analytic trace part 8.63 mV + pads)
  vbus   test_board /VBUS, +2 A / 2 x -0.25 A / ground  ->  V(VIN) = 38.09 mV
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CASES = [
    ("trace", "validation_trace.kicad_pcb", "validation_trace_terminals.json", "IN", 8.888e-3),
    ("vbus", "test_board.kicad_pcb", "test_board_vbus_terminals.json", "VIN", 38.09e-3),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--elmer", required=True, help="folder with ElmerSolver and ElmerGrid")
    ap.add_argument("--freecad-path", help="folder containing FreeCAD.pyd/.so")
    ap.add_argument("--tol", type=float, default=0.02, help="relative tolerance (default 2 %%)")
    a = ap.parse_args()
    if a.freecad_path:
        sys.path.insert(0, a.freecad_path)
    import FreeCAD as App   # noqa: F401  (must come before the workbench import)
    # FreeCAD's start-up code can clear __main__'s globals: import what we need again
    import glob
    import json
    import os
    import subprocess
    import tempfile
    import freecad
    if os.path.join(ROOT, "freecad") not in list(freecad.__path__):
        freecad.__path__.append(os.path.join(ROOT, "freecad"))
    from freecad.pcbfem import importer, fem, prefs
    exe = lambda n: os.path.join(a.elmer, n + (".exe" if os.name == "nt" else ""))  # noqa: E731
    ok_all = True
    for name, board, spec_file, term, expect in CASES:
        work = tempfile.mkdtemp(prefix="pcbfem_%s_" % name)
        with open(os.path.join(HERE, spec_file)) as f:
            spec = json.load(f)
        doc = App.newDocument("val_" + name)
        importer.convert(os.path.join(HERE, board), [spec["net"]], doc=doc, opts=prefs.options(),
                         log=lambda s: None)
        spec.setdefault("mesh", {})["now"] = True
        fem.apply_terminal_spec(doc, spec, log=lambda s: None)
        an = [o for o in doc.Objects if o.TypeId == "Fem::FemAnalysis"][0]
        solver = [m for m in an.Group if m.Name.startswith("SolverElmer")][0]
        from femsolver.elmer import writer
        w = writer.Writer(solver, work)
        w.write_solver_input()
        w.getMesh().FemMesh.write(os.path.join(work, "mesh.unv"))
        subprocess.run([exe("ElmerGrid"), "8", "2", os.path.join(work, "mesh.unv"), "-out", work],
                       cwd=work, capture_output=True)
        r = subprocess.run([exe("ElmerSolver"), "case.sif"], cwd=work, capture_output=True, text=True)
        vtus = glob.glob(os.path.join(work, "**", "*.vtu"), recursive=True)
        if r.returncode != 0 or not vtus:
            print("FAIL %-6s ElmerSolver rc=%s (see %s)" % (name, r.returncode, work))
            ok_all = False
            continue
        net_obj = fem.net_objects(doc)[0]
        rep = fem.terminal_report(doc, net_obj, max(vtus, key=os.path.getmtime), log=lambda s: None)
        v = [t["V_mean"] for t in rep["terminals"] if t["name"] == term][0]
        good = abs(v - expect) <= a.tol * expect
        ok_all &= good
        print("%s %-6s V(%s) = %.4f mV  (expected %.4f mV)" % ("PASS" if good else "FAIL", name, term,
                                                            v * 1e3, expect * 1e3))
        App.closeDocument(doc.Name)
    print("ALL PASS" if ok_all else "SOME CHECKS FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
