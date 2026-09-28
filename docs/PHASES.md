# DejaFix build phases

| # | Phase | Deliverables | Status |
|---|---|---|---|
| 1 | Setup & config | `requirements.txt`, `.env.example`, `.gitignore`, `backend/config.py` | ✅ Done |
| 2 | Realistic data | `data/seed_incidents.json` (18 postmortems, recurring patterns), `data/demo_alerts.json` | ✅ Done |
| 3 | Memory layer | `backend/memory.py`: Hindsight bank + retain/recall/reflect, offline `LocalMemory` | ✅ Done |
| 4 | LLM layer | `backend/llm.py`: Groq with retries, model fallback, tolerant JSON parsing | ✅ Done |
| 5 | Agent core | `backend/agent.py`: recall → plan → resolve/retain → reflect; rule-based fallback | ✅ Done |
| 6 | API | `backend/main.py` (FastAPI), `scripts/seed_memory.py` | ✅ Done |
| 7 | Dashboard | `frontend/` (first version; replaced in Phase 10) | ✅ Done |
| 8 | Tests | `tests/test_agent.py` (21 offline tests after Phase 10) | ✅ Done |
| 9 | Live integration | Real Hindsight Cloud + Groq, prompt and recall tuned on real output | ✅ Done |
| 10 | Two-mode redesign | Simple input, 🧠 known vs 🆕 unknown modes, didn't-work loop, dashboard (see below) | ✅ Done |
| 11 | Hindsight-first presentation | Memory Used card + flow, Why this recommendation, hypothesis vs confirmed, timeline, insights cards, learning progression, stricter relevance (see below) | ✅ Done |
| 12 | Submission | Demo video, article, social post, GitHub repo (pushed by you) | ⏳ Your team |

## Phase 11: make Hindsight the star
- **Backend:** Hindsight semantic similarity captured; structured metadata on new memories; per-attempt timestamps;
  matched-incident cards built from real recalled data; checkable matching signals; rejected look-alikes with reasons;
  `root_cause_status` (hypothesis / historical); `/api/insights/summary`; `connected` in `/api/health`;
  older records are filled in when read (nothing rewritten).
- **Relevance fix found in live testing:** `auth-api / OAUTH_TOKEN_INVALID` was tied to the old auth-service key-rotation
  incident (INC-1034) by an assumed cause. Now a memory also needs a concrete signal, and the LLM check may not assume
  causes. Same service + same error always counts, so terse reports of a known problem are recalled reliably.
- **Frontend:** Hindsight status + "Powered by Hindsight", Why Hindsight intro, KNOWN / NEW banners, Memory Used, New Pattern,
  hypothesis tags, Why this recommendation, timeline, richer active cards, insights page, favicon.
- **Tests:** 31 offline tests; live end-to-end run on Hindsight Cloud + Groq; headless-Chrome render of 8 screens with 0 console errors.

## Phase 10: two-mode redesign (simpler to explain)
| Step | What changed | Files |
|---|---|---|
| A. Input & API | Form is just **Service, Problem, Error** (+ optional logs) and **🔍 Investigate**. Incidents have a lifecycle: investigate → didn't work (investigate again) → resolved. | `backend/models.py`, `backend/main.py` |
| B. Agent | 🧠 **Known problem** → recall and recommend what worked. 🆕 **Unknown problem** → fresh investigation from current data. Answer = Root cause · Suggested fix · Evidence. A failed fix is retained in Hindsight and never suggested again. An LLM "same failure?" check stops look-alike memories (payment *gateway* timeout vs payments *DB* timeout) from counting as known. | `backend/agent.py` |
| C. Dashboard | Tabs: New incident · Active incidents · Past incidents · Memory insights. "🧠 Hindsight learned a new memory" card after resolving. | `frontend/*` |
| D. Data, tests, docs | 3 examples (2 known, 1 new: `PAYMENT_GATEWAY_TIMEOUT`), 21 offline tests, README demo script. Verified live: unknown → didn't work → resolved (5s → 15s) → same problem is now known. | `data/demo_alerts.json`, `tests/`, `README.md` |

## Phase 9 results (verified live)
- [x] `.env` has `HINDSIGHT_API_KEY` and `GROQ_API_KEY`
- [x] Seeding works: 18 postmortems → ~84 Hindsight memories (facts + observations) in ~12s
- [x] Header shows "Memory: Hindsight · incident-memory" and `openai/gpt-oss-120b`
- [x] All 4 demo alerts: the 3 recurring ones recall the right incidents with high confidence; the new one stays honest (low confidence, no forced match)
- [x] Resolve → re-triage loop: brand-new alert goes from low confidence to high confidence citing the resolved incident, and honours the engineer's feedback
- [x] Insights tab returns a Hindsight `reflect` report (~12s)

What Phase 9 changed after seeing real output:
- **gpt-oss returned empty answers** (reasoning used up the token budget) → set `reasoning_effort`, raised `max_tokens`.
- **`qwen/qwen3-32b` no longer exists on Groq** → fallback chain is now `qwen/qwen3.8-27b`, then `openai/gpt-oss-20b`.
- **Groq free tier is 8,000 tokens/min per model** → a 429 switches to the next model instantly; if all are limited, wait the few seconds Groq asks for, then retry.
- **Hindsight always returns results, even for unrelated alerts, and the LLM force-fit them** → a recall only counts as a match if Hindsight's reranker score reaches ≥ 0.05 (true matches are ~0.2–0.5, unrelated ones < 0.01). Weak recalls are shown in the UI but not given to the LLM.
- **Hindsight observations carry no metadata** → incident IDs are recovered from the text.
- The prompt now puts the fastest mitigation that worked before (rollback, pin version) as step 1.
- Insights render as formatted Markdown; the Hindsight client is closed on shutdown.

## Demo-day tips
- Run `python -m scripts.seed_memory --reset` before recording, so the `PAYMENT_GATEWAY_TIMEOUT` problem is new again.
- Leave ~20 seconds between clicks when recording: the Groq free tier is rate limited (the app copes, but answers are best from the primary model).

## Ideas if time remains
- Pre-deploy risk check: "this PR touches `@Transactional` in payments-api; 3 past incidents came from that"
- Slack slash command `/dejafix <alert>`
- Import real postmortems from Markdown/Confluence
