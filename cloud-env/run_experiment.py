"""Run one workload against one autoscaler on the local cluster, and save results.

    python cloud-env/run_experiment.py --workload spike --autoscaler keda
    python cloud-env/run_experiment.py --workload spike --autoscaler hpa

Every run starts from the same state (2 idle pods, fresh autoscaler), so HPA
and KEDA runs of the same workload are directly comparable. Results go to
cloud-env/results/<time>_<workload>_<autoscaler>/:

    replicas.csv        every 15 s: replicas, what the autoscaler wanted,
                        and the metric it saw (CPU % for HPA, req/s per pod for KEDA)
    locust_*.csv        Locust's own request rate / latency stats
    locust.log          Locust's console output (bursty's schedule, errors)
    run.json            what was run: workload, autoscaler, seed, git commit, times

Needs the setup in cloud-env/README.md (app deployed; for KEDA, Prometheus and
KEDA installed). If your kubectl doesn't match the cluster version, pass
--kubectl "minikube kubectl --".
"""

from __future__ import annotations

import argparse
import csv
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
WORKLOADS = ["ramp", "spike", "periodic", "bursty", "step", "smoke"]
SAMPLE_EVERY = 15  # seconds, same as the HPA sync period
START_REPLICAS = 2
# The HPA object each autoscaler creates (KEDA makes its own).
HPA_NAME = {"hpa": "scale-app", "keda": "keda-hpa-scale-app"}

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


def reset_cluster(autoscaler: str) -> None:
    """Remove any running test and autoscaler, then start from 2 idle pods."""
    log("resetting: removing old Locust job and both autoscalers")
    kubectl("delete", "job", "locust-run", "--ignore-not-found", "--wait=true")
    kubectl("delete", "scaledobject", "scale-app", "--ignore-not-found", check=False)
    kubectl("delete", "hpa", "scale-app", "keda-hpa-scale-app", "--ignore-not-found")

    log(f"scaling to {START_REPLICAS} pods and letting them idle for 60 s")
    kubectl("scale", "deployment/scale-app", f"--replicas={START_REPLICAS}")
    kubectl("rollout", "status", "deployment/scale-app", "--timeout=180s")
    time.sleep(60)  # so the first metrics the autoscaler sees are idle ones

    if autoscaler == "hpa":
        kubectl("apply", "-f", str(HERE / "k8s" / "hpa.yaml"))
    else:
        kubectl("apply", "-f", str(HERE / "keda" / "scaledobject.yaml"))
    for _ in range(30):  # wait until the autoscaler's HPA object exists
        if kubectl("get", "hpa", HPA_NAME[autoscaler], "--ignore-not-found"):
            break
        time.sleep(2)
    else:
        sys.exit(f"{HPA_NAME[autoscaler]} was never created -- is KEDA installed?")
    log(f"{autoscaler} is active")


def start_locust(workload: str, run_id: str, seed: int) -> None:
    """Load the workload scripts into a ConfigMap and start the Locust Job."""
    kubectl("delete", "configmap", "workloads", "--ignore-not-found")
    kubectl("create", "configmap", "workloads", f"--from-file={HERE / 'workloads'}")

    job = yaml.safe_load((HERE / "workloads" / "locust-job.yaml").read_text())
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    values = {"WORKLOAD": f"{workload}.py", "RUN_ID": run_id, "BURST_SEED": str(seed)}
    for var in env:
        var["value"] = values[var["name"]]
    kubectl("create", "-f", "-", stdin=yaml.safe_dump(job))
    log(f"Locust started: {workload}.py")


# --- recording --------------------------------------------------------------


def sample(autoscaler: str) -> dict:
    """One row of replicas.csv: what the autoscaler sees and decides right now."""
    hpa = json.loads(kubectl("get", "hpa", HPA_NAME[autoscaler], "-o", "json"))
    status = hpa.get("status", {})
    metric = ""
    for m in status.get("currentMetrics") or []:
        if m["type"] == "Resource":  # HPA: average CPU, % of request
            metric = m["resource"]["current"].get("averageUtilization", "")
        elif m["type"] == "External":  # KEDA: average req/s per pod
            metric = parse_quantity(m["external"]["current"].get("averageValue", ""))
    return {
        "replicas_ready": status.get("currentReplicas", ""),
        "replicas_desired": status.get("desiredReplicas", ""),
        "metric": metric,
    }


def parse_quantity(q: str) -> float | str:
    """Kubernetes writes 28.5 as '28500m'. Turn it back into a number."""
    if not q:
        return ""
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def job_finished() -> str:
    """'' while Locust is running, else 'Complete' or 'Failed'."""
    conditions = kubectl("get", "job", "locust-run",
                         "-o", "jsonpath={.status.conditions[*].type}")
    for state in ("Complete", "Failed"):
        if state in conditions:
            return state
    return ""


def record(autoscaler: str, out: Path) -> str:
    """Write a replicas.csv row every 15 s until Locust finishes."""
    start = time.time()
    with open(out / "replicas.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "elapsed_s", "replicas_ready", "replicas_desired", "metric"])
        writer.writeheader()
        while True:
            row = {"elapsed_s": round(time.time() - start), **sample(autoscaler)}
            writer.writerow(row)
            f.flush()
            log(f"t={row['elapsed_s']:>4}s  replicas={row['replicas_ready']}"
                f" -> {row['replicas_desired']}  metric={row['metric']}")
            state = job_finished()
            if state:
                return state
            time.sleep(SAMPLE_EVERY)


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
    parser.add_argument("--seed", type=int, default=42, help="bursty.py's seed")
    parser.add_argument("--kubectl", default="kubectl",
                        help='kubectl command, e.g. "minikube kubectl --"')
    args = parser.parse_args()
    KUBECTL = shlex.split(args.kubectl)

    run_id = f"{datetime.now():%Y%m%d-%H%M%S}_{args.workload}_{args.autoscaler}"
    out = HERE / "results" / run_id
    out.mkdir(parents=True)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    meta = {"workload": args.workload, "autoscaler": args.autoscaler,
            "seed": args.seed, "git_commit": commit, "started": datetime.now().isoformat()}

    check_metrics_resolution()
    reset_cluster(args.autoscaler)
    start_locust(args.workload, run_id, args.seed)
    meta["locust_result"] = record(args.autoscaler, out)
    meta["finished"] = datetime.now().isoformat()
    collect_locust_files(run_id, out)
    (out / "run.json").write_text(json.dumps(meta, indent=2))
    log(f"done ({meta['locust_result']}); results in {out}")


if __name__ == "__main__":
    main()
