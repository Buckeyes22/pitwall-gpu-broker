#!/usr/bin/env python3
"""Minimal stdlib verifier for Pitwall webhook request bytes."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import time


def verify(body: bytes, signature_header: str, secret: str, max_age_s: int = 300) -> bool:
    parts = dict(part.split("=", 1) for part in signature_header.split(",") if "=" in part)
    try:
        timestamp = int(parts["t"])
        received = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(int(time.time()) - timestamp) > max_age_s:
        return False
    message = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--body-file", required=True)
    args = parser.parse_args()
    with open(args.body_file, "rb") as body_file:
        body = body_file.read()
    return 0 if verify(body, args.signature, args.secret) else 1


if __name__ == "__main__":
    raise SystemExit(main())
