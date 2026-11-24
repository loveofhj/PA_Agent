import base64
import binascii
import csv
import hashlib
import io
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .core import CONFIG, RULE, SKILLS, dump, now, uid

IDS = ("order_id", "task_id", "lot_id", "jig_id", "product_id", "session_id")
VERSIONS = ("skill_version", "model_version", "rule_version", "calibration_version")
REQUIRED = (*IDS, *VERSIONS, "recipe_id", "tool_id", "instruction")
MODALITIES = {"video", "robot", "force", "equipment", "amr", "context", "event",
              "teleop", "simulation", "inspection"}
UNITS = {"m": ("mm", 1000), "cm": ("mm", 10), "mm": ("mm", 1), "N": ("N", 1),
         "N*m": ("N*m", 1), "N·m": ("N*m", 1), "Nmm": ("N*m", 0.001),
         "deg": ("rad", math.pi / 180), "rad": ("rad", 1)}
STEPS = {code: skills for code, _, skills in CONFIG["steps"]}


class Collector(Protocol):
    def read(self, payload: dict, episode: dict) -> tuple[list, bytes, str]: ...


def mock_data(payload, episode):
    del payload
    stamp = now()
    return [
        {"timestamp": stamp, "modality": "robot", "value": 10, "unit": "mm",
         "frame": "robot_base", "step_code": episode["step_code"]},
        {"timestamp": stamp, "modality": "inspection", "result": episode["final_inspection"],
         "step_code": episode["step_code"]},
    ]


class MockCollector:
    def read(self, payload, episode):
        records = mock_data(payload, episode)
        return records, dump(records).encode(), ".json"


class FileCollector:
    def read(self, payload, episode):
        del episode
        fmt = payload.get("format")
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("수집 파일 내용이 비어 있습니다.")
        if fmt == "json":
            records = json.loads(content)
        elif fmt == "jsonl":
            records = [json.loads(line) for line in content.splitlines() if line.strip()]
        elif fmt == "csv":
            records = [dict(row) for row in csv.DictReader(io.StringIO(content))]
        else:
            raise ValueError("지원 형식은 CSV, JSON 배열, JSONL입니다.")
        if not isinstance(records, list) or not 0 < len(records) <= 10000:
            raise ValueError("레코드 수는 1~10000이어야 합니다.")
        return records, content.encode(), "." + fmt


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("missing timezone")
    return stamp.astimezone(timezone.utc)


