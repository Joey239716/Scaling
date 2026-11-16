"""Offline checks for the Locust scenarios (experiments/scenarios/) and the KEDA
plugin manifest. No cluster or Locust run needed: each shape's tick() is called
with a faked clock and --run-time."""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from locust import LoadTestShape

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = REPO_ROOT / "experiments" / "scenarios"
NEW_SCENARIOS = ["step_up", "sudden_spike", "daily_wave", "random_bursts"]
MAX_USERS = 45  # ~10 pods at the HPA's 60% target (scenarios/README.md)


@pytest.fixture(scope="module", autouse=True)
def scenarios_on_path():
    sys.path.insert(0, str(SCENARIOS_DIR))  # scenarios do `import common`
    yield
    sys.path.remove(str(SCENARIOS_DIR))


def make_shape(name, duration):
    """The scenario's shape class, with a fake runner and a settable clock."""
    module = importlib.import_module(name)
    shapes = [v for v in vars(module).values()
              if isinstance(v, type) and issubclass(v, LoadTestShape)
              and v.__module__ == name]
    assert len(shapes) == 1, f"{name} should define exactly one shape, found {shapes}"
    shape = shapes[0]()
    shape.runner = SimpleNamespace(environment=SimpleNamespace(
        parsed_options=SimpleNamespace(run_time=duration)))
    shape.now = 0.0
    shape.get_run_time = lambda: shape.now
    return shape


def users_over_run(shape, duration, step=1.0):
    out, t = [], 0.0
    while t < duration:
        shape.now = t
        users, _ = shape.tick()
        out.append(users)
        t += step
    return out


@pytest.mark.parametrize("name", NEW_SCENARIOS)
def test_shape_stays_in_range_and_stops(name):
    shape = make_shape(name, 600)
    users = users_over_run(shape, 600)
    assert 1 <= min(users) and max(users) <= MAX_USERS
    shape.now = 600
    assert shape.tick() is None  # stops at --run-time
    assert users[0] == 10 and users[-1] == 10  # starts and ends at the base load


@pytest.mark.parametrize("name", NEW_SCENARIOS)
def test_duration_stretches_the_same_shape(name):
    short = users_over_run(make_shape(name, 300), 300, step=3)
    long = users_over_run(make_shape(name, 600), 600, step=6)
    assert short == long


@pytest.mark.parametrize("name", NEW_SCENARIOS)
def test_scenario_defines_one_app_user(name):
    module = importlib.import_module(name)
    common = importlib.import_module("common")
    users = [v for v in vars(module).values()
             if isinstance(v, type) and issubclass(v, common.AppUser) and v is not common.AppUser]
    assert len(users) == 1
    assert "wait_time" not in vars(users[0]) and "work" not in vars(users[0])


def test_step_up_levels():
    shape = make_shape("step_up", 1000)
    seen = []
    for t in range(0, 1000, 10):
        shape.now = t
        users = shape.tick()[0]
        if not seen or seen[-1] != users:
            seen.append(users)
    assert seen == [10, 17, 24, 31, 38, 10]


def test_sudden_spike_timing():
    shape = make_shape("sudden_spike", 1000)
    for t, expected in [(299, 10), (300, 40), (549, 40), (550, 10)]:
        shape.now = t
        assert shape.tick()[0] == expected


def test_daily_wave_peaks_twice():
    users = users_over_run(make_shape("daily_wave", 800), 800)
    peaks = [i for i in range(1, len(users) - 1)
             if users[i] == 38 and users[i - 1] < 38]
    assert len(peaks) == 2


def test_random_bursts_are_fixed_and_inside_the_run():
    module = importlib.import_module("random_bursts")
    assert module.BURSTS == module.make_bursts(42)  # same traffic every run
    assert len(module.BURSTS) >= 4
    previous_end = 0.0
    for start, end, users in module.BURSTS:
        assert previous_end <= start < end <= module.BURSTS_END
        assert 0.025 <= end - start <= 0.075 + 1e-3
        assert 20 <= users <= 42
        previous_end = end


def test_new_scenarios_dont_reuse_simulator_names():
    reserved = {p.stem for p in (REPO_ROOT / "experiments" / "configs").glob("*.yaml")}
    assert not reserved & set(NEW_SCENARIOS)


def test_keda_manifest_matches_shared_rules():
    keda = yaml.safe_load((REPO_ROOT / "controllers/keda/manifests/scaledobject.yaml").read_text())
    hpa = yaml.safe_load((REPO_ROOT / "controllers/hpa/manifests/hpa.yaml").read_text())
    spec = keda["spec"]
    assert keda["metadata"]["namespace"] == "scale"
    assert spec["scaleTargetRef"]["name"] == "scale-app"
    assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (1, 10)
    # Same scaling speed limits as the HPA baseline, so only the signal differs.
    assert spec["advanced"]["horizontalPodAutoscalerConfig"]["behavior"] == hpa["spec"]["behavior"]
    trigger = spec["triggers"][0]
    assert trigger["type"] == "prometheus"
    # The same request-rate query telemetry uses (docs/telemetry.md).
    assert ('scale_app_requests_total{namespace="scale",path="/work"}[1m]'
            in trigger["metadata"]["query"])
