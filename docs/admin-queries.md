# Pitwall Admin SQL Queries

Runnable SQL queries for operators using CloudBeaver or `psql`.

```sql
-- Connect: psql $DATABASE_URL
-- Or use CloudBeaver with the pitwall database connection.
```

## Cost & Spend

### Daily spend by capability class and provider type (last 30 days)

```sql
SELECT
    day,
    capability_class,
    provider_type,
    workload_count,
    cost_usd
FROM pitwall.cost_daily
WHERE day >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY day DESC, capability_class, provider_type;
```

### Monthly rollup (all time)

```sql
SELECT
    TO_CHAR(day, 'YYYY-MM') AS month,
    capability_class,
    provider_type,
    SUM(workload_count) AS total_workloads,
    SUM(cost_usd) AS total_cost_usd
FROM pitwall.cost_daily
GROUP BY month, capability_class, provider_type
ORDER BY month DESC, total_cost_usd DESC;
```

### Month-to-date (MTD) spend

```sql
SELECT
    capability_class,
    provider_type,
    SUM(workload_count) AS workloads,
    SUM(cost_usd) AS mtd_cost_usd
FROM pitwall.cost_daily
WHERE day >= DATE_TRUNC('month', CURRENT_DATE)
GROUP BY capability_class, provider_type
ORDER BY mtd_cost_usd DESC;
```

### Spend by workload state (last 7 days, from workloads table)

```sql
SELECT
    state,
    COUNT(*) AS workload_count,
    SUM(cost_estimate_usd) AS estimated_cost,
    SUM(cost_actual_usd) AS actual_cost
FROM pitwall.workloads
WHERE submitted_at >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY state
ORDER BY workload_count DESC;
```

## Workloads

### Workloads by state (active work)

```sql
SELECT
    state,
    COUNT(*) AS count
FROM pitwall.workloads
WHERE state IN ('queued', 'running')
GROUP BY state;
```

### Recent workload activity (last 100, any state)

```sql
SELECT
    id,
    capability_id,
    provider_id,
    type,
    state,
    submitted_at,
    started_at,
    completed_at,
    execution_ms,
    cost_estimate_usd,
    cost_actual_usd
FROM pitwall.workloads
ORDER BY submitted_at DESC
LIMIT 100;
```

### Failed workloads with errors (last 7 days)

```sql
SELECT
    id,
    capability_id,
    provider_id,
    type,
    submitted_at,
    error
FROM pitwall.workloads
WHERE state = 'failed'
  AND submitted_at >= CURRENT_DATE - INTERVAL '7 days'
ORDER BY submitted_at DESC;
```

### Running workloads with duration

```sql
SELECT
    id,
    capability_id,
    provider_id,
    type,
    started_at,
    NOW() - started_at AS duration
FROM pitwall.workloads
WHERE state = 'running'
ORDER BY started_at;
```

### Workloads missing cost data (completed but no actual cost)

```sql
SELECT
    id,
    capability_id,
    provider_id,
    state,
    submitted_at,
    completed_at,
    cost_actual_usd
FROM pitwall.workloads
WHERE state = 'completed'
  AND cost_actual_usd IS NULL
ORDER BY submitted_at DESC
LIMIT 50;
```

## Providers

### All providers with status

```sql
SELECT
    id,
    name,
    provider_type,
    capability_id,
    enabled,
    health_status,
    cooldown_until,
    consecutive_failures,
    cooldown_trips
FROM pitwall.providers
ORDER BY enabled, priority;
```

### Enabled providers only

```sql
SELECT
    id,
    name,
    provider_type,
    capability_id,
    health_status,
    priority
FROM pitwall.providers
WHERE enabled = true
ORDER BY priority;
```

### Providers in cooldown

```sql
SELECT
    id,
    name,
    provider_type,
    cooldown_until,
    consecutive_failures
FROM pitwall.providers
WHERE cooldown_until IS NOT NULL
  AND cooldown_until > NOW()
ORDER BY cooldown_until;
```

### Provider GPU type configuration

```sql
SELECT
    id,
    name,
    provider_type,
    config -> 'gpu_type_priority' AS gpu_type_priority
FROM pitwall.providers
WHERE config ? 'gpu_type_priority';
```

## Leases

### Active leases

```sql
SELECT
    id,
    provider_id,
    runpod_pod_id,
    state,
    created_at,
    expires_at,
    renewal_policy,
    cost_accrued_usd
FROM pitwall.leases
WHERE state = 'active'
ORDER BY expires_at;
```

