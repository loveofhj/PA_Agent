import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pa.core import Agent, Store
from pa.pipeline import collect, promote, catalog, REQUIRED
from pa.planner import Planner


class Prototype(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(tmp.name)
        with patch.dict("os.environ", {"LLM_PROVIDER":"mock"}):
            self.agent = Agent(self.store, Planner())

    def plan(self, side="LH"):
        return self.agent.plan({"instruction": f"LT2 FRT 도어트림 {side} 1개 조립"})

    def test_normal_and_recovery(self):
        for side, scenario in [("LH", "normal"), ("RH", "recover")]:
            task = self.plan(side)
            result = self.agent.run(task["TaskID"], scenario)
            self.assertEqual((result["state"], result["inspection"]), ("DONE", "OK"))
            self.assertEqual(bool(result["recoveries"]), scenario == "recover")
            events = [e for e in self.store.list("events") if e["TaskID"] == task["TaskID"]]
            self.assertEqual([e["StepCode"] for e in reversed(events) if e["kind"] == "STEP_DONE"],
                             [f"S{i}" for i in range(1, 9)])
            ids = [e["SkillInvocationID"] for e in events if e["kind"] == "SKILL_RESULT"]
            self.assertEqual(len(ids), len(set(ids)))
            with self.assertRaises(ValueError):
                self.agent.run(task["TaskID"], scenario)

    def test_stop_paths(self):
        for scenario, expected in [("mismatch","HOLD"), ("safe_stop","SAFE-STOP"), ("stale","HOLD"),
                                   ("ng","HOLD"), ("failure","HOLD"), ("timeout","HOLD"),
                                   ("low_confidence","HOLD")]:
            task = self.plan()
            self.assertEqual(self.agent.run(task["TaskID"], scenario)["state"], expected)
            results = [e for e in self.store.list("events") if e["TaskID"] == task["TaskID"]
                       and e["kind"] == "SKILL_RESULT"]
            self.assertFalse(any(e["StepCode"] == "S8" for e in results))
            if scenario in {"mismatch", "safe_stop", "stale"}:
                self.assertFalse(results)

    def test_ambiguous_instruction(self):
        task = self.agent.plan({"instruction": "도어트림 조립"})
        self.assertEqual(task["state"], "HOLD")

    def test_llm_validation_and_timeout(self):
        with patch.dict("os.environ", {"LLM_PROVIDER":"ollama"}):
            planner = Planner()
        agent = Agent(self.store, planner)
        with patch.object(planner, "request", return_value={"message":{"content":"invalid"}}) as request:
            task = agent.plan({"instruction":"LH 1개"})
            self.assertEqual(request.call_count, 2)
            self.assertEqual(task["state"], "HOLD")
        with patch.object(planner, "request", side_effect=TimeoutError("timeout")):
            task = agent.plan({"instruction":"LH 1개"})
            self.assertEqual(task["state"], "FAILED")
            self.assertEqual(self.store.list("events")[0]["cause"], "LLM_TIMEOUT")

    def test_llm_valid_decision(self):
        with patch.dict("os.environ", {"LLM_PROVIDER":"ollama"}):
            planner = Planner()

        def response(path, body, timeout):
            event = json.loads(body["messages"][1]["content"])
            decision = {key:event[key] for key in ("TaskID", "SubTaskID", "EventID")}
            decision.update(decision_type="plan", recipe="RH", quantity=1, reason="RH 단품 계획",
                            steps=[f"S{i}" for i in range(1,9)])
            return {"message":{"content":json.dumps(decision)}}

        with patch.object(planner, "request", side_effect=response):
            result = Agent(self.store, planner).plan({"instruction":"RH 1개 조립"})
        self.assertEqual((result["state"], result["RecipeID"]), ("READY","RH"))

    def test_quality_and_catalog(self):
        a = collect(self.store, {"mode":"mock"})
        b = collect(self.store, {"mode":"mock", "jig_id":a["jig_id"]})
        self.assertEqual(a["quality"]["grade"], "A")
        member = {"episode_id":a["episode_id"], "split":"train"}
        template = {"dataset_id":"demo", "members":[member]}
        self.assertEqual(promote(self.store, template)["version"], 1)
        self.assertEqual(promote(self.store, template)["version"], 2)
        self.assertEqual(len(catalog(self.store, {"source":"Sim","skill":"SKL-011"})), 2)
        with self.assertRaisesRegex(ValueError, "누수"):
            promote(self.store, {**template, "members":[member, {"episode_id":b["episode_id"],"split":"test"}]})

    def test_upload_label_mismatch(self):
        episode = collect(self.store, {"mode":"mock"})
        meta = {key:episode[key] for key in REQUIRED}
        rows = [{"timestamp":"2026-01-01T09:00:00+09:00", "modality":"robot", "value":0.01, "unit":"m"},
                {"timestamp":"2026-01-01T00:00:00Z", "modality":"inspection", "result":"NG"}]
        args = {**meta, "format":"jsonl", "content":"\n".join(map(json.dumps, rows)), "final_inspection":"OK",
                "label_confirmed":True}
        result = collect(self.store, args)
        self.assertEqual(result["quality"]["grade"], "C")
        self.assertIn("LABEL_MISMATCH", [d["code"] for d in result["quality"]["defects"]])
        rows[-1]["result"] = "OK"
        result = collect(self.store, {**args, "content":"\n".join(map(json.dumps, rows))})
        self.assertEqual(result["quality"]["grade"], "A")
        file = self.store.path / "episodes" / result["episode_id"] / "normalized.json"
        normalized = json.loads(file.read_text())
        self.assertEqual((normalized[0]["value"], normalized[0]["timestamp"]), (10,"2026-01-01T00:00:00+00:00"))

    def test_outbox_deduplicates(self):
        event = {"EventID":"test", "kind":"TEST"}
        with patch.object(self.store, "db", side_effect=sqlite3.OperationalError("busy")):
            self.store.event(event)
        self.store.event(event)
        self.assertEqual(self.store.replay(), 1)
        self.assertEqual(self.store.list("events"), [event])
