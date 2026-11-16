"""Shared pieces for every scenario.

AppUser: the Locust user. Import it instead of HttpUser:

    from common import AppUser

Why: Kubernetes balances *connections* across pods, not requests. A normal
Locust user keeps one connection open for the whole test, so pods added by a
controller mid-run would get almost no traffic. AppUser closes the connection
after every request, so each request is balanced across all ready pods.

DurationShape: an optional base class for a load shape written as
"users at this point of the run" (0.0 = start, 1.0 = end), so --duration
stretches the same shape. Use it as `import common` + `common.DurationShape`:
importing the class by name would make Locust see it as a second shape.
"""

from locust import HttpUser, LoadTestShape, between, task

DEFAULT_DURATION_S = 600  # if a scenario is run with plain `locust` and no --run-time


class AppUser(HttpUser):
    abstract = True               # Locust won't run this class on its own
    wait_time = between(0.4, 0.6)  # ~2 requests per second per user

    @task
    def work(self):
        self.client.get("/work", headers={"Connection": "close"})


class DurationShape(LoadTestShape):
    """Base class: subclasses implement users_at(progress) for 0 <= progress < 1.

    Handles reading --run-time and stopping at the end of the run.
    """

    use_common_options = True  # lets us read --run-time
    spawn_rate = 10  # users started/stopped per second when the count changes

    def users_at(self, progress: float) -> float:
        raise NotImplementedError

    def tick(self):
        duration = self.runner.environment.parsed_options.run_time or DEFAULT_DURATION_S
        progress = self.get_run_time() / duration
        if progress >= 1:
            return None  # stop the test
        return max(1, round(self.users_at(progress))), self.spawn_rate
