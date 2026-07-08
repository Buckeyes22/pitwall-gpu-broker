from __future__ import annotations

import pytest
from pydantic import ValidationError

from pitwall.api.provider_schemas import (
    EndpointCostConfig,
    EndpointRegistrationConfig,
    EndpointRegistrationRequest,
    ProviderCreate,
    expected_lb_base_url,
    expected_openai_base_url,
)
from pitwall.core.enums import ProviderType


def _provider_create(**overrides: object) -> ProviderCreate:
    payload: dict[str, object] = {
        "capability_id": "cap_llm_qwen3_32b",
        "name": "qwen3-32b-public",
        "provider_type": "public_endpoint",
        "runpod_endpoint_id": "qwen3-32b-awq",
        "priority": 2,
    }
    payload.update(overrides)
    return ProviderCreate(**payload)


def test_provider_create_accepts_valid_runpod_config() -> None:
    provider = _provider_create(
        config={
            "gpu_type": "NVIDIA L4",
            "gpu_type_priority": ["NVIDIA L4", "NVIDIA RTX A4000"],
            "openai_base_url": expected_openai_base_url(
                ProviderType.PUBLIC_ENDPOINT,
                "qwen3-32b-awq",
            ),
            "cost": {
                "mode": "per_token",
                "per_million_input_tokens": "0.30",
                "per_million_output_tokens": "0.60",
            },
        },
    )

    assert provider.priority == 2
    assert provider.config["gpu_type"] == "NVIDIA L4"


@pytest.mark.parametrize(
    "config",
    [
        {"gpu_type": "H100"},
        {"gpu_type_priority": ["L4"]},
        {"gpu_types": ["NVIDIA L4", "RTX4090"]},
    ],
)
def test_provider_create_rejects_non_canonical_gpu_names(
    config: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="not canonical"):
        _provider_create(provider_type="serverless_queue", config=config)


def test_provider_create_rejects_mismatched_openai_base_url() -> None:
    with pytest.raises(ValidationError, match="openai_base_url"):
        _provider_create(
            config={
                "openai_base_url": "https://api.runpod.ai/v2/wrong/openai/v1",
            },
        )


def test_provider_create_rejects_lb_base_url_on_non_lb_provider() -> None:
    with pytest.raises(ValidationError, match="serverless_lb"):
        _provider_create(
            config={
                "lb_base_url": expected_lb_base_url("qwen3-32b-awq"),
            },
        )


def test_provider_create_rejects_serverless_lb_without_existing_endpoint_id() -> None:
    with pytest.raises(ValidationError, match="existing runpod_endpoint_id"):
        _provider_create(
            provider_type="serverless_lb",
            runpod_endpoint_id=None,
        )


def test_provider_create_rejects_invalid_cost_mode() -> None:
    with pytest.raises(ValidationError, match="config.cost.mode"):
        _provider_create(config={"cost": {"mode": "spot"}})


def test_provider_create_accepts_capability_level_cost_modes_for_serverless() -> None:
    provider = _provider_create(
        provider_type="serverless_queue",
        config={
            "cost": {
                "mode": "per_token",
                "per_million_input_tokens": "0.30",
                "per_million_output_tokens": "0.60",
            },
        },
    )

    assert provider.config["cost"]["mode"] == "per_token"


def test_provider_create_rejects_non_integer_priority() -> None:
    with pytest.raises(ValidationError, match="priority"):
        _provider_create(priority=True)


def test_endpoint_cost_mode_requires_matching_cost_fields() -> None:
    with pytest.raises(ValidationError, match="per_million_output_tokens"):
        EndpointCostConfig(
            mode="per_token",
            per_million_input_tokens=0.30,
        )


def test_endpoint_registration_validates_gpu_cost_and_lb_url() -> None:
    req = EndpointRegistrationRequest(
        endpoint_id="eptest00000000",
        provider_type="serverless_lb",
        capability_id="cap_embedding_bge_m3",
        name="bge-m3-lb",
        config=EndpointRegistrationConfig(
            gpu_class="NVIDIA L4",
            cost=EndpointCostConfig(
                mode="per_second",
                per_second_active=0.000123,
            ),
            lb_base_url=expected_lb_base_url("eptest00000000"),
        ),
        priority=1,
    )

    assert req.config.gpu_class == "NVIDIA L4"


def test_endpoint_registration_validates_openai_base_url() -> None:
    req = EndpointRegistrationRequest(
        endpoint_id="qwen3-32b-awq",
        provider_type="public_endpoint",
        capability_id="cap_llm_qwen3_32b",
        name="qwen3-32b-public",
        config=EndpointRegistrationConfig(
            gpu_class="NVIDIA L4",
            openai_base_url=expected_openai_base_url(
                ProviderType.PUBLIC_ENDPOINT,
                "qwen3-32b-awq",
            ),
        ),
        priority=2,
    )

    assert req.config.openai_base_url == ("https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1")


def test_endpoint_registration_rejects_non_canonical_gpu_class() -> None:
    with pytest.raises(ValidationError, match="not canonical"):
        EndpointRegistrationConfig(gpu_class="RTX4090")


def test_endpoint_registration_rejects_mismatched_lb_url() -> None:
    with pytest.raises(ValidationError, match="lb_base_url"):
        EndpointRegistrationRequest(
            endpoint_id="eptest00000000",
            provider_type="serverless_lb",
            capability_id="cap_embedding_bge_m3",
            name="bge-m3-lb",
            config=EndpointRegistrationConfig(
                gpu_class="NVIDIA L4",
                lb_base_url=expected_lb_base_url("wrong"),
            ),
        )
