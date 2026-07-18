"""Unit tests for the seed loader (pitwall.seed).

Targets the previously-undercovered parsing/validation internals: the local
YAML-subset parser (used when PyYAML is absent), the scalar/validation helpers,
and the capability/provider application path with faked repositories.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall import seed
from pitwall.core.enums import CapabilitySource
from pitwall.seed import (
    SeedApplyResult,
    SeedValidationError,
    apply_seed_data,
    load_seed_documents,
    seed_files_from_paths,
)

# --------------------------------------------------------------------------- #
# Local YAML-subset parser (_parse_simple_yaml and friends)                   #
# --------------------------------------------------------------------------- #


def test_parse_simple_yaml_empty_is_empty_map() -> None:
    assert seed._parse_simple_yaml("") == {}


def test_parse_simple_yaml_flat_map_with_scalars() -> None:
    text = "name: embedding.demo\npriority: 3\nratio: 1.5\nenabled: true\noff: false\nnothing: null"
    parsed = seed._parse_simple_yaml(text)
    assert parsed == {
        "name": "embedding.demo",
        "priority": 3,
        "ratio": 1.5,
        "enabled": True,
        "off": False,
        "nothing": None,
    }


def test_parse_simple_yaml_quotes_and_inline_json() -> None:
    text = "single: 'it''s ok'\ndouble: \"a\\tb\"\njlist: [1, 2]\njmap: {\"k\": 1}"
    parsed = seed._parse_simple_yaml(text)
    assert parsed["single"] == "it's ok"
    assert parsed["double"] == "a\tb"
    assert parsed["jlist"] == [1, 2]
    assert parsed["jmap"] == {"k": 1}


def test_parse_simple_yaml_nested_map_and_list() -> None:
    text = (
        'cost:\n  mode: per_second\n  per_second_active: "0.001"\nproviders:\n  - alpha\n  - beta'
    )
    parsed = seed._parse_simple_yaml(text)
    assert parsed["cost"] == {"mode": "per_second", "per_second_active": "0.001"}
    assert parsed["providers"] == ["alpha", "beta"]


def test_parse_simple_yaml_list_of_inline_mappings_with_continuation() -> None:
    text = (
        "capabilities:\n"
        "  - name: embedding.demo\n"
        "    class: embedding\n"
        "  - name: llm.demo\n"
        "    class: llm\n"
    )
    parsed = seed._parse_simple_yaml(text)
    assert parsed["capabilities"] == [
        {"name": "embedding.demo", "class": "embedding"},
        {"name": "llm.demo", "class": "llm"},
    ]


def test_parse_simple_yaml_comments_stripped_but_preserved_in_quotes() -> None:
    text = "# leading comment\nname: demo  # trailing comment\nhash: '#notacomment'"
    parsed = seed._parse_simple_yaml(text)
    assert parsed == {"name": "demo", "hash": "#notacomment"}


def test_parse_simple_yaml_empty_value_then_no_child_is_empty_map() -> None:
    parsed = seed._parse_simple_yaml("config:\nname: demo")
    assert parsed == {"config": {}, "name": "demo"}


def test_parse_simple_yaml_rejects_invalid_mapping_line() -> None:
    with pytest.raises(SeedValidationError, match="invalid YAML mapping line"):
        seed._parse_simple_yaml("this line has no colon")


def test_parse_simple_yaml_top_level_list() -> None:
    assert seed._parse_simple_yaml("- one\n- two") == ["one", "two"]


# --------------------------------------------------------------------------- #
# Scalar / validation helpers                                                 #
# --------------------------------------------------------------------------- #


def test_int_value_rejects_bool_and_non_int() -> None:
    with pytest.raises(SeedValidationError):
        seed._int_value(True, "provider.priority")
    with pytest.raises(SeedValidationError):
        seed._int_value("not-an-int", "provider.priority")
    assert seed._int_value("7", "provider.priority") == 7


def test_dict_and_list_value_type_checks() -> None:
    assert seed._dict_value(None, "f") == {}
    assert seed._list_value(None, "f") == []
    with pytest.raises(SeedValidationError):
        seed._dict_value([1], "f")
    with pytest.raises(SeedValidationError):
        seed._list_value({"a": 1}, "f")


def test_required_string_and_id_from_name() -> None:
    with pytest.raises(SeedValidationError):
        seed._required_string({}, "name", "capability.name")
    with pytest.raises(SeedValidationError, match="cannot be generated"):
        seed._id_from_name("cap", "!!!")
    assert seed._id_from_name("cap", "Embedding Demo") == "cap_embedding_demo"


def test_string_choice_rejects_unknown_enum_value() -> None:
    from pitwall.core.enums import CostMode

    with pytest.raises(SeedValidationError, match="must be one of"):
        seed._string_choice("bogus", CostMode, "capability.cost_mode")


# --------------------------------------------------------------------------- #
# seed_files_from_paths / load_seed_documents                                 #
# --------------------------------------------------------------------------- #


def test_seed_files_from_paths_requires_paths() -> None:
    with pytest.raises(SeedValidationError, match="at least one seed file"):
        seed_files_from_paths([])


def test_seed_files_from_paths_missing_path() -> None:
    with pytest.raises(SeedValidationError, match="does not exist"):
        seed_files_from_paths(["/no/such/seed.yaml"])


def test_seed_files_from_paths_dir_filters_and_sorts(tmp_path: Path) -> None:
    (tmp_path / "b.yaml").write_text("name: b", encoding="utf-8")
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("nope", encoding="utf-8")
    found = seed_files_from_paths([tmp_path])
    assert [p.name for p in found] == ["a.json", "b.yaml"]


def test_seed_files_from_paths_empty_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(SeedValidationError, match="no seed files found"):
        seed_files_from_paths([tmp_path])


def test_load_seed_documents_json_and_hash(tmp_path: Path) -> None:
    f = tmp_path / "seed.json"
    f.write_text(json.dumps({"capabilities": []}), encoding="utf-8")
    docs = load_seed_documents([f])
    assert len(docs) == 1
    assert docs[0].payload == {"capabilities": []}
    assert len(docs[0].content_hash) == 64


def test_load_seed_documents_rejects_non_object_root(tmp_path: Path) -> None:
    f = tmp_path / "seed.json"
    f.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SeedValidationError, match="seed root must be an object"):
        load_seed_documents([f])


# --------------------------------------------------------------------------- #
# apply_seed_data — capability + provider application with faked repos        #
# --------------------------------------------------------------------------- #


def _fake_repos(monkeypatch: pytest.MonkeyPatch, *, existing_provider: object = None) -> None:
    """Patch the repository classes so apply_seed_data runs without a DB.

    create() echoes its argument back (the persisted row); get_by_name returns
    None for capabilities and `existing_provider` for providers.
    """

    cap_repo = MagicMock()
    cap_repo.get_by_name = AsyncMock(return_value=None)
    cap_repo.get = AsyncMock(return_value=None)
    cap_repo.create = AsyncMock(side_effect=lambda cap: cap)

    prov_repo = MagicMock()
    prov_repo.get_by_name = AsyncMock(return_value=existing_provider)
    prov_repo.create = AsyncMock(side_effect=lambda prov: prov)

    monkeypatch.setattr(seed, "CapabilityRepository", lambda _pool: cap_repo)
    monkeypatch.setattr(seed, "ProviderRepository", lambda _pool: prov_repo)


_SEED_PAYLOAD = {
    "capabilities": [{"name": "embedding.demo", "class": "embedding", "cost_mode": "per_second"}],
    "providers": [
        {
            "name": "demo-runpod-lb",
            "capability": "embedding.demo",
            "endpoint_id": "eptest00000000",
            "provider_type": "serverless_lb",
            "region": "US-EXAMPLE-1",
            "gpu_class": "NVIDIA L4",
            "priority": 1,
            "cost": {"mode": "per_second", "per_second_active": "0.001"},
        }
    ],
}


@pytest.mark.anyio
async def test_apply_seed_data_applies_capability_and_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_repos(monkeypatch)
    result = await apply_seed_data(_SEED_PAYLOAD, pool=MagicMock(), source=CapabilitySource.YAML)
    assert isinstance(result, SeedApplyResult)
    assert [c.name for c in result.capabilities] == ["embedding.demo"]
    assert len(result.providers) == 1
    prov = result.providers[0]
    assert prov.name == "demo-runpod-lb"
    assert prov.config["gpu_class"] == "NVIDIA L4"
    # _provider_config defaults applied
    assert prov.config["cost"]["mode"] == "per_second"
    assert prov.config["request_timeout_s"] == 330
    assert prov.config["lb_base_url"].endswith(".api.runpod.ai")


@pytest.mark.anyio
async def test_apply_seed_data_duplicate_provider_name_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clashing = MagicMock()
    clashing.id = "prov_someone_else"
    _fake_repos(monkeypatch, existing_provider=clashing)
    with pytest.raises(SeedValidationError, match="already exists"):
        await apply_seed_data(_SEED_PAYLOAD, pool=MagicMock(), source=CapabilitySource.YAML)


@pytest.mark.anyio
async def test_apply_seed_data_provider_missing_capability_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_repos(monkeypatch)
    payload = {
        "providers": [
            {
                "name": "orphan",
                "endpoint_id": "eptest00000000",
                "provider_type": "serverless_lb",
                "gpu_class": "NVIDIA L4",
            }
        ]
    }
    with pytest.raises(SeedValidationError, match="capability"):
        await apply_seed_data(payload, pool=MagicMock(), source=CapabilitySource.API)


@pytest.mark.anyio
async def test_apply_seed_data_rejects_unknown_provider_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_repos(monkeypatch)
    payload = {
        "capabilities": [{"name": "embedding.demo", "class": "embedding"}],
        "providers": [
            {
                "name": "demo",
                "capability": "embedding.demo",
                "provider_type": "not_a_real_type",
                "gpu_class": "NVIDIA L4",
            }
        ],
    }
    with pytest.raises(SeedValidationError, match="must be one of"):
        await apply_seed_data(payload, pool=MagicMock(), source=CapabilitySource.API)
