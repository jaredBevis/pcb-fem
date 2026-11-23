# PCB FEM – FreeCAD workbench (v0.3.1)

PCB FEM imports KiCad copper nets as FreeCAD solids and lets you put currents, grounds and
voltages on real pads and vias. It then builds an Elmer **Static Current** analysis for
DC IR-drop, current-density and Joule-heating studies.

It grew out of a single-file macro. Documents made with that macro are upgraded
automatically when you open them with the workbench installed.

## Install

**With the Addon Manager (until the add-on is in the official index):**
1. Open *Tools ▸ Addon Manager*, then the settings (gear) icon, and add a **custom repository**:
   `https://github.com/jaredBevis/pcb-fem`, branch `main`.
2. Find **PCB FEM** in the list, install it, and restart FreeCAD.

**Manually:** clone or unzip this repository into FreeCAD's user `Mod` folder, then restart
FreeCAD. In FreeCAD's Python console, `App.getUserAppDataDir()` shows the folder that
contains `Mod`, e.g. `%APPDATA%\FreeCAD\v1-1\Mod` on Windows. To develop from a checkout
elsewhere, link it in instead:
```bat
mklink /J "%APPDATA%\FreeCAD\v1-1\Mod\pcb-fem" "C:\path\to\pcb-fem"
```

**Requirements:** FreeCAD 1.0 or newer with the FEM workbench, Gmsh, and Elmer (ElmerSolver
and ElmerGrid), configured under *Preferences ▸ FEM ▸ Elmer* and *Gmsh*.

## Toolbar

| Command | What it does |
|---|---|
| **Import KiCad nets** | Pick a `.kicad_pcb`, tick nets, and get one PartDesign Body per net, built from the stackup. |
| **Add terminal from selection** | Click pads or vias in the 3D view (Ctrl+click for several), then press this. The terminal panel opens with them ticked. With a terminal selected, it opens that terminal for editing. |
| **Terminal manager** | Define, edit and delete terminals. The panel stays open, so you can keep clicking in the 3D view, and ticked pads are highlighted in yellow. |
| **Build FEM analysis** | Fuses the copper with the contact stubs, optionally drops islands that have no terminal, meshes (HXT with Netgen optimiser), prepares the mesh for ElmerGrid, and creates or updates the solver, Static Current equation (direct UMFPACK), copper material and one constraint per terminal. Afterwards it **makes the analysis active** and **hides every object except a solver result**, so FreeCAD's own FEM commands also act on this analysis. |
| **Prepare mesh for Elmer** | Prunes mesh groups to stay within ElmerGrid's 4000-boundary limit. This also happens **automatically** whenever a PCB FEM mesh is regenerated, including from FreeCAD's own Gmsh dialog, so you normally never need the button. |
| **Run Elmer** | Runs the solver of the active PCB FEM analysis, so you don't need to switch to the FEM workbench. When it finishes, everything except the result is hidden, the result shows the potential, and the terminal report goes to the Report view. A failed run is reported as an error. |
| **Report results** | After the Elmer run: each terminal's voltage (mean/min/max), V/I, and the dissipated power ΣV·I. An all-zero result is reported as a failed solve. |
| **Preferences** | *Edit ▸ Preferences ▸ PCB FEM*: plating, polygon cleanup, contact scale, stub height, mesh sizes, 3D algorithm, linear solver, island dropping, mesh after build. |

## Terminals are real objects

Each terminal is a `PcbFem Terminal` object with an icon for its type (red current, blue
ground, orange voltage).
* Edit its properties (Items, Current_A, Scale, StubHeight, …) in the property editor and
  it rebuilds its contact stubs on recompute.
* Double-click it, or right-click ▸ *Edit terminal*, to open it in the panel.
* **Current**: amps *into* the net; use a negative value for a load. **Ground**: 0 V.
  **Voltage**: fixed potential.
* **Uniform J** shares the current between pads by contact area; **Equal per item** puts
  I/N through each pad or via.
* Documents with terminals need the workbench installed to stay editable. Without it the
  geometry still opens.

## Command line / batch

```bat
"C:\Program Files\FreeCAD 1.0\bin\python.exe" pcbfem_cli.py board.kicad_pcb --nets GND -o gnd.FCStd
"C:\Program Files\FreeCAD 1.0\bin\python.exe" pcbfem_cli.py board.kicad_pcb --terminals spec.json -o gnd.FCStd
python pcbfem_cli.py board.kicad_pcb --list-nets      (no FreeCAD needed)
```

A terminal spec file (see `tests/*_terminals.json`):
```json
{"net": "GND",
 "mesh": {"max": 0.3, "min": 0.005, "algorithm3d": "HXT"},
 "drop_islands": true,
 "terminals": [
   {"name": "VIN", "type": "Current", "value": 3.0, "items": ["U1.1", "U1.2"],
    "distribution": "Equal per item"},
   {"name": "RTN", "type": "Ground", "items": ["J1.2@bottom"]}]}
```
Settings you leave out fall back to the preferences.

## Validation

`tests/validate.py` runs import → terminals → mesh → Elmer → check, against reference values:

```bat
"C:\Program Files\FreeCAD 1.0\bin\python.exe" tests\validate.py --elmer "C:\Program Files\Elmer 9.0-Release\bin"
```

Reference results (Elmer built from source, FreeCAD 26.3 dev, all through this workbench):

* **10 mm trace** (0.5 mm × 35 µm), +1 A: V_in = 8.888 mV. The trace's mid-section carries
  1.00000 A, the voltage gradient is within 0.02 % of ρ·I/(w·t), and total Joule heat
  matches I·V.
* **Test board /VBUS** (micro, blind, buried and through vias), +2 A in, 2 × −0.25 A loads,
  ground: V(VIN) = 38.09 mV. Trace sections carry 2.0000 A and 1.5000 A.

GUI smoke test (17 checks), run offscreen:
* workbench registration, 3D highlight, terminal objects, rebuild on property change
* Build meshes and prunes the mesh, automatic pruning after an external remesh
* double-click editing, save/reload with proxies, legacy-terminal upgrade, preferences page

## Package layout

```
PcbFem/
  package.xml, LICENSE, README.md, pcbfem_cli.py
  freecad/pcbfem/
    kicad.py        parser, stackup, board model, polygon cleanup   (no FreeCAD needed)
    shapes.py       copper solids, pads, barrels
    importer.py     nets -> bodies
    fem.py          terminals -> stubs, Elmer analysis, mesh preparation, results report
    solve.py        run Elmer from the workbench, active analysis, result-only view
    objects.py      Terminal document object + view provider, legacy upgrade
    observers.py    automatic mesh preparation, upgrade on open
    prefs.py        preferences
    commands.py, init_gui.py, gui/ (import dialog, terminal panel, preferences page)
    Resources/icons/
  tests/            validation boards, terminal specs, validate.py
```

## Known limits

* Copper graphics inside footprints that have no net, back-drilling, and via tenting or
  capping geometry are not modelled. Thermal reliefs come out exactly as KiCad poured them.
* Refill zones in KiCad (B) before importing. Unfilled zones are reported and skipped.
* Terminals re-read the source board (the net's `SourceBoard` property). If you move the
  board file, update that property.

## License

Copyright (C) 2026 Jared ([jaredBevis](https://github.com/jaredBevis)).
PCB FEM is free software under the GNU Lesser General Public License, version 2.1 or (at
your option) any later version, the same licence as FreeCAD. See [LICENSE](LICENSE).
