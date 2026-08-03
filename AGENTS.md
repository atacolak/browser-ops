# AGENTS.md — browser-ops

Auto-loaded by pi when cwd is `browser-ops`.  
**Operator truth** for browser agents (especially **navigator**).

---

## What this repo is

Browser automation plane: CloakBrowser + CDP daemon + domain procedure memory + **session leases** + **named profile registry** + **identity binding**.

| Path | Role |
|---|---|
| `bin/browserctl` | session leases + named profile lookup |
| `browserctl/` | lease manager, adapters, `profiles.py` registry |
| `identity_ops.py` | canonical xAI bind/start/stop/retire/stage |
| `daemon/` | CDP harness + active-target publish |
| `vpn/` | geo SOCKS egress helpers |
| `skills/domains/<site>/` | learned site procedures |
| `profiles/PROFILES.json` | **tracked** named selector registry (no secrets) |
| `profiles/IDENTITIES.json` | **runtime** bind map (gitignored; bootstrapped on first use) |
| `profiles/API_STAGES.json` | **runtime** S0–S6 ledger (gitignored; bootstrapped on first use) |
| `profiles/<kind>/` | **runtime** Cloak profile dirs (gitignored) |
| `state/control/` | leases, locks, optional `viewer-root` (no secrets) |

Human entry: [`README.md`](./README.md) · CLI: [`docs/browserctl.md`](./docs/browserctl.md) · Binding: [`profiles/BINDING.md`](./profiles/BINDING.md)

---

## Profiles: who does what

| Profile | Spawn for | Not for |
|---|---|---|
| **navigator** | browser tasks inside a `browserctl navigator spawn` binding | allocating CDP/ports/profiles; code edits |
| demiurge | code/config changes | driving the browser |
| scout | research / codebase search | browser mutation |
| orchestrator | `browserctl navigator spawn/cleanup` + task dispatch | hand-joining env/watch/release; raw daemon ports |

**Rule:** only navigator has browser tools. Other agents route browser work to navigator.  
**Rule:** navigator does **not** allocate raw browser resources. Orchestrator/human owns one resource pair: `navigator spawn` then `navigator cleanup`.

---

## Canonical lifecycle: browserctl

**Preferred (orchestrator resource pair — no hand-joined primitives):**

```text
browserctl navigator spawn  →  herdr-agent-ctl run (task)  →  browserctl navigator cleanup
         ↘ optional --watch after navigator pane exists
```

```bash
out=$(./bin/browserctl navigator spawn \
  --kind scratch --label demo --owner orch \
  --mode one_shot --watch --json)
# alternatives: --kind xai --email 'USER@host'  OR  --profile coal-demo

lease=$(jq -r .lease_id <<<"$out")
target=$(jq -r .next.run_target <<<"$out")

# dispatch the browser task to the returned navigator pane
herdr-agent-ctl run --target "$target" --prompt '…' --json

# ALWAYS in finally; safe to repeat
./bin/browserctl navigator cleanup --lease "$lease" --json
```

### What the resource pair owns

| Command | Contract |
|---|---|
| `navigator spawn` | name-collision check → acquire browser lease → create dedicated Herdr tab by default → inject exact lease env → spawn navigator as tab root → optional read-only mirror → persist binding → return `.next.run_target` |
| `navigator cleanup` | close/prove navigator absent → close/prove mirror absent → close owned tab → stop browser/daemon → wipe eligible ephemeral state/profile → clear binding → terminal receipt |

**Success gates:** spawn requires exit 0 and `.ok == true`. Cleanup requires `.ok == true`, `.settled == true`, `.retryable == false`, and `.proof.binding_cleared == true`.

**Failure semantics:** requested `--watch` failure makes spawn fail and rolls back navigator/tab/lease/profile/state. Duplicate active `--name` fails before acquiring. Ordinary cleanup fails closed if pane absence cannot be proved. `cleanup --force` may return terminal success with `forced: true` plus warnings when browser release succeeded but pane/tab evidence is incomplete.

