"""Tests for the MCP schema adapter."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from pitwall.mcp.schema_adapter import pydantic_to_mcp_schema


class _SimpleModel(BaseModel):
    name: str
    age: int = 0


class _DescribedModel(BaseModel):
    name: Annotated[str, Field(min_length=1, description="User name")]
    count: Annotated[int, Field(ge=0, description="Item count")] = 0


class _OptionalFieldModel(BaseModel):
    name: Annotated[str, Field(min_length=1)]
    nickname: str | None = None


class _NestedModel(BaseModel):
    label: str


class _ContainingModel(BaseModel):
    name: str
    nested: _NestedModel


class _EnumReferencingModel(BaseModel):
    model_config = {"use_enum_values": False}

    color: str


class TestPydanticToMcpSchemaBasics:
    def test_output_is_dict(self) -> None:
        result = pydantic_to_mcp_schema(_SimpleModel)
        assert isinstance(result, dict)

    def test_output_has_type_object(self) -> None:
        result = pydantic_to_mcp_schema(_SimpleModel)
        assert result["type"] == "object"

    def test_output_has_properties(self) -> None:
        result = pydantic_to_mcp_schema(_SimpleModel)
        assert "properties" in result
        assert "name" in result["properties"]
        assert "age" in result["properties"]

    def test_required_fields_listed(self) -> None:
        result = pydantic_to_mcp_schema(_SimpleModel)
        assert "required" in result
        assert "name" in result["required"]

    def test_optional_fields_not_required(self) -> None:
        result = pydantic_to_mcp_schema(_OptionalFieldModel)
        assert "required" in result
        assert "name" in result["required"]
        assert "nickname" not in result["required"]

    def test_default_values_preserved(self) -> None:
        result = pydantic_to_mcp_schema(_SimpleModel)
        age_prop = result["properties"]["age"]
        assert age_prop.get("default") == 0

    def test_string_type_preserved(self) -> None:
        result = pydantic_to_mcp_schema(_SimpleModel)
        assert result["properties"]["name"]["type"] == "string"

    def test_integer_type_preserved(self) -> None:
        result = pydantic_to_mcp_schema(_SimpleModel)
        assert result["properties"]["age"]["type"] == "integer"


class TestDescriptionsPreserved:
    def test_field_description_preserved(self) -> None:
        result = pydantic_to_mcp_schema(_DescribedModel)
        assert result["properties"]["name"].get("description") == "User name"

    def test_field_description_with_default_preserved(self) -> None:
        result = pydantic_to_mcp_schema(_DescribedModel)
        assert result["properties"]["count"].get("description") == "Item count"


class TestConstraintsPreserved:
    def test_min_length_preserved(self) -> None:
        result = pydantic_to_mcp_schema(_DescribedModel)
        assert result["properties"]["name"].get("minLength") == 1

    def test_ge_constraint_preserved(self) -> None:
        result = pydantic_to_mcp_schema(_DescribedModel)
        assert result["properties"]["count"].get("minimum") == 0


class TestNullableFields:
    def test_optional_string_produces_anyof(self) -> None:
        result = pydantic_to_mcp_schema(_OptionalFieldModel)
        nick = result["properties"]["nickname"]
        assert "anyOf" in nick or "type" in nick

    def test_optional_string_default_null(self) -> None:
        result = pydantic_to_mcp_schema(_OptionalFieldModel)
        nick = result["properties"]["nickname"]
        assert nick.get("default") is None


class TestDefsInlining:
    def test_no_defs_key_in_output(self) -> None:
        result = pydantic_to_mcp_schema(_ContainingModel)
        assert "$defs" not in result

    def test_no_ref_key_in_output(self) -> None:
        result = pydantic_to_mcp_schema(_ContainingModel)
        assert "$ref" not in str(result)

    def test_nested_model_inlined(self) -> None:
        result = pydantic_to_mcp_schema(_ContainingModel)
        nested = result["properties"]["nested"]
        assert isinstance(nested, dict)
        assert "properties" in nested
        assert "label" in nested["properties"]


class TestNoiseStripped:
    def test_no_title_at_top_level(self) -> None:
        result = pydantic_to_mcp_schema(_SimpleModel)
        assert "title" not in result

    def test_no_title_in_properties(self) -> None:
        result = pydantic_to_mcp_schema(_SimpleModel)
        for prop in result["properties"].values():
            assert "title" not in prop

    def test_no_additional_properties_at_top_level(self) -> None:
        result = pydantic_to_mcp_schema(_SimpleModel)
        assert "additionalProperties" not in result


class TestRealRestModels:
    def test_lease_create_schema(self) -> None:
        from pitwall.api.schemas.leases import LeaseCreate

        schema = pydantic_to_mcp_schema(LeaseCreate)
        assert schema["type"] == "object"
        assert "capability_id" in schema["properties"]
        assert "capability_id" in schema["required"]
        assert "$defs" not in schema
        assert "$ref" not in str(schema)

    def test_lease_renew_schema(self) -> None:
        from pitwall.api.schemas.leases import LeaseRenew

        schema = pydantic_to_mcp_schema(LeaseRenew)
        assert "extends_minutes" in schema["properties"]
        prop = schema["properties"]["extends_minutes"]
        assert prop.get("minimum") == 1
        assert prop.get("maximum") == 43200
        assert prop.get("default") == 60

    def test_lease_stop_schema(self) -> None:
        from pitwall.api.schemas.leases import LeaseStop

        schema = pydantic_to_mcp_schema(LeaseStop)
        assert "reason" in schema["properties"]
        assert "reason" not in schema.get("required", [])

    def test_inference_request_schema(self) -> None:
        from pitwall.api.schemas.inference import InferenceRequest

        schema = pydantic_to_mcp_schema(InferenceRequest)
        assert "capability_id" in schema["properties"]
        assert "capability_id" in schema["required"]
        assert "$defs" not in schema

    def test_capability_create_schema(self) -> None:
        from pitwall.api.capability_schemas import CapabilityCreate

        schema = pydantic_to_mcp_schema(CapabilityCreate)
        assert "name" in schema["properties"]
        assert "$defs" not in schema
        assert "$ref" not in str(schema)

    def test_provider_create_schema(self) -> None:
        from pitwall.api.provider_schemas import ProviderCreate

        schema = pydantic_to_mcp_schema(ProviderCreate)
        assert "capability_id" in schema["properties"]
        assert "$defs" not in schema

    def test_field_descriptions_from_rest_models_preserved(self) -> None:
        from pitwall.api.schemas.leases import LeaseCreate

        schema = pydantic_to_mcp_schema(LeaseCreate)
        cap_prop = schema["properties"]["capability_id"]
        assert "description" in cap_prop
        assert cap_prop["description"] == "Capability ID to fulfill"


class TestSchemaDoesNotDuplicateModels:
    def test_adapter_uses_model_class_directly(self) -> None:
        from pitwall.api.schemas.leases import LeaseCreate

        schema = pydantic_to_mcp_schema(LeaseCreate)
        original_fields = set(LeaseCreate.model_fields)
        schema_props = set(schema["properties"].keys())
        assert original_fields == schema_props

    def test_adapter_preserves_all_constraints(self) -> None:
        from pitwall.api.schemas.leases import LeaseRenew

        schema = pydantic_to_mcp_schema(LeaseRenew)
        prop = schema["properties"]["extends_minutes"]
        assert prop.get("minimum") == 1
        assert prop.get("maximum") == 43200
        assert prop.get("default") == 60
