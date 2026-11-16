"""Every Prometheus query the project uses, in one place.

The experiment runner (to export results) and the cluster LLM controller (to
observe the system) both read the app through these queries, so every
controller and every result file see the same numbers. Controllers must never
write their own PromQL: if you need a new signal, add it here and document it
in docs/telemetry.md.

Changing a query or window changes everyone's data. Do it in a PR, and don't
compare results produced before and after the change.
"""

# What we measure: fixed names, see cloud-env/k8s/deployment.yaml.
NAMESPACE = "scale"
DEPLOYMENT = "scale-app"
CONTAINER = "app"
POD_REGEX = f"{DEPLOYMENT}-.*"

# One snapshot every 15 s (= Prometheus' scrape interval).
INTERVAL_SECONDS = 15
# Rates and the CPU average look back this far from each snapshot's timestamp.
# 1 minute = 4 scrapes, so one late or missed scrape doesn't leave a gap.
RATE_WINDOW = "1m"

_ns = f'namespace="{NAMESPACE}"'
_deploy = f'{_ns},deployment="{DEPLOYMENT}"'
_requests = f'scale_app_requests_total{{{_ns},path="/work"}}'
_buckets = f'scale_app_request_duration_seconds_bucket{{{_ns},path="/work"}}'
# 0 if Prometheus is scraping at least one app pod, absent otherwise. Used so
# "no requests" reads as 0 while "app not scraped at all" stays missing.
_zero_if_app_scraped = f'(0 * count(up{{{_ns},service="{DEPLOYMENT}"}} == 1))'


def _latency_quantile(q: float) -> str:
    return (
        f"1000 * histogram_quantile({q}, "
        f"sum by (le) (rate({_buckets}[{RATE_WINDOW}])))"
    )


# Snapshot fields, in CSV column order, with the query that produces each one.
QUERIES: dict[str, str] = {
    # Replicas the Deployment asks for (what the controller set).
    "replicas_desired": f"kube_deployment_spec_replicas{{{_deploy}}}",
    # Pods passing their readiness probe: the ones receiving traffic.
    "replicas_ready": f"kube_deployment_status_replicas_ready{{{_deploy}}}",
    # Pods that exist but aren't ready yet (starting up).
    "replicas_starting": (
        f"clamp_min(kube_deployment_status_replicas{{{_deploy}}}"
        f" - kube_deployment_status_replicas_ready{{{_deploy}}}, 0)"
    ),
    # Kubernetes' definition (what HPA targets): CPU used / CPU requested, in %,
    # summed over the app's running pods. Can exceed 100 (the limit is 5x the request).
    "cpu_utilization_pct": (
        f"100 * sum(rate(container_cpu_usage_seconds_total"
        f'{{{_ns},pod=~"{POD_REGEX}",container="{CONTAINER}"}}[{RATE_WINDOW}]))'
        f" / sum(kube_pod_container_resource_requests"
        f'{{{_ns},pod=~"{POD_REGEX}",container="{CONTAINER}",resource="cpu"}}'
        f' * on (namespace, pod) group_left () (kube_pod_status_phase{{{_ns},phase="Running"}} == 1))'
    ),
    # Requests per second reaching /work, all pods together.
    "request_rate_rps": f"sum(rate({_requests}[{RATE_WINDOW}])) or {_zero_if_app_scraped}",
    "latency_p50_ms": _latency_quantile(0.50),
    "latency_p95_ms": _latency_quantile(0.95),
    # Fraction (0-1) of /work requests answered with a 5xx status.
    "error_rate": (
        f'(sum(rate(scale_app_requests_total{{{_ns},path="/work",status=~"5.."}}[{RATE_WINDOW}]))'
        f" or {_zero_if_app_scraped})"
        f" / sum(rate({_requests}[{RATE_WINDOW}]))"
    ),
}

FIELDS = list(QUERIES)
