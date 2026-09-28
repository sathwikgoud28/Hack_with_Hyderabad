# DejaFix ↺: incident response that remembers every outage

> Your 3 AM outage has probably happened before. DejaFix remembers how you fixed it.

DejaFix is an AI on-call assistant built on **[Hindsight](https://hindsight.vectorize.io/)** agent memory.
The engineer enters what's broken (**Service, Problem, Error**, optionally logs) and presses **🔍 Investigate**.

## Two modes

| | 🧠 Known problem | 🆕 Unknown problem |
|---|---|---|
| Hindsight finds… | a past incident with the same failure | nothing similar |
| DejaFix does… | recalls the fix that worked before and recommends it, warns about what failed before | runs a **fresh investigation** from the current error, problem and logs, and never invents past incidents |
| After the fix | the outcome is retained | the outcome is retained → **next time it's a known problem** |

```
New incident → Hindsight search ─┬─ similar memory found → recall past fixes ─┐
                                 └─ no memory found      → fresh investigation ┤
                                                                               ↓
                                     AI analysis → Root cause · Suggested fix · Evidence
                                                                               ↓
                                     Engineer reviews → ❌ didn't work → Hindsight remembers the failed fix → investigate again
                                                      → ✅ fix worked  → Hindsight learns the incident → next time: known problem
```

## The problem

When production breaks, on-call engineers start from zero. The fix from last time is buried in a Slack thread
or a postmortem nobody reads. Teams repeat the same failed reactions (restart the pods, scale up) and lose
minutes that cost real money. A stateless chatbot can't help, because it has never seen *your* incidents.

## How Hindsight memory is used

| Hindsight operation | Where | What it does in DejaFix |
|---|---|---|
| `create_bank` | [backend/memory.py](backend/memory.py) | Creates the `incident-memory` bank with a *mission* telling Hindsight to focus on services, symptoms, root causes, triggers, working fixes and failed fixes. |
| `retain` | `IncidentAgent.resolve`, `IncidentAgent.fail`, `seed_memory` | A resolved incident is stored as an `incident_postmortem` (problem, error, root cause, solution, what didn't work, time to fix). A fix that did not work is stored immediately as a `failed_fix`, so it is never suggested again. |
| `recall` | `IncidentAgent.recall` | Every investigation searches the bank with Hindsight's semantic + keyword + graph + temporal retrieval. |
| `reflect` | `IncidentAgent.insights` | The **Memory insights** tab asks Hindsight to reason over the whole bank: which services and changes cause most incidents, which fixes work, what to prevent. |

**Known or unknown?** Hindsight always returns the *closest* memories, and a "payment gateway timeout" scores
close to a "payments database timeout" just because the words overlap. A recalled incident only counts as a known
problem when all of these hold:
1. Hindsight's reranker score passes a threshold.
2. An LLM relevance check confirms it is the **same kind of failure**, using only what the new incident states.
3. At least one **concrete signal** matches: the same service, the same error code / message, or two or more shared kinds of symptom.
   This stops a vague report ("Users cannot log in, OAUTH_TOKEN_INVALID") being tied to an old incident by an assumed cause.

The same service **and** the same error always count, even if the LLM check hesitates on a terse report. Every
look-alike that was *not* used is listed in the UI with the reason, so judges can see relevant retrieval at work.

## What the engineer (and the judges) see

| Screen | Shows |
|---|---|
| **KNOWN PROBLEM · 🧠 Hindsight memory found** | **🧠 MEMORY USED** card: previous incident, Hindsight similarity %, previous problem, solution and outcome, the raw facts Hindsight recalled, and a flow: current incident → Hindsight memory → previous incident → successful fix → current recommendation |
| **NEW PROBLEM · 🆕 No relevant memory found** | **🆕 NEW PATTERN** panel: what the recommendation is based on, confidence, and the look-alikes Hindsight returned but that were rejected |
| **💡 Recommendation** | Likely root cause, tagged **Hypothesis · not yet confirmed** (new problem, with a ⚠️ confirm warning) or **From memory · confirmed in INC-xxxx**; suggested fix; evidence; what not to do |
| **🔍 Why this recommendation?** | Current vs previous incident, the matching signals that were actually checked, previous solution and outcome |
| **🕒 Incident timeline** | Detected → investigation → Hindsight searched → recommendation → fix applied → resolved → saved to Hindsight, with real timestamps only |
| **Active incidents** | One card per incident: service, problem, error, recent changes, 🧠 Known / 🆕 Fresh badge, recalled incident + similarity, suggested fix, status |
| **Memory insights** | Cards (total memories, resolved incidents, recurring patterns, new problems learned), a learning progression built from real incidents, patterns / services / triggers / fixes / failed fixes, and Hindsight `reflect` split into sections |

Links: `/#incident=INC-2011` opens an incident, `/#tab=insights` opens a tab.

## Architecture

```
frontend/ (vanilla JS dashboard: New incident · Active incidents · Past incidents · Memory insights)
      │  REST
backend/main.py (FastAPI)
      │
backend/agent.py  IncidentAgent ── recall / retain / reflect ──► backend/memory.py ──► Hindsight Cloud (bank: incident-memory)
      │                                                                          └─► LocalMemory (offline fallback)
      └── same-failure check + recommendation ──► backend/llm.py ──► Groq (gpt-oss-120b → qwen3.8-27b → gpt-oss-20b)
                                                                └─► rule-based recommendation if the LLM is unavailable
```

API: `POST /api/incidents` (investigate) · `POST /api/incidents/{id}/fail` (didn't work → investigate again) ·
`POST /api/incidents/{id}/resolve` (fix worked → Hindsight learns) · `GET /api/incidents?status=investigating` ·
`GET /api/history` · `GET /api/insights` (Hindsight reflect) · `GET /api/insights/summary` (cards, patterns,
learning progression) · `GET /api/examples` · `GET /api/health` (includes whether Hindsight is reachable).

New memories are retained with structured metadata (problem, error, root cause, solution, outcome), so a recalled
incident can be shown as "previous problem / solution / outcome" on any machine sharing the bank.

Reliability details:
- **LLM errors:** retries with backoff; JSON mode is dropped on the final attempt. A rate limit (429) hands over to the next model immediately; if every model is limited, it waits the few seconds Groq asks for, then retries. If all of that fails, a deterministic rule-based recommendation is built from the recalled memories.
- **Output validation:** the recommendation is validated with Pydantic. In fresh mode, confidence is capped at "medium" and any claimed past incidents are removed.
- **Memory failures are visible:** if recall fails, the page shows "Memory recall failed" instead of silently answering without memory. If a memory write fails, the incident stays open (HTTP 502) and nothing is lost.
- **Thread safety:** all Hindsight SDK calls run on one dedicated thread, because the SDK's connection is tied to the event loop it was first used on.
- **Input validation:** blank fields return a 422; changing a resolved incident returns a 409.
- **Offline mode:** without a Hindsight key, a local keyword store is used so the app and tests still run. The header shows which backend is active.

## Setup

Requires Python 3.11+.

```bash
git clone https://github.com/sathwikgoud28/Hack_with_Hyderabad.git
cd Hack_with_Hyderabad
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env - then fill in HINDSIGHT_API_KEY and GROQ_API_KEY
```

`.env` holds your keys and is git-ignored; never commit it.

- Hindsight Cloud key: https://ui.hindsight.vectorize.io → Connect (billing promo code `MEMHACK99`)
- Groq key: https://console.groq.com

```bash
python -m scripts.seed_memory --reset   # load 18 historical incidents into Hindsight (clears app incidents too)
uvicorn backend.main:app --port 8000    # open http://localhost:8000
```

Run the tests (offline, no keys needed):

```bash
$env:MEMORY_BACKEND="local"; python -m pytest -q     # Windows PowerShell
MEMORY_BACKEND=local python -m pytest -q             # macOS / Linux
```

## Demo script (about 2 minutes)

1. **Set the scene:** "It's 3 AM. Payments are failing. You're on call."
2. **Known problem:** Service `payments-api`, Problem "Payment API is slow", Error "Database connection timeout". Press **🔍 Investigate**. It shows 🧠 *Similar incident found* (INC-1004, INC-1047), the suggested fix "roll back", and what not to do.
3. **Unknown problem:** Service `payment-gateway`, Problem "Card payments failing at checkout", Error `PAYMENT_GATEWAY_TIMEOUT`. It shows 🆕 *No similar incidents found → fresh investigation*, with evidence taken from the current error and logs.
4. Click **❌ Didn't work**. Hindsight remembers the failed fix and DejaFix investigates again with a different fix.
5. Click **✅ Fix worked**: root cause "gateway changed its API timeout configuration", solution "timeout 5s → 15s". The **🧠 Hindsight learned a new memory** card appears.
6. **Investigate the same problem again.** Now it's 🧠 a known problem: "resolved before by raising the timeout to 15s; raising it to 12s failed." *That's the learning curve.*
7. Open **Memory insights**: Hindsight `reflect` summarises the team's incident patterns.

Run `python -m scripts.seed_memory --reset` before each rehearsal so the gateway problem is new again.

## Project layout

```
backend/        config, memory (Hindsight + local), llm (Groq), agent (two modes), models, FastAPI app
frontend/       index.html, app.js, style.css
data/           seed_incidents.json (18 realistic postmortems), demo_alerts.json (examples)
scripts/        seed_memory.py (load / reset the Hindsight memory bank)
tests/          offline test suite (fake LLM + local memory)
docs/           PHASES.md - build plan and what changed in each phase
.env.example    configuration template (copy to .env)
requirements.txt
```

All data is synthetic (fictional company "ShopNest").

Built for **HackwithHyderabad 3.0**, theme *"AI Agents That Learn Using Hindsight"*.
