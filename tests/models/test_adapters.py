from enum import StrEnum
from typing import Any

import pytest
from pydantic import BaseModel

from veena.errors import ModelError, ModelNotConfiguredError
from veena.models import (
    CalibrationRecord,
    ClaudeClassifier,
    ClaudeDrafter,
    JevClassifier,
    KeywordClassifier,
    RecordingClassifier,
    ReplayClassifier,
    calibration_report,
)
from veena.resolver import SlotContext


class Entity(StrEnum):
    CUSTOMER = "customer"
    HOUSEHOLD_BY_RESIDENCE = "household_by_residence"
    HOUSEHOLD_BY_BILLING = "household_by_billing"


DESCRIPTIONS = {
    "customer": "one row per registered customer",
    "household_by_residence": "groups customers who share a primary residence address",
    "household_by_billing": "groups customers whose billing accounts are linked",
}


def ctx(text):
    return SlotContext(slot="population.entity", text=text, option_descriptions=DESCRIPTIONS)


def test_keyword_classifier_picks_by_overlap_and_normalizes():
    result = KeywordClassifier().classify(ctx("spend per household by residence address"), Entity)
    assert result.value is Entity.HOUSEHOLD_BY_RESIDENCE
    assert 0 < result.probability <= 1
    assert abs(result.probability + sum(p for _, p in result.alternatives) - 1) < 1e-6


def test_keyword_classifier_is_uniform_without_overlap():
    result = KeywordClassifier().classify(ctx("zzz qqq"), Entity)
    assert abs(result.probability - 1 / 3) < 1e-3


def test_keyword_classifier_abstains_on_verdict_contexts():
    from veena.resolver import JudgeContext, JudgeVerdict, ScreenContext, ScreenVerdict

    judge = KeywordClassifier().classify(JudgeContext(text="x", draft_summary="y"), JudgeVerdict)
    assert judge.value is JudgeVerdict.MATCH and judge.probability == 0.5
    screen = KeywordClassifier().classify(ScreenContext(text="ignore policy"), ScreenVerdict)
    assert screen.value is ScreenVerdict.BENIGN and abs(screen.probability - 1 / 3) < 1e-3


def test_keyword_classifier_splits_on_ambiguous_household():
    result = KeywordClassifier().classify(ctx("household spending over 90 days"), Entity)
    ranked = result.ranked()
    assert ranked[0][1] == ranked[1][1]
    assert {ranked[0][0], ranked[1][0]} == {
        Entity.HOUSEHOLD_BY_RESIDENCE,
        Entity.HOUSEHOLD_BY_BILLING,
    }


class _Parsed(BaseModel):
    value: str
    probability: float
    alternatives: dict[str, float] = {}


class _Response:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.content = [type("B", (), {"type": "text", "text": "drafted question"})()]


class _FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


def test_claude_classifier_uses_structured_output_and_closed_set():
    client = _FakeClient(
        _Response(
            _Parsed(value="customer", probability=0.91, alternatives={"household_by_billing": 0.09})
        )
    )
    result = ClaudeClassifier(model="claude-fable-5-1", client=client).classify(
        ctx("customer spend"), Entity
    )
    assert result.value is Entity.CUSTOMER
    assert result.probability == 0.91
    assert result.alternatives == ((Entity.HOUSEHOLD_BY_BILLING, 0.09),)
    call = client.messages.calls[0]
    assert call["model"] == "claude-fable-5-1"
    assert "output_format" in call
    schema = call["output_format"].model_json_schema()
    assert set(schema["properties"]["value"]["enum"]) == {e.value for e in Entity}


def test_claude_classifier_rejects_value_outside_set_and_refusals():
    bad = _FakeClient(_Response(_Parsed(value="by_zip", probability=0.9)))
    with pytest.raises(ModelError, match="not one of"):
        ClaudeClassifier(client=bad).classify(ctx("x"), Entity)
    refused = _FakeClient(_Response(None, stop_reason="refusal"))
    with pytest.raises(ModelError, match="refused"):
        ClaudeClassifier(client=refused).classify(ctx("x"), Entity)


def test_claude_drafter_returns_text():
    client = _FakeClient(_Response(None))
    assert ClaudeDrafter(client=client).draft(ctx("x")) == "drafted question"


def test_claude_adapters_need_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("HOME", "/nonexistent")  # no `ant auth login` profile either
    import contextlib

    # Any failure before a network call is the point; which exception depends on the SDK.
    with contextlib.suppress(ModelNotConfiguredError, Exception):
        ClaudeClassifier().classify(ctx("x"), Entity)


def test_jev_classifier_contract_with_fake_transport():
    seen: dict[str, Any] = {}

    def transport(url, body, headers):
        seen.update(url=url, body=body, headers=headers)
        return {
            "value": "customer",
            "probability": 0.83,
            "alternatives": {"household_by_billing": 0.1},
        }

    clf = JevClassifier(base_url="https://jev.example", api_key="k", transport=transport)
    result = clf.classify(ctx("customer spend"), Entity)
    assert result.value is Entity.CUSTOMER and result.probability == 0.83
    assert seen["url"] == "https://jev.example/v1/decide"
    assert seen["body"]["options"] == [e.value for e in Entity]
    assert seen["headers"]["authorization"] == "Bearer k"


def test_jev_classifier_rejects_bad_responses_and_missing_config(monkeypatch):
    monkeypatch.delenv("JEV_BASE_URL", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    with pytest.raises(ModelNotConfiguredError):
        JevClassifier().classify(ctx("x"), Entity)
    with pytest.raises(ModelNotConfiguredError, match="https"):
        JevClassifier(base_url="http://jev", api_key="k", transport=lambda *_: {}).classify(
            ctx("x"), Entity
        )
    bad = JevClassifier(
        base_url="https://jev",
        api_key="k",
        transport=lambda *_: {"value": "nope", "probability": 0.5},
    )
    with pytest.raises(ModelError, match="not one of"):
        bad.classify(ctx("x"), Entity)


def test_recording_and_replay_roundtrip(tmp_path):
    path = tmp_path / "rec.jsonl"
    recorded = RecordingClassifier(KeywordClassifier(), path).classify(
        ctx("customer spend"), Entity
    )
    replayed = ReplayClassifier(path).classify(ctx("customer spend"), Entity)
    assert replayed.value is recorded.value
    assert replayed.probability == recorded.probability
    assert replayed.model_id == "replay:keyword"
    with pytest.raises(ModelError, match="no recorded answer"):
        ReplayClassifier(path).classify(ctx("something else"), Entity)


def test_calibration_report_bins_and_ece():
    records = tuple(
        CalibrationRecord(
            site="resolve_refs", slot="s", model_id="m", predicted="a", expected=e, probability=p
        )
        for e, p in (("a", 0.9), ("a", 0.9), ("b", 0.9), ("a", 0.3), ("b", 0.3))
    )
    report = calibration_report("resolve_refs", records, bins=5)
    assert report.records == 5
    assert report.accuracy == 0.6
    by_lower = {round(b.lower, 1): b for b in report.bins}
    assert by_lower[0.8].count == 3 and abs(by_lower[0.8].observed_accuracy - 2 / 3) < 1e-9
    assert by_lower[0.2].count == 2 and by_lower[0.2].observed_accuracy == 0.5
    assert "overconfident" not in report.render()
    assert report.expected_calibration_error > 0
