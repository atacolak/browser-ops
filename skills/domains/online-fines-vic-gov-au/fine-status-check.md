---
id: fines-vic-status-v1
domain: online.fines.vic.gov.au
category: monitoring
version: 1
status: active
project: omp
source: manual
created_at: 2026-07-07
tags:
  - government
  - fines
  - monitoring
  - cron
  - login
entities:
  - entity: online.fines.vic.gov.au
    type: website
    role: Fines Victoria online portal
triples:
  - subject: online.fines.vic.gov.au
    predicate: requires_multi_step_login
    object: "true"
  - subject: online.fines.vic.gov.au
    predicate: has_fine_stage_field
    object: Fine Stage
    note: Field on Fine Details page showing current stage (Infringement Notice, etc.)
params:
  - name: obligation_number
    description: "Obligation number, infringement number, or court case number"
    required: true
  - name: offence_day
    description: "Day of offence (DD)"
    required: true
  - name: offence_month
    description: "Month of offence (MM)"
    required: true
  - name: offence_year
    description: "Year of offence (YYYY)"
    required: true
  - name: driver_license
    description: "Driver license number"
    required: true
---

# Fines Victoria — Fine Status Check

Logs into the Fines Victoria portal, navigates to a specific fine, and reports the current Fine Stage.
Designed for cron-based monitoring — run twice daily to detect stage changes.

## Fast Path

No API exists. Full browser login required.

## Workflow

> ⚠️ **Use JS `.click()` for all button clicks.** This site's ASP.NET event handlers don't fire from CDP `Input.dispatchMouseEvent`. Use `evaluate("document.querySelector(selector).click()")` for Next, Login, and any navigation buttons.

### Step 1: Navigate to Login

```
navigate("https://online.fines.vic.gov.au/Your-Fines/Your-Fines-Login")
wait_for_load()
```

### Step 2: Enter Obligation Number + Click Next

```
fill_input(selector="input[name=Identifier]", text=params.obligation_number)
evaluate("document.querySelector('input[value=Next]').click()")
wait_for_load()
// Sleep 2s for ASP.NET partial postback
```

After clicking Next, the form expands to show Offence Date fields (DD/MM/YYYY row) and Driver Licence Number field.

### Step 2b: Enter Offence Date + License

```
fill_input(selector=".day-selector", text=params.offence_day)
fill_input(selector=".month-selector", text=params.offence_month)
fill_input(selector=".year-selector", text=params.offence_year)
fill_input(selector="#DriversLicenceNumber", text=params.driver_license)
evaluate("document.querySelector('input[value=Login]').click()")
wait_for_load()
```

### Step 3: Navigate to Fine Details

After login, navigate directly to the fine details page (skip clicking through the list UI):

```
navigate(`https://online.fines.vic.gov.au/Your-Fines/Fine-Details?obligation=${params.obligation_number}`)
wait_for_load()
```

### Step 4: Extract Fine Stage

On the Fine Details page, find the "Fine stage" row and extract the value:

```javascript
// The fine details are in a table. "Fine stage" is a <td>, value is in the next <td>.
Array.from(document.querySelectorAll("tr")).find(
  row => row.innerText && row.innerText.includes("Fine stage")
)?.innerText?.split("\\t")?.pop()?.trim()
```

Expected output: `"Infringement Notice"` (initial state). If this changes, trigger notification.

Known possible values: `Infringement Notice`, `Penalty Reminder Notice`, `Notice of Final Demand`, `Warrant`, `Paid`.

Current status (2026-07-08): **Infringement Notice** — Overdue, $305.00.

### Step 5: Report Outcome

```
report_outcome(
  skill_id="fines-vic-status-v1",
  success=True,
  stage=extracted_value
)
```

## Verified Flow (2026-07-08)

Live test confirmed the following sequence works against the actual site:

1. **Navigate** → `https://online.fines.vic.gov.au/Your-Fines/Your-Fines-Login`
2. **Fill** obligation number → `fill_input("input[name=Identifier]", params.obligation_number)`
3. **Click Next** → `evaluate("document.querySelector('input[value=Next]').click()")`  
   ⚠️ Must use JS `.click()` — CDP `Input.dispatchMouseEvent` doesn't fire the site's event handlers
4. **Wait** → form expands, `#LoginStep2Form` fields become visible
5. **Fill** date fields → DD/MM/YYYY into `.day-selector`, `.month-selector`, `.year-selector`
6. **Fill** license → `#DriversLicenceNumber`
7. **Click Login** → `evaluate("document.querySelector('input[value=Login]').click()")`
8. **Wait** → redirected to `Your-Fines-List` ("Your Fines List" title)
9. **Navigate** → `Fine-Details?obligation=<number>` directly (faster than clicking through list UI)
10. **Extract** → Fine stage from the details table

## Fine Details Page Structure

