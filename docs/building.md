# Build and release

## Source layout

`src/Bake_Groups/bg_version.py` is the version source of truth. The open-source series begins at `1.0.0`. Source files are maintained once; Git tags preserve future versions. Build output belongs in `build/` and distributable archives in `dist/`.

## Native ABI matrix

| Maya | CPython | Output |
|---|---|---|
| 2022 | 3.7 x64 | `build/native/2022/bg_math_core.pyd` |
| 2023 | 3.9 x64 | `build/native/2023/bg_math_core.pyd` |
| 2024 | 3.10 x64 | `build/native/2024/bg_math_core.pyd` |
| 2025 | 3.11 x64 | `build/native/2025/bg_math_core.pyd` |
| 2026 | 3.11 x64 | `build/native/2026/bg_math_core.pyd` |
| 2027 | 3.13 x64 | `build/native/2027/bg_math_core.pyd` |

The extension uses pybind11 and the C++ standard library. It does not link against Maya libraries. Use matching CPython headers and its import library, or matching headers from the Maya devkit plus a CPython import library. Install Visual Studio C++ Build Tools (x64) and pybind11 2.13 headers. No keys or private build inputs are needed.

```powershell
./tools/build-native.ps1 -MayaVersion 2027 `
  -PythonInclude C:/Python313/include `
  -PythonLibrary C:/Python313/libs/python313.lib `
  -PybindInclude C:/SDK/pybind11/include
```

Paths above are examples: pass the actual matching SDK paths. `-MayaSdkRoot` can locate headers under an Autodesk devkit. Repeat for each ABI/year before building a release.

## Tests

Run host-independent regression tests:

```powershell
python -m unittest discover -s tests -p "test_*.py" -q
```

Select an ABI-matching interpreter for the native regression suite:

```powershell
$env:BG_NATIVE_TEST_BIN = (Resolve-Path build/native/2027).Path
& C:/ProgramFiles/Autodesk/Maya2027/bin/mayapy.exe tests/test_native_math_core.py
```

Run Maya integration tests in a new standalone process, never in a production scene. Set `MAYA_LOCATION` and prepend its `bin` directory to `PATH` if required by that installation. `tests/test_export_cage_maya.py` covers export round trips; `tests/test_open_ui_maya.py` covers the open-source UI. These tests create disposable scenes.

CI runs unit tests and sample native ABI builds. Before release, local validation must cover all six native outputs and at least one full Maya integration run; a matching CPython test is not a claim that every Maya GUI was tested.

## Source checkout in Maya

After building the matching native module, add the repository's `src` directory to Maya's Python path and run:

```python
import Bake_Groups
Bake_Groups.launch()
```

The launcher finds `build/native/<year>` from a source checkout. Installed packages use `Bake_Groups/bin/<year>`. It refuses to silently fall back to slower geometry implementations when the required native binary is missing.

## Package and publish

1. Update `bg_version.py` and `CHANGELOG.md`.
2. Build and test all native outputs and the maintained regressions.
3. Run `python tools/build-release.py --help`, then build the full Windows package. The builder requires all six binaries and includes corresponding native source, build instructions, notices and checksums.
4. Install that exact ZIP into a disposable scripts directory and run its packaged runtime in a fresh Maya process.
5. Commit source only, create `v<version>` at the tested commit, and push it.
6. Create a GitHub Release for that tag and upload `Bake_Master_<version>_Windows_x64.zip` plus its external checksum. Each release contains only the assets for its own version.

Do not commit binaries, SDKs, ZIPs, local credentials or machine-specific paths. Keep subsequent open-source version history; the initial historical cleanup is not a recurring release practice.
