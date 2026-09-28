# Contributing

感谢帮助改进 Bake Master。请通过 Issue 描述 Maya 版本、操作步骤、期望结果和实际结果；场景文件请使用可公开分享的最小示例。

## Development workflow

1. Fork and clone the repository; create a focused branch.
2. Edit Python in `src/Bake_Groups` or native geometry in `native`.
3. Run `python -m unittest discover -s tests -p "test_*.py" -q`.
4. For geometry/export/UI changes, run the relevant tests in a fresh Maya standalone process. Never run scene-resetting tests inside an artist's working session.
5. For C++ changes, build and test every supported Python ABI before distributing binaries.
6. Open a pull request describing the behavior change and actual validation.

Keep generated binaries and ZIPs out of commits. Maya 2022 uses Python 3.7; runtime code must remain compatible with that syntax and PySide2 as well as PySide6.

Contributions are distributed under the project's GPL v3 license. Preserve upstream notices. See `docs/building.md` for the build matrix and release procedure.