def analyze(records, episode):
    defects, clean, seen, frames = [], [], set(), defaultdict(list)

    def flag(code, row=None, severity="C"):
        defects.append({"code": code, "row": row, "severity": severity})

    for index, item in enumerate(records, 1):
        if not isinstance(item, dict):
            flag("INVALID_RECORD", index)
            continue
        row = dict(item)
        try:
            stamp = timestamp(row.get("timestamp"))
        except ValueError:
            flag("INVALID_TIMESTAMP", index)
            continue
        modal = row.get("modality")
        if not isinstance(modal, str) or modal not in MODALITIES:
            flag("UNKNOWN_MODALITY", index)
            continue
        row["timestamp"] = stamp.isoformat()
        step = row.get("step_code") or episode["step_code"]
        step_id = episode["steps"].get(step) if isinstance(step, str) else None
        if not step_id:
            flag("STEP_MISMATCH", index)
            continue
        expected = {k: episode[k] for k in (*IDS, *VERSIONS, "episode_id", "recipe_id", "tool_id")}
        expected.update(step_id=step_id, step_code=step)
        skill = row.get("skill_id") or episode["skill_id"]
        if not isinstance(skill, str) or skill not in STEPS[step]:
            flag("SKILL_MISMATCH", index)
            continue
        expected["skill_id"] = skill
        if any(row.get(k) not in (None, "", v) for k, v in expected.items()):
            flag("ID_MISMATCH", index)
            continue
        row.update(expected)
        event_id = row.get("EventID") or row.get("event_id")
        key = ("id", str(event_id)) if event_id else ("record", modal, str(row.get("stream_id", "")), step,
                                                       row["timestamp"])
        if key in seen:
            flag("DUPLICATE", index, "B")
            continue
        seen.add(key)
        try:
            received = row.get("received_at")
            late = received and CONFIG["stale_sec"] is not None and (
                timestamp(received) - stamp).total_seconds() > CONFIG["stale_sec"]
            if row.get("status") == "STALE" or late:
                flag("STALE", index)
                row["status"] = "STALE"
        except ValueError:
            flag("INVALID_RECEIVED_AT", index)
        if row.get("value") not in (None, ""):
            try:
                value = float(row["value"])
                if not math.isfinite(value):
                    raise ValueError
                unit = row.get("unit")
                if unit:
                    standard, scale = UNITS[unit]
                    value *= scale
                    row["unit"] = standard
                if not math.isfinite(value):
                    raise ValueError
                row["value"] = value
                limits = CONFIG["outlier_ranges"].get(str(row.get("channel", modal)))
                if limits and not limits[0] <= value <= limits[1]:
                    flag("OUTLIER", index)
            except (ValueError, TypeError, KeyError):
                flag("INVALID_VALUE_OR_UNIT", index)
                row.pop("value", None)
        elif modal in {"robot", "force"} and not any(row.get(k) for k in ("joints", "tcp", "samples")):
            flag("MISSING_MEASUREMENT", index)
        if row.get("xyz") is not None:
            try:
                _, scale = UNITS[row.get("coordinate_unit", "mm")]
                if row.get("coordinate_unit", "mm") not in {"mm", "cm", "m"}:
                    raise ValueError
                xyz = [float(v) * scale for v in row["xyz"]]
                assert len(xyz) == 3 and all(math.isfinite(v) for v in xyz)
                if row.get("frame") != "robot_base":
                    transform = CONFIG["calibrations_mm"][episode["calibration_version"]][row["frame"]]
                    xyz = [sum(transform[i][j] * xyz[j] for j in range(3)) + transform[i][3] for i in range(3)]
                row.update(xyz=xyz, frame="robot_base", coordinate_unit="mm")
            except (ValueError, TypeError, KeyError, AssertionError, IndexError):
                flag("INVALID_COORDINATE_OR_CALIBRATION", index)
        if modal == "video" and row.get("frame_index") not in (None, ""):
            try:
                frame = int(row["frame_index"])
                if frame < 0 or str(frame) != str(row["frame_index"]):
                    raise ValueError
                frames[(step, str(row.get("stream_id", "")))].append(frame)
            except (ValueError, TypeError):
                flag("INVALID_FRAME_INDEX", index)
        clean.append(row)

    clean.sort(key=lambda row: row["timestamp"])
    by_step = defaultdict(list)
    for row in clean:
        by_step[row["step_code"]].append(row)
    for step in episode["steps"]:
        rows = by_step[step]
        present = {row["modality"] for row in rows if row.get("status") != "STALE"}
        for modal in CONFIG["required_modalities"]:
            if modal == "inspection" and step != episode["step_code"]:
                continue
            if modal not in present:
                flag(f"MISSING:{step}:{modal}")
        for modal in CONFIG["optional_modalities"]:
            if modal not in present:
                flag(f"MISSING_OPTIONAL:{step}:{modal}", severity="B")
        if CONFIG["sync_ms"] is not None:
            from bisect import bisect_left
            robot = [timestamp(r["timestamp"]) for r in rows if r["modality"] == "robot"]
            for row in rows:
                if row["modality"] == "video" and robot:
                    stamp = timestamp(row["timestamp"])
                    i = bisect_left(robot, stamp)
                    if min(abs((candidate - stamp).total_seconds()) for candidate in
                           robot[max(0, i - 1):i + 1]) * 1000 > CONFIG["sync_ms"]:
                        flag(f"SYNC_OFFSET:{step}")
    threshold = CONFIG["frame_gap_percent"]
    for indices in frames.values():
        span = max(indices) - min(indices) + 1
        missing = (span - len(set(indices))) * 100 / span
        if missing and (threshold is None or missing > threshold):
            flag("FRAME_GAP", severity="B" if threshold is None else "C")
    inspections = [r.get("result") for r in clean if r["modality"] == "inspection"]
    if not inspections or inspections[-1] != episode["final_inspection"]:
        flag("LABEL_MISMATCH")
    if not episode["label_confirmed"]:
        flag("LABEL_UNCONFIRMED", severity="B")
    triggers = []
    before, after = CONFIG["trigger_before_sec"], CONFIG["trigger_after_sec"]
    for row in clean:
        if row.get("kind") in ("HOLD", "SAFE-STOP", "LOW_CONFIDENCE", "NG") or row.get("result") == "NG":
            center = timestamp(row["timestamp"])
            trigger = {"timestamp": row["timestamp"], "window": "TBD"}
            if before is not None and after is not None:
                trigger = {"timestamp": row["timestamp"],
                           "start": (center - timedelta(seconds=before)).isoformat(),
                           "end": (center + timedelta(seconds=after)).isoformat()}
            triggers.append(trigger)
    grade = "C" if any(d["severity"] == "C" for d in defects) else "B" if defects else "A"
    return clean, {"grade": grade, "defects": defects, "triggers": triggers,
                   "step_counts": {k: len(v) for k, v in by_step.items()},
                   "ready": grade in CONFIG["accepted_grades"] and grade != "C" and episode["label_confirmed"],
                   "thresholds_TBD": [k for k in ("stale_sec", "sync_ms", "frame_gap_percent") if CONFIG[k] is None]}


