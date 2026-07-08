"""Framework-version-neutral access to FastAPI's effective HTTP routes."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any


def iter_effective_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Yield concrete routes from both flattened and lazy included routers.

    FastAPI 0.139 introduced ``_IncludedRouter`` entries whose public behavior
    is equivalent to the previously flattened ``APIRoute`` list. Its
    ``effective_candidates`` method supplies immutable route contexts with the
    same path, method, name, and regex attributes needed by contract tests.
    """

    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from candidates()
        else:
            yield route


def route_fully_matches(route: Any, *, method: str, path: str) -> bool:
    """Return whether a concrete HTTP route fully matches method and path."""

    methods = getattr(route, "methods", None)
    path_regex = getattr(route, "path_regex", None)
    return bool(methods and method in methods and path_regex and path_regex.fullmatch(path))
