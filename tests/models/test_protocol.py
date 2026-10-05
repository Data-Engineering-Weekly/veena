from enum import StrEnum

import pytest
from pydantic import BaseModel

from veena.models import ScriptedClassifier, ScriptedDrafter


class Household(StrEnum):
    PRIMARY_RESIDENCE = "primary_residence"
    LINKED_BILLING = "linked_billing"


class HouseholdContext(BaseModel):
    text: str


def test_scripted_classifier_returns_a_member_of_the_closed_set():
    clf = ScriptedClassifier({"HouseholdContext": ("primary_residence", 0.55)})
    result = clf.classify(HouseholdContext(text="household spend"), Household)
    assert result.value is Household.PRIMARY_RESIDENCE
    assert result.probability == 0.55


def test_scripted_classifier_cannot_return_an_invented_option():
    clf = ScriptedClassifier({"HouseholdContext": ("by_zip_code", 0.9)})
    with pytest.raises(ValueError):
        clf.classify(HouseholdContext(text="x"), Household)


def test_scripted_drafter_renders_text():
    drafter = ScriptedDrafter(lambda req: f"draft for {type(req).__name__}")
    assert drafter.draft(HouseholdContext(text="x")) == "draft for HouseholdContext"
