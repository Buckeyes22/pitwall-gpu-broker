from __future__ import annotations

from typing import Any

import pytest
from runpod.error import QueryError

from pitwall.runpod_client import templates
from tests.fakes.runpod import RunPodTemplateFake

pytestmark = pytest.mark.anyio


async def test_ensure_template_creates_named_template(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    monkeypatch.setattr(templates, "_sdk", lambda: runpod_template_fake.sdk)
    monkeypatch.setattr(templates, "_lookup_cached", runpod_template_fake.lookup_cached)
    monkeypatch.setattr(templates, "_insert_cache", runpod_template_fake.insert_cache)

    image_ref = "gitlab-registry.example.com/org/cloud-worker-embed:smoke-20260507-8e53f70"
    pool: Any = object()
    template_id = await templates.ensure_template(
        pool,
        image_ref,
        template_name="pitwall-book-embed",
        registry_auth_id="registry-1",
        container_disk_gb=80,
    )

    assert template_id == "template-1"
    assert runpod_template_fake.lookups == [("pitwall-book-embed", templates.image_sha(image_ref))]
    assert runpod_template_fake.inserted["name"] == "pitwall-book-embed"
    assert runpod_template_fake.inserted["template_id"] == "template-1"
    assert runpod_template_fake.inserted["image_ref"] == image_ref
    assert runpod_template_fake.inserted["registry_auth_id"] == "registry-1"
    assert runpod_template_fake.sdk.create_template_kwargs == {
        "name": templates.template_display_name("pitwall-book-embed", image_ref),
        "image_name": image_ref,
        "container_disk_in_gb": 80,
        "volume_mount_path": "/workspace",
        "env": dict.fromkeys(templates._TEMPLATE_ENV_KEYS, ""),
        "is_serverless": False,
        "registry_auth_id": "registry-1",
    }


async def test_ensure_template_uses_custom_graphql_url_for_creation(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    image_ref = "ghcr.io/org/cloud-worker:custom-graphql"
    clients: list[_FakeTemplateGraphQLClient] = []

    def fake_client_factory(**kwargs: Any) -> _FakeTemplateGraphQLClient:
        client = _FakeTemplateGraphQLClient(
            **kwargs,
            responses=[{"saveTemplate": {"id": "template-custom", "name": "created"}}],
        )
        clients.append(client)
        return client

    monkeypatch.setattr(templates, "_sdk", lambda **_kwargs: pytest.fail("SDK path used"))
    monkeypatch.setattr(templates, "RunpodGraphQLClient", fake_client_factory)
    monkeypatch.setattr(templates, "_lookup_cached", runpod_template_fake.lookup_cached)
    monkeypatch.setattr(templates, "_insert_cache", runpod_template_fake.insert_cache)

    template_id = await templates.ensure_template(
        object(),
        image_ref,
        template_name="pitwall-custom",
        registry_auth_id="registry-1",
        container_disk_gb=80,
        api_key="plugin-key",
        graphql_url="https://graphql.runpod.test/graphql",
    )

    assert template_id == "template-custom"
    assert [(client.api_key, client.graphql_url) for client in clients] == [
        ("plugin-key", "https://graphql.runpod.test/graphql")
    ]
    mutation = clients[0].queries[0]
    assert "saveTemplate" in mutation
    assert templates.template_display_name("pitwall-custom", image_ref) in mutation
    assert image_ref in mutation
    assert runpod_template_fake.inserted["template_id"] == "template-custom"


def test_template_env_schema_includes_vllm_model() -> None:
    """Verify the template env block includes VLLM_MODEL for vLLM inference.

    The vLLM worker requires VLLM_MODEL to be set in the container env so it
    knows which model to load.
    """
    assert "VLLM_MODEL" in templates._TEMPLATE_ENV_KEYS


async def test_ensure_template_reuses_cached_named_template(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    image_ref = "gitlab-registry.example.com/org/cloud-worker:abc123"
    runpod_template_fake.set_cached(
        "pitwall-book-parse-ocr",
        templates.image_sha(image_ref),
        "template-cached",
    )

    monkeypatch.setattr(templates, "_sdk", lambda: runpod_template_fake.sdk)
    monkeypatch.setattr(templates, "_lookup_cached", runpod_template_fake.lookup_cached)

    pool: Any = object()
    template_id = await templates.ensure_template(
        pool,
        image_ref,
        template_name="pitwall-book-parse-ocr",
    )

    assert template_id == "template-cached"
    assert runpod_template_fake.lookups == [
        ("pitwall-book-parse-ocr", templates.image_sha(image_ref))
    ]
    assert runpod_template_fake.sdk.create_template_kwargs is None


def test_image_sha_with_digest() -> None:
    assert templates.image_sha("ghcr.io/org/worker@sha256:abcdef123456") == "abcdef123456"


def test_image_sha_with_tag() -> None:
    assert templates.image_sha("ghcr.io/org/worker:v1.2.3") == "v1.2.3"


def test_image_sha_without_tag() -> None:
    assert templates.image_sha("ghcr.io/org/worker") == "latest"


def test_template_suffix_stable() -> None:
    ref = "ghcr.io/org/worker:abc"
    assert templates.template_suffix(ref) == templates.template_suffix(ref)
    assert len(templates.template_suffix(ref)) == 12


def test_normalize_template_name_strips_special_chars() -> None:
    assert templates.normalize_template_name("my template!!!") == "my-template"
    assert templates.normalize_template_name("  ") == templates.TEMPLATE_NAME


def test_template_display_name() -> None:
    name = templates.template_display_name("my-app", "ghcr.io/org/worker:v1")
    assert name.startswith("my-app-")
    assert name.endswith(templates.template_suffix("ghcr.io/org/worker:v1"))


def test_get_image_ref_from_env_raises_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PITWALL_CLOUD_WORKER_IMAGE", raising=False)
    with pytest.raises(RuntimeError, match="PITWALL_CLOUD_WORKER_IMAGE not set"):
        templates.get_image_ref_from_env()


def test_get_image_ref_from_env_returns_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PITWALL_CLOUD_WORKER_IMAGE", "ghcr.io/org/worker:v1")
    assert templates.get_image_ref_from_env() == "ghcr.io/org/worker:v1"


def test_get_registry_auth_id_from_env_picks_gitlab_for_glcr_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID", "ghcr-auth-id")
    monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID_GITLAB", "glcr-auth-id")

    assert (
        templates.get_registry_auth_id_from_env(
            "gitlab-registry.example.test/example/pitwall/cloud-worker:abc"
        )
        == "glcr-auth-id"
    )


def test_get_registry_auth_id_from_env_picks_ghcr_for_ghcr_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID", "ghcr-auth-id")
    monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID_GITLAB", "glcr-auth-id")

    assert (
        templates.get_registry_auth_id_from_env("ghcr.io/example/pitwall-cloud-worker:abc")
        == "ghcr-auth-id"
    )


def test_get_registry_auth_id_from_env_no_image_falls_back_to_legacy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID", "ghcr-auth-id")
    monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID_GITLAB", "glcr-auth-id")

    assert templates.get_registry_auth_id_from_env() == "ghcr-auth-id"


def test_get_registry_auth_id_from_env_glcr_image_without_gitlab_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID", "ghcr-auth-id")
    monkeypatch.delenv("RUNPOD_REGISTRY_AUTH_ID_GITLAB", raising=False)

    assert (
        templates.get_registry_auth_id_from_env(
            "gitlab-registry.example.test/example/pitwall/cloud-worker:abc"
        )
        == "ghcr-auth-id"
    )


def test_is_duplicate_template_name_error_matches_runpod_message() -> None:
    err = QueryError("Something went wrong. (Template name must be unique)")
    assert templates._is_duplicate_template_name_error(err) is True


def test_is_duplicate_template_name_error_case_insensitive() -> None:
    assert (
        templates._is_duplicate_template_name_error(QueryError("template NAME must be UNIQUE"))
        is True
    )


def test_is_duplicate_template_name_error_ignores_unrelated_errors() -> None:
    assert templates._is_duplicate_template_name_error(QueryError("rate limited")) is False
    assert templates._is_duplicate_template_name_error(ValueError("name")) is False


def test_resolve_existing_template_id_finds_by_name() -> None:
    tmpls = [{"id": "t-1", "name": "alpha"}, {"id": "t-2", "name": "beta"}]
    assert templates._resolve_existing_template_id(tmpls, "beta") == "t-2"


def test_resolve_existing_template_id_returns_none_when_absent() -> None:
    tmpls = [{"id": "t-1", "name": "alpha"}]
    assert templates._resolve_existing_template_id(tmpls, "beta") is None


def test_resolve_existing_template_id_skips_malformed_entries() -> None:
    tmpls = [{"id": "t-1"}, {"name": "beta"}, {"id": "t-2", "name": "beta"}]
    assert templates._resolve_existing_template_id(tmpls, "beta") == "t-2"


async def test_ensure_template_reuses_existing_on_name_collision(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    """DB cache miss + RunPod already has the template (display name collision).

    create_template raises a duplicate-name QueryError; ensure_template must
    resolve the existing template by its display name, cache it under the logical
    name, and return its id instead of propagating the collision.
    """
    image_ref = "ghcr.io/org/cloud-worker:reuse123"
    display_name = templates.template_display_name("pitwall-reuse", image_ref)

    def _raise_duplicate(**_kwargs: Any) -> dict[str, str]:
        raise QueryError("Something went wrong. (Template name must be unique)")

    monkeypatch.setattr(runpod_template_fake.sdk, "create_template", _raise_duplicate)
    monkeypatch.setattr(templates, "_sdk", lambda: runpod_template_fake.sdk)
    monkeypatch.setattr(templates, "_lookup_cached", runpod_template_fake.lookup_cached)
    monkeypatch.setattr(templates, "_insert_cache", runpod_template_fake.insert_cache)

    # _list_my_templates is sync in production (blocking GraphQL via requests);
    # ensure_template invokes it through asyncio.to_thread.
    def _fake_list_sync() -> list[dict[str, Any]]:
        return [{"id": "existing-tmpl", "name": display_name}]

    monkeypatch.setattr(templates, "_list_my_templates", _fake_list_sync)

    pool: Any = object()
    template_id = await templates.ensure_template(pool, image_ref, template_name="pitwall-reuse")

    assert template_id == "existing-tmpl"
    assert runpod_template_fake.inserted["template_id"] == "existing-tmpl"
    assert runpod_template_fake.inserted["name"] == "pitwall-reuse"


async def test_ensure_template_custom_graphql_url_lists_same_endpoint_on_name_collision(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    image_ref = "ghcr.io/org/cloud-worker:reuse-custom"
    display_name = templates.template_display_name("pitwall-reuse", image_ref)
    clients: list[_FakeTemplateGraphQLClient] = []
    responses: list[dict[str, Any] | BaseException] = [
        QueryError("Something went wrong. (Template name must be unique)"),
        {"myself": {"podTemplates": [{"id": "existing-custom", "name": display_name}]}},
    ]

    def fake_client_factory(**kwargs: Any) -> _FakeTemplateGraphQLClient:
        client = _FakeTemplateGraphQLClient(**kwargs, responses=responses)
        clients.append(client)
        return client

    monkeypatch.setattr(templates, "_sdk", lambda **_kwargs: pytest.fail("SDK path used"))
    monkeypatch.setattr(templates, "RunpodGraphQLClient", fake_client_factory)
    monkeypatch.setattr(templates, "_lookup_cached", runpod_template_fake.lookup_cached)
    monkeypatch.setattr(templates, "_insert_cache", runpod_template_fake.insert_cache)

    template_id = await templates.ensure_template(
        object(),
        image_ref,
        template_name="pitwall-reuse",
        api_key="plugin-key",
        graphql_url="https://graphql.runpod.test/graphql",
    )

    assert template_id == "existing-custom"
    assert [(client.api_key, client.graphql_url) for client in clients] == [
        ("plugin-key", "https://graphql.runpod.test/graphql"),
        ("plugin-key", "https://graphql.runpod.test/graphql"),
    ]
    assert "saveTemplate" in clients[0].queries[0]
    assert "podTemplates" in clients[1].queries[0]
    assert runpod_template_fake.inserted["template_id"] == "existing-custom"


async def test_get_template_returns_template(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    runpod_template_fake.set_template(
        {
            "id": "tmpl-abc123",
            "name": "my-template",
            "imageName": "ghcr.io/org/worker:v1",
            "dockerArgs": "python server.py",
            "containerDiskInGb": 50,
            "volumeInGb": 0,
            "volumeMountPath": "/workspace",
            "ports": "8000/http",
            "env": [{"key": "FOO", "value": "bar"}],
            "isServerless": False,
            "isPublic": False,
            "readme": "# My Template",
        }
    )

    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    template = await templates.get_template("tmpl-abc123")

    assert template.id == "tmpl-abc123"
    assert template.name == "my-template"
    assert template.image_name == "ghcr.io/org/worker:v1"
    assert template.docker_args == "python server.py"
    assert template.container_disk_in_gb == 50
    assert template.ports == "8000/http"
    assert template.env[0].key == "FOO"
    assert template.env[0].value == "bar"
    assert template.is_serverless is False
    assert template.is_public is False
    assert template.readme == "# My Template"
    assert "template(id:" in runpod_template_fake.graphql_calls[-1]


async def test_get_template_raises_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    with pytest.raises(templates.TemplateNotFoundError, match="tmpl-missing"):
        await templates.get_template("tmpl-missing")


async def test_get_template_raises_on_graphql_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def graphql_error(_query: str) -> dict[str, Any]:
        return {"errors": [{"message": "auth denied"}], "data": None}

    monkeypatch.setattr(templates, "_run_graphql", graphql_error)

    with pytest.raises(templates.RunpodGraphQLError, match="auth denied"):
        await templates.get_template("tmpl-auth")


async def test_update_template_updates_and_returns_template(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    runpod_template_fake.set_template(
        {
            "id": "tmpl-update-test",
            "name": "original-name",
            "imageName": "ghcr.io/org/worker:v1",
            "dockerArgs": "",
            "containerDiskInGb": 20,
            "volumeInGb": 0,
            "volumeMountPath": "/workspace",
            "ports": "",
            "env": [],
            "isServerless": False,
            "isPublic": False,
            "readme": "",
        }
    )

    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    updated = await templates.update_template(
        "tmpl-update-test",
        name="new-name",
        image_name="ghcr.io/org/worker:v2",
        container_disk_in_gb=80,
        env={"NEW_ENV": "value"},
    )

    assert updated.id == "tmpl-update-test"
    assert "updateTemplate(id:" in runpod_template_fake.graphql_calls[-1]
    assert "new-name" in runpod_template_fake.graphql_calls[-1]
    assert "ghcr.io/org/worker:v2" in runpod_template_fake.graphql_calls[-1]


async def test_update_template_serializes_graphql_string_literals(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    runpod_template_fake.set_template(
        {
            "id": "tmpl-escaped",
            "name": 'safe "name"',
            "imageName": "ghcr.io/org/worker:v2",
            "dockerArgs": 'python -c "print(1)"',
            "containerDiskInGb": 20,
            "volumeInGb": 0,
            "volumeMountPath": "/workspace",
            "ports": "8000/http",
            "env": [{"key": "QUOTE", "value": 'line1\n"value"\\tail'}],
            "isServerless": False,
            "isPublic": False,
            "readme": "line1\nline2",
        }
    )
    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    await templates.update_template(
        "tmpl-escaped",
        name='safe "name"',
        docker_args='python -c "print(1)"',
        env={"QUOTE": 'line1\n"value"\\tail'},
        readme="line1\nline2",
    )

    mutation = runpod_template_fake.graphql_calls[-1]
    assert 'name: "safe \\"name\\""' in mutation
    assert 'dockerArgs: "python -c \\"print(1)\\""' in mutation
    assert 'value: "line1\\n\\"value\\"\\\\tail"' in mutation
    assert 'readme: "line1\\nline2"' in mutation


async def test_update_template_raises_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    with pytest.raises(templates.TemplateNotFoundError, match="tmpl-does-not-exist"):
        await templates.update_template("tmpl-does-not-exist", name="new-name")


async def test_delete_template_returns_true(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    runpod_template_fake.set_template(
        {
            "id": "tmpl-to-delete",
            "name": "will-be-gone",
            "imageName": "ghcr.io/org/worker:v1",
            "dockerArgs": "",
            "containerDiskInGb": 10,
            "volumeInGb": 0,
            "volumeMountPath": "/workspace",
            "ports": "",
            "env": [],
            "isServerless": False,
            "isPublic": False,
            "readme": "",
        }
    )

    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    result = await templates.delete_template("tmpl-to-delete")

    assert result is True
    assert "deleteTemplate(id:" in runpod_template_fake.graphql_calls[-1]
    assert "tmpl-to-delete" in runpod_template_fake.graphql_calls[-1]


async def test_delete_template_raises_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    with pytest.raises(templates.TemplateDeleteError, match="tmpl-nonexistent"):
        await templates.delete_template("tmpl-nonexistent")


async def test_list_hub_templates_returns_templates(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    runpod_template_fake.set_hub_template(
        {
            "id": "hub-tmpl-1",
            "name": "vllm-worker",
            "imageName": "ghcr.io/runpod/vllm:latest",
            "githubUrl": "https://github.com/runpod/vllm-worker",
            "dockerArgs": "",
            "containerDiskInGb": 50,
            "volumeInGb": 0,
            "volumeMountPath": "/workspace",
            "ports": "8000/http",
            "env": [],
            "isServerless": False,
            "templateDescription": "Production vLLM server",
        }
    )
    runpod_template_fake.set_hub_template(
        {
            "id": "hub-tmpl-2",
            "name": "tensorrt-worker",
            "imageName": "ghcr.io/runpod/tensorrt:latest",
            "dockerArgs": "",
            "containerDiskInGb": 80,
            "volumeInGb": 0,
            "volumeMountPath": "/workspace",
            "ports": "8000/http",
            "env": [],
            "isServerless": False,
        }
    )

    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    hub_templates = await templates.list_hub_templates(limit=10)

    assert len(hub_templates) == 2
    assert hub_templates[0].id == "hub-tmpl-1"
    assert hub_templates[0].name == "vllm-worker"
    assert hub_templates[0].description is None
    assert hub_templates[1].id == "hub-tmpl-2"
    assert "podTemplates" in runpod_template_fake.graphql_calls[-1]
    assert "hubPodTemplates" not in runpod_template_fake.graphql_calls[-1]


async def test_list_hub_templates_respects_limit_offset(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    for i in range(5):
        runpod_template_fake.set_hub_template(
            {
                "id": f"hub-tmpl-{i}",
                "name": f"template-{i}",
                "imageName": f"ghcr.io/org/worker:{i}",
                "dockerArgs": "",
                "containerDiskInGb": 10,
                "volumeInGb": 0,
                "volumeMountPath": "/workspace",
                "ports": "",
                "env": [],
                "isServerless": False,
            }
        )

    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    page1 = await templates.list_hub_templates(limit=2, offset=0)
    page2 = await templates.list_hub_templates(limit=2, offset=2)

    assert len(page1) == 2
    assert page1[0].id == "hub-tmpl-0"
    assert page1[1].id == "hub-tmpl-1"
    assert len(page2) == 2
    assert page2[0].id == "hub-tmpl-2"
    assert page2[1].id == "hub-tmpl-3"


async def test_get_hub_template_returns_template(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    runpod_template_fake.set_hub_template(
        {
            "id": "hub-single-123",
            "name": "specific-hub-template",
            "imageName": "ghcr.io/runpod/specific:latest",
            "dockerArgs": "--port 8000",
            "containerDiskInGb": 100,
            "volumeInGb": 0,
            "volumeMountPath": "/workspace",
            "ports": "8000/http",
            "env": [{"key": "MODEL", "value": "mistral-7b"}],
            "isServerless": False,
        }
    )

    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    hub_template = await templates.get_hub_template("hub-single-123")

    assert hub_template.id == "hub-single-123"
    assert hub_template.name == "specific-hub-template"
    assert hub_template.docker_args == "--port 8000"
    assert hub_template.container_disk_in_gb == 100
    assert hub_template.env[0].key == "MODEL"
    assert hub_template.env[0].value == "mistral-7b"
    assert "podTemplate(id:" in runpod_template_fake.graphql_calls[-1]
    assert "hubPodTemplate(id:" not in runpod_template_fake.graphql_calls[-1]


async def test_get_hub_template_raises_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
    runpod_template_fake: RunPodTemplateFake,
) -> None:
    monkeypatch.setattr(templates, "_run_graphql", runpod_template_fake.run_graphql_fake)

    with pytest.raises(templates.TemplateNotFoundError, match="hub-nonexistent"):
        await templates.get_hub_template("hub-nonexistent")


def test_template_model_validation() -> None:
    tmpl = templates.Template(
        id="t-1",
        name="test",
        image_name="img:latest",
        docker_args=None,
        container_disk_in_gb=50,
        volume_in_gb=0,
        volume_mount_path="/workspace",
        ports="",
        env=None,
        is_serverless=False,
        is_public=False,
        readme="",
    )
    assert tmpl.id == "t-1"
    assert tmpl.docker_args is None
    assert tmpl.env is None


def test_hub_template_model_validation() -> None:
    hub = templates.HubTemplate(
        id="h-1",
        name="hub-test",
        image_name="hub/img:latest",
        description="A hub template",
        github_url=None,
        docker_args="python app.py",
        container_disk_in_gb=20,
        volume_in_gb=10,
        volume_mount_path="/data",
        ports="3000/http",
        env=[templates.TemplateEnvVar(key="KEY", value="VAL")],
        is_serverless=False,
        display_name="Hub Test",
        template_description=None,
    )
    assert hub.id == "h-1"
    assert hub.description == "A hub template"
    assert hub.docker_args == "python app.py"
    assert hub.volume_in_gb == 10
    assert hub.env[0].key == "KEY"


def test_template_not_found_error_is_runtime_error() -> None:
    err = templates.TemplateNotFoundError("template not found")
    assert isinstance(err, RuntimeError)


def test_template_delete_error_is_runtime_error() -> None:
    err = templates.TemplateDeleteError("cannot delete")
    assert isinstance(err, RuntimeError)


class _FakeTemplateGraphQLClient:
    def __init__(
        self,
        *,
        api_key: str,
        graphql_url: str,
        responses: list[dict[str, Any] | BaseException],
        **_: Any,
    ) -> None:
        self.api_key = api_key
        self.graphql_url = graphql_url
        self._responses = responses
        self.queries: list[str] = []
        self.closed = False

    async def _graphql(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.queries.append(query)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def aclose(self) -> None:
        self.closed = True
