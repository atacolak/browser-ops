# profiles/

| File | Tracked? | Role |
|---|---|---|
| `PROFILES.json` | yes (empty/schema) | named profile → launch selector |
| `IDENTITIES.example.json` | yes | empty schema for xAI bind map |
| `API_STAGES.example.json` | yes | empty schema for S0–S6 ledger |
| `IDENTITIES.json` | **no** (runtime) | live email ↔ worker ↔ port |
| `API_STAGES.json` | **no** (runtime) | live stage ledger |
| `xai/<slug>/`, `scratch/`, `vpn/` | **no** | Cloak user-data dirs |

## Bootstrap

```bash
# identity_ops creates IDENTITIES.json empty on first use if missing
python3 identity_ops.py list --json

# or copy examples
cp profiles/IDENTITIES.example.json profiles/IDENTITIES.json
cp profiles/API_STAGES.example.json profiles/API_STAGES.json
```

Do not commit emails, absolute host paths, or live stage history.
