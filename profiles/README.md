# profiles/

| Path | Tracked? | Role |
|---|---|---|
| `PROFILES.json` | **yes** | named profile → launch selector registry (no secrets) |
| `IDENTITIES.json` | **no** (runtime) | xAI email ↔ worker ↔ port bind map |
| `API_STAGES.json` | **no** (runtime) | S0–S6 API risk stage ledger |
| `xai/<slug>/`, `scratch/`, `vpn/`, … | **no** (runtime) | Cloak user-data / profile dirs |

## Layout law

- **`PROFILES.json`** is the only tracked data file here. It is the stable named selector registry used by `browserctl profiles …`.
- **`IDENTITIES.json`** and **`API_STAGES.json`** are created locally on first use by `identity_ops` (in-code empty defaults) and are gitignored. Do not commit them.
- **Profile directories** (`xai/`, `scratch/`, `vpn/`, …) are runtime Cloak user-data. Never commit cookies, caches, or live account material.

## Bootstrap

```bash
# creates profiles/IDENTITIES.json empty if missing
python3 identity_ops.py list --json

# creates profiles/API_STAGES.json empty if missing
python3 identity_ops.py stage-list --json
# stage-get / stage-set also bootstrap the ledger on first use
```

Do not commit emails, absolute host paths, live stage history, or profile dirs.
