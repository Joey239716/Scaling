"""Run one controller against one Locust scenario on the local cluster.

    python experiments/run_experiment.py --controller hpa --scenario gradual_ramp
        [--repeat N] [--duration S] [--initial-replicas N]

Every run follows the same procedure, so controllers are compared fairly:

  1. Reset: remove every controller, scale the app to --initial-replicas,
     wait until exactly that many pods are ready.
  2. Apply the controller's plugin (controllers/<name>/, see controllers/README.md).
  3. Settle: wait until all pods are ready, then SETTLE_SECONDS more.
  4. Run the Locust scenario (experiments/scenarios/<name>.py) headless for
     --duration seconds, against the app's Service via `minikube service`.
  5. Export one telemetry snapshot per 15 s for the run window to metrics.csv
     (format: docs/telemetry.md).
  6. Remove the controller.
  7. Write metadata.json (what ran, when, on which code, machine and cluster).

Results: experiments/results/<scenario>/<controller>/repeat_<N>/
    metrics.csv, metadata.json, locust_*.csv (Locust's own stats), controller.log
    (if the controller runs as a process). --repeat N runs repeats 1..N; an
    existing repeat directory is never overwritten.

This script is the cluster harness. The Camp QMIND simulator runner is
experiments/run.py and is unrelated.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from telemetry import FIELDS, INTERVAL_SECONDS, QUERIES, snapshots_between  # noqa: E402

# ---- Fixed procedure settings (the same for every controller) ---------------
KUBE_CONTEXT = os.getenv("KUBE_CONTEXT", "scale")  # Minikube profile from setup_cluster.sh
NAMESPACE = "scale"
DEPLOYMENT = "scale-app"
SERVICE = "scale-app"
APP_LABEL = "app.kubernetes.io/name=scale-app"
MIN_REPLICAS = 1    # bounds every controller must respect (passed to process plugins)
MAX_REPLICAS = 10
SETTLE_SECONDS = 60          # step 3: quiet time after pods are ready
EXPORT_DELAY_SECONDS = 30    # step 5: let Prometheus scrape the last interval first
DEFAULT_DURATION_S = 600
STOP_TIMEOUT_SECONDS = 20    # how long a controller process gets to exit on SIGTERM
# ------------------------------------------------------------------------------

CONTROLLERS_DIR = REPO_ROOT / "controllers"
SCENARIOS_DIR = REPO_ROOT / "experiments" / "scenarios"
RESULTS_DIR = REPO_ROOT / "experiments" / "results"
SIMULATOR_CONFIGS_DIR = REPO_ROOT / "experiments" / "configs"
# Where we remember a running controller process, so a crashed run can be cleaned up.
PID_FILE = REPO_ROOT / "experiments" / ".controller.pid"


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def kubectl(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, *args], capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


# ---- Controller plugins -------------------------------------------------------

def plugin_env() -> dict:
    """Environment for plugin scripts and controller processes: what to scale and
    how to observe it. Deliberately nothing about the experiment itself."""
    env = dict(os.environ)
    env.update({
        "KUBE_CONTEXT": KUBE_CONTEXT,
        "SCALE_NAMESPACE": NAMESPACE,
        "SCALE_DEPLOYMENT": DEPLOYMENT,
        "SCALE_MIN_REPLICAS": str(MIN_REPLICAS),
        "SCALE_MAX_REPLICAS": str(MAX_REPLICAS),
        "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}",
    })
    return env


def plugin_parts(plugin_dir: Path) -> dict:
    return {
        "manifests": (plugin_dir / "manifests").is_dir(),
        "apply": (plugin_dir / "apply.sh").is_file(),
        "remove": (plugin_dir / "remove.sh").is_file(),
        "start": (plugin_dir / "start.sh").is_file(),
        "stop": (plugin_dir / "stop.sh").is_file(),
    }


def run_script(script: Path) -> None:
    log(f"  running {script.relative_to(REPO_ROOT)}")
    subprocess.run(["bash", str(script)], cwd=script.parent, env=plugin_env(), check=True)


def apply_controller(name: str, run_dir: Path) -> None:
    plugin_dir = CONTROLLERS_DIR / name
    parts = plugin_parts(plugin_dir)
    if parts["manifests"]:
        log(f"  kubectl apply -f controllers/{name}/manifests/")
        kubectl("apply", "-f", str(plugin_dir / "manifests"))
    if parts["apply"]:
        run_script(plugin_dir / "apply.sh")
    if parts["start"]:
        log_file = open(run_dir / "controller.log", "w")
        process = subprocess.Popen(
            ["bash", str(plugin_dir / "start.sh")], cwd=plugin_dir, env=plugin_env(),
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group, so stop kills its children too
        )
        PID_FILE.write_text(str(process.pid))
        log(f"  started controller process (pid {process.pid}, log: controller.log)")
        time.sleep(3)
        if process.poll() is not None:
            raise RuntimeError(
                f"controller process exited right away (code {process.returncode}); "
                f"see {run_dir / 'controller.log'}"
            )


def stop_controller_process() -> None:
    """Stop the background controller process, if one is running."""
    if not PID_FILE.exists():
        return
    pid = int(PID_FILE.read_text())
    try:
        os.killpg(pid, signal.SIGTERM)
        log(f"  sent SIGTERM to controller process group {pid}")
        deadline = time.time() + STOP_TIMEOUT_SECONDS
        while time.time() < deadline:
            os.killpg(pid, 0)  # raises once the group is gone
            time.sleep(1)
        log("  controller didn't exit in time; sending SIGKILL")
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # already gone
    PID_FILE.unlink(missing_ok=True)


def remove_controller(name: str) -> None:
    plugin_dir = CONTROLLERS_DIR / name
    parts = plugin_parts(plugin_dir)
    if parts["start"]:
        stop_controller_process()
    if parts["stop"]:
        run_script(plugin_dir / "stop.sh")
    if parts["remove"]:
        run_script(plugin_dir / "remove.sh")
    if parts["manifests"]:
        kubectl("delete", "--ignore-not-found", "-f", str(plugin_dir / "manifests"))


def remove_all_controllers() -> None:
    """Make sure nothing is scaling the app: every plugin removed, plus any
    HPA or KEDA ScaledObject left in the namespace (e.g. one applied by hand)."""
    stop_controller_process()
    for plugin_dir in sorted(p for p in CONTROLLERS_DIR.iterdir() if p.is_dir()):
        if any(plugin_parts(plugin_dir).values()):
            remove_controller(plugin_dir.name)
    if kubectl("get", "crd", "scaledobjects.keda.sh", "--ignore-not-found").strip():
        kubectl("-n", NAMESPACE, "delete", "scaledobjects.keda.sh", "--all")
    kubectl("-n", NAMESPACE, "delete", "hpa", "--all")


# ---- Cluster state --------------------------------------------------------------

def app_pods() -> list[dict]:
    pods = json.loads(kubectl("-n", NAMESPACE, "get", "pods", "-l", APP_LABEL, "-o", "json"))
    return pods["items"]


def pod_is_ready(pod: dict) -> bool:
    if pod["metadata"].get("deletionTimestamp"):
        return False
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def wait_until_stable(expected: int | None, timeout: int = 300) -> int:
    """Wait until every app pod is ready and none are starting or terminating.
    With `expected`, also require exactly that many pods. Returns the pod count."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pods = app_pods()
        desired = int(kubectl("-n", NAMESPACE, "get", "deployment", DEPLOYMENT,
                              "-o", "jsonpath={.spec.replicas}"))
        ready = sum(pod_is_ready(p) for p in pods)
        if (len(pods) == ready == desired) and (expected is None or desired == expected):
            return ready
        time.sleep(3)
    raise RuntimeError(f"pods didn't settle within {timeout}s (expected {expected})")


