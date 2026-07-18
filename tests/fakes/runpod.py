"""RunPod response builders and shared fakes for hermetic tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from pitwall.core.enums import CapabilitySource, ProviderType
from pitwall.core.models import Provider


@dataclass(frozen=True)
class RunPodRestCall:
    method: str
    path: str
    json_body: dict[str, Any] | None
    params: dict[str, Any] | None
    timeout_s: float


RunPodRestHandler = Callable[[RunPodRestCall], Any]
RunPodRestResponse = Any | BaseException | RunPodRestHandler
ServerlessHandler = Callable[[httpx.Request], httpx.Response]
ServerlessResponse = httpx.Response | ServerlessHandler


def _response(
    status_code: int,
    *,
    json: Any | None = None,
    headers: Mapping[str, str] | None = None,
    request: httpx.Request | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=json,
        headers=dict(headers or {}),
        request=request,
    )


def _response_with_request(
    response: httpx.Response,
    request: httpx.Request,
) -> httpx.Response:
    try:
        _ = response.request
    except RuntimeError:
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
            extensions=response.extensions,
        )
    return response


@dataclass
class RunPodResponseFactory:
    model: str = "pitwall-test-model"

    def chat_completion(
        self,
        content: str = "OK",
        *,
        status_code: int = 200,
        model: str | None = None,
        prompt_tokens: int = 1,
        completion_tokens: int = 1,
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
    ) -> httpx.Response:
        return _response(
            status_code,
            json={
                "id": "chatcmpl-test",
                "model": model or self.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
            headers=headers,
            request=request,
        )

    def queue_status(
        self,
        status: str = "COMPLETED",
        *,
        job_id: str = "job-test",
        output: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
    ) -> httpx.Response:
        response_output = {"ok": True} if output is None else dict(output)
        return _response(
            200,
            json={
                "id": job_id,
                "status": status,
                "output": response_output,
            },
            headers=headers,
            request=request,
        )

    def queue_run(
        self,
        *,
        job_id: str = "job-test",
        status: str = "IN_QUEUE",
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
    ) -> httpx.Response:
        return _response(
            200,
            json={"id": job_id, "status": status},
            headers=headers,
            request=request,
        )

    def queue_failed(
        self,
        *,
        job_id: str = "job-test",
        error: str = "worker failed",
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
    ) -> httpx.Response:
        return _response(
            200,
            json={"id": job_id, "status": "FAILED", "error": error},
            headers=headers,
            request=request,
        )

    def embedding_payload(
        self,
        *,
        count: int = 1,
        dense_dim: int = 3,
        sparse_token_id: int = 42,
        include_dense: bool = True,
        include_sparse: bool = True,
        include_colbert: bool = False,
        model: str = "BAAI/bge-m3",
    ) -> dict[str, Any]:
        """Return a minimal BGE-M3-compatible embedding payload."""
        payload: dict[str, Any] = {"model": model}
        if include_dense:
            payload["dense"] = [
                [round((index + offset + 1) / 100, 4) for offset in range(dense_dim)]
                for index in range(count)
            ]
        if include_sparse:
            payload["sparse"] = [
                {sparse_token_id + index: round(0.5 + (index / 100), 4)} for index in range(count)
            ]
        if include_colbert:
            payload["colbert"] = [
                [[round((index + offset + 1) / 1000, 4) for offset in range(dense_dim)]]
                for index in range(count)
            ]
        return payload

    def embedding_response(
        self,
        *,
        count: int = 1,
        dense_dim: int = 3,
        include_dense: bool = True,
        include_sparse: bool = True,
        include_colbert: bool = False,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
    ) -> httpx.Response:
        return _response(
            status_code,
            json=self.embedding_payload(
                count=count,
                dense_dim=dense_dim,
                include_dense=include_dense,
                include_sparse=include_sparse,
                include_colbert=include_colbert,
            ),
            headers=headers,
            request=request,
        )

    def rate_limited(
        self,
        *,
        retry_after: str | int | None = "1",
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
    ) -> httpx.Response:
        response_headers = dict(headers or {})
        if retry_after is not None:
            response_headers["Retry-After"] = str(retry_after)
        return _response(
            429,
            json={"error": "rate_limited"},
            headers=response_headers,
            request=request,
        )

    def health(
        self,
        *,
        status_code: int = 200,
        status: str = "ready",
        workers: int = 1,
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
    ) -> httpx.Response:
        return _response(
            status_code,
            json={"status": status, "workers": workers},
            headers=headers,
            request=request,
        )

    def pod(
        self,
        *,
        pod_id: str = "pod-test",
        desired_status: str = "RUNNING",
        runtime: Mapping[str, Any] | None = None,
        gpu_type_id: str = "NVIDIA L4",
        cost_per_hr: float = 0.44,
        image_name: str = "ghcr.io/example/pitwall-worker:test",
    ) -> dict[str, Any]:
        if runtime is not None:
            pod_runtime = dict(runtime)
        elif desired_status == "RUNNING":
            pod_runtime = self.running_runtime()
        else:
            pod_runtime = None

        return {
            "id": pod_id,
            "desiredStatus": desired_status,
            "runtime": pod_runtime,
            "gpuTypeId": gpu_type_id,
            "costPerHr": cost_per_hr,
            "imageName": image_name,
        }

    def pod_response(
        self,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
        **pod_kwargs: Any,
    ) -> httpx.Response:
        return _response(
            status_code,
            json=self.pod(**pod_kwargs),
            headers=headers,
            request=request,
        )

    def pods_response(
        self,
        pods: list[Mapping[str, Any]] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
    ) -> httpx.Response:
        return _response(
            200,
            json=[self.pod()] if pods is None else list(pods),
            headers=headers,
            request=request,
        )

    def delete_pod_response(
        self,
        *,
        pod_id: str = "pod-test",
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
    ) -> httpx.Response:
        return _response(
            200,
            json={"id": pod_id, "deleted": True},
            headers=headers,
            request=request,
        )

    def capacity_error(
        self,
        *,
        status_code: int = 409,
        message: str = "There are no longer any instances available.",
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
    ) -> httpx.Response:
        return _response(
            status_code,
            json={"error": message},
            headers=headers,
            request=request,
        )

    def endpoint(
        self,
        *,
        endpoint_id: str = "ep-test",
        name: str = "test-endpoint",
        workers_min: int = 0,
        workers_max: int = 3,
        idle_timeout: int = 60,
        gpu_type_id: str | None = "NVIDIA L4",
        flashboot: bool = False,
        template_id: str | None = "tmpl-abc",
        created_at: str = "2026-05-28T12:00:00Z",
    ) -> dict[str, Any]:
        scaling: dict[str, Any] = {
            "workersMin": workers_min,
            "workersMax": workers_max,
            "idleTimeout": idle_timeout,
        }
        if gpu_type_id is not None:
            scaling["gpuTypeId"] = gpu_type_id
        if flashboot:
            scaling["flashboot"] = True
        result: dict[str, Any] = {
            "id": endpoint_id,
            "name": name,
            "scaling": scaling,
            "templateId": template_id,
            "createdAt": created_at,
        }
        return result

    def endpoint_response(
        self,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        request: httpx.Request | None = None,
        **endpoint_kwargs: Any,
    ) -> httpx.Response:
        return _response(
            status_code,
            json=self.endpoint(**endpoint_kwargs),
            headers=headers,
            request=request,
        )

    def running_runtime(self) -> dict[str, Any]:
        return {
            "ports": [{"ip": "127.0.0.1", "privatePort": 8000}],
            "uptimeInSeconds": 12,
        }


@dataclass
class RunPodRestFake:
    """Callable fake for ``pitwall.runpod_client.pods._rest_request``."""

    factory: RunPodResponseFactory = field(default_factory=RunPodResponseFactory)
    calls: list[RunPodRestCall] = field(default_factory=list)
    _routes: dict[tuple[str, str], list[RunPodRestResponse]] = field(default_factory=dict)

    def add(
        self,
        method: str,
        path: str,
        *responses: RunPodRestResponse,
    ) -> None:
        if not responses:
            raise ValueError("at least one response is required")
        route = self._routes.setdefault(self._key(method, path), [])
        route.extend(responses)

    def add_pod_lifecycle(
        self,
        *,
        created: Mapping[str, Any] | None = None,
        states: list[Mapping[str, Any]] | None = None,
        pod_id: str = "pod-test",
    ) -> None:
        created_pod = dict(created or self.factory.pod(pod_id=pod_id))
        self.add("POST", "pods", created_pod)
        if states is not None:
            self.add("GET", f"pods/{pod_id}", *(dict(state) for state in states))
        self.add("DELETE", f"pods/{pod_id}", {})

    def __call__(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_s: float = 60.0,
    ) -> Any:
        call = RunPodRestCall(
            method=method.upper(),
            path=path.lstrip("/"),
            json_body=json_body,
            params=params,
            timeout_s=timeout_s,
        )
        self.calls.append(call)
        response = self._next_response(call.method, call.path)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(call)
        return response

    @property
    def deleted_pod_ids(self) -> list[str]:
        return [
            call.path.removeprefix("pods/")
            for call in self.calls
            if call.method == "DELETE" and call.path.startswith("pods/")
        ]

    @staticmethod
    def _key(method: str, path: str) -> tuple[str, str]:
        return method.upper(), path.lstrip("/")

    def _next_response(self, method: str, path: str) -> RunPodRestResponse:
        key = self._key(method, path)
        responses = self._routes.get(key)
        if not responses:
            raise AssertionError(f"unexpected REST call {method} {path}")
        return responses.pop(0)


@dataclass
class RunPodServerlessFake:
    """Sequential ``httpx.MockTransport`` fake for RunPod Serverless clients."""

    factory: RunPodResponseFactory = field(default_factory=RunPodResponseFactory)
    responses: list[ServerlessResponse] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def add_response(self, response: httpx.Response) -> None:
        self.responses.append(response)

    def add_handler(self, handler: ServerlessHandler) -> None:
        self.responses.append(handler)

    def add_chat_completion(
        self,
        content: str = "ok",
        *,
        status_code: int = 200,
        model: str | None = None,
        prompt_tokens: int = 1,
        completion_tokens: int = 1,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.add_handler(
            lambda request: self.factory.chat_completion(
                content,
                status_code=status_code,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                headers=headers,
                request=request,
            )
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        response = self.responses.pop(0)
        if callable(response):
            return response(request)
        return _response_with_request(response, request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@dataclass
class QueuedJob:
    """Represents the state of a queue job within the fake."""

    job_id: str
    status: str = "IN_QUEUE"
    output: dict[str, Any] | None = None
    error: str | None = None
    webhook: str | None = None
    input: dict[str, Any] | None = None


@dataclass
class RunPodQueueFake:
    """Sequential ``httpx.MockTransport`` fake for RunPod queue-based Serverless API.

    Handles:
    - POST /v2/{endpoint_id}/run  — async job submission
    - POST /v2/{endpoint_id}/runsync — synchronous job submission
    - GET  /v2/{endpoint_id}/status/{job_id} — job status
    - POST /v2/{endpoint_id}/cancel/{job_id} — job cancellation

    Supports deterministic testing with pre-registered responses and
    controllable webhook delivery for async jobs.

    Example::

        from tests.fakes.runpod import RunPodQueueFake
        import httpx

        fake = RunPodQueueFake()
        fake.add_run_response(job_id="job-1", status="IN_QUEUE")
        fake.add_status_response(job_id="job-1", status="COMPLETED", output={"result": 42})

        client = QueueClient(api_key="test", transport=fake.transport())
        result = await client.run("ep-123", input={"prompt": "hello"})
        assert result.id == "job-1"
        assert result.status == "IN_QUEUE"

        status = await client.status("ep-123", "job-1")
        assert status.status == "COMPLETED"
        assert status.output == {"result": 42}

    For webhook testing::

        fake = RunPodQueueFake()
        fake.add_run_response(job_id="job-2", status="IN_QUEUE", webhook="https://example.com/hook")

        # Submit job with webhook
        result = await client.run("ep-123", input={"x": 1}, webhook="https://example.com/hook")

        # Retrieve captured webhooks for later delivery
        webhooks = fake.get_webhooks()
        assert len(webhooks) == 1
        assert webhooks[0]["job_id"] == "job-2"
        assert webhooks[0]["webhook_url"] == "https://example.com/hook"

        # Deliver the webhook manually when ready
        await fake.deliver_webhook("job-2", status="COMPLETED", output={"ok": True})
    """

    factory: RunPodResponseFactory = field(default_factory=RunPodResponseFactory)
    responses: list[ServerlessResponse] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    _jobs: dict[str, QueuedJob] = field(default_factory=dict)
    _webhooks: list[dict[str, Any]] = field(default_factory=list)
    _next_job_id: int = 0

    def _next_id(self) -> str:
        self._next_job_id += 1
        return f"fake-job-{self._next_job_id}"

    def add_run_response(
        self,
        job_id: str | None = None,
        status: str = "IN_QUEUE",
        *,
        webhook: str | None = None,
    ) -> None:
        resolved_id = job_id or self._next_id()
        self.add_handler(self._make_run_handler(resolved_id, status, webhook))

    def _make_run_handler(self, job_id: str, status: str, webhook: str | None) -> ServerlessHandler:
        def handler(request: httpx.Request) -> httpx.Response:
            return self._build_run_response(request, job_id, status, webhook)

        return handler

    def _build_run_response(
        self,
        request: httpx.Request,
        job_id: str,
        status: str,
        webhook: str | None,
    ) -> httpx.Response:
        body = request.read()
        import json

        input_data: dict[str, Any] = {}
        try:
            parsed = json.loads(body) if body else {}
            input_data = parsed.get("input", {})
            if webhook is None:
                webhook = parsed.get("webhook")
        except Exception:  # reason: fake tolerates malformed request bodies like the real API
            pass

        self._jobs[job_id] = QueuedJob(
            job_id=job_id,
            status=status,
            webhook=webhook,
            input=input_data,
        )

        if webhook:
            self._webhooks.append(
                {
                    "job_id": job_id,
                    "webhook_url": webhook,
                    "status": status,
                    "input": input_data,
                }
            )

        return self.factory.queue_run(
            job_id=job_id,
            status=status,
            request=request,
        )

    def add_runsync_response(
        self,
        job_id: str | None = None,
        status: str = "COMPLETED",
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        resolved_id = job_id or self._next_id()
        self.add_handler(self._make_runsync_handler(resolved_id, status, output, error))

    def _make_runsync_handler(
        self,
        job_id: str,
        status: str,
        output: dict[str, Any] | None,
        error: str | None,
    ) -> ServerlessHandler:
        def handler(request: httpx.Request) -> httpx.Response:
            return self._build_runsync_response(request, job_id, status, output, error)

        return handler

    def _build_runsync_response(
        self,
        request: httpx.Request,
        job_id: str,
        status: str,
        output: dict[str, Any] | None,
        error: str | None,
    ) -> httpx.Response:
        self._jobs[job_id] = QueuedJob(
            job_id=job_id,
            status=status,
            output=output,
            error=error,
        )
        if error:
            return self.factory.queue_failed(
                job_id=job_id,
                error=error,
                request=request,
            )
        return self.factory.queue_status(
            job_id=job_id,
            status=status,
            output=output,
            request=request,
        )

    def add_status_response(
        self,
        job_id: str,
        status: str = "COMPLETED",
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.add_handler(self._make_status_handler(job_id, status, output, error))

    def _make_status_handler(
        self,
        job_id: str,
        status: str,
        output: dict[str, Any] | None,
        error: str | None,
    ) -> ServerlessHandler:
        def handler(request: httpx.Request) -> httpx.Response:
            return self._build_status_response(request, job_id, status, output, error)

        return handler

    def _build_status_response(
        self,
        request: httpx.Request,
        job_id: str,
        status: str,
        output: dict[str, Any] | None,
        error: str | None,
    ) -> httpx.Response:
        if job_id in self._jobs:
            self._jobs[job_id].status = status
            self._jobs[job_id].output = output
            self._jobs[job_id].error = error
        else:
            self._jobs[job_id] = QueuedJob(
                job_id=job_id,
                status=status,
                output=output,
                error=error,
            )
        if error:
            return self.factory.queue_failed(job_id=job_id, error=error, request=request)
        return self.factory.queue_status(
            job_id=job_id,
            status=status,
            output=output,
            request=request,
        )

    def add_cancel_response(
        self,
        job_id: str,
        cancelled: bool = True,
    ) -> None:
        self.add_handler(self._make_cancel_handler(job_id, cancelled))

    def _make_cancel_handler(self, job_id: str, cancelled: bool) -> ServerlessHandler:
        def handler(request: httpx.Request) -> httpx.Response:
            return self._build_cancel_response(request, job_id, cancelled)

        return handler

    def _build_cancel_response(
        self,
        request: httpx.Request,
        job_id: str,
        cancelled: bool,
    ) -> httpx.Response:
        if job_id in self._jobs:
            self._jobs[job_id].status = "CANCELLED"
        return _response(
            200,
            json={"cancelled": cancelled},
            request=request,
        )

    def add_response(self, response: httpx.Response) -> None:
        self.responses.append(response)

    def add_handler(self, handler: ServerlessHandler) -> None:
        self.responses.append(handler)

    def get_webhooks(self) -> list[dict[str, Any]]:
        """Return list of captured webhook payloads.

        Each entry contains ``job_id``, ``webhook_url``, ``status``, and ``input``.
        """
        return list(self._webhooks)

    def get_job(self, job_id: str) -> QueuedJob | None:
        """Return the current state of a job, or None if not found."""
        return self._jobs.get(job_id)

    def set_job_state(
        self,
        job_id: str,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Pre-set a job state for subsequent status calls."""
        if job_id in self._jobs:
            self._jobs[job_id].status = status
            self._jobs[job_id].output = output
            self._jobs[job_id].error = error
        else:
            self._jobs[job_id] = QueuedJob(
                job_id=job_id,
                status=status,
                output=output,
                error=error,
            )

    async def deliver_webhook(
        self,
        job_id: str,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        """Deliver a webhook for a job using the registered webhook URL.

        Args:
            job_id: The job ID to deliver webhook for.
            status: The status to send in the webhook payload.
            output: Optional output data.
            error: Optional error message.
            http_client: Optional httpx client to use for delivery.

        Returns:
            The HTTP response from the webhook delivery.

        Raises:
            AssertionError: If no webhook URL is registered for the job.
        """
        job = self._jobs.get(job_id)
        if not job or not job.webhook:
            raise AssertionError(f"no webhook registered for job {job_id}")

        payload: dict[str, Any] = {
            "id": job_id,
            "status": status,
        }
        if output is not None:
            payload["output"] = output
        if error is not None:
            payload["error"] = error

        client = http_client or httpx.AsyncClient()
        try:
            response = await client.post(job.webhook, json=payload)
            return response
        finally:
            if http_client is None:
                await client.aclose()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        response = self.responses.pop(0)
        if callable(response):
            return response(request)
        return _response_with_request(response, request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@dataclass
class RunPodLBFake:
    """Sequential ``httpx.MockTransport`` fake for RunPod LB custom HTTP."""

    factory: RunPodResponseFactory = field(default_factory=RunPodResponseFactory)
    responses: list[ServerlessResponse] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def add_response(self, response: httpx.Response) -> None:
        self.responses.append(response)

    def add_handler(self, handler: ServerlessHandler) -> None:
        self.responses.append(handler)

    def add_embedding(
        self,
        *,
        count: int = 1,
        dense_dim: int = 3,
        include_dense: bool = True,
        include_sparse: bool = True,
        include_colbert: bool = False,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.add_handler(
            lambda request: self.factory.embedding_response(
                count=count,
                dense_dim=dense_dim,
                include_dense=include_dense,
                include_sparse=include_sparse,
                include_colbert=include_colbert,
                status_code=status_code,
                headers=headers,
                request=request,
            )
        )

    def add_health(
        self,
        *,
        status_code: int = 200,
        status: str = "ready",
        workers: int = 1,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.add_handler(
            lambda request: self.factory.health(
                status_code=status_code,
                status=status,
                workers=workers,
                headers=headers,
                request=request,
            )
        )

    def add_rate_limited(
        self,
        *,
        retry_after: str | int | None = "1",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.add_handler(
            lambda request: self.factory.rate_limited(
                retry_after=retry_after,
                headers=headers,
                request=request,
            )
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        response = self.responses.pop(0)
        if callable(response):
            return response(request)
        return _response_with_request(response, request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@dataclass
class FakePodStateMachine:
    pod_id: str = "pod-test"
    states: list[str] = field(default_factory=lambda: ["PENDING", "RUNNING"])
    index: int = 0

    def snapshot(self) -> dict[str, Any]:
        state = self.states[self.index]
        return {
            "id": self.pod_id,
            "desiredStatus": state,
            "runtime": (
                {"ports": [{"ip": "127.0.0.1", "privatePort": 8000}]}
                if state == "RUNNING"
                else None
            ),
        }

    def advance(self) -> dict[str, Any]:
        if self.index < len(self.states) - 1:
            self.index += 1
        return self.snapshot()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=self.snapshot(), request=request)
        return httpx.Response(200, json=self.advance(), request=request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeRunPodTemplateSdk:
    """Lightweight SDK stub that records ``create_template`` kwargs."""

    def __init__(
        self,
        *,
        template_id: str = "template-1",
        response: Mapping[str, str] | None = None,
    ) -> None:
        self.template_id = template_id
        self.response = dict(response) if response is not None else None
        self.create_template_kwargs: dict[str, Any] | None = None
        self.create_template_calls: list[dict[str, Any]] = []

    def create_template(self, **kwargs: Any) -> dict[str, str]:
        self.create_template_kwargs = dict(kwargs)
        self.create_template_calls.append(dict(kwargs))
        response = self.response if self.response is not None else {"id": self.template_id}
        return dict(response)


@dataclass
class RunPodTemplateFake:
    """Shared fake for ``templates.ensure_template`` cache + SDK interactions.

    Attributes:
        sdk: SDK stub for ``create_template``.
        cached_templates: In-memory cache of template_id by (name, sha).
        lookups: Record of all ``_lookup_cached`` calls.
        inserted: Most recently inserted cache record.
        insert_calls: All cache insert calls.
        templates: In-memory store of template_id -> template dict for
            get/update/delete GraphQL operations.
        hub_templates: In-memory store of Hub template data.
        graphql_calls: All GraphQL query strings executed via ``_run_graphql``.
    """

    sdk: FakeRunPodTemplateSdk = field(default_factory=FakeRunPodTemplateSdk)
    cached_templates: dict[tuple[str, str], str] = field(default_factory=dict)
    lookups: list[tuple[str, str]] = field(default_factory=list)
    inserted: dict[str, Any] = field(default_factory=dict)
    insert_calls: list[dict[str, Any]] = field(default_factory=list)
    templates: dict[str, dict[str, Any]] = field(default_factory=dict)
    hub_templates: dict[str, dict[str, Any]] = field(default_factory=dict)
    graphql_calls: list[str] = field(default_factory=list)

    def set_cached(self, name: str, sha: str, template_id: str) -> None:
        self.cached_templates[(name, sha)] = template_id

    async def lookup_cached(self, _: object, name: str, sha: str) -> str | None:
        self.lookups.append((name, sha))
        return self.cached_templates.get((name, sha))

    async def insert_cache(self, _: object, **kwargs: Any) -> None:
        values = dict(kwargs)
        self.inserted.update(values)
        self.insert_calls.append(values)

    def set_template(self, template_data: dict[str, Any]) -> None:
        """Register a template for get/update/delete GraphQL operations."""
        tid = template_data.get("id")
        if tid:
            self.templates[str(tid)] = dict(template_data)

    def set_hub_template(self, template_data: dict[str, Any]) -> None:
        """Register a Hub template for list/get Hub GraphQL operations."""
        tid = template_data.get("id")
        if tid:
            self.hub_templates[str(tid)] = dict(template_data)

    def run_graphql_fake(self, query: str) -> dict[str, Any]:
        """Fake ``run_graphql_query`` for get/update/delete/hub operations."""
        self.graphql_calls.append(query)
        q = query.strip()

        # RunPod removed hubPodTemplates/hubPodTemplate from its GraphQL schema
        # (2026-07); the community template list is served by podTemplates, which
        # takes no pagination arguments.
        if "podTemplate(id:" in q:
            import re

            match = re.search(r'podTemplate\(id:\s*"([^"]+)"\)', q)
            if match:
                tid = match.group(1)
                template = self.hub_templates.get(tid)
                if template:
                    return {"data": {"podTemplate": template}}
                return {"data": {"podTemplate": None}}

        if "podTemplates" in q:
            return {"data": {"podTemplates": list(self.hub_templates.values())}}

        if "deleteTemplate(id:" in q:
            import re

            match = re.search(r'deleteTemplate\(id:\s*"([^"]+)"\)', q)
            if match:
                tid = match.group(1)
                if tid in self.templates:
                    del self.templates[tid]
                    return {"data": {"deleteTemplate": True}}
                return {"data": {"deleteTemplate": False}}
            return {"data": {"deleteTemplate": False}}

        if "updateTemplate(id:" in q:
            import re

            match = re.search(r'updateTemplate\(id:\s*"([^"]+)"', q)
            if match:
                tid = match.group(1)
                if tid in self.templates:
                    self.templates[tid].update({"id": tid, "name": f"updated-{tid}"})
                    return {"data": {"updateTemplate": self.templates[tid]}}
                return {"data": {"updateTemplate": None}}
            return {"data": {"updateTemplate": None}}

        if "template(id:" in q:
            import re

            match = re.search(r'template\(id:\s*"([^"]+)"\)', q)
            if match:
                tid = match.group(1)
                template = self.templates.get(tid)
                if template:
                    return {"data": {"template": template}}
                return {"data": {"template": None}}

        return {"data": None}


@dataclass
class RunPodBillingData:
    """Billing data for a RunPod job or pod.

    Attributes:
        status: RunPod job/pod status string (COMPLETED, FAILED, CANCELLED, IN_QUEUE, etc.).
        cost_per_hr: Hourly cost rate for the pod/job.
        worker_time_ms: Execution time in milliseconds.
        completed_at: Optional completion timestamp.
    """

    status: str
    cost_per_hr: Decimal
    worker_time_ms: int
    completed_at: datetime | None = None


class RunPodBillingFake:
    """Deterministic RunPod billing data provider for hermetic tests.

    Provides terminal states and actual costs for jobs/pods without live API calls.
    Integrates with ``pitwall.reconciler.map_runpod_status``.

    Example::

        from tests.fakes.runpod import RunPodBillingFake
        from pitwall.reconciler import map_runpod_status

        billing = RunPodBillingFake()
        billing.set("job-1", status="COMPLETED", cost_per_hr=0.44, worker_time_ms=12000)

        data = billing.get("job-1")
        result = map_runpod_status(
            data.status,
            cost_per_hr=data.cost_per_hr,
            worker_time_ms=data.worker_time_ms,
        )
        assert result.terminal is True
        assert result.state.value == "completed"
    """

    def __init__(self) -> None:
        self._data: dict[str, RunPodBillingData] = {}
        self.calls: list[str] = []

    def set(
        self,
        job_id: str,
        *,
        status: str = "COMPLETED",
        cost_per_hr: float | Decimal = 0.44,
        worker_time_ms: int = 1000,
        completed_at: datetime | None = None,
    ) -> None:
        """Register billing data for a job or pod ID.

        Args:
            job_id: The RunPod job or pod ID.
            status: RunPod status string. Defaults to "COMPLETED".
            cost_per_hr: Hourly cost rate. Defaults to 0.44 (L4 GPU rate).
            worker_time_ms: Execution time in milliseconds. Defaults to 1000.
            completed_at: Optional completion timestamp. Defaults to now in UTC.
        """
        self._data[job_id] = RunPodBillingData(
            status=status,
            cost_per_hr=Decimal(str(cost_per_hr)),
            worker_time_ms=worker_time_ms,
            completed_at=completed_at or datetime.now(UTC),
        )

    def get(self, job_id: str) -> RunPodBillingData | None:
        """Return billing data for a job/pod ID, or None if not found.

        Args:
            job_id: The RunPod job or pod ID.

        Returns:
            RunPodBillingData if registered, None otherwise.
        """
        self.calls.append(job_id)
        return self._data.get(job_id)

    def terminal_statuses(self) -> dict[str, str]:
        """Return a dict of job_id -> status for all registered jobs."""
        return {job_id: data.status for job_id, data in self._data.items()}

    def actual_costs(self) -> dict[str, float]:
        """Return computed actual costs for all registered jobs.

        Costs are computed as: cost_per_hr * (worker_time_ms / 3_600_000)
        """
        from decimal import Decimal

        costs: dict[str, float] = {}
        for job_id, data in self._data.items():
            if data.worker_time_ms > 0:
                cost = (Decimal(str(data.cost_per_hr)) / Decimal(3_600_000)) * Decimal(
                    data.worker_time_ms
                )
                costs[job_id] = float(cost)
        return costs


_FakeRunPod = FakeRunPodTemplateSdk
FakeRunPodLB = RunPodLBFake
FakeRunPodQueue = RunPodQueueFake
FakeRunPodRest = RunPodRestFake
FakeRunPodServerless = RunPodServerlessFake
FakeRunPodTemplate = RunPodTemplateFake
FakeRunPodBilling = RunPodBillingFake


def bge_m3_lb_provider(
    *,
    id: str = "prov_01HQXREBGE3LBUSKS00001",
    capability_id: str = "cap_embedding_bge_m3",
    name: str = "bge-m3-lb-us-ks",
    runpod_endpoint_id: str = "eptest00000000",
    priority: int = 1,
    enabled: bool = True,
) -> Provider:
    """Return a Provider fixture for the §5.2 BGE-M3 LB endpoint.

    Example usage::

        from tests.fakes.runpod import bge_m3_lb_provider
        provider = bge_m3_lb_provider()
        assert provider.runpod_endpoint_id == "eptest00000000"
        assert provider.config["cost"]["per_second_active"] == "0.000123"
    """
    return Provider(
        id=id,
        capability_id=capability_id,
        name=name,
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id=runpod_endpoint_id,
        config={
            "lb_base_url": f"https://{runpod_endpoint_id}.api.runpod.ai",
            "custom_paths": {"embed": "/embed", "health": "/ping"},
            "max_payload_mb": 30,
            "request_timeout_s": 330,
            "cost": {
                "mode": "per_second",
                "per_second_active": "0.000123",
            },
        },
        priority=priority,
        enabled=enabled,
        health_status="healthy",
        source=CapabilitySource.API,
        updated_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
    )


def qwen3_32b_primary_provider(
    *,
    id: str = "prov_custom_qwen3_serverless",
    capability_id: str = "cap_llm_qwen3_32b",
    name: str = "prov_custom_qwen3_serverless",
    runpod_endpoint_id: str = "qwen3-32b-awq",
    fallback_chain: list[str] | None = None,
    priority: int = 1,
    enabled: bool = True,
) -> Provider:
    """Return a Provider fixture for the custom vLLM primary provider for llm.qwen3-32b.

    Example usage::

        from tests.fakes.runpod import qwen3_32b_primary_provider
        provider = qwen3_32b_primary_provider()
        assert provider.runpod_endpoint_id == "qwen3-32b-awq"
        assert provider.config["fallback_chain"] == []
    """
    return Provider(
        id=id,
        capability_id=capability_id,
        name=name,
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id=runpod_endpoint_id,
        region="US-KS-2",
        config={
            "gpu_type": "NVIDIA L4",
            "lb_base_url": f"https://{runpod_endpoint_id}.api.runpod.ai",
            "cost": {
                "mode": "per_second",
                "per_second_active": "0.001",
            },
            "fallback_chain": fallback_chain if fallback_chain is not None else [],
        },
        priority=priority,
        enabled=enabled,
        health_status="healthy",
        cold_start_p50_ms=2000,
        recent_error_rate=0.0,
        source=CapabilitySource.API,
        updated_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
    )


def qwen3_32b_public_fallback(
    *,
    id: str = "prov_qwen3_32b_public",
    capability_id: str = "cap_llm_qwen3_32b",
    name: str = "prov_qwen3_32b_public",
    runpod_endpoint_id: str = "qwen3-32b-awq",
    fallback_for: list[str] | None = None,
    priority: int = 2,
    enabled: bool = True,
) -> Provider:
    """Return a Provider fixture for the Public Endpoint fallback for qwen3-32b-awq.

    Example usage::

        from tests.fakes.runpod import qwen3_32b_public_fallback
        provider = qwen3_32b_public_fallback()
        assert provider.runpod_endpoint_id == "qwen3-32b-awq"
        assert provider.config["fallback_for"] == []
    """
    return Provider(
        id=id,
        capability_id=capability_id,
        name=name,
        provider_type=ProviderType.PUBLIC_ENDPOINT,
        runpod_endpoint_id=runpod_endpoint_id,
        region="US-KS-2",
        config={
            "gpu_type": "NVIDIA L4",
            "openai_base_url": f"https://api.runpod.ai/v2/{runpod_endpoint_id}/openai/v1",
            "cost": {
                "mode": "per_token",
                "per_million_input_tokens": "0.30",
                "per_million_output_tokens": "0.60",
            },
            "fallback_for": fallback_for if fallback_for is not None else [],
        },
        priority=priority,
        enabled=enabled,
        health_status="healthy",
        cold_start_p50_ms=0,
        recent_error_rate=0.0,
        source=CapabilitySource.API,
        updated_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
    )


__all__ = [
    "FakePodStateMachine",
    "FakeRunPodBilling",
    "FakeRunPodLB",
    "FakeRunPodQueue",
    "FakeRunPodRest",
    "FakeRunPodServerless",
    "FakeRunPodTemplate",
    "FakeRunPodTemplateSdk",
    "QueuedJob",
    "RunPodBillingData",
    "RunPodBillingFake",
    "RunPodLBFake",
    "RunPodQueueFake",
    "RunPodResponseFactory",
    "RunPodRestCall",
    "RunPodRestFake",
    "RunPodServerlessFake",
    "RunPodTemplateFake",
    "_FakeRunPod",
    "bge_m3_lb_provider",
    "qwen3_32b_primary_provider",
    "qwen3_32b_public_fallback",
]
