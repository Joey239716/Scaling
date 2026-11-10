"""Tests for controller/keda_baseline.py (KEDA-style request-rate baseline).

Run with:  pytest tests/core/test_keda.py
"""

import pytest

from controller.hpa_baseline import HPAController
from controller.keda_baseline import KEDAController
from tests.helpers import make_obs


def decide(controller, replicas, rate, tick=0, pending=0, cpu=50.0):
    obs = make_obs(
        replicas_ready=replicas,
        replicas_pending=pending,
        request_rate=rate,
        cpu_pct=cpu,
        tick=tick,
    )
    return controller.decide(obs).target_replicas


def test_worked_example_spike():
    # 2 replicas, 480 req/s: ceil(480 / 30) = 16
    assert decide(KEDAController(), replicas=2, rate=480, cpu=100) == 16


def test_sees_past_the_cpu_cap_where_hpa_cannot():
    obs = make_obs(replicas_ready=2, cpu_pct=100, request_rate=480)
    assert HPAController().decide(obs).target_replicas == 4
    assert KEDAController().decide(obs).target_replicas == 16


def test_scale_down():
    # 6 replicas at 90 req/s: ceil(90 / 30) = 3
    assert decide(KEDAController(), replicas=6, rate=90) == 3


@pytest.mark.parametrize("rate", [110, 120, 126, 130])
def test_inside_tolerance_keeps_current(rate):
    # 4 replicas * 30 = 120 req/s; within 10% -> no change
    assert decide(KEDAController(), replicas=4, rate=rate) == 4


def test_just_outside_tolerance_scales():
    # 140 / 120 = 1.17 -> ceil(140 / 30) = 5
    assert decide(KEDAController(), replicas=4, rate=140) == 5


def test_pending_replicas_count_as_current():
    # 2 ready + 2 pending = 4 total; 120 req/s is exactly on target -> stay at 4
    assert decide(KEDAController(), replicas=2, pending=2, rate=120) == 4


def test_scale_down_held_by_stabilization_window():
    keda = KEDAController()
    assert decide(keda, replicas=2, rate=480, tick=0) == 16
    for tick in range(1, 20):
        assert (
            decide(keda, replicas=16, rate=90, tick=tick) == 16
        ), f"scaled down too early at tick {tick}"
    assert decide(keda, replicas=16, rate=90, tick=20) == 3


def test_reset_clears_the_window():
    keda = KEDAController()
    decide(keda, replicas=2, rate=480)
    keda.reset()
    assert decide(keda, replicas=16, rate=90) == 3


def test_zero_traffic_keeps_one_replica():
    assert decide(KEDAController(), replicas=3, rate=0) == 1


def test_decision_has_a_reason():
    d = KEDAController().decide(make_obs(replicas_ready=2, request_rate=480))
    assert d.info.get("reason")


def test_ignores_operator_notes():
    a = KEDAController().decide(make_obs(replicas_ready=2, request_rate=480))
    b = KEDAController().decide(
        make_obs(replicas_ready=2, request_rate=480, operator_notes=["10x soon!"])
    )
    assert a.target_replicas == b.target_replicas
