"""Offline checks for the cluster harness: telemetry module and runner helpers.
No cluster needed: Prometheus replies are faked."""

import csv
import importlib.util
import math
from pathlib import Path

import pytest

import telemetry
from telemetry import prometheus, queries

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_experiment", REPO_ROOT / "experiments" / "run_experiment.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fields_match_documented_schema():
    assert telemetry.FIELDS == [
        "replicas_desired", "replicas_ready", "replicas_starting", "cpu_utilization_pct",
        "request_rate_rps", "latency_p50_ms", "latency_p95_ms", "error_rate",
    ]
    assert telemetry.INTERVAL_SECONDS == 15


def test_every_query_is_documented_in_telemetry_md():
    doc = (REPO_ROOT / "docs" / "telemetry.md").read_text()
    for field in telemetry.FIELDS:
        assert f"`{field}`" in doc, f"{field} missing from docs/telemetry.md"


def test_queries_target_the_shared_app():
    for promql in queries.QUERIES.values():
        assert 'namespace="scale"' in promql
        assert "{" in promql and promql.count("(") == promql.count(")")


def fake_range_reply(values_by_field):
    """Make a stand-in for prometheus._get that answers range queries."""
    def fake_get(api_path, params):
        field = next(f for f, q in queries.QUERIES.items() if q == params["query"])
        values = values_by_field.get(field)
        if values is None:
            return {"result": []}  # Prometheus has no data at all
        return {"result": [{"values": [[params["start"] + 15 * i, v] for i, v in enumerate(values)]}]}
    return fake_get


def test_snapshots_between_one_row_per_15s_and_missing_is_none(monkeypatch):
    values = {field: ["1", "2", "3"] for field in telemetry.FIELDS}
    values["latency_p95_ms"] = ["NaN", "5", "6"]   # no requests in the first window
    values["error_rate"] = None                     # never any data
    monkeypatch.setattr(prometheus, "_get", fake_range_reply(values))

    rows = telemetry.snapshots_between(1000.0, 1030.0)

    assert [r["unix_time"] for r in rows] == [1000.0, 1015.0, 1030.0]
    assert [r["replicas_ready"] for r in rows] == [1.0, 2.0, 3.0]
    assert rows[0]["latency_p95_ms"] is None and rows[1]["latency_p95_ms"] == 5.0
    assert all(r["error_rate"] is None for r in rows)


def test_metrics_csv_writes_missing_as_empty_cell(tmp_path):
    runner = load_runner()
    row = {"timestamp": "t", "unix_time": 1015.0, **{f: 1.23456 for f in telemetry.FIELDS}}
    row["latency_p50_ms"] = None
    runner.write_metrics_csv(tmp_path / "m.csv", [row], start=1000.0)

    with open(tmp_path / "m.csv") as f:
        written = list(csv.DictReader(f))
    assert written[0]["elapsed_s"] == "15"
    assert written[0]["interval_s"] == "15"
    assert written[0]["latency_p50_ms"] == ""
    assert math.isclose(float(written[0]["replicas_ready"]), 1.2346)


def test_plugin_without_parts_counts_as_not_implemented():
    runner = load_runner()
    assert not any(runner.plugin_parts(REPO_ROOT / "controllers" / "keda").values())
    assert runner.plugin_parts(REPO_ROOT / "controllers" / "hpa")["manifests"]


def test_plugin_env_says_what_to_scale_not_how_it_is_evaluated():
    env = load_runner().plugin_env()
    assert env["SCALE_NAMESPACE"] == "scale" and env["SCALE_DEPLOYMENT"] == "scale-app"
    assert (env["SCALE_MIN_REPLICAS"], env["SCALE_MAX_REPLICAS"]) == ("1", "10")
    for key in env:
        assert "SCENARIO" not in key and "RESULT" not in key


@pytest.mark.parametrize("scenario", ["ramp", "spike", "spike_with_warning"])
def test_runner_refuses_simulator_scenario_names(scenario, monkeypatch):
    runner = load_runner()
    # Pretend the scenario file exists and the cluster checks pass.
    monkeypatch.setattr(runner, "kubectl", lambda *a, **k: runner.KUBE_CONTEXT)
    monkeypatch.setattr(runner.shutil, "which", lambda tool: "/bin/" + tool)
    monkeypatch.setattr(runner, "SCENARIOS_DIR", REPO_ROOT / "experiments" / "configs")
    real_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda p: p.suffix == ".py" or real_is_file(p))

    class Args:
        controller, repeat, duration, initial_replicas = "hpa", 1, 180, 1
    Args.scenario = scenario
    with pytest.raises(SystemExit, match="simulator scenario"):
        runner.check_inputs(Args)
