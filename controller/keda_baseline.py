"""KEDA baseline: scale on request rate instead of CPU.

Tests: tests/core/test_keda.py. Run with
`python -m experiments.run --controller keda --scenario spike`.

KEDA (keda.sh) is the common way to autoscale Kubernetes on metrics other than
CPU -- queue length, requests/sec, etc. Under the hood it doesn't have its own
algorithm: it feeds the metric to a normal HPA as an "external metric" with an
AverageValue target ("each replica should handle N req/s"). So this is the HPA
algorithm from hpa_baseline.py, with request rate in place of CPU. Like HPA, it
ignores operator notes.

THE FORMULA
-----------
    desired = ceil(request_rate / target_rps_per_replica)

with target_rps_per_replica = 30. Why 30: one replica serves 50 req/s at 100%
CPU, and HPA targets 60% CPU, i.e. 30 req/s. Same target utilization for both
baselines, so any difference between them comes from the metric they watch.

Worked example: the spike scenario jumps to 480 req/s while 2 replicas run:
    desired = ceil(480 / 30) = 16
HPA sees the same moment as "2 replicas at 100% CPU" (CPU is capped), so it
asks for only ceil(2 * 100/60) = 4. Request rate is NOT capped -- it's the raw
incoming demand -- so KEDA can see how big the spike really is.

Scale-down: 6 replicas at 90 req/s -> ceil(90 / 30) = 3.

TOLERANCE (10%)
---------------
As in HPA: if request_rate / (target * current_replicas) is within 0.9..1.1,
keep the current count. 4 replicas at 126 req/s: 126 / 120 = 1.05 -> stay at 4.

current_replicas is obs.replicas_total (ready + pending): for external metrics
HPA compares against the replica count it has already asked for, not just the
ready ones.

SCALE-DOWN STABILIZATION (5 minutes = 20 ticks)
-----------------------------------------------
Same as HPA: scale up immediately, but use the max recommendation of the last
20 ticks, so scale-down waits 5 minutes after the last high recommendation.

NOT MODELLED
------------
KEDA can scale to zero (activationThreshold, cooldownPeriod). Our cluster never
goes below min_replicas >= 1, so that part doesn't apply. KEDA's pollingInterval
only matters for that 0 <-> 1 activation; above 1 replica the HPA loop drives it.

Like every controller, this doesn't clamp to min/max replicas or limit the step
size: the environment applies those guardrails equally to everyone.
"""

from __future__ import annotations

import math
from collections import deque

from controller.base import Controller, Decision, Observation


class KEDAController(Controller):
    name = "keda"

    def __init__(
        self,
        target_rps_per_replica: float = 30.0,
        tolerance: float = 0.1,
        stabilization_ticks: int = 20,
    ):
        self.target_rps_per_replica = target_rps_per_replica
        self.tolerance = tolerance
        self.stabilization_ticks = stabilization_ticks
        self.reset()

    def reset(self) -> None:
        self.recent_recommendations = deque(maxlen=self.stabilization_ticks)

    def decide(self, obs: Observation) -> Decision:
        current = max(obs.replicas_total, 1)
        rate = obs.request_rate
        ratio = rate / (self.target_rps_per_replica * current)

        if abs(ratio - 1.0) <= self.tolerance:
            recommendation = current
        else:
            recommendation = math.ceil(rate / self.target_rps_per_replica)
        recommendation = max(recommendation, 1)  # zero traffic still needs a replica
        self.recent_recommendations.append(recommendation)
        target = max(self.recent_recommendations)

        reason = (
            f"rate {rate:.0f} req/s / {self.target_rps_per_replica:.0f} per replica "
            f"on {current} replicas -> {recommendation}"
        )
        if target > recommendation:
            reason += f", held at {target} by the {self.stabilization_ticks}-tick window"
        return Decision(target_replicas=target, info={"reason": reason})
