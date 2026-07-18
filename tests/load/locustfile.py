"""Locust load profile for the Pitwall public API (release program).

Run against a live Pitwall API, e.g.:

    locust -f tests/load/locustfile.py --host http://127.0.0.1:8080 \
        --users 50 --spawn-rate 5 --run-time 2m --headless

The write task uses ``dry_run: true`` so the load profile performs routing +
cost estimation only and never triggers a paid RunPod call.
"""

from __future__ import annotations

from locust import HttpUser, between, task


class PitwallUser(HttpUser):
    wait_time = between(0.5, 2.0)

    @task(8)
    def list_capabilities(self) -> None:
        self.client.get("/v1/capabilities", name="GET /v1/capabilities")

    @task(2)
    def dry_run_inference(self) -> None:
        # dry_run=true → routing + cost estimation only, no paid RunPod call.
        self.client.post(
            "/v1/inference",
            json={"capability_id": "embedding.bge-m3", "dry_run": True},
            name="POST /v1/inference (dry_run)",
        )
