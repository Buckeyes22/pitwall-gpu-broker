"""Tests proving pitwall.kill_log and pitwall.config_audit are never archived or deleted.

Protect audit tables — archive policy preserves audit mutation trail
invariant indefinitely.

These tests verify that the retention/archive pipeline never touches audit tables.
"""
