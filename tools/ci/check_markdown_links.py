"""Validate repository Markdown links without making pull requests network-dependent."""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

_INLINE_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)]+)\)")
_REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(?P<target>\S+)", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*#*\s*$", re.MULTILINE)
_HTML_ANCHOR = re.compile(r"<(?:a|span)\s+(?:name|id)=[\"'](?P<anchor>[^\"']+)", re.I)
_IGNORED_SCHEMES = {"app", "data", "mailto", "tel"}


def markdown_files(root: Path) -> list[Path]:
    ignored = {".git", ".venv", ".mypy_cache", ".pytest_cache", "htmlcov", "node_modules"}
    return sorted(path for path in root.rglob("*.md") if not ignored.intersection(path.parts))


def link_targets(text: str) -> list[str]:
    targets = [match.group("target").strip() for match in _INLINE_LINK.finditer(text)]
    targets.extend(match.group("target").strip() for match in _REFERENCE_LINK.finditer(text))
    cleaned = []
    for target in targets:
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        cleaned.append(target.split(maxsplit=1)[0])
    return cleaned


def _slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return re.sub(r"\s", "-", text)


def anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    result = {match.group("anchor") for match in _HTML_ANCHOR.finditer(text)}
    seen: Counter[str] = Counter()
    for match in _HEADING.finditer(text):
        base = _slug(match.group("title"))
        if not base:
            continue
        index = seen[base]
        seen[base] += 1
        result.add(base if index == 0 else f"{base}-{index}")
    return result


def check_internal(root: Path, files: list[Path]) -> list[str]:
    failures: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}
    root = root.resolve()
    for source in files:
        for target in link_targets(source.read_text(encoding="utf-8")):
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme in {"http", "https"} or parsed.scheme in _IGNORED_SCHEMES:
                continue
            if parsed.scheme or parsed.netloc:
                failures.append(f"{source.relative_to(root)}: unsupported link {target!r}")
                continue
            raw_path = urllib.parse.unquote(parsed.path)
            if not raw_path:
                destination = source
            elif raw_path.startswith("/"):
                destination = root / raw_path.lstrip("/")
            else:
                destination = source.parent / raw_path
            destination = destination.resolve()
            try:
                destination.relative_to(root)
            except ValueError:
                failures.append(f"{source.relative_to(root)}: link escapes repository: {target!r}")
                continue
            if not destination.exists():
                failures.append(f"{source.relative_to(root)}: missing target {target!r}")
                continue
            fragment = urllib.parse.unquote(parsed.fragment)
            if fragment and destination.suffix.lower() == ".md":
                available = anchor_cache.setdefault(destination, anchors(destination))
                if fragment not in available:
                    failures.append(
                        f"{source.relative_to(root)}: missing anchor #{fragment} in "
                        f"{destination.relative_to(root)}"
                    )
    return failures


def _external_urls(files: list[Path]) -> list[str]:
    return sorted(
        {
            target
            for source in files
            for target in link_targets(source.read_text(encoding="utf-8"))
            if urllib.parse.urlsplit(target).scheme in {"http", "https"}
        }
    )


def _check_external(url: str, *, attempts: int = 3, timeout: float = 15.0) -> str | None:
    request = urllib.request.Request(url, headers={"User-Agent": "pitwall-link-check/1"})
    last_error = "unknown error"
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status < 400:
                    return None
                last_error = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 405, 429}:
                return None
            last_error = f"HTTP {exc.code}"
            if exc.code < 500:
                break
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        if attempt + 1 < attempts:
            time.sleep(2**attempt)
    return f"{url}: {last_error}"


def check_external(files: list[Path]) -> list[str]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = executor.map(_check_external, _external_urls(files))
    return [failure for failure in results if failure is not None]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--external", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    files = markdown_files(root)
    failures = check_internal(root, files)
    if args.external:
        failures.extend(check_external(files))
    if failures:
        for failure in failures:
            print(f"markdown-link-error: {failure}")
        return 1
    mode = "internal and external" if args.external else "internal"
    print(f"markdown links passed: {len(files)} files ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
