"""Shared telemetry: the one place that turns Prometheus data into snapshots.

The schema is defined in docs/telemetry.md; the queries are in queries.py.
"""

from telemetry.prometheus import latest_snapshot, snapshots_between
from telemetry.queries import FIELDS, INTERVAL_SECONDS, QUERIES

__all__ = ["FIELDS", "INTERVAL_SECONDS", "QUERIES", "latest_snapshot", "snapshots_between"]
