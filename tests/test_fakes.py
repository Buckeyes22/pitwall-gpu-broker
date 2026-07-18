from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.fakes.blob_store import FakeBlobStore, FakeGarage, _FakeGarage
from tests.fakes.runpod import (
    FakePodStateMachine,
    RunPodLBFake,
    RunPodQueueFake,
    RunPodResponseFactory,
    RunPodRestFake,
    RunPodServerlessFake,
    RunPodTemplateFake,
)


@pytest.mark.anyio
async def test_fake_blob_store_records_async_s3_operations() -> None:
    store = FakeBlobStore()

    await store.put_bytes("artifacts", "parsed/doc.json", b'{"ok": true}')
    await store.put_bytes("artifacts", "images/diagram.png", b"png")
    await store.put_bytes("other", "parsed/doc.json", b"other")

    listed = await store.list_objects("artifacts", "parsed/")
    assert listed == [
        {
            "key": "parsed/doc.json",
            "size": 12,
            "last_modified": listed[0]["last_modified"],
        }
    ]
    assert await store.get_bytes("artifacts", "parsed/doc.json") == b'{"ok": true}'

    await store.delete_object("artifacts", "parsed/doc.json")
    assert ("artifacts", "parsed/doc.json") not in store.store
    assert store.calls == [
        ("put_bytes", "artifacts", "parsed/doc.json"),
        ("put_bytes", "artifacts", "images/diagram.png"),
        ("put_bytes", "other", "parsed/doc.json"),
        ("list_objects", "artifacts", "parsed/"),
        ("get_bytes", "artifacts", "parsed/doc.json"),
        ("delete_object", "artifacts", "parsed/doc.json"),
    ]


def test_fake_blob_store_keeps_garage_aliases() -> None:
    assert FakeGarage is FakeBlobStore
    assert _FakeGarage is FakeBlobStore


def test_runpod_response_factory_builds_chat_completion() -> None:
    factory = RunPodResponseFactory(model="qwen-test")

    response = factory.chat_completion(
        "done",
        prompt_tokens=7,
        completion_tokens=3,
        headers={"x-test": "yes"},
    )

    assert response.status_code == 200
    assert response.headers["x-test"] == "yes"
    payload = response.json()
    assert payload["model"] == "qwen-test"
    assert payload["choices"][0]["message"]["content"] == "done"
    assert payload["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }


def test_runpod_response_factory_builds_queue_and_pod_shapes() -> None:
    factory = RunPodResponseFactory()

    assert factory.queue_run(job_id="job-1").json() == {
        "id": "job-1",
        "status": "IN_QUEUE",
    }
    assert factory.queue_status(job_id="job-1", output={"answer": 42}).json() == {
        "id": "job-1",
        "status": "COMPLETED",
        "output": {"answer": 42},
    }
    assert factory.queue_failed(job_id="job-1", error="boom").json() == {
        "id": "job-1",
        "status": "FAILED",
        "error": "boom",
    }
    assert factory.queue_status(output={}).json()["output"] == {}

    pod = factory.pod_response(pod_id="pod-1", desired_status="RUNNING").json()
    assert pod["id"] == "pod-1"
    assert pod["desiredStatus"] == "RUNNING"
    assert pod["runtime"]["ports"][0]["privatePort"] == 8000
    assert factory.pod(desired_status="PENDING")["runtime"] is None
    assert factory.pods_response(pods=[]).json() == []

    capacity_error = factory.capacity_error().json()
    assert "no longer any instances available" in capacity_error["error"]


def test_runpod_response_factory_builds_lb_embedding_shapes() -> None:
    factory = RunPodResponseFactory()

    payload = factory.embedding_response(count=2, dense_dim=2, include_colbert=True).json()
    assert payload["model"] == "BAAI/bge-m3"
    assert payload["dense"] == [[0.01, 0.02], [0.02, 0.03]]
    assert payload["sparse"] == [{"42": 0.5}, {"43": 0.51}]
    assert payload["colbert"] == [[[0.001, 0.002]], [[0.002, 0.003]]]

    limited = factory.rate_limited(retry_after=30)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"


@pytest.mark.anyio
async def test_fake_pod_state_machine_transport_advances_on_write() -> None:
    fake = FakePodStateMachine(states=["PENDING", "RUNNING"])

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://rest.runpod.io/v1",
    ) as client:
        first = await client.get("/pods/pod-test")
        assert first.json()["desiredStatus"] == "PENDING"
        assert first.json()["runtime"] is None

        created = await client.post("/pods")
        assert created.json()["desiredStatus"] == "RUNNING"

        second = await client.get("/pods/pod-test")
        assert second.json()["runtime"]["ports"][0]["ip"] == "127.0.0.1"


