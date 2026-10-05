import pytest
import yaml
from pydantic import ValidationError

from tests.conftest import make_spec
from veena.errors import SpecError
from veena.refs import OriginRef, SemanticRef
from veena.spec import TimeBucket, dump_spec, load_spec, parse_spec


def test_semantic_ref_parses_text():
    ref = SemanticRef.model_validate("semantic:purchase_amount_usd@4")
    assert ref.name == "purchase_amount_usd"
    assert ref.version == 4
    assert str(ref) == "semantic:purchase_amount_usd@4"


def test_semantic_ref_requires_version():
    with pytest.raises(ValidationError, match="needs a version"):
        SemanticRef.model_validate("semantic:customer")


def test_semantic_ref_rejects_other_kinds():
    with pytest.raises(ValidationError, match="kind"):
        SemanticRef.model_validate("policy:workspace_access@1")


def test_origin_ref_accepts_message_and_decision():
    assert str(OriginRef.model_validate("message:m3")) == "message:m3"
    assert str(OriginRef.model_validate("decision:d17")) == "decision:d17"
    with pytest.raises(ValidationError):
        OriginRef.model_validate("semantic:customer@1")


def test_bundled_specs_load_and_roundtrip(spec_dir):
    for path in sorted(spec_dir.glob("*.yaml")):
        spec = load_spec(path)
        again = parse_spec(yaml.safe_load(dump_spec(spec)), source=path.name)
        assert again == spec


def test_population_entity_must_be_a_grain_key():
    with pytest.raises(ValidationError, match="grain keys"):
        make_spec(keys=("semantic:household@1",))


def test_windowed_asset_needs_complete_days():
    with pytest.raises(ValidationError, match="complete_calendar_days"):
        make_spec(time_bucket=TimeBucket.NONE, complete_days=None, history_days=90)


def test_bucketed_asset_needs_history():
    with pytest.raises(ValidationError, match="history_days"):
        make_spec(time_bucket=TimeBucket.DAY, complete_days=30, history_days=None)


def test_load_spec_wraps_errors(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("- not\n- a mapping\n")
    with pytest.raises(SpecError, match="must be a mapping"):
        load_spec(bad)
    with pytest.raises(SpecError, match="cannot read"):
        load_spec(tmp_path / "missing.yaml")
