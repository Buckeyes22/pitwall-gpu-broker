# Public alpha support matrix

This matrix bounds the first public alpha. “Supported” means the repository has
an executable contract and release verification; it does not promise a hosted
service or production SLA.

| Surface or capability | Alpha status | Boundary |
| --- | --- | --- |
| REST API on loopback or authenticated non-loopback | Supported | Single-operator deployment; scoped bearer authorization |
| MCP over local stdio | Supported | Network MCP is rejected unless a future authenticated transport is added |
| Operational CLI and database migrations | Supported | Python 3.12–3.13; installed wheel resources |
| Postgres, Redis, reconciler, inbound webhook, cost exporter | Supported | Canonical Compose topology; loopback host bindings by default |
| Existing RunPod serverless queue/LB and public OpenAI-compatible endpoints | Supported | Operator supplies credentials and endpoint configuration |
| RunPod pod leases using an operator-supplied image | Alpha/limited | Pitwall brokers lifecycle; operator owns image/model provenance and runtime |
| Outbound signed webhooks | Supported | Public HTTPS:443 destinations only; version 1 envelope |
| In-repository vLLM/GPU worker image | Deferred/unavailable | No image, workflow, default deployment, or successful worker entry point |
| MCP network transport | Deferred/unavailable | Local stdio only; MCP is client-launched and is not a long-running Compose service |
| Hosted control plane, telemetry service, or SaaS | Not provided | Self-hosted software only; Langfuse integration is opt-in |
| Providers other than RunPod | Deferred/unavailable | No public support claim |

Pre-1.0 interfaces can change under the compatibility policy. Security fixes may
remove unsafe behavior without a deprecation period.
