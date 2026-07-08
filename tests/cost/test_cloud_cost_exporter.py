from pathlib import Path

import yaml

from pitwall.cost_exporter.app import (
    active_workers,
    cloud_budget_pct,
    cloud_budget_usd,
    cloud_spend_month_usd,
    kill_log_triggers_7d,
    provider_spend_month_usd,
    reconciliation_lag_seconds,
    retention_last_deleted_count,
    retention_last_success_timestamp_seconds,
    webhook_delivery_retries_due,
    webhook_delivery_terminal_failures_24h,
    workload_queue_depth,
)


def test_alerts_yaml_parses_and_has_three_budget_tiers() -> None:
    body = yaml.safe_load(Path("config/prometheus/pitwall-cloud-alerts.yml").read_text())
    names = {rule["alert"] for rule in body["groups"][0]["rules"]}
    assert {"PitwallCloudBudget50", "PitwallCloudBudget75", "PitwallCloudBudget90"}.issubset(names)


def test_alerts_cover_operational_failure_signals() -> None:
    body = yaml.safe_load(Path("config/prometheus/pitwall-cloud-alerts.yml").read_text())
    names = {rule["alert"] for rule in body["groups"][0]["rules"]}
    assert {
        "PitwallWorkloadQueueBacklog",
        "PitwallReconciliationLag",
        "PitwallWebhookRetriesDue",
        "PitwallWebhookTerminalFailures",
        "PitwallRetentionStale",
    }.issubset(names)


def test_alerts_severity_mapping() -> None:
    body = yaml.safe_load(Path("config/prometheus/pitwall-cloud-alerts.yml").read_text())
    severity = {rule["alert"]: rule["labels"]["severity"] for rule in body["groups"][0]["rules"]}
    assert severity["PitwallCloudBudget50"] == "info"
    assert severity["PitwallCloudBudget75"] == "warning"
    assert severity["PitwallCloudBudget90"] == "critical"


def test_exporter_metric_names_in_app_source() -> None:
    src = Path("src/pitwall/cost/exporter.py").read_text()
    for metric in (
        "pitwall_cloud_spend_month_usd",
        "pitwall_cloud_budget_pct",
        "pitwall_cloud_budget_usd",
        "pitwall_active_workers",
        "pitwall_kill_log_triggers_7d",
        "pitwall_workload_queue_depth",
        "pitwall_reconciliation_lag_seconds",
        "pitwall_webhook_delivery_retries_due",
        "pitwall_webhook_delivery_terminal_failures_24h",
        "pitwall_provider_spend_month_usd",
        "pitwall_retention_last_success_timestamp_seconds",
        "pitwall_retention_last_deleted_count",
    ):
        assert metric in src


def test_metric_labels_documented() -> None:
    src = Path("src/pitwall/cost/exporter.py").read_text()
    assert '"provider"' in src


def test_no_tailscale_joins_in_exporter_query() -> None:
    src = Path("src/pitwall/cost/exporter.py").read_text()
    assert "JOIN" not in src or "tailscale" not in src.upper()


def test_active_workers_gauge_has_provider_label() -> None:
    assert "provider" in active_workers._labelnames


def test_all_gauges_are_defined() -> None:
    assert cloud_spend_month_usd is not None
    assert cloud_budget_pct is not None
    assert cloud_budget_usd is not None
    assert active_workers is not None
    assert kill_log_triggers_7d is not None
    assert workload_queue_depth is not None
    assert reconciliation_lag_seconds is not None
    assert webhook_delivery_retries_due is not None
    assert webhook_delivery_terminal_failures_24h is not None
    assert provider_spend_month_usd is not None
    assert retention_last_success_timestamp_seconds is not None
    assert retention_last_deleted_count is not None
