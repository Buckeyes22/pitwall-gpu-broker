"""Entry-point for ``python -m pitwall.db``.

Delegates to :func:`pitwall.db.main`.
"""

from __future__ import annotations

from pitwall.db import main

if __name__ == "__main__":
    raise SystemExit(main())
