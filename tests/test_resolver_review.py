"""Regression tests for the resolver review findings."""

from datetime import timedelta

import pytest

from tests.conftest import make_spec
from tests.test_resolver import SALES, scripted
from veena.models import ScriptedClassifier
from veena.policy import ResolutionPolicy
from veena.registry import DecisionRecorded, Event, MemoryEventStore, Registry
from veena.resolver import (
    OpenQuestion,
    Refusal,
    Request,
    ResolvedSpec,
    Resolver,
    _include_inactive,
    _incorporate_seconds,
    _timezone,
    _window_days,
)
from veena.spec import Verdict, check


@pytest.fixture
def registry():
    return Registry(MemoryEventStore())


def decide(registry, question_id, selected, *, scope, decision_id="d1"):
    registry.append(
        Event(
            actor="user:owner",
            authority="role:data_owner",
            payload=DecisionRecorded(
                decision_id=decision_id,
                question_id=question_id,
                selected=selected,
                rationale="",
                scope=scope,
            ),
        )
    )


def test_window_extraction_prefers_the_led_duration_and_reports_candidates():
    assert _window_days("spend over the previous 30 complete calendar days")[0] == 30
    assert _window_days("orders settled within 2 days of purchase, over the last 30 days")[0] == 30
    assert _window_days("a 30-day window per customer")[0] == 30
    assert _window_days("spend in the last 2 weeks")[0] == 14
    assert _window_days("customer spend 30 days") == (30, (30,))
    assert _window_days("within 2 days ... and 30 days") == (None, (2, 30))
    assert _window_days("last 0 days")[0] is None
    assert _window_days("customer spend")[0] is None


def test_freshness_needs_context_and_timezones_must_exist():
    assert _incorporate_seconds("Reflect new data within 5 minutes.") == 300
    assert _incorporate_seconds("attributed within 15 minutes of purchase") is None
    assert _incorporate_seconds("refresh within 1 hour") == 3600
    assert _timezone("spend by Region/Country over 30 days") is None
    assert _timezone("days in America/Argentina/Buenos_Aires") == "America/Argentina/Buenos_Aires"
    assert _timezone("in UTC") == "UTC"


def test_inactive_member_phrasing():
    assert _include_inactive("Include customers with no qualifying purchases") == (True, True)
    assert _include_inactive("excluding inactive customers") == (False, True)
    assert _include_inactive("only customers who purchased") == (False, True)
    assert _include_inactive("Show only the total spend per customer") == (True, False)


def test_ambiguous_durations_open_a_question_listing_them(registry, semantic):
    request = Request(
        asset_id="x_30d", consumer="c", text="orders within 2 days, totals for 30 days and 90 days"
    )
    outcome = Resolver(registry, semantic, scripted()).resolve(request)
    assert isinstance(outcome, OpenQuestion)
    assert outcome.slot == "window.complete_calendar_days"
    assert outcome.alternatives == ("2", "30", "90")


def test_answered_window_question_is_consumed_on_resume(registry, semantic):
    resolver = Resolver(registry, semantic, scripted())
    first = resolver.resolve(Request(asset_id="x_30d", consumer="c", text="customer spend"))
    assert isinstance(first, OpenQuestion) and first.slot == "window.complete_calendar_days"
    decide(registry, first.question_id, "30", scope="asset:x_30d")
    resumed = resolver.resume("x_30d")
    assert isinstance(resumed, ResolvedSpec)
    assert resumed.spec.window.complete_calendar_days == 30
    assert str(resumed.spec.provenance["window.complete_calendar_days"]) == "decision:d1"
    assert str(resumed.spec.provenance["population.entity"]) == "message:m1"


