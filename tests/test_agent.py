import json

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.agent import MATCH_PROMPT, IncidentAgent, IncidentLog, incident_to_retain_item, matching_signals
from backend.config import DATA_DIR
from backend.llm import LLM, extract_json
from backend.memory import LocalMemory, MemoryItem
from backend.models import FailIn, IncidentIn, ResolveIn

SEED = json.loads((DATA_DIR / "seed_incidents.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((DATA_DIR / "demo_alerts.json").read_text(encoding="utf-8"))
KNOWN, ORDERS, NEW = 0, 1, 2  # indexes into demo_alerts.json


class FakeLLM(LLM):
    """LLM stand-in: `match` answers the same-failure check, `plan` answers the recommendation."""

    def __init__(self, plan=None, match=None):
        super().__init__(api_key="", model="fake")
        self.plan_response = plan
        self.match_response = match
        self.prompts = []

    @property
    def available(self):
        return True

    def complete_json(self, system, user):
        self.prompts.append(user)
        return self.match_response if system == MATCH_PROMPT else self.plan_response

    def complete_text(self, system, user):
        return None


@pytest.fixture
def agent(tmp_path):
    mem = LocalMemory(tmp_path / "mem.json")
    mem.retain([incident_to_retain_item(i) for i in SEED])
    return IncidentAgent(mem, LLM(api_key="", model="none"), IncidentLog(tmp_path / "log.json"), seed=SEED)


def example(i, **kw):
    d = {k: v for k, v in EXAMPLES[i].items() if k != "label"}
    return IncidentIn(**{**d, **kw})


# --------------------------------------------------------------------------- #
# Two modes
# --------------------------------------------------------------------------- #
def test_recall_finds_past_connection_pool_incidents(agent):
    memories, error = agent.recall(example(KNOWN))
    assert error is None
    assert {"INC-1004", "INC-1031", "INC-1047"} <= {m.metadata["incident_id"] for m in memories[:4]}


def test_known_problem_recommends_what_worked_before(agent):
    rec = agent.investigate(example(KNOWN))
    a = rec["attempts"][0]
    r = a["recommendation"]
    assert rec["id"] == "INC-2001" and rec["status"] == "investigating"
    assert a["mode"] == "memory" and a["memories"]
    assert r["confidence"] == "high"
    assert "roll" in r["suggested_fix"].lower()
    assert r["steps"][0]["source"].startswith("INC-")
    assert any("restart" in x["action"].lower() for x in r["avoid"])


def test_unknown_problem_is_a_fresh_investigation(agent):
    rec = agent.investigate(example(NEW))
    a = rec["attempts"][0]
    r = a["recommendation"]
    assert a["mode"] == "fresh" and a["memories"] == []
    assert r["confidence"] == "low" and r["matched_incidents"] == []
    assert any("PAYMENT_GATEWAY_TIMEOUT" in e for e in r["evidence"])


def test_full_learning_loop_unknown_then_known(agent):
    """New problem -> first fix fails -> investigate again -> resolved -> next time it is a known problem."""
    first = agent.investigate(example(NEW))
    failed_fix = first["attempts"][0]["recommendation"]["suggested_fix"]

    rec = agent.fail(first["id"], FailIn(note="no change"))
    assert [a["outcome"] for a in rec["attempts"]] == ["failed", None]
    second = rec["attempts"][1]["recommendation"]
    assert second["suggested_fix"] != failed_fix
    assert any(x["action"] == failed_fix for x in second["avoid"])

    rec = agent.resolve(first["id"], ResolveIn(
        root_cause="Payment gateway changed its API timeout configuration.",
        solution="Updated the gateway client timeout from 5s to 15s.",
        time_to_resolve_min=20,
    ))
    assert rec["status"] == "resolved"
    nm = rec["resolution"]["new_memory"]
    assert nm["solution"].startswith("Updated") and nm["didnt_work"] and nm["outcome"] == "Resolved in 20 min"

    again = agent.investigate(example(NEW))
    a = again["attempts"][0]
    assert a["mode"] == "memory"
    assert first["id"] in {m["metadata"]["incident_id"] for m in a["memories"]}
    assert "15s" in a["recommendation"]["suggested_fix"]
    assert any(failed_fix in x["action"] for x in a["recommendation"]["avoid"])


def test_resolved_incident_cannot_change_again(agent):
    rec = agent.investigate(example(ORDERS))
    agent.resolve(rec["id"], ResolveIn(root_cause="poison message", solution="skip the offset"))
    with pytest.raises(ValueError):
        agent.resolve(rec["id"], ResolveIn(root_cause="poison message", solution="skip the offset"))
    with pytest.raises(ValueError):
        agent.fail(rec["id"], FailIn())
    with pytest.raises(KeyError):
        agent.fail("INC-9999", FailIn())


def test_recall_failure_is_reported_not_hidden(agent):
    def boom(query, limit=8):
        raise RuntimeError("Timeout context manager should be used inside a task")

    agent.memory.recall = boom
    a = agent.investigate(example(KNOWN))["attempts"][0]
    assert a["mode"] == "fresh" and "RuntimeError" in a["memory_error"]


# --------------------------------------------------------------------------- #
# Same-failure check and LLM recommendation
# --------------------------------------------------------------------------- #
def test_llm_rejects_lookalike_memories(agent):
    """High Hindsight score but a different failure (e.g. gateway vs DB pool timeout) -> fresh investigation."""
    agent.llm = FakeLLM(plan={"summary": "fresh", "suggested_fix": "raise timeout"}, match={"same_as": []})
    a = agent.investigate(example(KNOWN))["attempts"][0]
    assert a["mode"] == "fresh" and a["memories"] == []
    assert "no similar past incidents" in agent.llm.prompts[-1]


def test_llm_confirms_only_the_real_matches(agent):
    agent.llm = FakeLLM(plan={"summary": "s", "suggested_fix": "rollback", "confidence": "HIGH"}, match={"same_as": ["INC-1004"]})
    a = agent.investigate(example(KNOWN))["attempts"][0]
    assert a["mode"] == "memory"
    assert {m["metadata"]["incident_id"] for m in a["memories"]} == {"INC-1004"}
    assert a["recommendation"]["generated_by"] == "llm" and a["recommendation"]["confidence"] == "high"


def test_fresh_mode_never_claims_past_incidents(agent):
    agent.llm = FakeLLM(plan={"suggested_fix": "x", "confidence": "high", "matched_incidents": [{"id": "INC-1004"}],
                              "evidence": [{"point": "gateway latency 8.7s"}]}, match={"same_as": []})
    r = agent.investigate(example(NEW))["attempts"][0]["recommendation"]
    assert r["confidence"] == "medium" and r["matched_incidents"] == []
    assert r["evidence"] == ["gateway latency 8.7s"]


def test_invalid_llm_output_falls_back_to_rules(agent):
    agent.llm = FakeLLM(plan={"steps": "not a list"}, match={"same_as": ["INC-1004", "INC-1031"]})
    r = agent.investigate(example(KNOWN))["attempts"][0]["recommendation"]
    assert r["generated_by"] == "rules" and r["confidence"] == "high"


def _fact(text, iid, score, context="incident_postmortem", **md):
    return MemoryItem(id=text[:8], text=text, type="world", context=context,
                      metadata={"incident_id": iid, **md} if iid else md, score=score)


def test_rule_based_understands_hindsight_facts():
    memories = [
        _fact("Incident INC-1004 caused 502s due to connection pool exhaustion. | When: 2026-03-09", "INC-1004", 0.53, time_to_resolve_min="54"),
        _fact("Incident INC-1031 caused payment failures from pool exhaustion. | When: 2026-05-19", "INC-1031", 0.23, time_to_resolve_min="22"),
        _fact("The incident was resolved by rolling back to v4.10.3. | When: 2026-03-09", "INC-1004", 0.02),
        _fact("Restarting pods failed to resolve the incident. | When: 2026-03-09", "INC-1004", 0.01),
        _fact("Unrelated fix on notifications was resolved by enabling Gupshup.", "INC-1056", 0.004),
    ]
    r = IncidentAgent.rule_based(example(KNOWN), memories, failed=[])
    assert r.confidence == "high"
    assert [m.id for m in r.matched_incidents] == ["INC-1004", "INC-1031"]
    assert r.suggested_fix == "The incident was resolved by rolling back to v4.10.3."
    assert r.avoid[0].action.startswith("Restarting pods")
    assert r.estimated_time_to_resolve_min == 22
    assert "Gupshup" not in r.model_dump_json()

    retry = IncidentAgent.rule_based(example(KNOWN), memories, failed=[{"fix": r.suggested_fix, "note": ""}])
    assert retry.suggested_fix != r.suggested_fix
    assert retry.avoid[0].action == r.suggested_fix


# --------------------------------------------------------------------------- #
# Hindsight-first presentation: memory cards, signals, hypothesis, timeline
# --------------------------------------------------------------------------- #
ORDERS_NEW = dict(service="orders-api", problem="Orders are failing intermittently", error="ORDER_QUEUE_TIMEOUT",
                  details="Release v3.7.0 deployed 20 minutes ago; queue processing time doubled.")
ORDERS_AGAIN = dict(service="orders-api", problem="Orders are getting stuck in the processing queue", error="ORDER_QUEUE_TIMEOUT",
                    details="Deployed v3.8.0 an hour ago, queue processing is slow.")
UNRELATED = dict(service="auth-api", problem="Users cannot log in", error="OAUTH_TOKEN_INVALID")


def _resolve_orders(agent):
    first = agent.investigate(IncidentIn(**ORDERS_NEW))
    agent.resolve(first["id"], ResolveIn(root_cause="Regression in v3.7.0 slowed queue processing past the timeout.",
                                         solution="Rolled back orders-api from v3.7.0 to v3.6.x", time_to_resolve_min=10))
    return first


def test_matching_signals_only_lists_true_signals():
    prev = "orders are failing intermittently ORDER_QUEUE_TIMEOUT Regression in v3.7.0 slowed queue processing"
    signals = matching_signals(IncidentIn(**ORDERS_AGAIN), "orders-api", prev)
    assert signals[0] == "Same service (orders-api)"
    assert "Same error (ORDER_QUEUE_TIMEOUT)" in signals
    assert "Both happened around a deployment / release" in signals
    assert any(s.startswith("Similar symptoms:") and "queue processing" in s for s in signals)
    assert matching_signals(IncidentIn(**UNRELATED), "orders-api", prev) == []


def test_new_then_similar_incident_shows_the_memory_used(agent):
    """The exact learning flow: new -> fresh -> confirmed fix -> saved -> similar incident recalls it."""
    first = _resolve_orders(agent)
    assert first["attempts"][0]["mode"] == "fresh"
    assert first["attempts"][0]["root_cause_status"] == "hypothesis"

    again = agent.investigate(IncidentIn(**ORDERS_AGAIN))
    a = again["attempts"][0]
    assert a["mode"] == "memory" and a["root_cause_status"] == "historical"
    card = a["matched"][0]
    assert card["id"] == first["id"]
    assert card["solution"] == "Rolled back orders-api from v3.7.0 to v3.6.x"
    assert card["outcome"] == {"status": "resolved", "minutes": 10}
    assert "Same error (ORDER_QUEUE_TIMEOUT)" in card["signals"] and card["facts"]
    assert "v3.6.x" in a["recommendation"]["suggested_fix"]


def test_resolved_memory_carries_structured_metadata(agent):
    first = _resolve_orders(agent)
    doc = next(d for d in agent.memory.all_docs() if d["metadata"].get("incident_id") == first["id"])
    md = doc["metadata"]
    assert md["solution"].startswith("Rolled back") and md["outcome"] == "resolved" and md["error"].startswith("ORDER_QUEUE_TIMEOUT")


def test_unrelated_incident_does_not_recall_orders_memory(agent):
    first = _resolve_orders(agent)
    a = agent.investigate(IncidentIn(**UNRELATED))["attempts"][0]
    assert first["id"] not in {m["id"] for m in a["matched"]}
    assert first["id"] not in {m["metadata"].get("incident_id") for m in a["memories"]}


def test_vague_incident_is_not_force_matched_without_a_concrete_signal(agent):
    """Regression (seen live): OAUTH_TOKEN_INVALID on auth-api was linked to the auth-service key-rotation incident
    only because both involve logins. With no shared service, error code or symptoms, it must stay a new problem."""

    class StubMemory:
        name = "stub"

        def recall(self, query, limit=8):
            return [MemoryItem(id="m1", text="Incident INC-1034 on auth-service: all users getting 401 Unauthorized after login",
                               context="incident_postmortem", metadata={"incident_id": "INC-1034", "service": "auth-service"},
                               score=0.4, similarity=0.68)]

    agent.memory = StubMemory()
    agent.llm = FakeLLM(plan={"suggested_fix": "check the OAuth client secret"}, match={"same_as": ["INC-1034"], "reason": "both invalid tokens"})
    a = agent.investigate(IncidentIn(**UNRELATED))["attempts"][0]
    assert a["mode"] == "fresh" and a["matched"] == [] and a["memories"] == []
    assert a["rejected"] == [{"id": "INC-1034", "similarity": 0.68, "reason": "no concrete matching signal (different service, different error)"}]


def test_same_service_and_same_error_counts_even_if_llm_check_hesitates(agent):
    """A terse report ("Payment API is slow / Database connection timeout") matching a past incident's service and
    exact error must be recalled consistently, not depend on the LLM check's mood."""
    past = agent.investigate(IncidentIn(service="payments-api", problem="payment api is slow", error="database connection timeout",
                                        details="release v4.18.0 deployed, pool usage 98%"))
    agent.resolve(past["id"], ResolveIn(root_cause="v4.18.0 held DB connections too long", solution="Rolled back payments-api to v4.17.0"))
    agent.llm = FakeLLM(plan={"suggested_fix": "Roll back payments-api to v4.17.0", "confidence": "high"},
                        match={"same_as": [], "reason": "No matching root-cause pattern described"})
    a = agent.investigate(IncidentIn(service="payments-api", problem="Payment API is slow", error="Database connection timeout"))["attempts"][0]
    assert a["mode"] == "memory"
    card = next(c for c in a["matched"] if c["id"] == past["id"])
    assert 'Same error message ("Database connection timeout")' in card["signals"]
    assert a["match_reason"] == f"Same service and same error as {past['id']}"


def test_rejected_lookalikes_are_recorded(agent):
    agent.llm = FakeLLM(plan={"suggested_fix": "raise timeout"}, match={"same_as": [], "reason": "gateway timeout, not a DB pool"})
    a = agent.investigate(example(KNOWN))["attempts"][0]
    assert a["mode"] == "fresh" and a["matched"] == []
    assert {"INC-1004", "INC-1031"} <= {r["id"] for r in a["rejected"]}
    assert a["match_reason"] == "gateway timeout, not a DB pool"


def test_timeline_timestamps_are_recorded_in_order(agent):
    rec = agent.investigate(example(NEW))
    a = rec["attempts"][0]
    assert rec["detected_at"] <= a["started_at"] <= a["searched_at"] <= a["recommended_at"]
    rec = agent.fail(rec["id"], FailIn(note="no change"))
    assert rec["attempts"][0]["outcome_at"] <= rec["attempts"][0]["failed_saved_at"] <= rec["attempts"][1]["started_at"]
    rec = agent.resolve(rec["id"], ResolveIn(root_cause="gateway timeout config", solution="timeout 5s -> 15s"))
    assert rec["attempts"][1]["outcome_at"] == rec["resolved_at"] <= rec["resolution"]["saved_at"]


def test_view_fills_in_old_records_without_changing_them(agent):
    first = _resolve_orders(agent)
    again = agent.investigate(IncidentIn(**ORDERS_AGAIN))
    stored = agent.log.get(again["id"])
    for key in ("matched", "rejected", "match_reason", "root_cause_status"):
        del stored["attempts"][0][key]  # what a record saved by the previous version looks like
    view = agent.view(stored)
    assert view["attempts"][0]["matched"][0]["id"] == first["id"]
    assert "matched" not in stored["attempts"][0]


def test_insights_summary_is_computed_from_real_data(agent):
    first = _resolve_orders(agent)
    again = agent.investigate(IncidentIn(**ORDERS_AGAIN))
    s = agent.summary()
    assert s["cards"]["resolved_incidents"] == 19
    assert s["cards"]["new_problems_learned"] == 1 and s["cards"]["recognised_by_memory"] == 1
    assert any({first["id"], again["id"]} <= set(p["incidents"]) for p in s["patterns"])
    assert any(set(p["incidents"]) == {"INC-1004", "INC-1031", "INC-1047"} for p in s["patterns"])
    assert s["progression"]["first"]["id"] == first["id"] and s["progression"]["second"]["id"] == again["id"]
    assert dict(s["ineffective"])["Restarting pods / services / consumers"] >= 5


# --------------------------------------------------------------------------- #
# Infrastructure
# --------------------------------------------------------------------------- #
def test_hindsight_calls_all_run_on_one_thread(monkeypatch):
    """Regression: FastAPI's thread pool must not spread SDK calls across event loops."""
    import sys
    import threading
    import types
    from concurrent.futures import ThreadPoolExecutor

    from backend.memory import HindsightMemory

    seen: set[int] = set()

    class FakeSDK:
        def __init__(self, **kwargs):
            seen.add(threading.get_ident())

        def create_bank(self, **kwargs):
            seen.add(threading.get_ident())

        def recall(self, **kwargs):
            seen.add(threading.get_ident())
            return types.SimpleNamespace(results=[])

    monkeypatch.setitem(sys.modules, "hindsight_client", types.SimpleNamespace(Hindsight=FakeSDK))
    mem = HindsightMemory("http://x", "key", "bank")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: mem.recall("q"), range(20)))
    mem.close()
    assert len(seen) == 1


