"""Tests for pitwall.observability.langfuse."""

from __future__ import annotations

import sys
import types

import pytest


def _reset_caches() -> None:
    from pitwall import config as _cfg

    _cfg.get_settings.cache_clear()


class TestLangfuseNoopWhenUnconfigured:
    def test_noop_when_host_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.observability.langfuse import InferenceTrace, reset_client_for_tests

        _reset_caches()
        reset_client_for_tests()

        class _S:
            langfuse_host = ""
            langfuse_public_key = ""
            langfuse_secret_key = ""

        monkeypatch.setattr("pitwall.observability.langfuse.get_settings", lambda: _S())
        trace = InferenceTrace(None, {}, 0.0)
        assert isinstance(trace, InferenceTrace)
        trace.finish(status="success")

    def test_noop_when_public_key_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.observability.langfuse import InferenceTrace, reset_client_for_tests

        _reset_caches()
        reset_client_for_tests()

        class _S:
            langfuse_host = "http://x"
            langfuse_public_key = ""
            langfuse_secret_key = "sk"

        monkeypatch.setattr("pitwall.observability.langfuse.get_settings", lambda: _S())
        trace = InferenceTrace(None, {}, 0.0)
        trace.finish(status="success")

    def test_noop_when_secret_key_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.observability.langfuse import InferenceTrace, reset_client_for_tests

        _reset_caches()
        reset_client_for_tests()

        class _S:
            langfuse_host = "http://x"
            langfuse_public_key = "pk"
            langfuse_secret_key = ""

        monkeypatch.setattr("pitwall.observability.langfuse.get_settings", lambda: _S())
        trace = InferenceTrace(None, {}, 0.0)
        trace.finish(status="success")


class TestStartInferenceTrace:
    def test_returns_inference_trace_when_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pitwall.observability.langfuse import (
            InferenceTrace,
            reset_client_for_tests,
            start_inference_trace,
        )

        _reset_caches()
        reset_client_for_tests()

        class _S:
            langfuse_host = ""
            langfuse_public_key = ""
            langfuse_secret_key = ""

        monkeypatch.setattr("pitwall.observability.langfuse.get_settings", lambda: _S())
        trace = start_inference_trace(
            workload_id="wkl_123",
            capability_name="embedding.bge-m3",
            provider_id="prov_1",
            provider_type="serverless_lb",
        )
        assert isinstance(trace, InferenceTrace)
        assert trace.trace_id is None


class TestTracerProtocol:
    def test_langfuse_tracer_matches_protocol(self) -> None:
        from pitwall.observability.langfuse import (
            InferenceTrace,
            LangfuseTracer,
            Trace,
            Tracer,
            get_tracer,
        )

        assert isinstance(InferenceTrace(None, {}, 0.0), Trace)
        assert isinstance(LangfuseTracer(), Tracer)
        assert isinstance(get_tracer(), Tracer)


class TestEmitInferenceTrace:
    def test_returns_none_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.observability.langfuse import (
            emit_inference_trace,
            reset_client_for_tests,
        )

        _reset_caches()
        reset_client_for_tests()

        class _S:
            langfuse_host = ""
            langfuse_public_key = ""
            langfuse_secret_key = ""

        monkeypatch.setattr("pitwall.observability.langfuse.get_settings", lambda: _S())
        trace_id = emit_inference_trace(
            workload_id="wkl_123",
            capability_name="embedding.bge-m3",
            provider_id="prov_1",
            provider_type="serverless_lb",
            runpod_endpoint_id="endpoint_1",
            cost_estimate_usd=0.001,
            input_bytes=100,
            output_bytes=200,
            execution_ms=50,
            status="success",
        )
        assert trace_id is None


class TestLangfuseClientSingleton:
    def test_singleton_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pitwall.observability.langfuse as module

        _reset_caches()
        module.reset_client_for_tests()

        class _S:
            langfuse_host = "http://langfuse.test:3101"
            langfuse_public_key = "pk_test"
            langfuse_secret_key = "sk_test"

        class DummyLangfuse:
            def __init__(self, **_: object) -> None:
                pass

        dummy_module = types.ModuleType("langfuse")
        dummy_module.Langfuse = DummyLangfuse

        monkeypatch.setattr("pitwall.observability.langfuse.get_settings", lambda: _S())
        module.reset_client_for_tests()
        monkeypatch.setitem(sys.modules, "langfuse", dummy_module)

        a = module._get_client()
        b = module._get_client()
        assert a is b


class TestTraceName:
    def test_trace_name_format(self) -> None:
        from pitwall.observability.langfuse import _trace_name

        assert _trace_name("embedding.bge-m3") == "pitwall.inference.embedding.bge-m3"
        assert _trace_name("llm.qwen3-32b") == "pitwall.inference.llm.qwen3-32b"


class TestSafeTraceValue:
    def test_passthrough_primitives(self) -> None:
        from pitwall.observability.langfuse import _safe_trace_value

        assert _safe_trace_value(None) is None
        assert _safe_trace_value(True) is True
        assert _safe_trace_value(False) is False
        assert _safe_trace_value(42) == 42
        assert _safe_trace_value(3.14) == 3.14

    def test_truncates_long_strings(self) -> None:
        from pitwall.observability.langfuse import _safe_trace_value

        long_string = "x" * 1000
        result = _safe_trace_value(long_string)
        assert len(result) == 500

    def test_converts_other_types(self) -> None:
        from pitwall.observability.langfuse import _safe_trace_value

        class Custom:
            pass

        obj = Custom()
        result = _safe_trace_value(obj)
        assert result == repr(obj)[:500]
