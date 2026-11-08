# Perseus Vault Integrations

Perseus Vault is MCP-native: any MCP-compatible client can connect directly.
This directory holds the first-class integration maintained for this project
and the shared client it builds on.

| Integration | Type | Directory |
|---|---|---|
| **Hermes** (primary target) | MemoryProvider plugin + lifecycle hooks | [`hermes/`](hermes/) |
| **Python Client** | Shared stdio transport client (`VaultClient`, `EphemeralAdmissionFixture`) | [`client/`](client/) |
| **Chancery** | Configuration notes | [`chancery/`](chancery/) |
| **Noisegate** | `prepare` golden fixtures + regeneration | [`noisegate/`](noisegate/) |

Configuration guides for other MCP hosts live under
[`../docs/integration/`](../docs/integration/) (general MCP, Claude Code,
Cursor, DeepSeek Harness).

## Requirements

All integrations require Perseus Vault v1.0.0+ installed:

```bash
curl -sSL https://raw.githubusercontent.com/sowerkoku/Perseus-Continuum/main/scripts/bootstrap.sh | bash
```
