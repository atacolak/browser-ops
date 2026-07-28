---
id: bot-flag-lifecycle
domain: x.ai
category: auth
status: active
version: 1
validated: 2026-07-16
triggers:
  - bot_flag_source
  - access denied
  - quarantine
  - xai 403
  - coal stage
  - permission-denied
endpoints:
  - GET /v0/management/auth-files
  - GET /v0/management/xai-auth-url
  - DELETE /v0/management/auth-files?name=<filename.json>
domains:
  - x.ai
  - cpa-manager
  - accounts.x.ai
related:
  - cpa-manager/xai-dashboard-deposit
  - x.ai/reauth
---

# xAI coal API risk stages (`bot_flag_source` + 403)

**Canon** for orchestrator + navigator. Built from:

1. Live browser-ops experiments (example-account 2026-07-16 E0–E6)
2. Community/CPA-proxy reports (operator-ingested): `bot_flag_source` is a **risk feature**, not an official documented ban bit; some tagged tokens still 200; 403 can be dynamic

Do **not** invent ad-hoc “operator quarantine” language in reports — use **stage IDs** below.

## How we know the stage (source of truth)

| Store | Role |
|---|---|
| **`profiles/API_STAGES.json`** | **Primary ledger** — `api_stage`, S3 counters, next retry, last probe, history |
| `profiles/IDENTITIES.json` | Mirror field `api_stage` (+ notes) when identity is bound |
| CPA `auth-files` / live billing | **Evidence** for classification — not the stage store |
| `operator notes` | local only — not in repo |

### Read

```bash
python3 identity_ops.py stage-get --email 'USER@host' --json
python3 identity_ops.py stage-list
python3 identity_ops.py show --email 'USER@host' --json   # includes api_stage_detail
```

If no ledger row exists → treat as **inferred S0** until first probe writes one.

### Write (after every deposit / reauth / heal probe)

```bash
# success
python3 identity_ops.py stage-set --email 'USER@host' --stage S0 \
  --bot-flag absent --billing-http 200 --full-oauth-ok --note 'deposit verified'

# flag but APIs work
python3 identity_ops.py stage-set --email 'USER@host' --stage S1 \
  --bot-flag 1 --billing-http 200 --full-oauth-ok

# first fresh full OAuth still 403 → enter/stay S3 and count fail (+6h)
python3 identity_ops.py stage-set --email 'USER@host' --stage S3 \
  --bot-flag 1 --billing-http 403 --full-oauth-fail --note 'device reauth still 403'

# budget exhausted → S4 (also auto if --full-oauth-fail reaches 3)
python3 identity_ops.py stage-set --email 'USER@host' --stage S4 \
  --bot-flag 1 --billing-http 403 --note 'pool-exclude'
```

`stage-set --full-oauth-fail` increments `s3_full_oauth_fails`, sets `s3_next_retry_at` (+6h), and **auto-promotes to S4 at ≥3 fails**.

### Who updates what

| Actor | Duty |
|---|---|
| **Orchestrator** | Before heal: `stage-get` / `stage-list`. Skip S3 retry if before `s3_next_retry_at` or stage S4. After navigator returns probe results: **`stage-set`**. After job: `stop`. |
| **Navigator** | Run hard probe; report stage recommendation + bot_flag + billing HTTP in summary. Prefer not to edit registry itself unless tools allow; orchestrator owns `stage-set` if navigator cannot. |
| **Operator tooling** | No autonomous healer or destructive metabolism is shipped in this clean tree. S5 is operator-classified; retirement is explicit through `identity_ops retire` after external evidence. |

### Before spawning a healer

```text
st = stage-get email
if st.api_stage in (S4, S5, S6): action_required — no reauth heal; operator/orchestrator decides retire
if st.api_stage == S3 and now < s3_next_retry_at: skip full OAuth
if st.api_stage == S3 and s3_full_oauth_fails >= 3: stage-set S4 (action_required; not auto-delete)
if st.api_stage in (S0,S1): no heal needed unless CPA says rotten
if st.api_stage == S2 or (S3 and retry due): ensure → navigator reauth once → stage-set → stop
```

