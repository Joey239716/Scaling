"""Ramp workload: mirrors experiments/configs/ramp.yaml, then drops.

Each user sends 1 request/s, so the number of users = req/s.
Timeline (1 simulator tick = 15 s):
  0:00-2:30   hold 100 req/s       (ticks 0-10)
  2:30-12:30  ramp 100 -> 500      (ticks 10-50)
  12:30-17:30 hold 500
  17:30-25:00 drop to 20 req/s, so you can watch HPA's 5-minute scale-down
Then the test stops.
"""

from locust import FastHttpUser, LoadTestShape, constant_throughput, task

START, PEAK, IDLE = 100, 500, 20
RAMP_START, RAMP_END, HOLD_END, STOP = 150, 750, 1050, 1500  # seconds


class AppUser(FastHttpUser):
    wait_time = constant_throughput(1)  # 1 request/s per user

    @task
    def work(self):
        # New connection per request. Kubernetes picks a pod per *connection*,
        # so with keep-alive each user stays stuck on whichever pod it first
        # hit: early users pile onto the original pods and pods HPA adds
        # later sit half idle. Closing the connection spreads every request.
        self.client.get("/work", headers={"Connection": "close"})


class Ramp(LoadTestShape):
    def tick(self):
        t = self.get_run_time()
        if t < RAMP_START:
            users = START
        elif t < RAMP_END:
            frac = (t - RAMP_START) / (RAMP_END - RAMP_START)
            users = round(START + frac * (PEAK - START))
        elif t < HOLD_END:
            users = PEAK
        elif t < STOP:
            users = IDLE
        else:
            return None  # end the test
        return users, 100  # (users, spawn/stop rate per second)
