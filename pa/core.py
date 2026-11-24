import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads(Path(os.getenv("PA_CONFIG", ROOT / "config.json")).read_text())
SKILLS = json.loads((Path(__file__).parent / "skills.json").read_text())
DATA = Path(os.getenv("PA_DATA_DIR", ROOT / "data"))
RULE = "PA-PROMPT-0.1"


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    def __init__(self, path=DATA):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.outbox = self.path / "outbox"
        self.outbox.mkdir(exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records
                    (kind TEXT, id TEXT, body TEXT, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS datasets
                    (id TEXT, version INTEGER, body TEXT, PRIMARY KEY(id,version));
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path / "pa.sqlite")
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, kind, key, value):
        with self.db() as db:
            db.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) "
                       "DO UPDATE SET body=excluded.body", (kind, key, dump(value)))

    def get(self, kind, key):
        with self.db() as db:
            row = db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def list(self, kind):
        with self.db() as db:
            rows = db.execute("SELECT body FROM records WHERE kind=? ORDER BY rowid DESC", (kind,))
            return [json.loads(row[0]) for row in rows]

    def event(self, value):
        try:
            with self.db() as db:
                db.execute("INSERT OR IGNORE INTO records VALUES ('events',?,?)",
                           (value["EventID"], dump(value)))
        except sqlite3.Error:
            tmp = self.outbox / (uid() + ".tmp")
            tmp.write_text(dump(value), encoding="utf-8")
            tmp.replace(self.outbox / (value["EventID"] + ".json"))

    def replay(self):
        replayed = 0
        for file in self.outbox.glob("*.json"):
            value = json.loads(file.read_text())
            with self.db() as db:
                db.execute("INSERT OR IGNORE INTO records VALUES ('events',?,?)",
                           (value["EventID"], dump(value)))
            file.unlink(missing_ok=True)
            replayed += 1
        return replayed


def check_interlock(task, snapshot, step):
    expected = {key: task[key] for key in ("JigID", "RecipeID", "ToolID")}
    checks = {key: snapshot.get(key) == value for key, value in expected.items()}
    checks.update(PLC=snapshot.get("PLC") == "READY", fresh=snapshot.get("fresh") is True,
                  safety=snapshot.get("safety_permit") is True,
                  approved=snapshot.get("approved") is True)
    if step != "S1":
        checks["clamp"] = snapshot.get("clamp") is True
    if step == "S8":
        checks["load_ready"] = snapshot.get("load_ready") is True
        checks["inspection"] = task.get("inspection") == "OK"
    state = "SAFE-STOP" if not checks["safety"] else "HOLD"
    return {"rule": "IL-00", "checks": checks, "allowed": all(checks.values()), "blocked_state": state}


CHECKS = {
    "SKL-005": "gripped", "SKL-006": "seated", "SKL-011": "inspection",
    "SKL-013": "arrived", "SKL-014": "docked_clamp", "SKL-017": "fasteners_ok",
    "SKL-018": "press_ok", "SKL-019": "coupling_ok", "SKL-023": "load_complete",
}
ERRORS = {"SKL-005": "ERR-10", "SKL-006": "ERR-10", "SKL-011": "ERR-07",
          "SKL-013": "ERR-06", "SKL-014": "ERR-06", "SKL-017": "ERR-11",
          "SKL-018": "ERR-12", "SKL-019": "ERR-12"}


class SkillExecutor(Protocol):
    def execute(self, invocation: dict, scenario: str, attempt: int) -> dict: ...


class MockExecutor:
    def execute(self, invocation, scenario, attempt):
        step, skill = invocation["StepCode"], invocation["SkillID"]
        failure = scenario in {"failure", "recover"} and step == "S3" and (
            scenario == "failure" or attempt == 0)
        if scenario == "ng" and step == "S7":
            failure = True
        if scenario == "low_confidence" and step == "S2":
            return {"status": "ABORT", "cause": "ERR-09", "signals": {}}
        if scenario == "timeout" and step == "S3":
            return {"status": "ABORT", "cause": "ERR-05", "signals": {}}
        signal = CHECKS[skill]
        value = ("NG" if failure else "OK") if signal == "inspection" else not failure
        return {"status": "FAIL" if failure else "RECOVERED" if attempt else "SUCCESS",
                "cause": ERRORS.get(skill, "ERR-02") if failure else None,
                "signals": {signal: value}}


def mock_snapshot(task, scenario):
    return {"JigID": "MISMATCH" if scenario == "mismatch" else task["JigID"],
            "RecipeID": task["RecipeID"], "ToolID": task["ToolID"], "PLC": "READY",
            "fresh": scenario != "stale", "safety_permit": scenario != "safe_stop",
            "approved": True, "clamp": True, "load_ready": True}