**Env-contract only** (platform/debug; do not teach to fresh orchestrators):

```text
browserctl launch|acquire  →  manually spawn with returned env  →  optional watch  →  release
```

| Kind | Adapter | Notes |
|---|---|---|
| `xai` | `identity_ops ensure/stop` | one email ↔ worker ↔ port; **not** `default` |
| `scratch` / `adhoc` | unique token worker when ad-hoc; **stable** when `--worker` or `--profile` (named → `scratch-profile-<name>` unless launch.worker set) | demos / finite named pool; wipe only if `ephemeral_wipe_v1` (never legacy `ephemeral_profile` alone) |
| `vpn` | `vpn/spawn_vpn_browser` | attach existing or start region worker |

### Default topology and viewport

Default `--watch` topology owns a new dedicated tab and uses:

```text
navigator tab root, split none
├─ navigator transcript: 37% left
└─ observe_mirror:        63% right
```

| Viewport mode | Behavior |
|---|---|
| `fixed` (default) | Actual page layout is pinned once to **1150×902**. Later terminal/pane resize only contain-fits the frame; page breakpoints and navigator coordinates stay stable. |
| `follow-pane` | Opt-in `--viewport follow-pane`; pane resize updates CDP device metrics and reflows responsive page layout. |
| `preserve` | Never mutates the attached browser viewport; useful when another controller owns layout. |

Exact default geometry is guaranteed only when browserctl creates the dedicated tab. Explicit `--tab` is advanced caller-owned topology (`owns_tab=false`, `geometry_guaranteed=false`); cleanup never closes it.

### Named Herdr sessions

A named session can contain multiple workspaces. Ambient `HERDR_SOCKET_PATH` + `HERDR_PANE_ID` identify the server and orchestrator authority, but **do not rely on the session's focused workspace**. Derive the orchestrator pane's workspace and pass `--workspace` explicitly; otherwise the dedicated navigator tab may land in another focused workspace and same-workspace helper control will correctly reject it.

```bash
workspace=$(herdr pane get "$HERDR_PANE_ID" | jq -r .result.pane.workspace_id)
out=$(./bin/browserctl navigator spawn \
  --kind scratch --label demo --owner orch --mode one_shot --watch \
  --workspace "$workspace" --json)
```

For explicit targeting from another shell, provide the target session endpoint, ambient pane, and matching workspace. `HERDR_PANE_ID` must come from that target session because `herdr-agent-ctl` uses it as the orchestrator authority/receipt anchor:

```bash
session=navigator-demo
socket="$HOME/.config/herdr/sessions/$session/herdr.sock"
workspace=w3
export HERDR_SESSION="$session"
export HERDR_SOCKET_PATH="$socket"
export HERDR_PANE_ID=w3:p1   # existing pane in THAT session/workspace

out=$(./bin/browserctl navigator spawn \
  --kind scratch --label demo --owner orch --mode one_shot --watch \
  --herdr-session "$session" --herdr-socket "$socket" \
  --workspace "$workspace" --json)
```

Bindings persist the exact session/socket, so later `navigator cleanup --lease …` targets the correct Herdr server even if the shell's ambient session differs. Do not mix a socket from one session with a pane/workspace id from another.

**Recommended:** run the orchestrator inside the target Herdr session **and pass its derived workspace explicitly**. Cross-session bootstrap from another shell is proven for spawn/watch/cleanup, but native orchestrator helper control remains same-workspace/session scoped. If you explicitly bootstrap across sessions, every `herdr-agent-ctl run/status/close` must use the target session's socket + ambient pane authority; do not expect the current workspace's orchestrator tools to control the foreign pane.

### Named profile registry

```bash
./bin/browserctl profiles register coal-demo --kind xai --email 'USER@host' --json
./bin/browserctl profiles associate coal-demo x.ai 'USER@host' --json
./bin/browserctl profiles resolve x.ai --account 'USER@host' --json
./bin/browserctl navigator spawn --profile coal-demo --owner orch --watch --json
# → lease.profile_name + env.BROWSERCTL_PROFILE_NAME + navigator binding
```

