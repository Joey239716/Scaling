"""Run one workload against one autoscaler on the local cluster, and save results.

    python cloud-env/run_experiment.py --workload spike --autoscaler keda
    python cloud-env/run_experiment.py --workload spike --autoscaler hpa
    python cloud-env/run_experiment.py --workload spike --autoscaler hpa --long

Runs are short by default: the 20-minute pattern played 4x faster, plus a
2-minute cooldown (7 min), with both autoscalers' scale-down wait cut from
5 minutes to 1 so the cooldown shows it. --long plays the pattern in real
time with the default 5-minute wait (27.5 min).

Every run starts from the same state (2 idle pods, fresh autoscaler), so HPA
and KEDA runs of the same workload are directly comparable. Results go to
cloud-env/results/<time>_<workload>_<autoscaler>/:

    replicas.csv        every 5 s: pods running and ready, what the autoscaler
                        wanted, and the metric it saw (CPU % for HPA, req/s per
                        pod for KEDA); `time` is Unix time, as in Locust's CSVs
    server.csv          every 15 s, from Prometheus: req/s and p95 latency as
                        the app itself measured them
    locust_*.csv        Locust's own request rate / latency stats
    locust.log          Locust's console output (bursty's schedule, errors)
    run.json            what was run: workload, autoscaler, length, seed,
                        scale-down wait, git commit, times

Needs the setup in cloud-env/README.md (app deployed; for KEDA, Prometheus and
KEDA installed). If your kubectl doesn't match the cluster version, pass
--kubectl "minikube kubectl --".
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shlex
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
WORKLOADS = ["ramp", "spike", "periodic", "bursty", "step", "smoke"]
SAMPLE_EVERY = 5  # seconds; fine enough to time scale-up and scale-down
START_REPLICAS = 2
IDLE_BEFORE_RUN = 30  # seconds: 2 metrics refreshes, so CPU and req/s read idle
# The HPA object each autoscaler creates (KEDA makes its own).
HPA_NAME = {"hpa": "scale-app", "keda": "keda-hpa-scale-app"}
# Seconds the autoscaler waits after load drops before removing pods.
# 300 is the Kubernetes default (and what the manifests say); short runs use
# 60 so scale-down fits in their 2-minute cooldown. Same value for both.
SCALE_DOWN_WAIT = {"short": 60, "long": 300}

KUBECTL: list[str] = ["kubectl"]


def kubectl(*args: str, check: bool = True, stdin: str | None = None) -> str:
    """Run a kubectl command and return its output."""
    result = subprocess.run(
        KUBECTL + list(args), input=stdin, capture_output=True, text=True
    )
    if check and result.returncode != 0:
        sys.exit(f"kubectl {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout.strip()


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --- setup ------------------------------------------------------------------


def check_metrics_resolution() -> None:
    """HPA should see CPU every 15 s, as KEDA sees req/s. Minikube resets the
    metrics-server addon to 60 s on restart, so fix it here if needed."""
    args = json.loads(kubectl(
        "-n", "kube-system", "get", "deploy", "metrics-server",
        "-o", "jsonpath={.spec.template.spec.containers[0].args}",
    ))
    for i, arg in enumerate(args):
        if arg.startswith("--metric-resolution") and arg != "--metric-resolution=15s":
            log(f"metrics-server has {arg}; setting it to 15s")
            patch = [{"op": "replace", "path": f"/spec/template/spec/containers/0/args/{i}",
                      "value": "--metric-resolution=15s"}]
            kubectl("-n", "kube-system", "patch", "deploy", "metrics-server",
                    "--type=json", "-p", json.dumps(patch))
            kubectl("-n", "kube-system", "rollout", "status", "deploy/metrics-server")


def autoscaler_manifest(autoscaler: str, scale_down_wait: int) -> str:
    """The autoscaler's manifest, with its scale-down wait set."""
    if autoscaler == "hpa":
        doc = yaml.safe_load((HERE / "k8s" / "hpa.yaml").read_text())
        behavior = doc["spec"]["behavior"]
    else:
        doc = yaml.safe_load((HERE / "keda" / "scaledobject.yaml").read_text())
        behavior = doc["spec"]["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]
    behavior["scaleDown"]["stabilizationWindowSeconds"] = scale_down_wait
    return yaml.safe_dump(doc)


def reset_cluster(autoscaler: str, scale_down_wait: int) -> None:
    """Remove any running test and autoscaler, then start from 2 idle pods."""
    log("resetting: removing old Locust job and both autoscalers")
    kubectl("delete", "job", "locust-run", "--ignore-not-found", "--wait=true")
    kubectl("delete", "scaledobject", "scale-app", "--ignore-not-found", check=False)
    kubectl("delete", "hpa", "scale-app", "keda-hpa-scale-app", "--ignore-not-found")

    log(f"scaling to {START_REPLICAS} pods and letting them idle for {IDLE_BEFORE_RUN} s")
    kubectl("scale", "deployment/scale-app", f"--replicas={START_REPLICAS}")
    kubectl("rollout", "status", "deployment/scale-app", "--timeout=180s")
    time.sleep(IDLE_BEFORE_RUN)  # so the first metrics the autoscaler sees are idle ones

    kubectl("apply", "-f", "-", stdin=autoscaler_manifest(autoscaler, scale_down_wait))
    for _ in range(30):  # wait until the autoscaler's HPA object exists
        if kubectl("get", "hpa", HPA_NAME[autoscaler], "--ignore-not-found"):
            break
        time.sleep(2)
    else:
        sys.exit(f"{HPA_NAME[autoscaler]} was never created -- is KEDA installed?")
    log(f"{autoscaler} is active (scale-down wait {scale_down_wait} s)")


def start_locust(workload: str, run_id: str, seed: int, length: str) -> None:
    """Load the workload scripts into a ConfigMap and start the Locust Job."""
    kubectl("delete", "configmap", "workloads", "--ignore-not-found")
    kubectl("create", "configmap", "workloads", f"--from-file={HERE / 'workloads'}")

    job = yaml.safe_load((HERE / "workloads" / "locust-job.yaml").read_text())
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    values = {"WORKLOAD": f"{workload}.py", "RUN_ID": run_id,
              "BURST_SEED": str(seed), "RUN_LENGTH": length}
    for var in env:
        var["value"] = values[var["name"]]
    kubectl("create", "-f", "-", stdin=yaml.safe_dump(job))
    log(f"Locust started: {workload}.py ({length})")


# --- recording --------------------------------------------------------------


def sample(autoscaler: str) -> tuple[dict, str]:
    """One row of replicas.csv, and the Locust job's state ('' while running,
    else 'Complete' or 'Failed'). One kubectl call, so it fits in SAMPLE_EVERY."""
    items = json.loads(kubectl(
        "get", "deployment/scale-app", f"hpa/{HPA_NAME[autoscaler]}", "job/locust-run",
        "-o", "json"))["items"]
    by_kind = {i["kind"]: i for i in items}
    deploy, hpa, job = by_kind["Deployment"], by_kind["HorizontalPodAutoscaler"], by_kind["Job"]
    dstatus, hstatus = deploy.get("status", {}), hpa.get("status", {})
    metric = ""
    for m in hstatus.get("currentMetrics") or []:
        if m["type"] == "Resource":  # HPA: average CPU, % of request
            metric = m["resource"]["current"].get("averageUtilization", "")
        elif m["type"] == "External":  # KEDA: average req/s per pod
            metric = parse_quantity(m["external"]["current"].get("averageValue", ""))
    row = {
        "pods_total": dstatus.get("replicas", 0),  # running pods, ready or starting (what you pay for)
        "pods_ready": dstatus.get("readyReplicas", 0),  # pods passing readiness, serving traffic
        "desired": hstatus.get("desiredReplicas", ""),  # what the autoscaler asked for
        "metric": metric,
    }
    conditions = [c["type"] for c in job.get("status", {}).get("conditions") or []
                  if c.get("status") == "True"]
    state = next((c for c in ("Complete", "Failed") if c in conditions), "")
    return row, state


def parse_quantity(q: str) -> float | str:
    """Kubernetes writes 28.5 as '28500m'. Turn it back into a number."""
    if not q:
        return ""
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def record(autoscaler: str, out: Path) -> tuple[str, float, float]:
    """Write a replicas.csv row every SAMPLE_EVERY s until Locust finishes.
    Returns the job's final state and the first and last sample times."""
    start = time.time()
    n = 0
    with open(out / "replicas.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "time", "elapsed_s", "pods_total", "pods_ready", "desired", "metric"])
        writer.writeheader()
        while True:
            now = time.time()
            row, state = sample(autoscaler)
            row = {"time": round(now, 1), "elapsed_s": round(now - start, 1), **row}
            writer.writerow(row)
            f.flush()
            if n % 3 == 0:  # log every 15 s, record every 5 s
                log(f"t={row['elapsed_s']:>6}s  pods ready={row['pods_ready']}"
                    f"/{row['pods_total']} -> {row['desired']}  metric={row['metric']}")
            if state:
                return state, start, now
            n += 1
            time.sleep(max(0.0, start + n * SAMPLE_EVERY - time.time()))


# Server-side numbers from Prometheus (scraped every 15 s, so smoother than Locust's).
PROM_QUERIES = {
    "server_rps": 'sum(rate(app_requests_total{app="scale-app"}[45s]))',
    "server_p95_ms": 'histogram_quantile(0.95, sum by (le) '
                     '(rate(app_request_duration_seconds_bucket{app="scale-app"}[45s]))) * 1000',
}


def collect_prometheus(start: float, end: float, out: Path) -> None:
    """Save Prometheus's request rate and p95 latency over the run to server.csv."""
    columns: dict[float, dict] = {}
    for name, query in PROM_QUERIES.items():
        params = urllib.parse.urlencode(
            {"query": query, "start": int(start), "end": int(end) + 15, "step": 15})
        raw = kubectl("get", "--raw", "/api/v1/namespaces/monitoring/services/"
                      f"prometheus-server:80/proxy/api/v1/query_range?{params}", check=False)
        try:
            series = json.loads(raw)["data"]["result"]
        except (ValueError, KeyError):
            log(f"could not read {name} from Prometheus")
            continue
        for t, v in (series[0]["values"] if series else []):
            value = float(v)
            columns.setdefault(t, {})[name] = "" if math.isnan(value) else round(value, 2)
    with open(out / "server.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["time", "elapsed_s", *PROM_QUERIES])
        writer.writeheader()
        for t in sorted(columns):
            writer.writerow({"time": t, "elapsed_s": round(t - start, 1), **columns[t]})


def collect_locust_files(run_id: str, out: Path) -> None:
    """Copy Locust's CSVs off the Minikube node, and save its log."""
    (out / "locust.log").write_text(kubectl("logs", "job/locust-run", check=False))
    for suffix in ("stats", "stats_history", "failures", "exceptions"):
        src = f"minikube:/data/results/{run_id}_{suffix}.csv"
        dst = out / f"locust_{suffix}.csv"
        # Use the same minikube as --kubectl if it names one (it may not be on PATH).
        minikube = KUBECTL[0] if "minikube" in Path(KUBECTL[0]).name else "minikube"
        result = subprocess.run([minikube, "cp", src, str(dst)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            log(f"could not copy {src} (not on Minikube?): {result.stderr.strip()}")


# --- main -------------------------------------------------------------------


def main() -> None:
    global KUBECTL
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--workload", required=True, choices=WORKLOADS)
    parser.add_argument("--autoscaler", required=True, choices=["hpa", "keda"])
    parser.add_argument("--long", action="store_true",
                        help="27.5-min run with the default 5-min scale-down wait "
                             "(default: 7-min run, 1-min wait)")
    parser.add_argument("--seed", type=int, default=42, help="bursty.py's seed")
    parser.add_argument("--kubectl", default="kubectl",
                        help='kubectl command, e.g. "minikube kubectl --"')
    args = parser.parse_args()
    KUBECTL = shlex.split(args.kubectl)
    length = "long" if args.long else "short"
    scale_down_wait = SCALE_DOWN_WAIT[length]

    run_id = f"{datetime.now():%Y%m%d-%H%M%S}_{args.workload}_{args.autoscaler}"
    if args.long:
        run_id += "_long"
    out = HERE / "results" / run_id
    out.mkdir(parents=True)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    meta = {"workload": args.workload, "autoscaler": args.autoscaler,
            "length": length, "scale_down_wait_s": scale_down_wait,
            "seed": args.seed, "git_commit": commit, "started": datetime.now().isoformat()}

    check_metrics_resolution()
    reset_cluster(args.autoscaler, scale_down_wait)
    start_locust(args.workload, run_id, args.seed, length)
    meta["locust_result"], started, ended = record(args.autoscaler, out)
    meta["finished"] = datetime.now().isoformat()
    collect_locust_files(run_id, out)
    collect_prometheus(started, ended, out)
    (out / "run.json").write_text(json.dumps(meta, indent=2))
    log(f"done ({meta['locust_result']}); results in {out}")


if __name__ == "__main__":
    main()
