---
id: oauth-deposit
domain: x.ai
category: auth
status: active
version: 2
superseded_by: cpa-manager/xai-dashboard-deposit
triggers:
  - xai
  - oauth
  - deposit
  - grok
  - x.ai
  - account deposit
  - coal
---

# xAI OAuth Account Deposit (pointer)

**Canonical skill moved.** Operator-true path is dashboard-first:

→ **`skills/domains/cpa-manager/xai-dashboard-deposit.md`**  
→ `browser_skill op:get domain:"cpa-manager" skill_id:"xai-dashboard-deposit"`

## Why

Live CPA Manager Plus flow (validated 2026-07-13):

1. `http://127.0.0.1:8316/management.html#/oauth`
2. **Start xAI Login**
3. **Open Link**
4. Device Continue → (optional sign-in + Turnstile) → **Allow** (form `action=allow` + submit)
5. Device Authorized → CPA success → auth file on bouncer

Raw `GET /v0/management/xai-auth-url` currently returns **device** flow only; older PKCE + fetch-intercept notes are historical. Do not start by free-handing grok.com.

## Identity

One account ↔ one Cloak profile — see `browser-ops/profiles/IDENTITIES.json`.

Follow the canonical skill for full steps, heals, and verify oracles.