def reset_app(initial_replicas: int) -> None:
    log("Step 1: reset")
    remove_all_controllers()
    kubectl("-n", NAMESPACE, "scale", "deployment", DEPLOYMENT, f"--replicas={initial_replicas}")
    wait_until_stable(initial_replicas)
    log(f"  {initial_replicas} pod(s) ready, no controller active")


# ---- Traffic ------------------------------------------------------------------------

def open_app_url() -> tuple[str, subprocess.Popen | None]:
    """URL Locust should hit. `minikube service --url` reaches the NodePort, which
    balances connections across all ready pods (unlike `kubectl port-forward`,
    which pins everything to one pod). On macOS it keeps a tunnel open, so we
    leave the process running until the load test ends."""
    if os.getenv("APP_URL"):
        return os.environ["APP_URL"], None
    process = subprocess.Popen(
        ["minikube", "service", SERVICE, "-n", NAMESPACE, "-p", KUBE_CONTEXT, "--url"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        line = process.stdout.readline()
        match = re.search(r"https?://\S+", line)
        if match:
            return match.group(0), process
        if not line and process.poll() is not None:
            break
    process.kill()
    raise RuntimeError("couldn't get the app URL from `minikube service --url`")


def run_locust(scenario_file: Path, url: str, duration: int, run_dir: Path) -> int:
    cmd = [
        sys.executable, "-m", "locust", "-f", str(scenario_file),
        "--headless", "--host", url, "--run-time", f"{duration}s",
        "--csv", str(run_dir / "locust"), "--only-summary",
        "--stop-timeout", "5",
    ]
    log(f"  locust for {duration}s against {url}")
    with open(run_dir / "locust.log", "w") as locust_log:
        result = subprocess.run(cmd, cwd=scenario_file.parent, stdout=locust_log,
                                stderr=subprocess.STDOUT, timeout=duration + 120)
    # Locust exits with 1 if any request failed. That's a result, not a crash.
    return result.returncode


# ---- Results ------------------------------------------------------------------------

def write_metrics_csv(path: Path, rows: list[dict], start: float) -> None:
    columns = ["timestamp", "unix_time", "elapsed_s", "interval_s", *FIELDS]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            out = {
                **row,
                "elapsed_s": round(row["unix_time"] - start),
                "interval_s": INTERVAL_SECONDS,
            }
            # None (no data) becomes an empty cell; round to keep files readable.
            for field in FIELDS:
                value = row.get(field)
                out[field] = "" if value is None else round(value, 4)
            writer.writerow(out)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_output(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def machine_info() -> dict:
    memory_bytes = None
    if sys.platform == "darwin":
        out = command_output(["sysctl", "-n", "hw.memsize"])
        memory_bytes = int(out) if out else None
    elif Path("/proc/meminfo").exists():
        kb = int(Path("/proc/meminfo").read_text().split()[1])
        memory_bytes = kb * 1024
    return {
        "hostname_hash": hashlib.sha256(platform.node().encode()).hexdigest()[:12],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory_gb": round(memory_bytes / 2**30, 1) if memory_bytes else None,
        "python": platform.python_version(),
        "locust": command_output([sys.executable, "-m", "locust", "--version"]),
    }


def cluster_info() -> dict:
    deployment = json.loads(kubectl("-n", NAMESPACE, "get", "deployment", DEPLOYMENT, "-o", "json"))
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    node = json.loads(kubectl("get", "nodes", "-o", "json"))["items"][0]
    version = json.loads(kubectl("version", "-o", "json"))
    profiles = command_output(["minikube", "profile", "list", "-o", "json"])
    minikube_config = None
    if profiles:
        for profile in json.loads(profiles).get("valid", []):
            if profile["Name"] == KUBE_CONTEXT:
                cfg = profile["Config"]
                minikube_config = {"cpus": cfg.get("CPUs"), "memory_mb": cfg.get("Memory"),
                                   "driver": cfg.get("Driver")}
    helm = command_output(["helm", "--kube-context", KUBE_CONTEXT, "list", "-A", "-o", "json"])
    image_ids = sorted({
        s.get("imageID", "") for p in app_pods()
        for s in p.get("status", {}).get("containerStatuses", [])
    })
    return {
        "kube_context": KUBE_CONTEXT,
        "kubernetes_version": version.get("serverVersion", {}).get("gitVersion"),
        "minikube": minikube_config,
        "node_allocatable": node["status"]["allocatable"],
        "helm_releases": [
            {"name": r["name"], "namespace": r["namespace"], "chart": r["chart"]}
            for r in json.loads(helm)
        ] if helm else None,
        "app": {
            "namespace": NAMESPACE,
            "deployment": DEPLOYMENT,
            "image": container["image"],
            "image_ids": image_ids,
            "resources": container.get("resources"),
            "env": container.get("env"),
        },
    }


def git_info() -> dict:
    return {
        "commit": command_output(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]),
        "branch": command_output(["git", "-C", str(REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"]),
        # Uncommitted changes mean the commit alone doesn't reproduce this run.
        "dirty": bool(command_output(["git", "-C", str(REPO_ROOT), "status", "--porcelain"])),
    }


# ---- One run ----------------------------------------------------------------------

def run_once(args, repeat: int, run_dir: Path) -> None:
    scenario_file = SCENARIOS_DIR / f"{args.scenario}.py"
    plugin_dir = CONTROLLERS_DIR / args.controller
    run_dir.mkdir(parents=True)
    log(f"=== {args.controller} / {args.scenario} / repeat {repeat} -> {run_dir.relative_to(REPO_ROOT)}")

    tunnel = None
    try:
        reset_app(args.initial_replicas)

        log(f"Step 2: apply controller '{args.controller}'")
        apply_controller(args.controller, run_dir)

        log(f"Step 3: settle (all pods ready, then {SETTLE_SECONDS}s)")
        wait_until_stable(expected=None)
        time.sleep(SETTLE_SECONDS)

        log("Step 4: load test")
        url, tunnel = open_app_url()
        start = time.time()
        locust_exit_code = run_locust(scenario_file, url, args.duration, run_dir)
        end = time.time()
        log(f"  done ({end - start:.0f}s, locust exit code {locust_exit_code})")
    finally:
        if tunnel:
            tunnel.terminate()
        # Step 6 always runs, even if something above failed, so the next run
        # (or the next person) never inherits an active controller.
        log("Step 6: remove controller")
        remove_controller(args.controller)

    log(f"Step 5: export telemetry (waiting {EXPORT_DELAY_SECONDS}s for the last scrape)")
    time.sleep(EXPORT_DELAY_SECONDS)
    rows = snapshots_between(start, end)
    write_metrics_csv(run_dir / "metrics.csv", rows, start)
    replicas = [r["replicas_desired"] for r in rows if r["replicas_desired"] is not None]
    log(f"  metrics.csv: {len(rows)} rows, desired replicas {min(replicas, default='?')}"
        f"-{max(replicas, default='?')}")

    log("Step 7: metadata")
    metadata = {
        "controller": args.controller,
        "scenario": args.scenario,
        "repeat": repeat,
        "start": datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="seconds"),
        "end": datetime.fromtimestamp(end, timezone.utc).isoformat(timespec="seconds"),
        "start_unix": start,
        "end_unix": end,
        "duration_s": args.duration,
        "initial_replicas": args.initial_replicas,
        "settle_s": SETTLE_SECONDS,
        "replica_bounds": [MIN_REPLICAS, MAX_REPLICAS],
        "locust_exit_code": locust_exit_code,
        "telemetry": {"interval_s": INTERVAL_SECONDS, "queries": QUERIES},
        "scenario_sha256": sha256_of(scenario_file),
        "controller_files_sha256": {
            str(p.relative_to(plugin_dir)): sha256_of(p)
            for p in sorted(plugin_dir.rglob("*")) if p.is_file()
        },
        "git": git_info(),
        "machine": machine_info(),
        "cluster": cluster_info(),
    }
    (run_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    log(f"=== finished: {run_dir.relative_to(REPO_ROOT)}")


# ---- CLI --------------------------------------------------------------------------

def check_inputs(args) -> None:
    for tool in ("kubectl", "minikube"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found. Run scripts/setup_cluster.sh first (docs/onboarding/setup.md).")
    if kubectl("config", "get-contexts", KUBE_CONTEXT, "-o", "name", check=False).strip() != KUBE_CONTEXT:
        sys.exit(f"No kubectl context '{KUBE_CONTEXT}'. Run scripts/setup_cluster.sh first.")

    plugin_dir = CONTROLLERS_DIR / args.controller
    if not plugin_dir.is_dir():
        available = sorted(p.name for p in CONTROLLERS_DIR.iterdir() if p.is_dir())
        sys.exit(f"Unknown controller '{args.controller}'. Available: {', '.join(available)}")
    if not any(plugin_parts(plugin_dir).values()):
        sys.exit(f"Controller '{args.controller}' isn't implemented yet: controllers/"
                 f"{args.controller}/ has no manifests/, apply.sh or start.sh. "
                 f"See controllers/README.md.")

    scenario_file = SCENARIOS_DIR / f"{args.scenario}.py"
    if not scenario_file.is_file():
        available = sorted(p.stem for p in SCENARIOS_DIR.glob("*.py") if p.stem != "common")
        sys.exit(f"Unknown scenario '{args.scenario}'. Available: {', '.join(available)}")
    # The simulator writes to experiments/results/<scenario>/ too; never mix the two.
    if (SIMULATOR_CONFIGS_DIR / f"{args.scenario}.yaml").exists():
        sys.exit(f"'{args.scenario}' is also a simulator scenario name "
                 f"(experiments/configs/). Rename the Locust scenario so results don't mix.")
    if args.repeat < 1 or args.duration < 30 or not (MIN_REPLICAS <= args.initial_replicas <= MAX_REPLICAS):
        sys.exit(f"Need --repeat >= 1, --duration >= 30 and "
                 f"{MIN_REPLICAS} <= --initial-replicas <= {MAX_REPLICAS}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--controller", required=True, help="folder name under controllers/")
    parser.add_argument("--scenario", required=True, help="file name (no .py) under experiments/scenarios/")
    parser.add_argument("--repeat", type=int, default=1, help="number of repeats (default 1)")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION_S,
                        help=f"load test length in seconds (default {DEFAULT_DURATION_S})")
    parser.add_argument("--initial-replicas", type=int, default=1,
                        help="pods at the start of each run (default 1)")
    args = parser.parse_args()
    check_inputs(args)

    base = RESULTS_DIR / args.scenario / args.controller
    for repeat in range(1, args.repeat + 1):
        run_dir = base / f"repeat_{repeat}"
        if run_dir.exists():
            log(f"skipping repeat {repeat}: {run_dir.relative_to(REPO_ROOT)} already exists")
            continue
        try:
            run_once(args, repeat, run_dir)
        except BaseException:
            # Keep what was written for debugging, but out of the way, so a
            # re-run redoes this repeat and nobody analyses a broken run.
            if run_dir.exists():
                failed = run_dir.with_name(f"{run_dir.name}_failed_{int(time.time())}")
                run_dir.rename(failed)
                log(f"run failed; partial output moved to {failed.relative_to(REPO_ROOT)}")
            raise


if __name__ == "__main__":
    main()
