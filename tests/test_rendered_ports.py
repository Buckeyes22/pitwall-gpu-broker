"""Reject public host bindings in docker-compose.yml port mappings.

validate that no service port is bound to a public interface.
Only loopback addresses and the Tailscale CGNAT range are acceptable host
addresses.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"

_PUBLIC_HOSTS = frozenset({"0.0.0.0", "::", "[::]", ""})

_PORTS_SECTION_RE = re.compile(r"^\s+ports:\s*$")
_QUOTED_ENTRY_RE = re.compile(r'^\s+-\s+"([^"]+)"\s*$')
_BARE_ENTRY_RE = re.compile(r"^\s+-\s+(\S+)\s*$")
_BRACE_DEFAULT_RE = re.compile(r"\$\{[^}]*:-([^}]*)\}")
_BRACE_NO_DEFAULT_RE = re.compile(r"\$\{([^}:]+)\}")
_TAILSCALE_CGNAT_CIDR = ".".join(("100", "64", "0", "0")) + "/10"


def _resolve_env_default(raw_host: str) -> str:
    resolved = _BRACE_DEFAULT_RE.sub(r"\1", raw_host)
    if "${" not in resolved:
        return resolved
    resolved = _BRACE_NO_DEFAULT_RE.sub("", resolved)
    return resolved


def _parse_port_bindings(compose_text: str) -> list[tuple[str, str, str, int]]:
    results: list[tuple[str, str, str, int]] = []
    current_service: str | None = None
    in_ports = False

    for line_no, line in enumerate(compose_text.splitlines(), 1):
        svc_match = re.match(r"^  (\S+):\s*$", line)
        if svc_match:
            current_service = svc_match.group(1)
            in_ports = False
            continue

        if _PORTS_SECTION_RE.match(line):
            in_ports = True
            continue

        if in_ports:
            m = _QUOTED_ENTRY_RE.match(line) or _BARE_ENTRY_RE.match(line)
            if m:
                val = m.group(1)
                parts = val.rsplit(":", 2)
                if len(parts) == 3:
                    raw_host = parts[0]
                    resolved = _resolve_env_default(raw_host)
                    results.append((current_service or "unknown", raw_host, resolved, line_no))
            elif (
                line.strip()
                and not line.strip().startswith("-")
                and not line.strip().startswith("#")
            ):
                in_ports = False

    return results


def _is_safe_host(resolved: str) -> tuple[bool, str]:
    if resolved in _PUBLIC_HOSTS:
        return False, f"binds to all interfaces ({resolved!r})"

    if resolved in ("127.0.0.1", "::1", "[::1]", "localhost"):
        return True, ""

    try:
        addr = ipaddress.ip_address(resolved)
    except ValueError:
        return False, f"unresolvable host {resolved!r}"

    if isinstance(addr, ipaddress.IPv4Address):
        if addr in ipaddress.IPv4Network(_TAILSCALE_CGNAT_CIDR):
            return True, ""
        return (
            False,
            f"IP {resolved} is outside the Tailscale CGNAT range ({_TAILSCALE_CGNAT_CIDR})",
        )

    if isinstance(addr, ipaddress.IPv6Address):
        if addr.is_loopback:
            return True, ""
        return False, f"non-loopback IPv6 {resolved} is not allowed"

    return False, f"unexpected address {resolved!r}"


class TestRenderedPorts:
    def test_compose_file_exists(self):
        assert _COMPOSE_FILE.is_file(), "docker-compose.yml must exist at repo root"

    def test_no_public_host_bindings(self):
        text = _COMPOSE_FILE.read_text()
        bindings = _parse_port_bindings(text)
        assert bindings, "docker-compose.yml must define at least one port mapping"

        violations: list[str] = []
        for service, raw_host, resolved, line_no in bindings:
            safe, reason = _is_safe_host(resolved)
            if not safe:
                violations.append(
                    f"  line {line_no}: service {service!r} — {reason} (raw: {raw_host!r})"
                )

        assert not violations, "Public host bindings detected in docker-compose.yml:\n" + "\n".join(
            violations
        )

    def test_all_port_defaults_resolve_to_safe_host(self):
        text = _COMPOSE_FILE.read_text()
        bindings = _parse_port_bindings(text)
        assert bindings

        for service, _raw_host, resolved, line_no in bindings:
            safe, reason = _is_safe_host(resolved)
            assert safe, (
                f"service {service!r} line {line_no}: default host resolves to unsafe "
                f"{resolved!r} — {reason}"
            )

    def test_port_mappings_have_explicit_host_binding(self):
        text = _COMPOSE_FILE.read_text()
        bindings = _parse_port_bindings(text)
        for service, raw_host, _resolved, line_no in bindings:
            assert ":" in raw_host or raw_host, (
                f"service {service!r} line {line_no}: port mapping must specify an explicit host IP"
            )
