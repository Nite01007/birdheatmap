"""Plot registry — auto-discovers every module in this package and registers
those that expose the required interface (NAME, DISPLAY_NAME, DESCRIPTION,
PARAMS, render).

Usage:
    from birdheatmap.plots import registry
    plot = registry["annual_heatmap"]
    png_bytes = plot.render(db_conn, species_id=42, year=2025)
"""

import importlib
import pkgutil
from types import ModuleType
from typing import Any


class PlotModule:
    """Thin wrapper around a plot module that validates its interface."""

    def __init__(self, module: ModuleType) -> None:
        for attr in ("NAME", "DISPLAY_NAME", "DESCRIPTION", "PARAMS", "render"):
            if not hasattr(module, attr):
                raise AttributeError(
                    f"Plot module {module.__name__!r} is missing required attribute {attr!r}"
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

    @property
    def params(self) -> list[dict[str, Any]]:
        return self._module.PARAMS  # type: ignore[attr-defined]

    def render(self, db, species_id: int, **kwargs: Any) -> bytes:
        return self._module.render(db, species_id, **kwargs)  # type: ignore[attr-defined]


def _discover() -> dict[str, PlotModule]:
    """Import all sub-modules in the plots package and collect those with NAME."""
    found: dict[str, PlotModule] = {}
    package = __name__  # "birdheatmap.plots"
    for _finder, module_name, _is_pkg in pkgutil.iter_modules(__path__):
        full_name = f"{package}.{module_name}"
        module = importlib.import_module(full_name)
        if hasattr(module, "NAME"):
            pm = PlotModule(module)
            found[pm.name] = pm
    return found


# Module-level registry populated at import time.
registry: dict[str, PlotModule] = _discover()
