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


RECONNECT_EVERY = 10  # requests (~10 s) per connection


class AppUser(FastHttpUser):
    """Sends 1 request/s, reconnecting every RECONNECT_EVERY requests.

    Why reconnect at all: Kubernetes picks a pod per *connection*, so with
    plain keep-alive each user stays on whichever pod it first hit. Early
    users pile onto the original pods while pods the autoscaler adds later
    sit half idle. Reconnecting every ~10 s moves load onto new pods quickly.

    Why not a new connection per request: at ~300 req/s that churns so many
    connections that the node's connection table reuses ports too soon and
    drops connection attempts; each drop costs a 1 s TCP retry, and median
    latency jumped from ~100 ms to ~1.1 s even on idle pods.
    """

    wait_time = constant_throughput(1)  # 1 request/s per user

    def on_start(self):
        self.sent = 0

    @task
    def work(self):
        self.sent += 1
        if self.sent % RECONNECT_EVERY == 0:
            # Server closes the connection after this reply; the next
            # request opens a new one, which may land on a different pod.
            self.client.get("/work", headers={"Connection": "close"})
        else:
            self.client.get("/work")


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
