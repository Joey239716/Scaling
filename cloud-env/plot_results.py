"""Compare HPA and KEDA runs from cloud-env/results/.

    python cloud-env/plot_results.py

For each workload, takes the newest HPA run and the newest KEDA run of the
same length (short or --long) and writes to cloud-env/results/:

    plots/<workload>_<length>.png   three stacked panels over time:
                                    traffic, ready pods, p95 latency
    summary.md                      one row per run: latency, failures,
                                    pods used (cost)

Only reads results; never touches the cluster. Times are from Locust's start;
replicas.csv starts a few seconds earlier (while Locust's pod starts), which
is close enough to compare the two autoscalers by eye.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # write files only, never open a window
import matplotlib.pyplot as plt

RESULTS = Path(__file__).resolve().parent / "results"
# Same colours and order as experiments/plot.py (checked for colour-blind safety).
COLOURS = {"hpa": "#2a78d6", "keda": "#eb6834"}
LABELS = {"hpa": "HPA (CPU)", "keda": "KEDA (req/s)"}
INK, MUTED, GRID = "#1f2328", "#6e7781", "#e6e8eb"
PER_POD_TARGET = 30  # req/s a pod handles at the 60% CPU target


def load_run(folder: Path) -> dict | None:
    """Read one run folder; None if it is incomplete."""
    meta_file = folder / "run.json"
    history_file = folder / "locust_stats_history.csv"
    if not meta_file.exists() or not history_file.exists():
        return None
    meta = json.loads(meta_file.read_text())
    meta.setdefault("length", "long")  # runs from before the short/long option
    if meta["workload"] == "smoke":
        meta["length"] = "5min"  # smoke.py ignores the run length

    history = [r for r in csv.DictReader(history_file.open()) if r["Name"] == "Aggregated"]
    t0 = int(history[0]["Timestamp"])
    traffic = {"t": [], "users": [], "rps": [], "p95": []}
    for r in history:
        traffic["t"].append((int(r["Timestamp"]) - t0) / 60)
        traffic["users"].append(int(r["User Count"]))
        traffic["rps"].append(float(r["Requests/s"]))
        traffic["p95"].append(float(r["95%"]) if r["95%"] not in ("N/A", "0") else math.nan)

    replicas = {"t": [], "ready": []}
    for r in csv.DictReader((folder / "replicas.csv").open()):
        if r["replicas_ready"]:
            replicas["t"].append(int(r["elapsed_s"]) / 60)
            replicas["ready"].append(int(r["replicas_ready"]))

    totals = {}
    stats_file = folder / "locust_stats.csv"
    if stats_file.exists():
        for r in csv.DictReader(stats_file.open()):
            if r["Name"] == "Aggregated":
                totals = r
    return {"meta": meta, "traffic": traffic, "replicas": replicas,
            "totals": totals, "folder": folder.name}


def pod_minutes(replicas: dict) -> float:
    """Area under the ready-pods line: the cost of the run in pod-minutes."""
    t, n = replicas["t"], replicas["ready"]
    return sum((t[i + 1] - t[i]) * n[i] for i in range(len(t) - 1))


def newest_runs() -> dict:
    """{(workload, length): {autoscaler: run}}, newest run of each kind."""
    groups: dict = {}
    for folder in sorted(RESULTS.iterdir()):  # names start with the time, so newest last
        if not folder.is_dir() or folder.name == "plots":
            continue
        run = load_run(folder)
        if run is None:
            continue
        m = run["meta"]
        groups.setdefault((m["workload"], m["length"]), {})[m["autoscaler"]] = run
    return groups


def style(ax, ylabel: str) -> None:
    ax.set_ylabel(ylabel, color=MUTED)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, length=0)


def plot(workload: str, length: str, runs: dict, out: Path) -> None:
    fig, (ax_load, ax_pods, ax_lat) = plt.subplots(
        3, 1, figsize=(10, 8), sharex=True, gridspec_kw={"height_ratios": [1, 1.3, 1.3]})
    fig.suptitle(f"{workload} ({length} run): HPA vs KEDA", color=INK, x=0.06, ha="left",
                 fontsize=13, fontweight="bold")

    # Traffic: the target (users, 1 req/s each) is the same for both runs.
    first = next(iter(runs.values()))["traffic"]
    ax_load.plot(first["t"], first["users"], color=MUTED, linewidth=2)
    for name, run in runs.items():
        ax_load.plot(run["traffic"]["t"], run["traffic"]["rps"], color=COLOURS[name],
                     linewidth=1, alpha=0.8)
    style(ax_load, "req/s")

    # Pods, with the count needed to sit at the target for reference.
    needed = [max(1, math.ceil(u / PER_POD_TARGET)) for u in first["users"]]
    ax_pods.plot(first["t"], needed, color=MUTED, linewidth=1, linestyle="--")
    for name, run in runs.items():
        r = run["replicas"]
        ax_pods.step(r["t"], r["ready"], where="post", color=COLOURS[name], linewidth=2)
    style(ax_pods, "ready pods")

    for name, run in runs.items():
        ax_lat.plot(run["traffic"]["t"], run["traffic"]["p95"], color=COLOURS[name],
                    linewidth=1.5)
    ax_lat.set_yscale("log")
    style(ax_lat, "p95 latency (ms, log)")
    ax_lat.set_xlabel("minutes since Locust started", color=MUTED)

    # One legend for all panels, under the title, so it never covers data.
    handles = [plt.Line2D([], [], color=COLOURS[n], linewidth=2, label=LABELS[n]) for n in runs]
    handles += [plt.Line2D([], [], color=MUTED, linewidth=2, label="target req/s"),
                plt.Line2D([], [], color=MUTED, linewidth=1, linestyle="--",
                           label=f"pods needed (req/s ÷ {PER_POD_TARGET})")]
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.055, 0.955), ncol=4,
               frameon=False, fontsize=9, labelcolor=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out, dpi=130)
    plt.close(fig)


def summary_row(run: dict) -> str:
    t, m = run["totals"], run["meta"]
    reqs = int(t.get("Request Count", 0) or 0)
    fails = int(t.get("Failure Count", 0) or 0)
    fail_pct = f"{100 * fails / reqs:.2f}%" if reqs else "-"
    cells = [m["workload"], m["length"], LABELS[m["autoscaler"]], f"{reqs:,}", fail_pct,
             t.get("50%", "-"), t.get("95%", "-"), t.get("99%", "-"),
             str(max(run["replicas"]["ready"], default="-")),
             f"{pod_minutes(run['replicas']):.0f}", run["folder"]]
    return "| " + " | ".join(cells) + " |"


def main() -> None:
    groups = newest_runs()
    if not groups:
        raise SystemExit(f"no complete runs in {RESULTS}")
    (RESULTS / "plots").mkdir(exist_ok=True)
    lines = ["| Workload | Length | Autoscaler | Requests | Failed | Median ms | p95 ms "
             "| p99 ms | Peak pods | Pod-minutes | Run |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for (workload, length), runs in sorted(groups.items()):
        runs = {k: runs[k] for k in ("hpa", "keda") if k in runs}
        out = RESULTS / "plots" / f"{workload}_{length}.png"
        plot(workload, length, runs, out)
        print(f"wrote {out.relative_to(RESULTS.parent)}")
        lines += [summary_row(run) for run in runs.values()]
    (RESULTS / "summary.md").write_text(
        "Newest HPA and KEDA run per workload. Pod-minutes = area under the "
        "ready-pods line (cost). Latency is over the whole run.\n\n" + "\n".join(lines) + "\n")
    print(f"wrote {(RESULTS / 'summary.md').relative_to(RESULTS.parent)}")


if __name__ == "__main__":
    main()