def test_answered_freshness_question_is_consumed_and_abandon_refuses(registry, semantic):
    resolver = Resolver(registry, semantic, scripted())
    request = Request(
        asset_id="fast_30d",
        consumer="c",
        text="customer spend 30 days; reflect new data within 1 minute",
    )
    first = resolver.resolve(request)
    assert isinstance(first, OpenQuestion) and first.slot == "service.incorporate_within"
    decide(registry, first.question_id, "accept_300_seconds", scope="asset:fast_30d")
    resumed = resolver.resume("fast_30d")
    assert isinstance(resumed, ResolvedSpec)
    assert resumed.spec.service.incorporate_within == timedelta(seconds=300)

    other = resolver.resolve(
        Request(
            asset_id="slow_30d",
            consumer="c",
            text="customer spend 30 days; reflect new data within 1 minute",
        )
    )
    assert isinstance(other, OpenQuestion)
    decide(registry, other.question_id, "abandon_request", scope="asset:slow_30d", decision_id="d2")
    assert isinstance(resolver.resume("slow_30d"), Refusal)


def test_draft_decision_is_consumed(registry, semantic):
    resolver = Resolver(
        registry,
        semantic,
        scripted(JudgeContext=("mismatch", 0.9)),
        policy=ResolutionPolicy(max_draft_retries=0),
    )
    first = resolver.resolve(SALES)
    assert isinstance(first, OpenQuestion) and first.slot == "draft"
    decide(registry, first.question_id, "accept_draft", scope="asset:customer_spend_30d")
    assert isinstance(resolver.resume("customer_spend_30d"), ResolvedSpec)


def test_single_scored_option_still_yields_two_alternatives(registry, semantic):
    clf = ScriptedClassifier(
        {
            "ScreenContext": ("benign", 0.99),
            "JudgeContext": ("match", 0.95),
            "SlotContext:population.entity": ("customer", 0.3),
            "SlotContext:measures.calculation": ("purchase_amount_usd", 0.9),
        }
    )
    outcome = Resolver(registry, semantic, clf).resolve(SALES)
    assert isinstance(outcome, OpenQuestion)
    assert len(outcome.alternatives) >= 2 and outcome.alternatives[0] == "customer"


def test_decision_reuse_requires_the_same_option_set(registry, semantic):
    ambiguous = scripted(
        **{
            "SlotContext:measures.calculation": (
                "purchase_amount_usd",
                0.5,
                {"purchase_count": 0.5},
            )
        }
    )
    resolver = Resolver(registry, semantic, ambiguous)
    first = resolver.resolve(
        Request(asset_id="a_30d", consumer="app:a", text="customer activity 30 days")
    )
    assert isinstance(first, OpenQuestion)
    decide(registry, first.question_id, "purchase_count", scope="workspace")
    grown = semantic.model_copy(
        update={
            "calculations": (
                *semantic.calculations,
                semantic.calculations[0].model_copy(update={"name": "refund_usd", "version": 1}),
            )
        }
    )
    again = Resolver(registry, grown, ambiguous).resolve(
        Request(asset_id="b_30d", consumer="app:b", text="customer activity 30 days")
    )
    assert isinstance(
        again, OpenQuestion
    )  # a third option exists now; the old decision does not apply


def test_resume_attributes_provenance_to_the_latest_request(registry, semantic):
    resolver = Resolver(registry, semantic, scripted())
    resolver.resolve(Request(asset_id="x_30d", consumer="c", text="customer spend"))
    second = resolver.resolve(
        Request(asset_id="x_30d", consumer="c", text="customer spend 30 days")
    )
    assert isinstance(second, ResolvedSpec)
    seq = registry.state.assets["x_30d"].request_seq
    assert str(second.spec.provenance["population.entity"]) == f"message:m{seq}"
    assert seq > 1


def test_subsumption_sees_service_terms():
    slow = make_spec().model_copy(
        update={
            "service": make_spec().service.model_copy(
                update={"incorporate_within": timedelta(hours=1)}
            )
        }
    )
    result = check(slow, make_spec())
    assert result.verdict is Verdict.CANNOT and any(
        "freshness too slow" in r for r in result.reasons
    )
    assert check(make_spec(), slow).verdict is Verdict.SERVES