def test_llm_errors_fall_back_to_second_model():
    llm = LLM(api_key="", model="primary", fallback_model="backup", retries=0)
    calls = []

    def fake_chat(model, messages, json_mode):
        calls.append(model)
        if model == "primary":
            raise RuntimeError("tool_use_failed")
        return '<think>hmm</think>```json\n{"summary": "ok"}\n```'

    llm.client = object()
    llm._chat = fake_chat
    assert llm.complete_json("s", "u") == {"summary": "ok"}
    assert calls == ["primary", "backup"]


def test_rate_limit_switches_model_without_retrying():
    llm = LLM(api_key="", model="primary", fallback_model="backup", retries=2)
    calls = []

    class RateLimited(Exception):
        status_code = 429

    def fake_chat(model, messages, json_mode):
        calls.append(model)
        if model == "primary":
            raise RateLimited("tokens per minute")
        return '{"summary": "from backup"}'

    llm.client = object()
    llm._chat = fake_chat
    assert llm.complete_json("s", "u") == {"summary": "from backup"}
    assert calls == ["primary", "backup"]


def test_all_models_rate_limited_waits_then_retries_primary(monkeypatch):
    llm = LLM(api_key="", model="primary", fallback_model="backup1, backup2")
    assert llm.models == ["primary", "backup1", "backup2"]
    calls, slept = [], []
    monkeypatch.setattr("backend.llm.time.sleep", slept.append)

    class RateLimited(Exception):
        status_code = 429

    def fake_chat(model, messages, json_mode):
        calls.append(model)
        if len(calls) <= 3:
            raise RateLimited(f"Please try again in {len(calls)}.5s")
        return '{"summary": "after wait"}'

    llm.client = object()
    llm._chat = fake_chat
    assert llm.complete_json("s", "u") == {"summary": "after wait"}
    assert calls == ["primary", "backup1", "backup2", "primary"]
    assert slept == [1.5]


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('Sure! {"a": 1} hope that helps', {"a": 1}),
    ("<think>x</think>\n{\"a\": 2}", {"a": 2}),
    ("no json here", None),
    ("[1, 2]", None),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_api_end_to_end(tmp_path, monkeypatch):
    mem = LocalMemory(tmp_path / "mem.json")
    ag = IncidentAgent(mem, LLM(api_key="", model="none"), IncidentLog(tmp_path / "log.json"), seed=SEED)
    monkeypatch.setattr(main, "memory", mem)
    monkeypatch.setattr(main, "agent", ag)
    c = TestClient(main.app)

    assert c.post("/api/admin/seed").json()["seeded"] == 18
    assert c.get("/api/health").json()["memory_count"] == 18
    ex = c.get("/api/examples").json()
    assert len(ex["examples"]) == 3 and "payments-api" in ex["services"]

    payload = {k: v for k, v in EXAMPLES[NEW].items() if k != "label"}
    inc = c.post("/api/incidents", json=payload).json()
    assert inc["attempts"][0]["mode"] == "fresh"
    assert [i["id"] for i in c.get("/api/incidents?status=investigating").json()] == [inc["id"]]

    rec = c.post(f"/api/incidents/{inc['id']}/fail", json={"note": "still timing out"}).json()
    assert len(rec["attempts"]) == 2

    assert c.post(f"/api/incidents/{inc['id']}/resolve", json={"root_cause": "x", "solution": "y"}).status_code == 422
    ok = c.post(f"/api/incidents/{inc['id']}/resolve",
                json={"root_cause": "Gateway timeout config changed", "solution": "Timeout 5s -> 15s", "time_to_resolve_min": 12})
    assert ok.status_code == 200 and ok.json()["status"] == "resolved"
    assert c.post(f"/api/incidents/{inc['id']}/resolve", json={"root_cause": "again", "solution": "again"}).status_code == 409
    assert c.post("/api/incidents/INC-9999/fail", json={}).status_code == 404
    assert c.post("/api/incidents", json={"service": " ", "problem": "slow", "error": "timeout"}).status_code == 422

    assert c.get("/api/incidents?status=investigating").json() == []
    hist = c.get("/api/history").json()
    assert hist[0]["id"] == inc["id"] and hist[0]["source"] == "dejafix" and len(hist) == 19
    assert "incidents in memory" in c.get("/api/insights").json()["text"]
    summary = c.get("/api/insights/summary").json()
    assert summary["cards"]["resolved_incidents"] == 19 and summary["cards"]["total_memories"] > 18
    health = c.get("/api/health").json()
    assert health["connected"] is True and health["memory_backend"] == "local"
    detail = c.get(f"/api/incidents/{inc['id']}").json()
    assert detail["attempts"][0]["root_cause_status"] == "hypothesis" and "matched" in detail["attempts"][0]
    assert c.get("/").status_code == 200
