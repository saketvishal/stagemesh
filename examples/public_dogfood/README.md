# Public Dogfood Demos

These manifests are public-safe demo inputs for the dogfood acceptance suite in
`docs/dogfood/acceptance-suite.yaml`. They are not credentials-bearing runtime
configs. Treat them as templates for disposable repositories and fake or local
scripted workers.

| Manifest | Demonstrates |
|---|---|
| `single_agent_demo.yaml` | One scripted worker executing a self-reviewed task |
| `staged_agents_demo.yaml` | Separate builder and independent reviewer |
| `provider_fallback_demo.yaml` | Checkpoint, interruption, and replacement worker recovery |
| `high_risk_governance_demo.yaml` | Two-reviewer governance and fail-closed validation |
| `multi_project_demo.yaml` | Bounded global capacity across multiple projects |

Keep all values generic. Do not paste private task text, private repository
names, or credential material into these files.

