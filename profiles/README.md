# profiles/

| Path | Tracked? | Role |
|---|---|---|
| `PROFILES.json` | **yes** | named profile → launch selector registry (no secrets) |
| `scratch/`, `vpn/`, … | **no** (runtime) | Cloak user-data / profile dirs |

## Layout law

- **`PROFILES.json`** is the only tracked data file here. It is the stable named selector registry used by `browserctl profiles …`.
- **Profile directories** (`scratch/`, `vpn/`, …) are runtime Cloak user-data. Never commit cookies, caches, or live account material.

Do not commit emails, absolute host paths, or profile dirs.
