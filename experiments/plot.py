"""Compare every run in experiments/results/.

    python -m experiments.plot

Writes to experiments/results/:
    plots/<scenario>_timeseries.png  all controllers overlaid on one scenario
    plots/leaderboard.png            bar chart: SLA violations and cost per controller
    leaderboard.md                   the table (mean +/- std when there are repeats)

For the time-series figure, each controller is drawn from its lowest seed and
repeat 0, so they all share the same load trace.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # write files only, never open a window
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from simulator.config import SimConfig

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# ---------------------------------------------------------------- styling
# Categorical palette, validated for colour-blind safety *in this order*.
# Colour follows the controller (not its rank), so HPA is always blue, etc.
PALETTE = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
]
FIXED_COLORS = {
    "hpa": PALETTE[0],
    "llm": PALETTE[1],
    "llm_context": PALETTE[2],
    "scheduled": PALETTE[3],
    "keda": PALETTE[4],
}
NEUTRAL = "#8a8984"  # static baselines and overflow
TEXT = "#0b0b0b"
TEXT_MUTED = "#52514e"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "axes.edgecolor": GRID,
        "axes.labelcolor": TEXT_MUTED,
        "axes.titlecolor": TEXT,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "xtick.color": TEXT_MUTED,
        "ytick.color": TEXT_MUTED,
        "legend.frameon": False,
        "font.size": 9,
    }
)


def controller_colors(labels: list[str]) -> dict[str, str]:
    """Stable colour per controller label."""
    colors = {}
    free = PALETTE[4:]
    for label in sorted(labels):
        if label in FIXED_COLORS:
            colors[label] = FIXED_COLORS[label]
        elif label.startswith("static"):
            colors[label] = NEUTRAL
        elif free:
            colors[label] = free.pop(0)
        else:
            colors[label] = NEUTRAL
    return colors


# ---------------------------------------------------------------- loading


def load_summaries(results_dir: Path) -> pd.DataFrame:
    """One row per run, with metrics flattened into columns."""
    rows = []
    for path in sorted(results_dir.glob("*/*/*.json")):
        summary = json.loads(path.read_text())
        row = {k: v for k, v in summary.items() if k not in ("metrics", "config")}
        row.update(summary["metrics"])
        row["csv"] = str(path.with_suffix(".csv"))
        row["sla_latency_ms"] = summary["config"]["sla_latency_ms"]
        rows.append(row)
    return pd.DataFrame(rows)


def read_run(csv_path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["operator_notes"] = df["operator_notes"].fillna("")
    df["minutes"] = df["time_s"] / 60
    return df


# ---------------------------------------------------------------- time series


def plot_timeseries(
    runs: list[tuple[str, pd.DataFrame]],
    title: str,
    out_path: Path,
    sla_latency_ms: float,
    colors: dict[str, str] | None = None,
) -> None:
    """Three stacked panels sharing the time axis: load vs capacity, replicas, latency."""
    colors = colors or controller_colors([label for label, _ in runs])
    fig, (ax_load, ax_rep, ax_lat) = plt.subplots(3, 1, figsize=(10, 8.5), sharex=True)

    reference = runs[0][1]  # every run on a scenario+seed has the same load trace

    # Shade the ticks where an operator note was visible.
    note_ticks = reference.loc[reference["operator_notes"] != "", "minutes"]
    if len(note_ticks):
        tick_min = reference["minutes"].diff().iloc[1]
        for ax in (ax_load, ax_rep, ax_lat):
            ax.axvspan(
                note_ticks.min(),
                note_ticks.max() + tick_min,
                color="#f0efec",
                zorder=0,
                lw=0,
            )
        ax_load.annotate(
            "operator note visible",
            xy=(note_ticks.min(), 1.0),
            xycoords=("data", "axes fraction"),
            xytext=(3, -12),
            textcoords="offset points",
            color=TEXT_MUTED,
            fontsize=8,
        )

    ax_load.plot(
        reference["minutes"],
        reference["load"],
        color=TEXT,
        lw=1.2,
        label="load (same for all)",
    )
    for label, df in runs:
        c = colors[label]
        ax_load.plot(
            df["minutes"],
            df["capacity"],
            color=c,
            lw=2,
            drawstyle="steps-post",
            label=label,
        )
        ax_rep.plot(
            df["minutes"], df["replicas_ready"], color=c, lw=2, drawstyle="steps-post"
        )
        ax_lat.plot(df["minutes"], df["latency_ms"], color=c, lw=2)

    ax_load.set_title("Load vs. capacity")
    ax_load.set_ylabel("req/s")
    ax_load.set_ylim(bottom=0)

    ax_rep.set_title("Ready replicas")
    ax_rep.set_ylabel("replicas")
    ax_rep.set_ylim(bottom=0)
    ax_rep.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))

    ax_lat.set_title("Latency")
    ax_lat.set_ylabel("ms (log scale)")
    ax_lat.set_yscale("log")
    ax_lat.set_yticks([50, 100, 300, 1000, 2000])
    ax_lat.get_yaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax_lat.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax_lat.axhline(sla_latency_ms, color=TEXT_MUTED, lw=1.2, ls="--")
    ax_lat.annotate(
        f"SLA {sla_latency_ms:.0f} ms",
        xy=(1.0, sla_latency_ms),
        xycoords=("axes fraction", "data"),
        xytext=(-4, 4),
        textcoords="offset points",
        ha="right",
        color=TEXT_MUTED,
        fontsize=8,
    )
    ax_lat.set_xlabel("simulated time (minutes)")

    handles, labels = ax_load.get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        loc="upper right",
        ncol=min(len(labels), 6),
        bbox_to_anchor=(0.99, 0.995),
    )
    fig.suptitle(
        title, x=0.01, y=0.995, ha="left", fontsize=12, fontweight="bold", color=TEXT
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def plot_single_run(
    csv_path: Path, out_path: Path, label: str, scenario: str, config: SimConfig
) -> None:
    """Used by experiments.run to make a quick plot of one run."""
    plot_timeseries(
        [(label, read_run(csv_path))],
        title=f"{scenario}: {label}",
        out_path=out_path,
        sla_latency_ms=config.sla_latency_ms,
    )


# ---------------------------------------------------------------- leaderboard

METRICS = [
    ("sla_violation_ticks", "SLA-violating ticks"),
    ("p95_latency_ms", "p95 latency (ms)"),
    ("mean_latency_ms", "mean latency (ms)"),
    ("replica_minutes", "replica-min"),
    ("scaling_actions", "scaling actions"),
    ("invalid_decisions", "invalid"),
    ("llm_calls", "LLM calls"),
    ("llm_latency_total_s", "LLM time (s)"),
]


def aggregate(summaries: pd.DataFrame) -> pd.DataFrame:
    """Mean and std of every metric per (scenario, controller label)."""
    cols = [m for m, _ in METRICS]
    grouped = summaries.groupby(["scenario", "label"])[cols]
    agg = (
        grouped.mean().add_suffix("_mean").join(grouped.std(ddof=1).add_suffix("_std"))
    )
    agg["runs"] = summaries.groupby(["scenario", "label"]).size()
    return agg.reset_index()


def _fmt(mean: float, std: float, runs: int) -> str:
    digits = 0 if abs(mean) >= 100 else 1
    text = f"{mean:.{digits}f}"
    if runs > 1 and not np.isnan(std):
        text += f" ± {std:.{digits}f}"
    return text


def write_leaderboard_md(agg: pd.DataFrame, out_path: Path) -> None:
    lines = [
        "# Leaderboard",
        "",
        "Ranked by SLA-violating ticks (fewer is better), then replica-minutes (cheaper is better).",
        "Values are mean ± std across runs (seeds × repeats) when there is more than one run.",
        "",
    ]
    for scenario, group in agg.groupby("scenario"):
        group = group.sort_values(["sla_violation_ticks_mean", "replica_minutes_mean"])
        lines += [f"## {scenario}", ""]
        header = ["#", "controller", "runs"] + [name for _, name in METRICS]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for rank, (_, row) in enumerate(group.iterrows(), start=1):
            cells = [str(rank), row["label"], str(int(row["runs"]))]
            cells += [
                _fmt(row[f"{m}_mean"], row[f"{m}_std"], int(row["runs"]))
                for m, _ in METRICS
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    out_path.write_text("\n".join(lines))


def plot_leaderboard(agg: pd.DataFrame, out_path: Path, colors: dict[str, str]) -> None:
    """Grouped bars: one group per scenario, one bar per controller. Two separate
    panels (reliability and cost) rather than two y-axes on one chart."""
    scenarios = sorted(agg["scenario"].unique())
    labels = sorted(agg["label"].unique())
    panels = [
        ("sla_violation_ticks", "SLA-violating ticks (lower is better)"),
        ("replica_minutes", "Replica-minutes / cost (lower is better)"),
    ]

    fig, axes = plt.subplots(1, 2, figsize=(max(8, 2.2 * len(scenarios) * 2), 4.2))
    width = 0.8 / len(labels)
    x = np.arange(len(scenarios))

    for ax, (metric, title) in zip(axes, panels):
        for i, label in enumerate(labels):
            sub = agg[agg["label"] == label].set_index("scenario").reindex(scenarios)
            std = sub[f"{metric}_std"].fillna(0).to_numpy()
            ax.bar(
                x - 0.4 + width * (i + 0.5),
                sub[f"{metric}_mean"].to_numpy(),
                width=width,
                color=colors[label],
                edgecolor=SURFACE,
                linewidth=1.5,  # thin surface-coloured gap between adjacent bars
                yerr=std if std.any() else None,
                error_kw={"ecolor": TEXT_MUTED, "elinewidth": 1, "capsize": 2},
                label=label,
            )
        ax.set_title(title)
        ax.set_xticks(x, scenarios)
        ax.grid(axis="x", visible=False)
        ax.set_ylim(bottom=0)

    handles, legend_labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        legend_labels,
        loc="lower center",
        ncol=min(len(labels), 6),
        bbox_to_anchor=(0.5, 0.0),
    )
    fig.tight_layout(rect=(0, 0.1, 1, 1))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    summaries = load_summaries(args.results_dir)
    if summaries.empty:
        print(
            f"No results in {args.results_dir}. Run something first, e.g.\n"
            "  python -m experiments.run --controller static --scenario spike"
        )
        return 1

    colors = controller_colors(list(summaries["label"].unique()))
    plots_dir = args.results_dir / "plots"

    for scenario, group in summaries.groupby("scenario"):
        seed = int(group["seed"].min())
        reps = group[(group["seed"] == seed) & (group["repeat"] == 0)].sort_values(
            "label"
        )
        runs = [(row["label"], read_run(row["csv"])) for _, row in reps.iterrows()]
        out = plots_dir / f"{scenario}_timeseries.png"
        plot_timeseries(
            runs,
            f"{scenario} (seed {seed})",
            out,
            float(reps["sla_latency_ms"].iloc[0]),
            colors,
        )
        print(f"wrote {out}")

    agg = aggregate(summaries)
    md_path = args.results_dir / "leaderboard.md"
    write_leaderboard_md(agg, md_path)
    print(f"wrote {md_path}")
    bar_path = plots_dir / "leaderboard.png"
    plot_leaderboard(agg, bar_path, colors)
    print(f"wrote {bar_path}")
    print()
    print(md_path.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
