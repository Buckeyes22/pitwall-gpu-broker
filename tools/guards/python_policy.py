#!/usr/bin/env python3
"""Pre-commit policy guard for Python source files.

Rejects:
  - ``# type: ignore`` without an error code and ``# reason:``
  - ``# noqa`` without a rule code and ``# reason:``
  - bare ``except:`` (any except without a concrete exception type)
  - ``except Exception`` without a ``# reason:`` in the trailing comment

Uses ``ast`` for exception-handling checks and ``tokenize`` for comment checks.
"""

from __future__ import annotations

import ast
import re
import sys
import tokenize
from pathlib import Path

_TYPE_IGNORE_RE = re.compile(r"#\s*type:\s*ignore\b(.*)")
_NOQA_RE = re.compile(r"#\s*noqa\b(.*)")
_REASON_RE = re.compile(r"#\s*reason:\s*\S")
_EXCEPT_EXCEPTION_RE = re.compile(r"except\s+Exception\s*:")


def _check_comments(source: str, filename: str) -> list[str]:
    errors: list[str] = []
    try:
        tokens = list(tokenize.generate_tokens(iter(source.splitlines(True)).__next__))
    except tokenize.TokenError:
        return errors

    for tok_type, tok_string, (srow, _scol), _end, _line in tokens:
        if tok_type != tokenize.COMMENT:
            continue
        text = tok_string

        m = _TYPE_IGNORE_RE.search(text)
        if m:
            remainder = m.group(1)
            if not remainder.strip():
                errors.append(
                    f"{filename}:{srow}: bare '# type: ignore' — "
                    f"must specify error code and # reason: "
                    f"(e.g. '# type: ignore[arg-type]  # reason: …')"
                )
            elif "# reason:" not in remainder:
                errors.append(
                    f"{filename}:{srow}: '# type: ignore' missing # reason: — "
                    f"add '  # reason: <justification>'"
                )

        m = _NOQA_RE.search(text)
        if m:
            remainder = m.group(1)
            if not remainder.strip():
                errors.append(
                    f"{filename}:{srow}: bare '# noqa' — "
                    f"must specify rule code and # reason: "
                    f"(e.g. '# noqa: F401  # reason: …')"
                )
            elif "# reason:" not in remainder:
                errors.append(
                    f"{filename}:{srow}: '# noqa' missing # reason: — "
                    f"add '  # reason: <justification>'"
                )

    return errors


def _is_exception_type(node: ast.expr) -> bool:
    if isinstance(node, ast.Name) and node.id == "Exception":
        return True
    if isinstance(node, ast.Tuple):
        return any(_is_exception_type(elt) for elt in node.elts)
    return False


def _check_exception_handling(source: str, filename: str) -> list[str]:
    errors: list[str] = []
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return errors

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        if node.type is None:
            errors.append(
                f"{filename}:{node.lineno}: bare 'except:' is forbidden — "
                f"catch a specific exception type"
            )
        elif _is_exception_type(node.type):
            source_lines = source.splitlines()
            end_line = getattr(node, "end_lineno", None) or node.lineno
            has_reason = False
            for ln in range(node.lineno, min(end_line + 1, len(source_lines) + 1)):
                line_text = source_lines[ln - 1]
                comment_part = line_text.split("#", 1)
                if len(comment_part) > 1 and _REASON_RE.search("#" + comment_part[1]):
                    has_reason = True
                    break
            if not has_reason:
                errors.append(
                    f"{filename}:{node.lineno}: 'except Exception' requires "
                    f"a '# reason: <justification>' comment"
                )

    return errors


def check_file(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path}: cannot read: {exc}"]

    errors: list[str] = []
    errors.extend(_check_comments(source, str(path)))
    errors.extend(_check_exception_handling(source, str(path)))
    return errors


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python tools/guards/python_policy.py FILE [FILE …]", file=sys.stderr)
        return 2

    all_errors: list[str] = []
    for filepath in args:
        all_errors.extend(check_file(Path(filepath)))

    for err in all_errors:
        print(err, file=sys.stderr)

    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