**Resolve** is exact/deterministic (never fuzzy). Scratch stays explicit; resolve emits selectors only.

| Rule | Detail |
|---|---|
| Strict selectors | `--profile` **excludes** `--kind/--email/--worker/--label/--cdp-port/--country/--city/--headed/--no-start/--attach-only` |
| Stamp | successful `--profile` launch writes `lease.profile_name` + `BROWSERCTL_PROFILE_NAME` |
| Association learning | **explicit only** after a proven successful login: `profiles associate <name> <site> [account]` — never infer from the live URL |
| Accountless resolve | without `--account`, only accountless rows match; account-scoped-only site → `PROFILE_NOT_FOUND` (+ hint) |
| Ambiguity | multi-match → `PROFILE_AMBIGUOUS`; lock timeout → `PROFILE_LOCK_TIMEOUT` |

### Scratch: stable vs ephemeral

| Policy | Use |
|---|---|
| Ephemeral one-shot | `navigator spawn --kind scratch --label demo --mode one_shot` — unique worker token; cleanup kills daemon/chrome and wipes eligible profile/state |
| Stable named selector | `profiles register lab-scratch --kind scratch --label lab` + `navigator spawn --profile lab-scratch` |
| Ports | scratch CDP 9300–9399 under global `ports.lock` + reservation retry (collision-safe) |

### Watch laws

1. Wait for CDP `/json/version`, non-null `active_target_id`, **and** that id in CDP `/json/list` before splitting (bounded; default 20s).
2. **Never** seed a null active-target stub (that freezes observe_mirror on `about:blank`).
3. Watch pane env must set bounded 1:1 screencast and `HERDR_BROWSER_VIEWER_WATCH_RESIZE=1`. Default viewport is **fixed** 1150×902 (`HERDR_BROWSER_VIEWPORT_MODE=fixed` + width/height) so pane resize scales the frame without reflowing page layout. Opt-in `--viewport follow-pane` sets `VIEWPORT_MODE=follow-pane` and legacy `FOLLOW_PANE_VIEWPORT=1` for dynamic reflow. Input stays read-only in all modes.
4. After pane run, verify viewer process started; on failure close the newly split pane.
5. Default split ratio **0.37** (herdr first-child = agent left 37%, browser right 63%). `--ratio` overrides.
6. Exact herdr endpoint (`--herdr-socket` / `HERDR_SOCKET_PATH`) required — fail closed if ambiguous.
7. Fresh orchestrators do not call `unwatch`/`release`; `navigator cleanup` proves navigator, mirror, and owned-tab absence before releasing. Low-level unwatch/release remain platform/debug surfaces.

### Lease laws

1. **One mutation lease per worker** — duplicate acquire → `LEASE_CONFLICT`.
2. **No managed `default`** for leased work.
3. Control plane is atomic files under `state/control/` (no secrets).
4. Harness publishes `state/<worker>/control/active-target.json` for mirrors.
5. **Orchestrator owns the resource pair.** `navigator cleanup` in `finally`; do not manually sequence watch/release/mark-exit/reap. Scheduled `browserctl reap` is only a **crash backstop** for eligible leases (`one_shot`, `expiring`, explicit `auto_reap`). For navigator-bound leases it delegates through navigator cleanup, settling panes/tab/binding before stamping `reaped`. It does **not** kill ordinary active persistent sessions past the 1h TTL stamp. Timer units: `packaging/systemd/user/` + `./bin/browserctl-reap-timer install` (host-admin action).

---

## xAI / coal (browserctl → identity_ops)

**One account ↔ one Cloak profile ↔ one daemon worker ↔ one CDP port.**  
Runtime files: `profiles/IDENTITIES.json` and `profiles/API_STAGES.json` (bootstrapped empty on first `identity_ops` list/get/set; not committed). Profile dirs under `profiles/` are runtime only.

