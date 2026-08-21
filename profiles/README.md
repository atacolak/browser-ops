# profiles/

| Path | Tracked? | Role |
|---|---|---|
| `PROFILES.example.json` | **yes** | anonymous demo registry (`scratch-general-1..3`, `shared-headed-demo`) |
| `PROFILES.json` | **no** (local) | named profile → launch selector used by `browserctl` |
| `BINDING.md` | **yes** | identity invariants |
| `scratch/`, `vpn/`, … | **no** (runtime) | Cloak user-data / profile dirs |

## Layout

- Copy [`PROFILES.example.json`](./PROFILES.example.json) to `PROFILES.json` for a local registry. `browserctl profiles …` reads `PROFILES.json`.
- **Profile directories** (`scratch/`, `vpn/`, …) are runtime Cloak user-data. Never commit cookies, caches, or live account material.

Do not commit emails, absolute host paths, operator identities, or profile dirs.
