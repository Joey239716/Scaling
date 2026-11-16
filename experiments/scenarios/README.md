# experiments/scenarios/: Locust traffic for cluster experiments

Each `<name>.py` here is one scenario, run with `--scenario <name>`. Every controller
gets exactly the same traffic for a given scenario.

| Scenario | Shape |
|---|---|
| `gradual_ramp.py` | Example: 2 users, ramp to 30, hold, back to 2 (stages are fractions of `--duration`) |

`common.py` isn't a scenario: it holds `AppUser`, the shared user class.

## Adding a scenario

1. Copy `gradual_ramp.py` to `<your_name>.py` and change `STAGES` (or write any
   `LoadTestShape`).
2. Keep these three things, or results stop being comparable:
   - **Subclass `AppUser`** from `common.py`. It closes the connection after every request so
     Kubernetes spreads requests across all pods; a plain `HttpUser` sticks to the first pods.
     Don't change its `wait_time` or task in your scenario: vary the *number of users* instead.
   - **Read the duration from `--run-time`** (`use_common_options = True` and
     `self.runner.environment.parsed_options.run_time`), so `--duration` works.
   - **No randomness that differs between runs.** If you want noise, seed it
     (`random.Random(42)`) so every controller sees the same traffic.
3. Don't reuse a simulator scenario name (`ramp`, `spike`, `spike_with_warning`, ...: anything in
   `experiments/configs/`). Both write to `experiments/results/<scenario>/`, so the runner refuses those names.
4. Try it without a controller first:
   ```bash
   minikube service scale-app -n scale -p scale --url     # prints the URL (keep it running on macOS)
   cd experiments/scenarios
   locust -f <your_name>.py --headless --host <URL> --run-time 120s
   ```
5. Describe the shape (and what it's meant to test) in the table above.

Peak load guide: each user sends about 2 req/s, each request costs about 7 ms of CPU, and a pod
at the HPA's 60% target handles about 9 req/s. So 30 users needs about 6 to 7 pods, and the
10-pod maximum is reached around 45 users. Minikube has 4 CPUs in total, so going far beyond
that saturates the cluster.
