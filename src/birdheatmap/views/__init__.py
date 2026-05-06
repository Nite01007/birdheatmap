"""View registry — auto-discovers every module in this package and registers
those that expose the required interface (NAME, DISPLAY_NAME, DESCRIPTION,
render_data).

Unlike plot modules (which return PNG bytes), view modules return a plain dict
that the Flask route passes to a Jinja2 template as template context.

Usage:
    from birdheatmap.views import registry
    view = registry["arrivals"]
    data = view.render_data(db_conn, period="month", theme="dark")
"""

import importlib
import pkgutil
from types import ModuleType
from typing import Any


class ViewModel:
    """Thin wrapper around a view module that validates its interface."""

    def __init__(self, module: ModuleType) -> None:
        for attr in ("NAME", "DISPLAY_NAME", "DESCRIPTION", "render_data"):
            if not hasattr(module, attr):
                raise AttributeError(
                    f"View module {module.__name__!r} is missing required attribute {attr!r}"
                )
        self._module = module

    @property
    def name(self) -> str:
        return self._module.NAME  # type: ignore[attr-defined]

    @property
    def display_name(self) -> str:
        return self._module.DISPLAY_NAME  # type: ignore[attr-defined]

    @property
    def description(self) -> str:
        return self._module.DESCRIPTION  # type: ignore[attr-defined]

    def render_data(self, db, **kwargs: Any) -> dict:
        return self._module.render_data(db, **kwargs)  # type: ignore[attr-defined]


def _discover() -> dict[str, ViewModel]:
    """Import all sub-modules in the views package and collect those with NAME."""
    found: dict[str, ViewModel] = {}
    package = __name__  # "birdheatmap.views"
    for _finder, module_name, _is_pkg in pkgutil.iter_modules(__path__):
        full_name = f"{package}.{module_name}"
        module = importlib.import_module(full_name)
        if hasattr(module, "NAME"):
            vm = ViewModel(module)
            found[vm.name] = vm
    return found


# Module-level registry populated at import time.
registry: dict[str, ViewModel] = _discover()
