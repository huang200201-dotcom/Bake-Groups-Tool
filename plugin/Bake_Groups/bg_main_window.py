from __future__ import print_function, division, absolute_import

import os


def main():
    script_dir = os.path.normpath(os.path.dirname(os.path.abspath(__file__)))
    launcher_path = os.path.join(script_dir, "launcher.py")
    if not os.path.exists(launcher_path):
        raise RuntimeError("Bake Master launcher not found: {}".format(launcher_path))
    namespace = {"__file__": launcher_path, "__name__": "__main__"}
    with open(launcher_path, "rb") as handle:
        source = handle.read()
    if not isinstance(source, str):
        source = source.decode("utf-8", "replace")
    exec(compile(source, launcher_path, "exec"), namespace, namespace)


if __name__ == "__main__":
    main()