## Hard probes (always)

After every deposit/reauth:

1. Decode access JWT → note `bot_flag_source` (present/`1` vs absent)
2. Bearer `GET https://cli-chat-proxy.grok.com/v1/billing` (and optionally `/v1/models`)
3. Management row after a real probe (brief `active` then error = fail)

**File on disk alone is never success.**

## Stages (six + billing/ban)

| Stage | Name | Signals | Behavior |
|---|---|---|---|
| **S0** | **Verified healthy** | no blocking probe fail; billing/models **200**; flag usually absent | keep in pool; `identity_ops stop` after job |
| **S1** | **Annotated but usable** | `bot_flag_source: 1` (or other risk claim) but billing/models **200** | **keep using** — flag alone is not failure; log annotation |
| **S2** | **Stale / bad auth** | 403 or bad-credentials on **old** token; no successful fresh full OAuth yet this incident | **one** full device reauth on **bound** profile (not token refresh only); then hard probe → reclassify |
| **S3** | **Fresh-auth denied** | still 403 Access denied / permission-denied **after one** correct full OAuth (scopes like healthy peers) | **cool-off schedule** (below) — not discard; profile kept |
| **S4** | **Persistent proxy-unusable → action_required** | cool-off budget exhausted **or** ≥3 full OAuth still 403 | **action_required** — orchestrator/operator decides teardown. Not automatic. Typical coal path: DELETE CPA auth + receipt + `identity_ops retire`. Do not loop device OAuth. |
| **S5** | **Terminal weekly quota** | operator has durable evidence of terminal weekly-quota exhaustion; billing percentage is telemetry only | explicit CPA delete/receipt + `identity_ops retire`; no destructive automation in this tree |
| **S6** | **Product dead** | suspended / banned / deactivated UI or explicit account death | discard + retire |

### Stage transitions (normal)

```text
deposit/reauth
  → probe
      200 & (flag or not)     → S0 or S1
      403 on old token        → S2 → one full OAuth → probe
      403 after that one OAuth → S3 (enter cool-off, attempt_count=1)
      S3 retry still 403       → stay S3 until budget exhausted → S4
      any probe 200            → S0 or S1 (reset cool-off counters)
terminal weekly-quota evidence → operator classifies S5
billing percentage             → telemetry only; never classifies S5
S5                              → explicit delete/receipt + retire
unknown/transient evidence      → keep; do not classify or delete
ban UI                        → S6
```

Community ladder maps as:  
`flag+200`→**S1** · `403 old token`→**S2** · `403 after fresh auth`→**S3** · `persistent 403`→**S4**.

## S3 cool-off schedule (canon — not “retry forever”)

**Problem:** “retry later” without numbers becomes thrash or neglect.  
**Rule:** S3 is a **budgeted** cool-off, not infinite 6h spam and not a single forever wait.

### Counters (store on identity notes / findings / ledger)

| Field | Meaning |
|---|---|
| `api_stage` | `S3` or `S4` |
| `s3_full_oauth_fails` | count of **full** device OAuth attempts that still 403'd (not refresh-only, not extra xai-auth-url probes) |
| `s3_entered_at` | UTC when first entered S3 this incident |
| `s3_next_retry_at` | UTC earliest next full OAuth allowed |
| `s3_probe_only_at` | optional: next **probe-only** (no OAuth) time |

### Budget