class Agent:
    def __init__(self, store, planner, executor=None):
        self.store = store
        self.planner = planner
        self.executor = executor or MockExecutor()

    def emit(self, task, kind, context=None, **details):
        event = {key: task.get(key) for key in
                 ("OrderID", "TaskID", "LotID", "JigID", "EpisodeID", "ModelVersion", "RuleVersion")}
        event.update(dict.fromkeys(("SubTaskID", "StepCode", "SkillID", "SkillInvocationID", "SkillVersion")))
        event.update(context or {})
        event.update(EventID=uid(), timestamp=now(), kind=kind, state=task["state"], **details)
        self.store.event(event)
        return event["EventID"]

    def state(self, task, state, context=None, **details):
        task["state"] = state
        self.store.put("tasks", task["TaskID"], task)
        return self.emit(task, "STATE", context, **details)

    def plan(self, payload):
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
            raise ValueError("작업지시는 1~2000자여야 합니다.")
        task = {key: payload.get(key, uid()) for key in ("OrderID", "LotID", "ProductID")}
        task.update(JigID=payload.get("JigID", "JIG-DEMO"), ToolID=payload.get("ToolID", "TOOL-DEMO"))
        for value in task.values():
            if not isinstance(value, str) or not re.fullmatch(r"[\w.-]{1,128}", value):
                raise ValueError("작업 ID 형식 오류")
        task.update(TaskID=uid(), EpisodeID=uid(), RuleVersion=RULE, ModelVersion=self.planner.version,
                    instruction=instruction, current_step=None, recoveries=[], inspection=None)
        self.state(task, "IDLE")
        event_id = self.state(task, "PLANNING")
        try:
            decision = self.planner.plan(task, event_id)
        except (ValueError, TimeoutError, OSError) as error:
            task["error"] = str(error)
            self.state(task, "FAILED" if isinstance(error, (TimeoutError, OSError)) else "HOLD",
                       cause="LLM_TIMEOUT" if isinstance(error, TimeoutError) else "LLM_PLAN_ERROR",
                       related_event=event_id, message=str(error))
            return task
        task["decision"] = decision
        if decision["decision_type"] != "plan":
            self.state(task, "HOLD", cause="NEEDS_CONFIRMATION", related_event=event_id)
            return task
        task["RecipeID"] = decision["recipe"]
        task["steps"] = [{"StepCode": code, "name": name, "SubTaskID": uid(), "skills": [
            {"SkillID": sid, "SkillVersion": SKILLS[sid]["version"]} for sid in ids]}
            for code, name, ids in CONFIG["steps"]]
        quantity = decision["quantity"]
        if quantity > 1:
            lot_size = CONFIG.get("preview_lot_size", 10)
            full, remainder = divmod(quantity, lot_size)
            task["batch_preview"] = {"quantity": quantity, "lot_size": lot_size,
                                     "lot_count": full + bool(remainder), "remainder": remainder}
        state = "PREVIEW_ONLY" if quantity > 1 else "READY"
        self.state(task, state, decision=decision, related_event=event_id)
        return task

    def run(self, task_id, scenario):
        if scenario not in {"normal","recover","failure","mismatch","safe_stop","stale",
                            "ng","low_confidence","timeout"}:
            raise ValueError("알 수 없는 Mock 시나리오")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM records WHERE kind='tasks' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = json.loads(row[0])
            if task["state"] == "PREVIEW_ONLY":
                raise ValueError("배치 계획은 미리보기 전용입니다. Mock 실행은 1개 작업을 생성하세요.")
            if task["state"] != "READY":
                raise ValueError("READY 작업만 실행 가능합니다.")
            task["state"] = "VALIDATING"
            db.execute("UPDATE records SET body=? WHERE kind='tasks' AND id=?", (dump(task), task_id))
        evidence = self.store.path / "evidence" / task["EpisodeID"]
        evidence.mkdir(parents=True, exist_ok=True)
        for step in task["steps"]:
            code = task["current_step"] = step["StepCode"]
            for skill in step["skills"]:
                context = {**skill, "StepCode": code, "SubTaskID": step["SubTaskID"]}
                attempt = 0
                while True:
                    self.state(task, "VALIDATING", context)
                    interlock = check_interlock(task, mock_snapshot(task, scenario), code)
                    event_id = self.emit(task, "INTERLOCK", context, result=interlock)
                    if not interlock["allowed"]:
                        cause = "ERR-13" if not interlock["checks"]["safety"] else (
                            "ERR-08" if not interlock["checks"]["fresh"] else "ERR-04")
                        self.state(task, interlock["blocked_state"], context, cause=cause,
                                   related_event=event_id, retry=attempt, EvidenceURI=None)
                        return task
                    invocation = {**context, "SkillInvocationID": uid()}
                    request = {**invocation, **{key: task[key] for key in (
                        "TaskID", "EpisodeID", "JigID", "RecipeID", "ToolID", "ProductID")}}
                    called = self.state(task, "RUNNING", invocation)
                    result = self.executor.execute(request, scenario, attempt)
                    expected = "OK" if skill["SkillID"] == "SKL-011" else True
                    valid = (result["status"] in {"SUCCESS", "RECOVERED"}
                             and result["signals"].get(CHECKS[skill["SkillID"]]) == expected)
                    cause = result["cause"] or (None if valid else "ERR-02")
                    file = evidence / (invocation["SkillInvocationID"] + ".json")
                    file.write_text(dump(result))
                    outcome = self.emit(task, "SKILL_RESULT", invocation,
                                        result=result, verified=valid, retry=attempt, cause=cause,
                                        EvidenceURI=file.resolve().as_uri(), related_event=called)
                    if valid:
                        if code == "S7":
                            task["inspection"] = "OK"
                        break
                    if code == "S7":
                        task["inspection"] = "NG"
                    limit = CONFIG["mock_rework_limit"] if cause in {"ERR-07", "ERR-11"} else CONFIG["mock_retry_limit"]
                    detail = dict(cause=cause, related_event=outcome, retry=attempt,
                                  EvidenceURI=file.resolve().as_uri())
                    if result["status"] == "ABORT" or attempt >= limit:
                        self.state(task, "HOLD", invocation, **detail)
                        return task
                    self.state(task, "RECOVERING", invocation, **detail)
                    task["recoveries"].append({"step": code, **detail})
                    attempt += 1
            self.emit(task, "STEP_DONE", {"StepCode": code, "SubTaskID": step["SubTaskID"]})
        self.state(task, "DONE")
        return task
