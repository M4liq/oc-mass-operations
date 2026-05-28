from __future__ import annotations

_MODULE_NAMES = (
    "common",
    "params",
    "skill_install",
    "manifest",
    "workflow_model",
    "prompting",
    "runners",
    "operation",
    "workflow_run",
    "status",
    "worktrees",
    "state",
    "planner",
)

_modules = []
_exports: dict[str, object] = {}

# The first extraction pass keeps the old single-module call graph intact by
# wiring moved top-level functions into each module's globals after import.
for _name in _MODULE_NAMES:
    _module = __import__(f"{__package__}.{_name}", fromlist=["*"])
    _modules.append(_module)
    for _key, _value in vars(_module).items():
        if not _key.startswith("_"):
            _exports[_key] = _value

for _module in _modules:
    vars(_module).update(_exports)

globals().update(_exports)
__all__ = sorted(_exports)
