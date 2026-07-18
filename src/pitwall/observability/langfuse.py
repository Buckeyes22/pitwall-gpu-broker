"""Langfuse trace emission for Pitwall inference requests.

This module provides trace emission for synchronous inference requests.
Traces are emitted per-request when Langfuse environment variables are configured.
Failures in Langfuse emission do not affect inference processing.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, Protocol, runtime_checkable

from pitwall.config import get_settings
from pitwall.security.redaction import redact_text

log = logging.getLogger(__name__)

_client: Any | None = None
_flush_interval: int | None = None
_ALLOWED_EXTRA_METADATA = frozenset({"attempt", "cold_start_ms", "fallback_count", "queue_ms"})


def _get_client() -> Any | None:
    global _client
    settings = get_settings()
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None
    if _client is None:
        try:
            from langfuse import Langfuse

            _client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host or "https://cloud.langfuse.com",
                flush_interval=5,
                flush_at=20,
            )
        except Exception as exc:  # reason: tracing is optional; init failure degrades to no-op
            # Include the reason in the message: ``extra`` fields are invisible
            # under the default formatter, which turns this into an unexplained
            # "langfuse_client_init_failed" (e.g. langfuse not installed).
            log.warning("langfuse_client_init_failed: %s", redact_text(exc))
            return None
    return _client


def reset_client_for_tests() -> None:
    global _client
    _client = None


def _trace_name(capability_name: str) -> str:
    return f"pitwall.inference.{capability_name}"


def _safe_trace_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)[:500]
    return redact_text(value)[:500]


@runtime_checkable
class Trace(Protocol):
    """Active trace handle exposed by Pitwall tracing backends."""

    @property
    def trace_id(self) -> str | None: ...

    def event(self, name: str, **kwargs: Any) -> None: ...

    def finish(
        self,
        *,
        status: str,
        execution_ms: int | None = None,
        error: BaseException | None = None,
        input_bytes: int | None = None,
        output_bytes: int | None = None,
        **extra_metadata: Any,
    ) -> None: ...


@runtime_checkable
class Tracer(Protocol):
    """Tracing backend contract used by inference and API call sites."""

    def start_inference_trace(
        self,
        workload_id: str,
        capability_name: str,
        provider_id: str,
        provider_type: str,
        runpod_endpoint_id: str | None = None,
        cost_estimate_usd: float | None = None,
        input_bytes: int | None = None,
        **extra_tags: Any,
    ) -> Trace: ...

    def emit_inference_trace(
        self,
        workload_id: str,
        capability_name: str,
        provider_id: str,
        provider_type: str,
        runpod_endpoint_id: str | None = None,
        cost_estimate_usd: float | None = None,
        input_bytes: int | None = None,
        output_bytes: int | None = None,
        execution_ms: int | None = None,
        status: str = "success",
        error: BaseException | None = None,
    ) -> str | None: ...


class InferenceTrace:
    def __init__(self, lf_trace: Any | None, metadata: dict[str, Any], started_at: float):
        self._t = lf_trace
        self._metadata = dict(metadata)
        self._started_at = started_at
        self._trace_id: str | None = None
        if lf_trace is not None:
            with contextlib.suppress(Exception):
                self._trace_id = getattr(lf_trace, "id", None) or str(id(lf_trace))

    @property
    def trace_id(self) -> str | None:
        return self._trace_id

    def event(self, name: str, **kwargs: Any) -> None:
        if self._t is None:
            return
        try:
            metadata = {k: _safe_trace_value(v) for k, v in kwargs.items()}
            if hasattr(self._t, "event"):
                self._t.event(name=name, metadata=metadata)
        except Exception as exc:  # reason: tracing must never break the traced operation
            log.warning("langfuse_event_failed", extra={"name": name, "error": str(exc)})

    def finish(
        self,
        *,
        status: str,
        execution_ms: int | None = None,
        error: BaseException | None = None,
        input_bytes: int | None = None,
        output_bytes: int | None = None,
        **extra_metadata: Any,
    ) -> None:
        if self._t is None:
            return

        duration_ms = round((time.perf_counter() - self._started_at) * 1000, 2)
        if execution_ms is not None:
            duration_ms = float(execution_ms)

        metadata: dict[str, Any] = {**self._metadata}
        for k, v in extra_metadata.items():
            if k in _ALLOWED_EXTRA_METADATA:
                metadata[k] = _safe_trace_value(v)

        if input_bytes is not None:
            metadata["input_bytes"] = input_bytes
        if output_bytes is not None:
            metadata["output_bytes"] = output_bytes

        metadata["duration_ms"] = duration_ms
        metadata["status"] = status

        if error is not None:
            metadata["error_type"] = type(error).__name__

        try:
            output: dict[str, Any] = {"status": status}
            if "error_type" in metadata:
                output["error_type"] = metadata["error_type"]
            self._t.update(
                metadata=metadata,
                output=output,
            )
        except Exception as exc:  # reason: tracing must never break the traced operation
            log.warning("langfuse_trace_finish_failed", extra={"error": str(exc)})


class LangfuseTracer:
    def start_inference_trace(
        self,
        workload_id: str,
        capability_name: str,
        provider_id: str,
        provider_type: str,
        runpod_endpoint_id: str | None = None,
        cost_estimate_usd: float | None = None,
        input_bytes: int | None = None,
        **extra_tags: Any,
    ) -> InferenceTrace:
        lf = _get_client()
        if lf is None:
            return InferenceTrace(None, {}, time.perf_counter())

        name = _trace_name(capability_name)
        metadata: dict[str, Any] = {
            "workload_id": workload_id,
            "capability": capability_name,
            "provider_id": provider_id,
            "provider_type": provider_type,
        }

        tags = ["pitwall", capability_name, provider_type]
        if runpod_endpoint_id:
            metadata["runpod_endpoint_id"] = runpod_endpoint_id
        if cost_estimate_usd is not None:
            metadata["cost_estimate_usd"] = cost_estimate_usd
        if input_bytes is not None:
            metadata["input_bytes"] = input_bytes

        for k, v in extra_tags.items():
            if k in _ALLOWED_EXTRA_METADATA:
                metadata[k] = _safe_trace_value(v)

        trace: Any | None = None
        started_at = time.perf_counter()
        try:
            if hasattr(lf, "trace"):
                trace = lf.trace(
                    name=name,
                    tags=tags,
                    metadata=metadata,
                )
            elif hasattr(lf, "start_span"):
                trace = lf.start_span(name=name, metadata=metadata)
        except Exception as exc:  # reason: tracing must never break the traced operation
            log.warning(
                "langfuse_trace_start_failed",
                extra={"workload_id": workload_id, "error": str(exc)},
            )

        return InferenceTrace(trace, metadata, started_at)

    def emit_inference_trace(
        self,
        workload_id: str,
        capability_name: str,
        provider_id: str,
        provider_type: str,
        runpod_endpoint_id: str | None = None,
        cost_estimate_usd: float | None = None,
        input_bytes: int | None = None,
        output_bytes: int | None = None,
        execution_ms: int | None = None,
        status: str = "success",
        error: BaseException | None = None,
    ) -> str | None:
        trace = self.start_inference_trace(
            workload_id=workload_id,
            capability_name=capability_name,
            provider_id=provider_id,
            provider_type=provider_type,
            runpod_endpoint_id=runpod_endpoint_id,
            cost_estimate_usd=cost_estimate_usd,
            input_bytes=input_bytes,
        )
        trace.finish(
            status=status,
            execution_ms=execution_ms,
            error=error,
            input_bytes=input_bytes,
            output_bytes=output_bytes,
        )
        return trace.trace_id


_default_tracer: Tracer = LangfuseTracer()


def get_tracer() -> Tracer:
    return _default_tracer


def start_inference_trace(
    workload_id: str,
    capability_name: str,
    provider_id: str,
    provider_type: str,
    runpod_endpoint_id: str | None = None,
    cost_estimate_usd: float | None = None,
    input_bytes: int | None = None,
    **extra_tags: Any,
) -> Trace:
    return _default_tracer.start_inference_trace(
        workload_id=workload_id,
        capability_name=capability_name,
        provider_id=provider_id,
        provider_type=provider_type,
        runpod_endpoint_id=runpod_endpoint_id,
        cost_estimate_usd=cost_estimate_usd,
        input_bytes=input_bytes,
        **extra_tags,
    )


def emit_inference_trace(
    workload_id: str,
    capability_name: str,
    provider_id: str,
    provider_type: str,
    runpod_endpoint_id: str | None = None,
    cost_estimate_usd: float | None = None,
    input_bytes: int | None = None,
    output_bytes: int | None = None,
    execution_ms: int | None = None,
    status: str = "success",
    error: BaseException | None = None,
) -> str | None:
    return _default_tracer.emit_inference_trace(
        workload_id=workload_id,
        capability_name=capability_name,
        provider_id=provider_id,
        provider_type=provider_type,
        runpod_endpoint_id=runpod_endpoint_id,
        cost_estimate_usd=cost_estimate_usd,
        input_bytes=input_bytes,
        output_bytes=output_bytes,
        execution_ms=execution_ms,
        status=status,
        error=error,
    )


__all__ = [
    "InferenceTrace",
    "LangfuseTracer",
    "Trace",
    "Tracer",
    "emit_inference_trace",
    "get_tracer",
    "reset_client_for_tests",
    "start_inference_trace",
]