def test_runpod_rest_fake_records_calls_and_returns_registered_responses() -> None:
    fake = RunPodRestFake()
    fake.add("POST", "pods", {"id": "pod-1"})
    fake.add("DELETE", "pods/pod-1", {})

    created = fake("POST", "pods", json_body={"name": "pitwall-test"}, timeout_s=5)
    deleted = fake("DELETE", "/pods/pod-1")

    assert created == {"id": "pod-1"}
    assert deleted == {}
    assert fake.calls[0].json_body == {"name": "pitwall-test"}
    assert fake.calls[0].timeout_s == 5
    assert fake.deleted_pod_ids == ["pod-1"]


@pytest.mark.anyio
async def test_runpod_serverless_fake_serves_sequential_mock_transport() -> None:
    fake = RunPodServerlessFake()
    fake.add_response(httpx.Response(503))
    fake.add_chat_completion("done", prompt_tokens=2, completion_tokens=3)

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://api.runpod.ai/v2/test/openai/v1",
    ) as client:
        failed = await client.post("/chat/completions", json={"model": "m"})
        completed = await client.post("/chat/completions", json={"model": "m"})

    assert failed.status_code == 503
    assert completed.json()["choices"][0]["message"]["content"] == "done"
    assert completed.json()["usage"]["total_tokens"] == 5
    assert [request.url.path for request in fake.requests] == [
        "/v2/test/openai/v1/chat/completions",
        "/v2/test/openai/v1/chat/completions",
    ]


@pytest.mark.anyio
async def test_runpod_lb_fake_serves_sequential_mock_transport() -> None:
    fake = RunPodLBFake()
    fake.add_rate_limited(retry_after="2")
    fake.add_embedding(count=1)

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://eptest00000000.api.runpod.ai",
    ) as client:
        limited = await client.post("/embed", json={"texts": ["hello"]})
        completed = await client.post("/embed", json={"texts": ["hello"]})

    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "2"
    assert completed.json()["dense"] == [[0.01, 0.02, 0.03]]
    assert [request.url.path for request in fake.requests] == ["/embed", "/embed"]


@pytest.mark.anyio
async def test_runpod_template_fake_records_cache_and_sdk_calls() -> None:
    fake = RunPodTemplateFake()

    assert await fake.lookup_cached(object(), "pitwall-worker", "sha-1") is None

    fake.set_cached("pitwall-worker", "sha-1", "template-cached")
    assert await fake.lookup_cached(object(), "pitwall-worker", "sha-1") == "template-cached"

    await fake.insert_cache(
        object(),
        template_id="template-1",
        name="pitwall-worker",
        sha="sha-2",
        image_ref="image:sha-2",
        registry_auth_id=None,
    )
    assert fake.inserted["template_id"] == "template-1"

    assert fake.sdk.create_template(name="template-name") == {"id": "template-1"}
    assert fake.sdk.create_template_kwargs == {"name": "template-name"}


def test_seeded_registry_row_fixtures_are_isolated(
    seeded_registry_rows: dict[str, list[dict[str, object]]],
    seeded_capability_rows: list[dict[str, object]],
) -> None:
    seeded_registry_rows["capabilities"][0]["name"] = "changed"

    assert seeded_capability_rows[0]["name"] == "embedding.bge-m3"
    assert seeded_registry_rows["providers"][0]["capability_id"] == "cap_embedding_bge_m3"


@pytest.mark.anyio
async def test_fake_asyncpg_pool_fixture_records_async_calls(fake_asyncpg_pool: Any) -> None:
    async with fake_asyncpg_pool.acquire() as conn:
        await conn.execute("SELECT 1")

    fake_asyncpg_pool.acquire.assert_called_once()
    fake_asyncpg_pool.conn.execute.assert_awaited_once_with("SELECT 1")


@pytest.mark.anyio
async def test_runpod_queue_fake_run_submits_job_and_returns_id() -> None:
    fake = RunPodQueueFake()
    fake.add_run_response(job_id="job-1", status="IN_QUEUE")

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://api.runpod.ai/v2/abc123",
    ) as client:
        response = await client.post("/run", json={"input": {"prompt": "hello"}})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "job-1"
    assert payload["status"] == "IN_QUEUE"
    assert len(fake.requests) == 1
    job = fake.get_job("job-1")
    assert job is not None
    assert job.job_id == "job-1"
    assert job.status == "IN_QUEUE"


