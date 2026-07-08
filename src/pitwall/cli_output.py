"""Rich-based CLI output layer for pitwall.

Provides table, panel, and JSON renderers with a unified API.
All commands should use :class:`Output` rather than bare ``print()``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


def _safe_json(value: Any) -> Any:
    """Serialize a value for JSON output, handling Pydantic models and other types."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return value.__dict__
    return value


class Output:
    """Unified CLI output handler supporting Rich and JSON modes."""

    def __init__(
        self,
        json_mode: bool = False,
        *,
        stdout_file: Any | None = None,
        stderr_file: Any | None = None,
    ) -> None:
        self.json_mode = json_mode
        self._stdout = Console(
            file=stdout_file,
            stderr=False,
            markup=False,
            highlight=False,
        )
        self._stderr = Console(
            file=stderr_file,
            stderr=True,
            markup=False,
            highlight=False,
        )
        self._result: dict[str, Any] = {}

    # -- text / rich ---------------------------------------------------------

    def print(self, message: str = "") -> None:
        """Print a plain message (no-op in JSON mode)."""
        if not self.json_mode:
            self._stdout.print(message)

    def print_table(
        self,
        title: str,
        columns: list[str],
        rows: list[list[Any]],
        *,
        caption: str | None = None,
    ) -> None:
        """Render a table or record data for JSON."""
        if self.json_mode:
            key = title.lower().replace(" ", "_").replace("-", "_")
            self._result[key] = [dict(zip(columns, row, strict=False)) for row in rows]
            return
        table = Table(title=title, caption=caption)
        for col in columns:
            table.add_column(col)
        for row in rows:
            table.add_row(*(str(cell) if cell is not None else "" for cell in row))
        self._stdout.print(table)

    def print_panel(
        self,
        content: str,
        *,
        title: str | None = None,
        border_style: str = "green",
    ) -> None:
        """Render a panel or record for JSON."""
        if self.json_mode:
            self._result.setdefault("panels", []).append(
                {"title": title, "content": content, "style": border_style}
            )
            return
        self._stdout.print(Panel(content, title=title, border_style=border_style))

    def print_error(self, message: str) -> None:
        """Render an error panel or record error for JSON."""
        if self.json_mode:
            self._result["error"] = message
            return
        self._stderr.print(Panel(message, title="Error", border_style="red"))

    def print_warning(self, message: str) -> None:
        """Render a warning panel or record warning for JSON."""
        if self.json_mode:
            self._result.setdefault("warnings", []).append(message)
            return
        self._stderr.print(Panel(message, title="Warning", border_style="yellow"))

    def print_success(self, message: str) -> None:
        """Render a success panel or record success for JSON."""
        if self.json_mode:
            self._result["success"] = message
            return
        self._stdout.print(Panel(message, title="Success", border_style="green"))

    # -- json ----------------------------------------------------------------

    def set_json(self, data: dict[str, Any]) -> None:
        """Replace the entire JSON result object."""
        self._result = data

    def add_json(self, key: str, value: Any) -> None:
        """Add a key to the JSON result object."""
        self._result[key] = value

    def emit(self) -> None:
        """Emit collected JSON if in JSON mode."""
        if self.json_mode:
            json.dump(self._result, sys.stdout, indent=2)
            sys.stdout.write("\n")


def add_json_argument(parser: Any) -> None:
    """Add ``--json`` argument to an argparse parser."""
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of human-friendly formatted text.",
    )


def json_mode(args: Any) -> bool:
    """Return whether JSON mode is enabled from parsed args."""
    return getattr(args, "json", False)
