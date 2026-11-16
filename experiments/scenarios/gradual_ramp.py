"""Example scenario: gradual ramp. Light load, ramp up, hold, drop back.

Stages are fractions of the run duration (--duration in run_experiment.py,
passed to Locust as --run-time), so a 3-minute and a 10-minute run have the
same shape:

    users
     30 |            ______________
        |          /               \\
        |        /                  \\
      2 |______/                     \\______
        0     10%      40%       70%  75%   100%   of the duration
"""

from locust import LoadTestShape

from common import AppUser

BASE_USERS = 2
PEAK_USERS = 30   # ~60 req/s, roughly 6 pods' worth of CPU at the 60% HPA target

# (end of stage as a fraction of the run, users at that point). Linear in between.
STAGES = [
    (0.10, BASE_USERS),
    (0.40, PEAK_USERS),
    (0.70, PEAK_USERS),
    (0.75, BASE_USERS),
    (1.00, BASE_USERS),
]
DEFAULT_DURATION_S = 600  # if run with plain `locust` and no --run-time


class User(AppUser):
    """Each user requests /work about twice a second (see common.py)."""


class GradualRamp(LoadTestShape):
    use_common_options = True  # lets us read --run-time

    def tick(self):
        duration = self.runner.environment.parsed_options.run_time or DEFAULT_DURATION_S
        progress = self.get_run_time() / duration
        if progress >= 1:
            return None  # stop the test
        start_fraction, start_users = 0.0, STAGES[0][1]
        for end_fraction, end_users in STAGES:
            if progress < end_fraction:
                f = (progress - start_fraction) / (end_fraction - start_fraction)
                users = round(start_users + f * (end_users - start_users))
                return max(users, 1), 10  # (users, spawn rate per second)
            start_fraction, start_users = end_fraction, end_users
        return None