def collect(store, payload):
    mode = payload.get("mode", "upload")
    if mode not in {"mock", "upload"}:
        raise ValueError("지원되는 수집 모드는 mock, upload입니다.")
    eid = uid()
    mock = mode == "mock"
    defaults = {key: f"DEMO-{key}-{eid[:8]}" for key in REQUIRED} if mock else {}
    if mock:
        defaults.update(recipe_id="LH", instruction="LT2 FRT 도어트림 LH 1개 조립",
                        skill_version="mock-0.1", model_version="mock-0.1",
                        rule_version=RULE, calibration_version="mock-base-0.1")
    episode = {key: payload.get(key, defaults.get(key)) for key in (*REQUIRED, "robot_id", "asset_id")}
    if any(not isinstance(episode[key], str) or not episode[key].strip() for key in REQUIRED):
        raise ValueError("필수 ID, 버전 및 작업 지시를 입력하세요.")
    if any(not re.fullmatch(r"[\w.-]{1,128}", episode[key]) for key in IDS + VERSIONS + ("tool_id",)):
        raise ValueError("ID 또는 버전 형식 오류")
    if episode["recipe_id"] not in CONFIG["approved_recipes"]:
        raise ValueError("미승인 레시피")
    source = payload.get("source", "Sim" if mock else "Real")
    if source not in ["Real", "Sim"] or (mock and source != "Sim"):
        raise ValueError("파일은 Real/Sim, Mock은 Sim으로 수집합니다.")
    step = payload.get("step_code", "S7")
    skill = payload.get("skill_id", "SKL-011")
    steps = payload.get("steps") or {step: payload.get("step_id") or uid()}
    if not isinstance(steps, dict) or not steps or any(
        k not in STEPS or not isinstance(v, str) or not re.fullmatch(r"[\w.-]{1,128}", v) for k, v in steps.items()
    ) or len(set(steps.values())) != len(steps) or step not in steps or skill not in STEPS[step]:
        raise ValueError("STEP/Skill 매핑 또는 step_id 형식 오류")
    label = payload.get("final_inspection", "OK" if mock else None)
    series = payload.get("series", "normal")
    recovery = payload.get("recovery_history", [])
    if label not in {"OK", "NG"} or series not in {"normal", "variation", "recovery"}:
        raise ValueError("결과는 OK/NG, 계열은 normal/variation/recovery")
    if not isinstance(recovery, list) or (series == "recovery" and not recovery):
        raise ValueError("복구 계열에는 복구 이력을 입력하세요.")
    episode.update(episode_id=eid, step_code=step, step_id=steps[step], skill_id=skill, steps=steps,
                   timestamp=now(), source=source, series=series, recovery_history=recovery,
                   final_inspection=label, success=label == "OK",
                   label_confirmed=payload.get("label_confirmed", mock) is True)
    collector = MockCollector() if mock else FileCollector()
    rows, raw, suffix = collector.read(payload, episode)
    clean, quality = analyze(rows, episode)
    normalized = dump(clean)
    video = payload.get("video")
    footage = None
    if video is not None:
        if not isinstance(video, str):
            raise ValueError("영상은 Base64 문자열이어야 합니다.")
        try:
            footage = base64.b64decode(video, validate=True)
        except binascii.Error as error:
            raise ValueError("영상 Base64 형식 오류") from error
        if len(footage) < 12 or footage[4:8] != b"ftyp":
            raise ValueError("MP4 원본만 지원합니다.")
    folder = store.path / "episodes" / eid
    folder.mkdir(parents=True)
    origin = folder / ("raw" + suffix)
    origin.write_bytes(raw)
    processed = folder / "normalized.json"
    processed.write_text(normalized, encoding="utf-8")
    lineage = {"raw": origin.resolve().as_uri(), "sha256": hashlib.sha256(raw).hexdigest(),
               "normalized": processed.resolve().as_uri(), "pipeline_version": "pa-data-0.1"}
    if footage is not None:
        target = folder / "original.mp4"
        target.write_bytes(footage)
        lineage.update(video=target.resolve().as_uri(), video_sha256=hashlib.sha256(footage).hexdigest())
    episode.update(quality=quality, lineage=lineage)
    store.put("episodes", eid, episode)
    store.event({"EventID": uid(), "TaskID": episode["task_id"], "EpisodeID": eid,
                 "SubTaskID": steps[step], "StepCode": step, "SkillID": skill, "SkillInvocationID": None,
                 "SkillVersion": episode["skill_version"], "ModelVersion": episode["model_version"],
                 "RuleVersion": episode["rule_version"], "timestamp": now(), "kind": "DATA_QUALITY",
                 "result": quality, "EvidenceURI": lineage["normalized"]})
    return episode


