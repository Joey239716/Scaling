# llm: our LLM-based controller (to build)

**Status: not implemented.** The runner refuses to run it until this folder has a `start.sh`.
Read the contract in [controllers/README.md](../README.md) first.

This controller runs as a **separate process** on your machine for the whole run:

```
controllers/llm/
├── README.md
├── start.sh        # exec python -m controllers.llm.main   (runs until SIGTERM)
└── main.py         # loop: observe -> ask the model -> guardrails -> scale
```

Contract details that matter here:
- **Observe** with `from telemetry import latest_snapshot` (one dict per call, fields in
  [docs/telemetry.md](../../docs/telemetry.md)). No PromQL of your own.
- **Act** with `kubectl --context "$KUBE_CONTEXT" -n "$SCALE_NAMESPACE" scale deployment
  "$SCALE_DEPLOYMENT" --replicas=N`, **only after** guardrails: clamp to
  `SCALE_MIN_REPLICAS`..`SCALE_MAX_REPLICAS`, enforce a cooldown, reject unparseable output (AGENTS.md).
- **Stop** cleanly on `SIGTERM` (the runner sends it at the end of every run; `SIGKILL` follows after 20 s).
- **Log** every decision to stdout: it lands in `controller.log` next to the run's `metrics.csv`.
- **API key:** read `ANTHROPIC_API_KEY` (or `OPENROUTER_API_KEY`) from the environment or the gitignored
  `.env`. Never commit it. The runner passes its own environment through to `start.sh`.

The simulator's prompt, parser and guardrails in `controller/llm_controller.py` and
`controller/llm_client.py` are a good starting point, but don't import experiment configs or
scenario details: the controller must not know it's being evaluated.

Write down here: model and version, temperature, decision interval, cooldown, prompt version.
