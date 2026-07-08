"""Tests for the Rich-based CLI output layer.

Covers:
- Plain text rendering in non-terminal mode
- Table rendering and JSON fallback
- Panel rendering and JSON fallback
- Error/warning/success rendering
- JSON emission and accumulation
- Hermetic operation (no network, no DB)
"""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

from pitwall.cli_output import Output, _safe_json, add_json_argument, json_mode


class TestOutputPlainText:
    """Plain-text output when not in JSON mode."""

    def test_print_outputs_to_stdout(self) -> None:
        buf = StringIO()
        out = Output(json_mode=False, stdout_file=buf)
        out.print("hello world")
        assert "hello world" in buf.getvalue()

    def test_print_empty_line(self) -> None:
        buf = StringIO()
        out = Output(json_mode=False, stdout_file=buf)
        out.print()
        # Console.print("") writes a newline
        assert "\n" in buf.getvalue()


class TestOutputJsonMode:
    """JSON output behaviour."""

    def test_json_mode_does_not_emit_rich(self) -> None:
        buf = StringIO()
        out = Output(json_mode=True, stdout_file=buf)
        out.print("this is hidden")
        assert buf.getvalue() == ""

    def test_emit_empty_object_by_default(self) -> None:
        out = Output(json_mode=True)
        buf = StringIO()
        with patch("sys.stdout", buf):
            out.emit()
        assert json.loads(buf.getvalue()) == {}

    def test_add_json_key_value(self) -> None:
        out = Output(json_mode=True)
        out.add_json("key", "value")
        buf = StringIO()
        with patch("sys.stdout", buf):
            out.emit()
        assert json.loads(buf.getvalue()) == {"key": "value"}

    def test_set_json_replaces_whole_object(self) -> None:
        out = Output(json_mode=True)
        out.add_json("old", 1)
        out.set_json({"new": 2})
        buf = StringIO()
        with patch("sys.stdout", buf):
            out.emit()
        assert json.loads(buf.getvalue()) == {"new": 2}

    def test_table_in_json_mode_collects_rows(self) -> None:
        out = Output(json_mode=True)
        out.print_table("Test Table", ["a", "b"], [[1, 2], [3, 4]])
        buf = StringIO()
        with patch("sys.stdout", buf):
            out.emit()
        data = json.loads(buf.getvalue())
        assert data == {
            "test_table": [
                {"a": 1, "b": 2},
                {"a": 3, "b": 4},
            ]
        }

    def test_panel_in_json_mode_collects_panels(self) -> None:
        out = Output(json_mode=True)
        out.print_panel("content", title="Title", border_style="blue")
        buf = StringIO()
        with patch("sys.stdout", buf):
            out.emit()
        data = json.loads(buf.getvalue())
        assert data == {"panels": [{"title": "Title", "content": "content", "style": "blue"}]}

    def test_error_in_json_mode_sets_error_key(self) -> None:
        out = Output(json_mode=True)
        out.print_error("something broke")
        buf = StringIO()
        with patch("sys.stdout", buf):
            out.emit()
        assert json.loads(buf.getvalue()) == {"error": "something broke"}

    def test_warning_in_json_mode_collects_warnings(self) -> None:
        out = Output(json_mode=True)
        out.print_warning("warn 1")
        out.print_warning("warn 2")
        buf = StringIO()
        with patch("sys.stdout", buf):
            out.emit()
        assert json.loads(buf.getvalue()) == {"warnings": ["warn 1", "warn 2"]}

    def test_success_in_json_mode_sets_success_key(self) -> None:
        out = Output(json_mode=True)
        out.print_success("done")
        buf = StringIO()
        with patch("sys.stdout", buf):
            out.emit()
        assert json.loads(buf.getvalue()) == {"success": "done"}


class TestOutputTableRendering:
    """Rich table rendering in non-JSON mode."""

    def test_table_renders_with_title(self) -> None:
        buf = StringIO()
        out = Output(json_mode=False, stdout_file=buf)
        out.print_table("My Table", ["x", "y"], [["a", "b"]])
        text = buf.getvalue()
        assert "My Table" in text
        assert "a" in text
        assert "b" in text

    def test_table_handles_none_cells(self) -> None:
        buf = StringIO()
        out = Output(json_mode=False, stdout_file=buf)
        out.print_table("T", ["c"], [[None]])
        text = buf.getvalue()
        assert text  # should not crash


class TestOutputPanelRendering:
    """Rich panel rendering in non-JSON mode."""

    def test_panel_renders_title_and_content(self) -> None:
        buf = StringIO()
        out = Output(json_mode=False, stdout_file=buf)
        out.print_panel("body text", title="Panel Title", border_style="red")
        text = buf.getvalue()
        assert "Panel Title" in text
        assert "body text" in text


class TestOutputErrorRendering:
    """Error/warning/success panel rendering."""

    def test_error_goes_to_stderr(self) -> None:
        buf = StringIO()
        out = Output(json_mode=False, stderr_file=buf)
        out.print_error("fail")
        assert "fail" in buf.getvalue()

    def test_warning_goes_to_stderr(self) -> None:
        buf = StringIO()
        out = Output(json_mode=False, stderr_file=buf)
        out.print_warning("watch out")
        assert "watch out" in buf.getvalue()

    def test_success_goes_to_stdout(self) -> None:
        buf = StringIO()
        out = Output(json_mode=False, stdout_file=buf)
        out.print_success("great")
        assert "great" in buf.getvalue()


class TestAddJsonArgument:
    """``add_json_argument`` helper."""

    def test_adds_json_flag_default_false(self) -> None:
        import argparse

        parser = argparse.ArgumentParser()
        add_json_argument(parser)
        ns = parser.parse_args([])
        assert ns.json is False

    def test_json_flag_explicit_true(self) -> None:
        import argparse

        parser = argparse.ArgumentParser()
        add_json_argument(parser)
        ns = parser.parse_args(["--json"])
        assert ns.json is True


class TestJsonModeHelper:
    """``json_mode`` helper."""

    def test_returns_false_when_missing(self) -> None:
        class Dummy:
            pass

        assert json_mode(Dummy()) is False

    def test_returns_value_when_present(self) -> None:
        class Dummy:
            json = True

        assert json_mode(Dummy()) is True


class TestSafeJson:
    """``_safe_json`` helper."""

    def test_pydantic_model_dump(self) -> None:
        from pydantic import BaseModel

        class M(BaseModel):
            x: int

        assert _safe_json(M(x=5)) == {"x": 5}

    def test_fallback_to_dict(self) -> None:
        class Obj:
            def __init__(self) -> None:
                self.y = 2

        assert _safe_json(Obj()) == {"y": 2}

    def test_primitive_pass_through(self) -> None:
        assert _safe_json(42) == 42
        assert _safe_json("hi") == "hi"
