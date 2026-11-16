"""Run one controller on one scenario and save the results.

Examples:
    python -m experiments.run --controller static --scenario spike
    python -m experiments.run --controller hpa --scenario ramp --seed 3
    python -m experiments.run --controller llm_context --scenario spike_with_warning --repeats 3
    python -m experiments.run --controller human --scenario spike_with_warning --label alice
    python -m experiments.run --controller llm_context --scenario spike_with_warning \
        --provider openrouter --model meta-llama/llama-3.3-70b-instruct --openrouter-upstream Fireworks

For each run (seed, repeat) this writes, under experiments/results/<scenario>/<label>/:
    seed<S>_rep<R>.csv   one row per tick
    seed<S>_rep<R>.json  summary metrics (what the leaderboard uses)
    seed<S>_rep<R>.png   quick plot of this run

Then `python -m experiments.plot` compares everything you've run.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from controller.base import Controller
from controller.hpa_baseline import HPAController
from controller.human import HumanController, QuitGame
from controller.keda_baseline import KEDAController
from controller.llm_client import LLMClient
from controller.llm_controller import LLMController
from controller.openrouter_client import OpenRouterClient, parse_upstream
from controller.scheduled_baseline import ScheduledController, parse_schedule
from controller.static import StaticController
from simulator.config import SimConfig
from simulator.env import ScalingEnv, run_episode
from simulator.metrics import summarize
from simulator.scenario import Scenario, load_scenario

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "experiments" / "configs"
RESULTS_DIR = REPO_ROOT / "experiments" / "results"
CACHE_DIR = REPO_ROOT / "experiments" / "cache"

CONTROLLERS = ["static", "human", "hpa", "keda", "llm", "llm_context", "scheduled"]
PROVIDERS = ["anthropic", "openrouter"]


def resolve_scenario(name_or_path: str) -> Scenario:
    path = Path(name_or_path)
    if not path.exists():
        path = CONFIG_DIR / f"{name_or_path}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in CONFIG_DIR.glob("*.yaml"))
        raise SystemExit(
            f"Unknown scenario {name_or_path!r}. Available: {', '.join(available)}"
        )
    return load_scenario(path)


def make_llm_client(args: argparse.Namespace, repeat_index: int) -> LLMClient:
    """Build the API client for the LLM controllers, for the chosen --provider."""
    if args.provider == "openrouter":
        client = OpenRouterClient(
            model=args.model,
            cache_dir=args.cache_dir,
            offline=args.offline,
            repeat_index=repeat_index,
            upstream=parse_upstream(args.openrouter_upstream),
        )
        if not client.model:
            raise SystemExit(
                "--provider openrouter needs a model: pass --model <id> "
                "(e.g. meta-llama/llama-3.3-70b-instruct) or set OPENROUTER_MODEL in .env"
            )
        return client
    return LLMClient(
        model=args.model,
        cache_dir=args.cache_dir,
        offline=args.offline,
        repeat_index=repeat_index,
    )


def default_label(controller: Controller) -> str:
    """The leaderboard name when --label isn't given.

    Normally the controller's name. OpenRouter runs add the model, so trying a
    second model doesn't overwrite the first one's results (or the Anthropic run's).
    """
    if (
        isinstance(controller, LLMController)
        and controller.client.provider_name == "openrouter"
    ):
        return f"{controller.name}-{controller.client.model}"
    return controller.name


def make_controller(
    args: argparse.Namespace, repeat_index: int, cfg: SimConfig, label: str
) -> Controller:
    """Build the controller. This is the only place that knows about experiment
    settings -- the controller itself just gets its constructor arguments."""
    if args.controller == "static":
        return StaticController(replicas=args.replicas)
    if args.controller == "human":
        return HumanController(name=label, sla_latency_ms=cfg.sla_latency_ms)
    if args.controller == "hpa":
        return HPAController()
    if args.controller == "keda":
        return KEDAController()
    if args.controller in ("llm", "llm_context"):
        return LLMController(
            make_llm_client(args, repeat_index),
            use_context=(args.controller == "llm_context"),
            use_history=not args.no_history,
        )
    if args.controller == "scheduled":
        if not args.schedule:
            raise SystemExit(
                '--controller scheduled needs --schedule, e.g. --schedule "26:17,52:2"'
            )
        return ScheduledController(parse_schedule(args.schedule))
    raise SystemExit(f"Unknown controller {args.controller!r}")


def safe_label(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "run"


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=False,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def print_human_intro(cfg: SimConfig) -> None:
    print(f"""
=== You are the autoscaler ===
Each tick is {cfg.tick_seconds:.0f} simulated seconds. Each ready replica handles
{cfg.capacity_per_replica:.0f} req/s. Keep latency under {cfg.sla_latency_ms:.0f} ms without wasting replicas.

  * New replicas take {cfg.startup_delay_ticks} ticks ({cfg.startup_delay_ticks * cfg.tick_seconds:.0f} s) to start. Removing is instant.
  * You can change by at most {cfg.max_step} per tick, between {cfg.min_replicas} and {cfg.max_replicas} replicas.
  * Watch for OPERATOR NOTES -- they might tell you something useful.
""")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--controller", required=True, choices=CONTROLLERS)
    parser.add_argument(
        "--scenario",
        required=True,
        help="name in experiments/configs/ (or a YAML path)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="same seed = identical load trace (default 0)",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="run N times on the same trace (for LLM variance)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="LLM: only replay cached responses, no API calls",
    )
    parser.add_argument(
        "--provider",
        choices=PROVIDERS,
        default="anthropic",
        help="LLM: which API serves the model (default anthropic)",
    )
    parser.add_argument(
        "--model",
        help="LLM: model id (default: SCALE_MODEL for anthropic, "
        "OPENROUTER_MODEL for openrouter)",
    )
    parser.add_argument(
        "--openrouter-upstream",
        help='openrouter: pin the upstream provider(s), e.g. "Fireworks" '
        "(default: OPENROUTER_UPSTREAM, else unpinned)",
    )
    parser.add_argument(
        "--label",
        help="name on the leaderboard (default: the controller's name; "
        "openrouter runs add the model)",
    )
    parser.add_argument(
        "--replicas",
        type=int,
        default=2,
        help="static: fixed replica count (default 2)",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="LLM: hide recent history from the prompt",
    )
    parser.add_argument(
        "--schedule", help='scheduled: "tick:replicas,..." e.g. "26:17,52:2"'
    )
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--no-plot", action="store_true", help="skip the per-run PNG")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log every guardrail clamp and LLM detail",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    scenario = resolve_scenario(args.scenario)
    cfg = ScalingEnv(scenario, seed=args.seed).config

    label = args.label
    if args.controller == "human":
        print_human_intro(cfg)
        if not label:
            label = input("Player name (for the leaderboard): ").strip() or "anonymous"
        label = f"human-{label}" if not label.startswith("human") else label

    out_dir = None
    for repeat in range(args.repeats):
        controller = make_controller(args, repeat, cfg, label or "")
        run_label = safe_label(label or default_label(controller))
        env = ScalingEnv(scenario, seed=args.seed)

        started = time.perf_counter()
        try:
            records = run_episode(env, controller)
        except NotImplementedError as e:
            print(
                f"\n'{args.controller}' isn't implemented yet: {e}\n"
                "That's the Camp QMIND task -- see docs/onboarding/camp_qmind.md.",
                file=sys.stderr,
            )
            return 2
        except (QuitGame, KeyboardInterrupt, EOFError):
            print("\nQuit -- nothing saved.")
            return 1
        elapsed = time.perf_counter() - started
        if args.controller == "human":
            print("\n\n=== Game over! ===")

        metrics = summarize(records, env.config.tick_seconds)
        out_dir = args.results_dir / scenario.name / run_label
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = out_dir / f"seed{args.seed}_rep{repeat}"

        pd.DataFrame(records).to_csv(stem.with_suffix(".csv"), index=False)
        summary = {
            "label": run_label,
            "controller": controller.name,
            "scenario": scenario.name,
            "seed": args.seed,
            "repeat": repeat,
            "provider": (
                controller.client.provider_name
                if isinstance(controller, LLMController)
                else None
            ),
            "model": (
                controller.client.model
                if isinstance(controller, LLMController)
                else None
            ),
            # OpenRouter only: which hosting provider(s) the model was pinned to.
            "upstream": (
                getattr(controller.client, "upstream", None)
                if isinstance(controller, LLMController)
                else None
            ),
            "offline": args.offline,
            "wall_time_s": round(elapsed, 2),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_commit": git_commit(),
            "config": asdict(env.config),
            "metrics": metrics,
        }
        stem.with_suffix(".json").write_text(json.dumps(summary, indent=2))

        if not args.no_plot:
            from experiments.plot import (
                plot_single_run,
            )  # imported lazily: matplotlib is slow to load

            plot_single_run(
                stem.with_suffix(".csv"),
                stem.with_suffix(".png"),
                run_label,
                scenario.name,
                env.config,
            )

        m = metrics
        print(
            f"[{run_label} | {scenario.name} | seed {args.seed} | rep {repeat}] "
            f"SLA violations {m['sla_violation_ticks']}/{m['ticks']} ticks, "
            f"p95 latency {m['p95_latency_ms']:.0f} ms, "
            f"replica-min {m['replica_minutes']:.1f}, "
            f"scaling actions {m['scaling_actions']}, "
            f"invalid {m['invalid_decisions']}, clamped {m['clamped_decisions']}"
            + (
                f", LLM calls {m['llm_calls']} ({m['llm_latency_total_s']:.1f} s)"
                if m["llm_calls"]
                else ""
            )
        )

    print(f"Results: {out_dir}")
    print("Compare all runs: python -m experiments.plot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
