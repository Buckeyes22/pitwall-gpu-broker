"""Entry-point for ``python -m pitwall``.

Dispatches to subcommand groups (e.g. ``pitwall-gpu-broker db``).
"""

from __future__ import annotations

from pitwall.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