| | Policy |
|---|---|
| **Max full OAuth while in S3** | **3 total** per incident (the failing auth that entered S3 counts as #1) |
| **Retries after entry** | **2** more full OAuth max (`s3_full_oauth_fails` goes 1 → 2 → 3) |
| **Min spacing between full OAuth** | **≥ 6 hours** (`s3_next_retry_at = last_full_oauth + 6h`) |
| **Probe-only between OAuth** | allowed **once** at ~**+1h** after last fail: billing/models only — **no** new device code if still 403 |
| **On 3rd full OAuth still 403** | promote **S3 → S4** immediately |
| **On any 200** | → S0/S1; clear `s3_*` counters |
| **Wall clock cap** | if still S3 after **48h** from `s3_entered_at` with ≥2 full fails → promote **S4** even if 3rd OAuth not run |

### Timeline example

```text
T+0h   full OAuth #1 → 403  → S3  (s3_full_oauth_fails=1)
       next full OAuth not before T+6h
T+1h   optional probe-only (no OAuth)
T+6h   full OAuth #2 → 403  → still S3 (fails=2)
T+12h  full OAuth #3 → 403  → S4 pool-exclude
       (if #2 had been 200 → S0/S1 and stop)
```

### S4 after promotion — action_required (not automatic)

**Operator law:** S4 is **action_required**, not an automatic delete. Orchestrator/human chooses teardown for disposable coal; nothing in this repo auto-nukes on stage-set S4.

| On entering **S4** | Mark `action_required`; stop reauth loops; optional operator path: CPA DELETE + `identity_ops retire --email … --reason s4_action` |
| Weekly probe | optional observability only — does not auto-retire |

```bash
# optional operator teardown (explicit)
python3 identity_ops.py stage-set --email 'USER@host' --stage S4 --bot-flag 1 --billing-http 403 --note 's4_action_required'
# … CPA DELETE if desired …
python3 identity_ops.py retire --email 'USER@host' --reason s4_action
```

### Why 6h / 3 attempts (not once forever, not every hour)

- Community: 403 can be **dynamic** → worth more than a single try, but not a hammer.
- Our example-account data: **same-day** multi-OAuth did not help → dense retries waste device codes and risk more bot heat.
- **6h** ≈ few tries per day without looking like a tight bot loop.
- **3 full OAuth** then S4 = clear stop condition so orchestrators don't invent “one more.”

### Orchestrator checklist before any S3 retry

```text
if api_stage == S4: do not S3-retry (weekly probe-only only)
if api_stage == S3:
  if now < s3_next_retry_at: skip (or probe-only if due)
  if s3_full_oauth_fails >= 3: set S4; skip
  if now - s3_entered_at >= 48h and fails >= 2: set S4; skip
  else: ensure → one full OAuth → hard probe → update counters/stage → stop browser
```

## What `bot_flag_source: 1` means

- xAI-issued JWT claim (proxy does not invent it)
- **Risk annotation**, not proven permanent ban
- **Not** sufficient alone to delete or retire
- Correlate with 403: our coal so far only saw example-account as flag+403; community reports flag+200 exists elsewhere — always **probe**

## Orchestrator / navigator rules

| If stage | Do | Don't |
|---|---|---|
| S0/S1 | use in CPA pool | treat flag as discard |
| S2 | one full reauth + hard verify | refresh-only thrash; multi `xai-auth-url` spam |
| S3 | cool-off; log; retry-later window | immediate second/third reauth |
| S4 | **action_required** — operator may DELETE auth + retire | silent ignore; more device OAuth loops |
| S5/S6 | full discard path | leave zombie IDENTITIES row |

### Chat shipment / deposit

Classify each account into S0–S4 after hard verify before calling deposit “done.”

### Operator reauth routing

- bad-credentials without fresh-auth-403 history → S2 path once  
- already S3/S4 in the stage ledger → skip automatic reauth loops  

## Worked example (sanitized)

After repeated full OAuth still 403 with `bot_flag_source: 1` → **S4 action_required**. Operator ran explicit retire; stage ledger annotated. No automatic delete from stage-set alone.

## Reference

- Findings: `browser-ops/(local operator notes; not tracked)`
- `xai-auth-url` = official CPA OAuth start (live: device flow)
