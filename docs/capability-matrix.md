# Public capability matrix

| Capability | REST | MCP stdio | CLI | Background service | Alpha status |
| --- | --- | --- | --- | --- | --- |
| Read capabilities/providers | Yes | Yes | Seed/register | Reconciler reads | Supported |
| Dry-run inference and cost gate | Yes | Yes | No | No | Supported |
| RunPod serverless/public inference | Yes | Yes | Configure | Reconciler | Supported |
| Pod lease create/read/mutate | Yes | Yes | Pod terminate/configure | Reconciler | Alpha/limited |
| Webhook subscription lifecycle | Yes | No | No | Dispatcher | Supported through REST |
| Inbound RunPod callback | Receiver endpoint | No | No | Receiver/queue | Supported |
| Database migrate/status/reset | No | No | Yes | Migration job | Supported |
| Retention archive/purge | No | No | Yes | Reconciler schedule | Supported |
| Operator dashboard | No | No | Yes | No | Read-only alpha |
| In-repository GPU worker | No | No | Fail-closed tombstone | No | Deferred |
| Non-RunPod providers | No | No | No | No | Deferred |
| Authenticated network MCP | No | No | No | No | Deferred; stdio only |

Unsupported calls must return a stable 4xx/424 or an explicit unavailable exit,
never a successful no-op. See `docs/support-matrix.md` for deployment boundaries.
