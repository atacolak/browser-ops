# metabolism_core

Pure, side-effect-free **terminal-quota classification** and **strike-ledger
semantics** salvaged from legacy metabolism tooling.

This package is a **library only**. It does not:

- call CPA / management / billing / chat APIs
- read environment keys or credentials
- delete, retire, or mutate browser identities
- touch the filesystem or absolute paths
- auto-act on threshold

Callers own **persistence** of strike state and any **explicit** follow-up when
`action_required` becomes true (for example S4 / operator review — never implied
automatic teardown in this tree).

---

## Layout

| Path | Role |
|---|---|
| `metabolism_core/classify.py` | management candidacy + pinned probe classifier |
| `metabolism_core/strikes.py` | pure strike-state transitions |
| `tests/test_metabolism_terminal_probe.py` | focused unit coverage |

---

## Management candidacy (quota-like)

```python
from metabolism_core import select_probe_candidate_from_auth_file

result = select_probe_candidate_from_auth_file(auth_file_row)
# result["discard_candidate"]  → bool
# result["matched_quota_strings"]
# result["candidate_reasons"]
```

Rules (fail-closed):

- Only status / message / error-like **text** can nominate a probe candidate.
- Counters (`failed`, `success`, `recent_requests`) alone never nominate.
- **Billing / `used_pct` is never a trigger**, even if present on the row.
- Bare `disabled` / `unavailable` / non-quota errors do not nominate.

---

## Pinned probe response classifier

```python
from metabolism_core import classify_terminal_probe_response, is_terminal_quota_probe

probe = classify_terminal_probe_response({
    "status_code": 403,
    "body": '{"error":{"message":"You have reached your weekly response limit."}}',
})
# probe["classifier"] == "terminal_weekly_quota_exhausted"
# is_terminal_quota_probe(probe) is True
```

Fail-closed highlights:

- HTTP 200 with nonempty assistant content → keep
- rate limit / auth expiry / access denied / parse errors → keep (non-terminal)
- access-denied text wins even if weekly-quota words also appear
- only 400/403 + terminal weekly-quota strings → terminal class

`delete_allowed` on a probe result means **“this response is terminal-class”**
(legacy field name). It does **not** mean the library deleted anything.

---

## Strike ledger transition (pure)

```python
from metabolism_core import apply_terminal_strike_transition, empty_strike_state

state = None  # or caller-loaded prior state
for run_id in ("run-1", "run-2", "run-3"):
    state = apply_terminal_strike_transition(
        state,
        probe,
        run_id=run_id,
        email="user@example.test",
        auth_id="xai-user@example.test.json",
        auth_index="auth-idx-1",
    )
# after 3 distinct runs with terminal probes:
# state["count"] == 3
# state["action_required"] is True   # caller decides next step
# state["delete_allowed"] is True    # alias of action_required (compat)
```

Rules:

| Condition | Effect |
|---|---|
| terminal weekly-quota probe | at most **one** strike per `run_id` |
| same `run_id` again | no increment (`already_recorded_this_run`) |
| `auth_id` / `auth_index` change | prior strikes reset, then record new |
| non-terminal / unknown / success | immediate reset (`reset:<classifier>`) |
| `count >= 3` (default) | `action_required=True` — **no deletion** |

Strike evidence is secret-safe (bearer/JWT/email redacted). Caller supplies
`prior_state`, `run_id`, and auth identity; caller persists the returned state.

---

## What stays out of this package

- Live probe transport (`api-call`, billing fetch, auth-files list)
- Policy allowlists / coal batch membership
- Discard archives, identity retire, CPA admin keys
- Cron / metabolism orchestration

Those belong to orchestrators that **import** this library, run probes externally,
and act only with explicit operator policy.
