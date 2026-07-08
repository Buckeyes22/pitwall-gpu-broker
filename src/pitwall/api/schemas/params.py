"""Shared FastAPI parameter validators for API boundary hardening."""

from __future__ import annotations

from typing import Annotated

from fastapi import Path, Query

PathId = Annotated[str, Path(pattern=r"^[^\x00]+$")]
OptionalStrQuery = Annotated[str | None, Query(pattern=r"^[^\x00]+$")]

__all__ = [
    "OptionalStrQuery",
    "PathId",
]
