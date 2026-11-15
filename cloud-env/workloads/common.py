"""Shared pieces for every workload in this folder.

Each workload file imports AppUser and subclasses RateShape. Because each
user sends 1 request/s, "users" and "req/s" are the same number throughout.
(That holds while responses take under 1 s; slower responses mean each user
sends less, so the real rate drops below the target.)

Each workload writes rate(t) for a 20-minute pattern (the simulator's 80
ticks of 15 s). RUN_LENGTH (env var, set by run_experiment.py) picks how
fast that pattern is played:

  short (default)  4x faster: 5-min pattern + 2-min cooldown = 7 min.
                   run_experiment.py shortens the autoscalers' scale-down
                   wait from 5 min to 1 min to match.
  long             real time: 20-min pattern + 7.5-min cooldown = 27.5 min,
                   with the default 5-min scale-down wait.

The cooldown runs at IDLE req/s, so you can watch the autoscaler scale down.
"""

import os
import random
import time

from locust import FastHttpUser, LoadTestShape, task

TICK = 15  # seconds per simulator tick
PATTERN = 80 * TICK  # 1200 s: the timeline rate(t) is written for

LONG = os.environ.get("RUN_LENGTH", "short") == "long"
SPEED = 1 if LONG else 4  # pattern seconds played per real second
PATTERN_END = PATTERN / SPEED  # real seconds: 1200 long, 300 short
COOLDOWN_END = PATTERN_END + (450 if LONG else 120)  # 1650 long, 420 short
IDLE = 20  # req/s during cooldown


RECONNECT_EVERY = 10  # requests (~10 s) per connection


class AppUser(FastHttpUser):
    """Sends 1 request/s on average, reconnecting every RECONNECT_EVERY requests.

    Why reconnect at all: Kubernetes picks a pod per *connection*, so with
    plain keep-alive each user stays on whichever pod it first hit. Early
    users pile onto the original pods while pods the autoscaler adds later
    sit half idle. Reconnecting every ~10 s moves load onto new pods quickly.

    Why not a new connection per request: at ~300 req/s that churns so many
    connections that the node's connection table reuses ports too soon and
    drops connection attempts; each drop costs a 1 s TCP retry, and median
    latency jumped from ~100 ms to ~1.1 s even on idle pods.
    """

    def wait_time(self):
        """Random gap, averaging 1 request/s per user.

        Not constant_throughput(1): that keeps every user exactly 1 s apart,
        and since Locust starts users within a fraction of a second, they all
        fired in one clump each second. Pods then worked through the clump
        one request at a time: at 50 req/s on 2 idle pods, median latency was
        62 ms instead of 5 ms. Exponential gaps are how independent users
        arrive. Like constant_throughput, the time the request took is
        subtracted, so slow responses don't lower the rate.
        """
        return max(0.0, random.expovariate(1.0) - (time.monotonic() - self.started))

    def on_start(self):
        self.sent = 0

    @task
    def work(self):
        self.started = time.monotonic()
        self.sent += 1
        if self.sent % RECONNECT_EVERY == 0:
            # Server closes the connection after this reply; the next
            # request opens a new one, which may land on a different pod.
            self.client.get("/work", headers={"Connection": "close"})
        else:
            self.client.get("/work")


class RateShape(LoadTestShape):
    """Base class: subclasses implement rate(t) -> req/s for 0 <= t < PATTERN,
    with t on the 20-minute timeline even in a short run.

    Handles the speed-up, the cooldown and stopping, so each workload file only describes
    its pattern. Import this module as `import common` and subclass
    `common.RateShape`, so Locust doesn't see this base class as a second shape.
    """

    spawn_rate = 100  # users started/stopped per second when the rate changes

    def rate(self, t: float) -> float:
        raise NotImplementedError

    def tick(self):
        t = self.get_run_time()
        if t < PATTERN_END:
            users = self.rate(t * SPEED)
        elif t < COOLDOWN_END:
            users = IDLE
        else:
            return None  # end the test
        return max(1, round(users)), self.spawn_rate
