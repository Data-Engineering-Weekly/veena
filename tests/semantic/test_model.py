import pytest

from veena.errors import SemanticRefError, SpecError
from veena.refs import SemanticRef
from veena.semantic import load_semantic_model


def test_bundled_semantic_model_loads(semantic_path):
    model = load_semantic_model(semantic_path)
    calc = model.calculation(SemanticRef.model_validate("semantic:purchase_amount_usd@4"))
    assert calc.unit == "USD"
    policy = model.identity_policy(
        SemanticRef.model_validate("semantic:customer_identity_point_in_time@2")
    )
    assert policy.valid_to == "valid_to"


def test_unknown_version_names_known_versions(semantic_path):
    model = load_semantic_model(semantic_path)
    with pytest.raises(SemanticRefError, match=r"known versions: \['purchase_amount_usd@4'\]"):
        model.calculation(SemanticRef.model_validate("semantic:purchase_amount_usd@3"))


def test_internal_names_are_checked(tmp_path):
    bad = tmp_path / "semantic.yaml"
    bad.write_text(
        """
sources:
  - {name: customers, table: customers, primary_key: [customer_id], columns: [customer_id]}
entities:
  - {name: customer, version: 1, source: customers, key: missing_column}
"""
    )
    with pytest.raises(SemanticRefError, match="not a column"):
        load_semantic_model(bad)


def test_malformed_file_is_a_spec_error(tmp_path):
    bad = tmp_path / "semantic.yaml"
    bad.write_text("sources: 3\n")
    with pytest.raises(SpecError):
        load_semantic_model(bad)