@pytest.mark.anyio
async def test_runpod_queue_fake_status_returns_job_state() -> None:
    fake = RunPodQueueFake()
    fake.add_run_response(job_id="job-1")
    fake.add_status_response(job_id="job-1", status="COMPLETED", output={"result": 42})

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://api.runpod.ai/v2/abc123",
    ) as client:
        await client.post("/run", json={"input": {}})
        status_response = await client.get("/status/job-1")

    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["id"] == "job-1"
    assert payload["status"] == "COMPLETED"
    assert payload["output"] == {"result": 42}


@pytest.mark.anyio
async def test_runpod_queue_fake_cancel_cancels_job() -> None:
    fake = RunPodQueueFake()
    fake.add_run_response(job_id="job-1")
    fake.add_cancel_response(job_id="job-1", cancelled=True)

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://api.runpod.ai/v2/abc123",
    ) as client:
        await client.post("/run", json={"input": {}})
        cancel_response = await client.post("/cancel/job-1")

    assert cancel_response.status_code == 200
    assert cancel_response.json() == {"cancelled": True}
    job = fake.get_job("job-1")
    assert job is not None
    assert job.status == "CANCELLED"


@pytest.mark.anyio
async def test_runpod_queue_fake_captures_webhook_on_run() -> None:
    fake = RunPodQueueFake()
    fake.add_run_response(job_id="job-1", status="IN_QUEUE")

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://api.runpod.ai/v2/abc123",
    ) as client:
        await client.post(
            "/run",
            json={"input": {"x": 1}, "webhook": "https://example.com/hook"},
        )

    webhooks = fake.get_webhooks()
    assert len(webhooks) == 1
    assert webhooks[0]["job_id"] == "job-1"
    assert webhooks[0]["webhook_url"] == "https://example.com/hook"
    assert webhooks[0]["input"] == {"x": 1}


@pytest.mark.anyio
async def test_runpod_queue_fake_runsync_returns_completed_job() -> None:
    fake = RunPodQueueFake()
    fake.add_runsync_response(job_id="job-1", status="COMPLETED", output={"ok": True})

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://api.runpod.ai/v2/abc123",
    ) as client:
        response = await client.post("/runsync", json={"input": {"prompt": "hello"}})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "job-1"
    assert payload["status"] == "COMPLETED"
    assert payload["output"] == {"ok": True}


@pytest.mark.anyio
async def test_runpod_queue_fake_set_job_state_controls_status() -> None:
    fake = RunPodQueueFake()
    fake.set_job_state("job-pre", status="IN_PROGRESS", output={"step": 1})
    fake.add_status_response(job_id="job-pre", status="COMPLETED", output={"step": 2})

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://api.runpod.ai/v2/abc123",
    ) as client:
        status_response = await client.get("/status/job-pre")

    assert status_response.json()["output"] == {"step": 2}


@pytest.mark.anyio
async def test_runpod_queue_fake_sequential_responses() -> None:
    fake = RunPodQueueFake()
    fake.add_run_response(job_id="job-1")
    fake.add_run_response(job_id="job-2")
    fake.add_status_response(job_id="job-1", status="COMPLETED")
    fake.add_status_response(job_id="job-2", status="IN_PROGRESS")

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://api.runpod.ai/v2/abc123",
    ) as client:
        r1 = await client.post("/run", json={"input": {}})
        r2 = await client.post("/run", json={"input": {}})
        s1 = await client.get("/status/job-1")
        s2 = await client.get("/status/job-2")

    assert r1.json()["id"] == "job-1"
    assert r2.json()["id"] == "job-2"
    assert s1.json()["status"] == "COMPLETED"
    assert s2.json()["status"] == "IN_PROGRESS"


@pytest.mark.anyio
async def test_runpod_queue_fake_auto_generates_job_id() -> None:
    fake = RunPodQueueFake()
    fake.add_run_response()
    fake.add_run_response()

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="https://api.runpod.ai/v2/abc123",
    ) as client:
        r1 = await client.post("/run", json={"input": {}})
        r2 = await client.post("/run", json={"input": {}})

    assert r1.json()["id"] == "fake-job-1"
    assert r2.json()["id"] == "fake-job-2"
