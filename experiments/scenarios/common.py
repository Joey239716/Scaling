"""Shared Locust user for every scenario. Import it instead of HttpUser:

    from common import AppUser

Why: Kubernetes balances *connections* across pods, not requests. A normal
Locust user keeps one connection open for the whole test, so pods added by a
controller mid-run would get almost no traffic. AppUser closes the connection
after every request, so each request is balanced across all ready pods.
"""

from locust import HttpUser, between, task


class AppUser(HttpUser):
    abstract = True               # Locust won't run this class on its own
    wait_time = between(0.4, 0.6)  # ~2 requests per second per user

    @task
    def work(self):
        self.client.get("/work", headers={"Connection": "close"})
