from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pitwall.core import (
    Capability,
    CapabilityClass,
    Lease,
    LeaseReadiness,
    LeaseState,
    Workload,
    WorkloadState,
)


def test_capability_accepts_spec_aliases_and_serializes_class_alias() -> None:
    capability = Capability(
        id="cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
        name="embedding.bge-m3",
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode="per_second",
        created_at="2026-05-26T14:00:00Z",
        updated_at="2026-05-26T14:00:00Z",
    )

    assert capability.class_ is CapabilityClass.EMBEDDING
    assert capability.capability_class is CapabilityClass.EMBEDDING
    assert capability.model_dump(by_alias=True)["class"] == CapabilityClass.EMBEDDING
    assert capability.model_dump(mode="json")["created_at"] == "2026-05-26T14:00:00Z"


def test_workload_rejects_unknown_state_and_naive_timestamp() -> None:
    base = {
        "id": "wkl_01HQXRXK9N3JZQP7VW4MEX2YBA",
        "capability_id": "cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
        "provider_id": "prov_01HQXR9K4M2BZQP7VW4MEX2",
        "type": "inference",
        "submitted_at": "2026-05-26T14:22:11Z",
    }

    workload = Workload(**base, state="completed")
    assert workload.state is WorkloadState.COMPLETED
    assert workload.submitted_at == datetime(2026, 5, 26, 14, 22, 11, tzinfo=UTC)

    with pytest.raises(ValidationError):
        Workload(**base, state="launching")

    with pytest.raises(ValidationError):
        Workload(**{**base, "submitted_at": "2026-05-26T14:22:11"}, state="queued")


def test_workload_rejects_non_string_state_input() -> None:
    with pytest.raises(ValidationError):
        Workload(
            id="wkl_01HQXRXK9N3JZQP7VW4MEX2YBA",
            capability_id="cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
            provider_id="prov_01HQXR9K4M2BZQP7VW4MEX2",
            type="inference",
            state=b"queued",
            submitted_at="2026-05-26T14:22:11Z",
        )


def test_lease_requires_readiness_signals_when_active() -> None:
    base = {
        "id": "lse_01HQXRYK9N3JZQP7VW4MEX2YBA",
        "provider_id": "prov_01HQXRBK4M2BZQP7VW4MEX2",
        "runpod_pod_id": "eptest00000003",
        "created_at": "2026-05-26T14:00:00Z",
        "expires_at": "2026-05-26T16:00:00Z",
        "renewal_policy": "manual",
    }

    lease = Lease(
        **base,
        state="active",
        endpoints={
            "http": {"8000": "https://eptest00000003-8000.proxy.runpod.net"},
            "tcp": {"22": {"host": "ssh.example.test", "port": 22}},
        },
        readiness={
            "runtime_seen_at": "2026-05-26T14:00:18Z",
            "port_mappings_seen_at": "2026-05-26T14:00:19Z",
            "probe_passed_at": "2026-05-26T14:00:34Z",
            "probe_method": "ssh_localhost",
        },
    )

    assert lease.state is LeaseState.ACTIVE
    assert isinstance(lease.readiness, LeaseReadiness)
    assert lease.readiness.has_active_signals

    with pytest.raises(ValidationError):
        Lease(**base, state="active")

    creating = Lease(**base, state="creating")
    assert creating.state is LeaseState.CREATING