def promote(store, payload):
    dataset_id = payload.get("dataset_id")
    members = payload.get("members")
    if not isinstance(dataset_id, str) or not re.fullmatch(r"[\w.-]{1,80}", dataset_id):
        raise ValueError("데이터셋 ID 형식 오류")
    if not isinstance(members, list) or not members or len(members) > 1000:
        raise ValueError("데이터셋에는 1~1000개 에피소드가 필요합니다.")
    assignments, seen, entries = {}, set(), []
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("잘못된 멤버")
        eid, split = member.get("episode_id"), member.get("split")
        if not isinstance(eid, str) or eid in seen or split not in ("train", "validation", "test"):
            raise ValueError("중복 EpisodeID 또는 분할 오류")
        seen.add(eid)
        episode = store.get("episodes", eid)
        if not episode["quality"]["ready"] or not episode["lineage"].get("sha256"):
            raise ValueError(f"AI Ready 기준 미충족: {eid}")
        for key in ("product_id", "jig_id", "session_id"):
            group = (key, episode[key])
            if assignments.setdefault(group, split) != split:
                raise ValueError(f"분할 누수: {key}={episode[key]}")
        entries.append({**episode, "split": split})

    sources = sorted({e["source"] for e in entries})
    record = {"dataset_id": dataset_id, "created_at": now(), "members": entries,
              "source": sources[0] if len(sources) == 1 else "Co-training", "episode_count": len(entries),
              "steps": sorted({step for e in entries for step in e["steps"]}),
              "skills": sorted({e["skill_id"] for e in entries}),
              "quality": {g: sum(e["quality"]["grade"] == g for e in entries) for g in "ABC"},
              "security": {key: payload.get(key, default) for key, default in
                           {"owner": "local", "access": "local", "classification": "internal",
                            "retention": "TBD"}.items()}}
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM datasets WHERE id=?",
                             (dataset_id,)).fetchone()[0]
        record["version"] = version
        db.execute("INSERT INTO datasets VALUES (?,?,?)", (dataset_id, version, dump(record)))
    store.event({"EventID": uid(), "kind": "DATASET_PROMOTED", "timestamp": now(),
                 "RuleVersion": RULE, "dataset_id": dataset_id, "version": version,
                 "episodes": [e["episode_id"] for e in entries],
                 **dict.fromkeys(("TaskID", "EpisodeID", "SubTaskID", "StepCode", "SkillID",
                                  "SkillInvocationID", "SkillVersion", "ModelVersion"))})
    return record


def catalog(store, filters):
    with store.db() as db:
        records = [json.loads(row[0]) for row in db.execute("SELECT body FROM datasets ORDER BY rowid DESC")]
    for key, field in (("step", "steps"), ("skill", "skills"), ("source", "source"),
                       ("dataset_id", "dataset_id"), ("version", "version"), ("grade", "quality")):
        if key in filters:
            val = filters[key]
            if key in ("step", "skill"):
                records = [r for r in records if val in r[field]]
            elif key == "grade":
                records = [r for r in records if r[field].get(val, 0)]
            else:
                records = [r for r in records if val == str(r[field])]
    return records