```bash
./bin/browserctl navigator spawn --kind xai --email 'USER@host' --owner orch --watch --json
./bin/browserctl navigator cleanup --lease '<lease-id>' --json
python3 identity_ops.py ensure|stop|list|show|retire|stage-get|stage-set …
python3 identity_ops.py ensure --email 'USER@host' --revive   # only if retired
```

**Identity law**

- `active ∩ retired = ∅` (enforced on every registry write)
- `ensure` on retired email **fails** unless `--revive`
- `retire` stops daemon, drops active row, appends `retired_identities`, updates stage ledger, wipes profile unless `--keep-profile`
- paths stored repo-relative under `profiles/` and `state/`

**Hard verify** after every deposit/reauth (not file-on-disk alone):

- access JWT has **no** `bot_flag_source` (or flag alone with billing 200 → S1 keep)
- live bearer billing returns **200**
- management row healthy after a real probe

**Credentials:** `CPA_ADMIN_KEY` from environment only. No admin literals in repo.

Deposit skill: `cpa-manager/xai-dashboard-deposit`. Reauth: `skills/domains/x.ai/reauth.md`.  
Stage canon: `skills/domains/x.ai/bot-flag-lifecycle.md`.

---

## API risk stages (S0–S6) — stand-alone

| Stage | Signal | Action |
|---|---|---|
| **S0** | billing/models 200; flag usually absent | keep in pool |
| **S1** | `bot_flag_source:1` but APIs **200** | **keep** — flag alone ≠ fail |
| **S2** | 403 / bad-credentials on **old** token | **one** full device reauth + hard probe |
| **S3** | 403 / denied **after that fresh** full OAuth | cool-off: max **3** full OAuth/incident, **≥6h** apart; if **48h+** still bad → **S4** |
| **S4** | budget exhausted / still proxy-unusable | **action_required** — orchestrator decides; not automatic delete |
| **S5** | quota-like terminal evidence (operator path) | discard + `identity_ops retire` |
| **S6** | ban/suspend UI | discard + retire |

S3/S4 ≠ S5. Do not loop device reauth past the S3 budget.  
**S4 is never implied-automatic teardown** in this tree.

---

## Memory / skills

| Kind | Where | Discovery |
|---|---|---|
| Domain procedure | `skills/domains/<domain>/<skill>.md` | `browser_skill` list/get/search |
| Interaction patterns | `skills/interactions/*.md` | read when needed |
| Named profiles | `profiles/PROFILES.json` | `browserctl profiles …` |

On learn: prove once → `browser_skill op:write` with frontmatter (`id`, `domain`, `category`, `status`).

---

## What is not in this clean tree

- Destructive live metabolism / auto-discard cron (not shipped)
- Account marketplace scrapers, proxy dump files, therapy datasets
- Payment/SEPA experiments, containment notes, live profiles, findings
- Healing scripts with embedded admin keys

### Quarantine branch (local-only)

Unverified / legacy domain procedures (amazon, facebook, github scraping drafts, etc.) live on the **local** branch `quarantine/legacy-browser-skills` and optional worktree — **not** on `main`, **not** shipped, **not** agent-default memory.

- Do **not** treat quarantine skills as operator truth.
- Do **not** merge quarantine into main without revalidation.
- Edits to quarantine content require a **separate commit on the quarantine branch** (this clean tree does not modify that worktree).
- Revalidation path: prove once against a live lease → rewrite under `skills/domains/<site>/` on main with proper frontmatter.

---

## Hard rules

- Do not commit secrets; no secrets in lease/target/PROFILES JSON
- Do not use `default` worker for coal
- Always run `browserctl navigator cleanup` in `finally`; do not hand-join navigator/watch/release lifecycle
- CDP on localhost only
- Do not open `browserctl watch` before daemon target publish settles; do not seed null targets
- Do not auto-associate profiles from URLs — associate explicitly after proven login
- Do not load skills from the quarantine branch as production procedures