### Leases expiring within 1 hour

```sql
SELECT
    id,
    provider_id,
    state,
    expires_at,
    expires_at - NOW() AS time_remaining
FROM pitwall.leases
WHERE state = 'active'
  AND expires_at <= NOW() + INTERVAL '1 hour'
ORDER BY expires_at;
```

### Lease state distribution

```sql
SELECT
    state,
    COUNT(*) AS count
FROM pitwall.leases
GROUP BY state
ORDER BY count DESC;
```

### All leases with provider names

```sql
SELECT
    l.id,
    l.provider_id,
    p.name AS provider_name,
    l.state,
    l.created_at,
    l.expires_at,
    l.cost_accrued_usd
FROM pitwall.leases l
JOIN pitwall.providers p ON p.id = l.provider_id
ORDER BY l.state, l.expires_at;
```

## Capabilities

### All capabilities

```sql
SELECT
    id,
    name,
    version,
    class,
    cost_mode,
    enabled,
    source,
    last_applied_yaml_hash
FROM pitwall.capabilities
ORDER BY class, name;
```

### Enabled capabilities only

```sql
SELECT
    id,
    name,
    version,
    class,
    cost_mode
FROM pitwall.capabilities
WHERE enabled = true
ORDER BY class, name;
```

## Volumes

### All registered volumes

```sql
SELECT
    id,
    name,
    runpod_volume_id,
    datacenter_id,
    size_gb,
    purpose,
    monthly_cost_usd
FROM pitwall.volumes
ORDER BY datacenter_id, name;
```

## Rate Buckets

### Rate bucket status

```sql
SELECT
    endpoint_id,
    operation,
    capacity,
    tokens,
    last_refilled_at,
    recent_429_at
FROM pitwall.rate_buckets
ORDER BY endpoint_id, operation;
```

### Recent 429 events

```sql
SELECT
    endpoint_id,
    operation,
    recent_429_at
FROM pitwall.rate_buckets
WHERE recent_429_at IS NOT NULL
ORDER BY recent_429_at DESC;
```

## Audit Log

### Recent config changes (last 50)

```sql
SELECT
    id,
    created_at,
    actor,
    action,
    entity_type,
    entity_id,
    change_reason
FROM pitwall.config_audit
ORDER BY created_at DESC
LIMIT 50;
```

### Config changes by entity

```sql
SELECT
    entity_type,
    entity_id,
    action,
    actor,
    created_at,
    old_value,
    new_value
FROM pitwall.config_audit
WHERE entity_type = 'providers'
  AND entity_id = 'your-provider-id-here'
ORDER BY created_at DESC
LIMIT 20;
```

### Config changes by actor

```sql
SELECT
    id,
    created_at,
    action,
    entity_type,
    entity_id,
    change_reason
FROM pitwall.config_audit
WHERE actor = 'rest:admin'
ORDER BY created_at DESC
LIMIT 50;
```

## Kill Log

### Recent kill events

```sql
SELECT
    id,
    triggered_at,
    reason,
    actor,
    pods_terminated,
    endpoints_hibernated,
    workloads_cancelled,
    total_duration_ms,
    errors
FROM pitwall.kill_log
ORDER BY triggered_at DESC
LIMIT 20;
```

### Kill events with errors

```sql
SELECT
    id,
    triggered_at,
    reason,
    actor,
    pods_terminated,
    errors
FROM pitwall.kill_log
WHERE jsonb_array_length(errors) > 0
ORDER BY triggered_at DESC;
```

### Kill event summary stats

```sql
SELECT
    COUNT(*) AS total_kills,
    SUM(pods_terminated) AS total_pods_terminated,
    SUM(endpoints_hibernated) AS total_endpoints_hibernated,
    SUM(workloads_cancelled) AS total_workloads_cancelled,
    AVG(total_duration_ms) AS avg_duration_ms
FROM pitwall.kill_log;
```

## Alert Events

### Alert events sent this month

```sql
SELECT
    month,
    threshold_pct,
    sent_at
FROM pitwall.alert_events
WHERE month = TO_CHAR(CURRENT_DATE, 'YYYY-MM')
ORDER BY threshold_pct;
```

### All alert events by month

```sql
SELECT
    month,
    threshold_pct,
    sent_at
FROM pitwall.alert_events
ORDER BY month DESC, threshold_pct;
```
