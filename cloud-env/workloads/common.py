"""Shared pieces for every workload in this folder.

Each workload file imports AppUser and subclasses RateShape. Because each
user sends 1 request/s, "users" and "req/s" are the same number throughout.
(That holds while responses take under 1 s; slower responses mean each user
sends less, so the real rate drops below the target.)

Every workload follows the same timeline, matching the simulator:
  0 - 20:00     the pattern itself (80 ticks x 15 s)
  20:00 - 27:30 cooldown at IDLE req/s, to watch HPA's 5-minute scale-down
"""

from locust import FastHttpUser, LoadTestShape, constant_throughput, task

TICK = 15  # seconds per simulator tick
PATTERN_END = 80 * TICK  # 1200 s
COOLDOWN_END = PATTERN_END + 450  # 1650 s
IDLE = 20  # req/s during cooldown


class AppUser(FastHttpUser):
    wait_time = constant_throughput(1)  # 1 request/s per user

    @task
    def work(self):
        # New connection per request. Kubernetes picks a pod per *connection*,
        # so with keep-alive each user stays stuck on whichever pod it first
        # hit: early users pile onto the original pods and pods HPA adds
        # later sit half idle. Closing the connection spreads every request.
        self.client.get("/work", headers={"Connection": "close"})


class RateShape(LoadTestShape):
    """Base class: subclasses implement rate(t) -> req/s for 0 <= t < PATTERN_END.

    Handles the cooldown and stopping, so each workload file only describes
    its pattern. Import this module as `import common` and subclass
    `common.RateShape`, so Locust doesn't see this base class as a second shape.
    """

    spawn_rate = 100  # users started/stopped per second when the rate changes

    def rate(self, t: float) -> float:
        raise NotImplementedError

    def tick(self):
        t = self.get_run_time()
        if t < PATTERN_END:
            users = self.rate(t)
        elif t < COOLDOWN_END:
            users = IDLE
        else:
            return None  # end the test
        return max(1, round(users)), self.spawn_rate
