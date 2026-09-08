"""Shared pytest fixtures: repo root on sys.path + a stub `iterm2` module.

server.py imports `iterm2` at module level and touches a handful of its
attributes at class-definition / function-definition time (isinstance checks
against iterm2.Session inside function bodies only run when called, but the
module-level `import iterm2` itself must succeed, and any attribute accessed
during import — none currently — must exist on the stub).
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "iterm2" not in sys.modules:
    iterm2_stub = types.ModuleType("iterm2")

    class _Session:
        pass

    class _App:
        pass

    class _Connection:
        pass

    class _LocalWriteOnlyProfile:
        def set_normal_font(self, *a, **k):
            pass

    class _Size:
        def __init__(self, w, h):
            self.width = w
            self.height = h

    util_mod = types.ModuleType("iterm2.util")
    util_mod.Size = _Size

    def run_until_complete(fn):
        pass

    iterm2_stub.Session = _Session
    iterm2_stub.App = _App
    iterm2_stub.Connection = _Connection
    iterm2_stub.LocalWriteOnlyProfile = _LocalWriteOnlyProfile
    iterm2_stub.util = util_mod
    iterm2_stub.run_until_complete = run_until_complete

    sys.modules["iterm2"] = iterm2_stub
    sys.modules["iterm2.util"] = util_mod