```
Obligation number    <OBLIGATION_NUMBER>
Fine stage           Infringement Notice
Due date             Overdue
Amount due           $305.00
Offence description  DRIVE UNLAWFULLY IN BICYCLE LANE (CODE:8352)
Offence date         24/03/2026
Offence location     CHAPEL STREET, PRAHRAN
```

## Selectors

> ⚠️ **Critical**: This site uses ASP.NET WebForms with progressive form expansion. Use `element.click()` via evaluate, NOT CDP coordinate clicks, or the JS event handlers won't fire.

| Step | Purpose | Selector | Type | Last Verified |
|------|---------|----------|------|---------------|
| 1 | Obligation number input | `input[name="Identifier"]` | input | 2026-07-08 |
| 1 | Next button | `input[value="Next"]` | submit | 2026-07-08 |
| 2 | Offence day (DD) | `.day-selector` (first visible in `#LoginStep2Form`) | tel | 2026-07-08 |
| 2 | Offence month (MM) | `.month-selector` | tel | 2026-07-08 |
| 2 | Offence year (YYYY) | `.year-selector` | tel | 2026-07-08 |
| 2 | License number input | `#DriversLicenceNumber` | text | 2026-07-08 |
| 2 | Login button | `input[value="Login"]` | submit | 2026-07-08 |
| 3 | Fine Details page | Navigate to `https://online.fines.vic.gov.au/Your-Fines/Fine-Details?obligation=<obligation_number>` | URL | 2026-07-08 |
| 3 | Fine Stage value | Look for `<td>` containing "Fine stage", value is in the sibling `<td>`/`<span>` | text | 2026-07-08 |

## Cron Setup

Two options: direct daemon call (recommended for cron) or via browser-harness extension.

### Option A: Direct daemon (bare Python, no agent overhead)

```bash
#!/bin/bash
# ./bin/check-fine.sh  # operator-local helper, not shipped
RESULT=$(python3 -c "
import asyncio, json
from daemon.backend.cloak import CloakBackend

async def check():
    backend = CloakBackend('fine-watcher')
    await backend.connect('http://127.0.0.1:9222')
    
    # Step 1-2: Login
    await backend.navigate('https://online.fines.vic.gov.au/Your-Fines/Your-Fines-Login')
    await backend.wait_for_load()
    await backend.fill_input('input[name=Identifier]', '<OBLIGATION_NUMBER>')
    await backend.evaluate('document.querySelector(\"input[value=Next]\").click()')
    await backend.wait_for_load()
    await asyncio.sleep(2)
    
    # Step 2 fill
    await backend.fill_input('.day-selector', '24')
    await backend.fill_input('.month-selector', '03')
    await backend.fill_input('.year-selector', '2026')
    await backend.fill_input('#DriversLicenceNumber', '<DRIVER_LICENSE>')
    await backend.evaluate('document.querySelector(\"input[value=Login]\").click()')
    await backend.wait_for_load()
    
    # Step 3: Details
    await backend.navigate('https://online.fines.vic.gov.au/Your-Fines/Fine-Details?obligation=<OBLIGATION_NUMBER>')
    await backend.wait_for_load()
    
    # Step 4: Extract stage
    stage = await backend.evaluate(
        'Array.from(document.querySelectorAll(\"tr\"))' +
        '.find(r => r.innerText && r.innerText.includes(\"Fine stage\"))' +
        '?.innerText?.split(\"\\\\t\")?.pop()?.trim()'
    )
    print(json.dumps({'stage': stage.get('value') if isinstance(stage, dict) else stage}))
    await backend.disconnect()

result = asyncio.run(check())
")

STAGE=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['stage'])")

if [ "$STAGE" != "Infringement Notice" ]; then
    echo "[$(date)] FINE STAGE CHANGED: $STAGE" | tee -a /tmp/fine-watcher.log
    # TODO: send notification (email, SMS, pushover, etc.)
fi
```

### Option B: Via browser-harness extension (agent drives it)

```
browser_skill replay domain:online.fines.vic.gov.au skill_id:fines-vic-status-v1 params:{obligation_number:"<OBLIGATION_NUMBER>",offence_day:"DD",offence_month:"MM",offence_year:"YYYY",driver_license:"<DRIVER_LICENSE>"}
```

### Cron entry

```cron
# Check fine status twice daily (9 AM and 5 PM AEST = 23:00 and 07:00 UTC)
0 23,7 * * * ./bin/check-fine.sh  # operator-local helper, not shipped
```

## Gotchas

- **Multi-step login**: The form expands after entering the obligation number — wait_for_load() between steps.
- **Session expiry**: Unknown. If session expires between steps, start over.
- **CAPTCHA**: Unknown if present. If encountered, needs manual intervention.
- **Rate limiting**: Unknown. Two checks per day should be safe.
- **Fine Stage values**: Currently "Infringement Notice". Possible transitions: "Enforcement", "Warrant", "Paid", "Withdrawn" — confirm actual values on first run.
