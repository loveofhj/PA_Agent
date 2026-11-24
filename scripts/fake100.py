"""Exercise the real HTTP API against an isolated Mock server."""
import json
import os
import threading
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pa.core import Agent, Store
from pa.pipeline import REQUIRED
from pa.planner import Planner
from pa.server import Handler, ThreadingHTTPServer


def validate(base):
    def api(path, data=None, expected=200):
        request = Request(base + "/api/" + path,
                          data=None if data is None else json.dumps(data).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            body = json.load(response)
            if response.code != expected:
                raise RuntimeError(f"{path}: expected HTTP {expected}, got {response.code}: {body}")
            return body

    def post(path, data):
        return api(path, data, 201)

    plan = post("tasks", {"instruction": "LT2 FRT 도어트림 LH 100개 조립"})
    assert plan["state"] == "PREVIEW_ONLY"
    assert plan["batch_preview"] == {"quantity": 100, "lot_size": 10, "lot_count": 10, "remainder": 0}
    assert [step["StepCode"] for step in plan["steps"]] == [f"S{i}" for i in range(1, 9)]
    api(f"tasks/{plan['TaskID']}/run", {"scenario": "normal"}, 400)

    single = {}
    for scenario, state in (("normal", "DONE"), ("recover", "DONE"), ("mismatch", "HOLD"), ("ng", "HOLD")):
        task = post("tasks", {"instruction": "LT2 FRT 도어트림 LH 1개 조립"})
        result = post(f"tasks/{task['TaskID']}/run", {"scenario": scenario})
        assert result["state"] == state
        assert bool(result["recoveries"]) is (scenario in {"recover", "ng"})
        events = [event for event in api("events") if event["TaskID"] == task["TaskID"]]
        assert any(e["kind"] == "STEP_DONE" and e["StepCode"] == "S8" for e in events) == (state == "DONE")
        if scenario == "mismatch":
            assert not any(event["kind"] == "SKILL_RESULT" for event in events)
        single[scenario] = state

    totals, grades, lots = Counter(), Counter(), {}
    members, episodes = [], []
    for product in range(1, 101):
        lot_number = (product - 1) // 10 + 1
        lot = f"FAKE-LOT-{lot_number:02d}"
        recovered = product in (10, 45, 80)
        rejected = product in (84, 99)
        result = "NG" if rejected else "OK"
        episode = post("episodes", {
            "mode": "mock", "source": "Sim", "step_code": "S7", "skill_id": "SKL-011",
            "order_id": plan["OrderID"], "task_id": plan["TaskID"], "lot_id": lot,
            "product_id": f"FAKE-PRODUCT-{product:03d}", "jig_id": f"FAKE-JIG-{lot_number:02d}",
            "session_id": f"FAKE-SESSION-{lot_number:02d}", "recipe_id": "LH",
            "instruction": "FAKE 100개 생산 검증용 S7 검사",
            "final_inspection": result, "series": "recovery" if recovered else "normal",
            "recovery_history": [{"kind": "synthetic_rework", "step_code": "S6"}] if recovered else [],
        })
        assert episode["final_inspection"] == result
        assert episode["success"] is (not rejected)
        assert bool(episode["recovery_history"]) is recovered
        assert episode["quality"]["ready"] and episode["lineage"]["sha256"]
        category = "ng" if rejected else "recovered_ok" if recovered else "first_pass_ok"
        totals[category] += 1
        lots.setdefault(lot, Counter())[category] += 1
        grades[episode["quality"]["grade"]] += 1
        split = "train" if lot_number <= 7 else "validation" if lot_number <= 9 else "test"
        members.append({"episode_id": episode["episode_id"], "split": split})
        episodes.append(episode)

    assert len({m["episode_id"] for m in members}) == 100
    assert totals == {"first_pass_ok": 95, "recovered_ok": 3, "ng": 2}
    assert len(lots) == 10 and all(sum(lot.values()) == 10 for lot in lots.values())
    assert grades == {"A": 100}
    assert Counter(m["split"] for m in members) == {"train": 70, "validation": 20, "test": 10}

    leak = api("datasets", {"dataset_id": "fake100-leak", "members": [
        members[0], {**members[1], "split": "test"}]}, 400)
    assert "분할 누수" in leak["error"]

    bad_episode = post("episodes", {
        **{key: episodes[0][key] for key in REQUIRED},
        "mode": "upload", "source": "Sim", "format": "json", "label_confirmed": True,
        "final_inspection": "OK", "content": json.dumps([
            {"timestamp": "2026-01-01T00:00:00Z", "modality": "inspection", "result": "NG"}
        ]),
    })
    assert bad_episode["quality"]["grade"] == "C" and not bad_episode["quality"]["ready"]
    bad = api("datasets", {"dataset_id": "fake100-invalid", "members": [
        {"episode_id": bad_episode["episode_id"], "split": "train"}]}, 400)
    assert "AI Ready 기준 미충족" in bad["error"]

    dataset = post("datasets", {"dataset_id": "fake100", "members": members})
    assert dataset["version"] == 1 and dataset["episode_count"] == 100 and dataset["source"] == "Sim"
    found = api("datasets?dataset_id=fake100&step=S7&source=Sim&grade=A")
    assert len(found) == 1 and found[0]["members"] == dataset["members"]
    assert not api("datasets?dataset_id=fake100-leak") and not api("datasets?dataset_id=fake100-invalid")
    assert sum(event["kind"] == "DATA_QUALITY" for event in api("events")) == 101
    return {"batch_preview": plan["batch_preview"], "single_mock": single,
            "inspection": dict(totals), "lots": {key: dict(value) for key, value in lots.items()},
            "data_quality": dict(grades), "splits": dict(Counter(m["split"] for m in members)),
            "dataset": {"id": dataset["dataset_id"], "version": dataset["version"]},
            "negative_controls": ["batch execution blocked", "split leakage blocked", "grade C promotion blocked"]}


def main():
    from argparse import ArgumentParser

    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="write result JSON to this new file")
    args = parser.parse_args()
    os.environ["LLM_PROVIDER"] = "mock"
    with TemporaryDirectory(prefix="pa-fake100-") as path:
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.store = Store(path)
            server.agent = Agent(server.store, Planner())
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                report = validate(f"http://127.0.0.1:{server.server_port}")
            finally:
                server.shutdown()
                worker.join()
    output = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        with args.report.open("x", encoding="utf-8") as target:
            target.write(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
