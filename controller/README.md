# controller/

Decision-making logic for autoscaling — both the baselines and the LLM-based controllers.

## Current layout (Camp QMIND)

| File | Status | What it is |
|---|---|---|
| `base.py` | done | `Observation`, `Decision`, `Controller`: the interface every controller follows |
| `static.py` | done | fixed replica count; the smallest complete example, read it first |
| `human.py` | done | you are the autoscaler (interactive CLI) |
| `llm_client.py` | done | Anthropic API wrapper: `.env` key, retries, timeout, disk cache, offline replay |
| `hpa_baseline.py` | done | Kubernetes HPA formula (Pair A) |
| `keda_baseline.py` | done | KEDA-style scaling on request rate instead of CPU |
| `openrouter_client.py` | done | same interface, but calls OpenRouter (non-Anthropic models); reuses the cache and offline replay |
| `llm_controller.py` | **partly stub** | plumbing done; prompt + parser are Pair B's |
| `scheduled_baseline.py` | **stub** | cron-style pre-scaling (stretch goal) |

See [camp_qmind.md](../docs/onboarding/camp_qmind.md) for the tasks. Specs for the stubs are in `tests/todo/`.

## Longer term

Expect subfolders per controller variant as the design solidifies, e.g.:

```
controller/
├── hpa_baseline/        # wraps/mirrors Kubernetes HPA behavior for fair comparison
├── llm_metric_only/     # LLM controller with only telemetry as input
├── llm_with_history/    # + recent state/action history
├── llm_contextual/      # + natural-language operator context
└── hierarchical/        # LLM planner + fast low-level controller
```

Controllers must not know they are being evaluated — see [AGENTS.md](../AGENTS.md) and [CONTRIBUTING.md](../CONTRIBUTING.md).
