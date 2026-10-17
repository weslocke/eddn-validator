import pytest

from eddn_validator.scorer import confidence_weight, weak_spots

GRADES = {"A": 90, "B": 80, "C": 70, "D": 60}


def row(event_type, score, messages, confidence="ok", recognised=True, branch="live"):
    return {"event_type": event_type, "branch": branch, "score": score, "messages": messages,
            "confidence": confidence, "recognised": recognised}


def test_confidence_weight_ramps_without_a_jump():
    assert confidence_weight(10, 30) == pytest.approx(10 * 10 / 30)
    assert confidence_weight(29, 30) < confidence_weight(30, 30) == 30
    assert confidence_weight(29, 30) > 0.9 * 29     # no halving at the threshold
    assert confidence_weight(1000, 30) == 1000


def test_weak_spots_need_enough_data_and_a_grade_below_c():
    rows = [row("journal/1:FSDJump", 99.0, 100_000), row("commodity/3", 20.0, 100),
            row("shipyard/2", 69.9, 40), row("outfitting/2", 70.0, 40),
            row("navroute/1", 10.0, 12, confidence="low"), row("x/1", 5.0, 50, recognised=False),
            row("codexentry/1", None, 3, confidence="insufficient")]
    spots = weak_spots(rows, GRADES)
    assert [(s["event_type"], s["grade"]) for s in spots] == [("commodity/3", "F"), ("shipyard/2", "D")]
