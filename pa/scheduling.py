"""Deterministic, finite-capacity WP1 scheduling preview for configured Mock cells."""
import json
import re
from datetime import timedelta

from .core import CONFIG, SKILLS, dump, now
from .pipeline import timestamp

ID = re.compile(r"[\w.-]{1,80}")


def identifier(value, label):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(f"{label} 형식 오류")
    return value


def integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label}은 {minimum}~{maximum} 정수여야 합니다.")
    return value


def date(value, label):
    try:
        result = timestamp(value)
        if not 2000 <= result.year <= 2100:
            raise ValueError
        return result
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{label}에 타임존이 포함된 ISO 8601 일시를 입력하세요.") from error


def preview(store, payload):
    plan_id = identifier(payload.get("plan_id"), "WP1 계획 ID")
    revision = integer(payload.get("revision", 1), 1, 1000000, "개정 번호")
    horizon = date(payload.get("start_at"), "계획 시작")
    items = payload.get("orders")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("WP1 작업을 1~100건 입력하세요.")
    orders = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("WP1 작업은 JSON 객체여야 합니다.")
        order_id = identifier(item.get("order_id"), "WP1 작업 ID")
        recipe = item.get("recipe_id")
        if recipe not in CONFIG["approved_recipes"]:
            raise ValueError(f"{order_id}: 미승인 레시피")
        orders.append({
            "order_id": order_id, "recipe_id": recipe,
            "quantity": integer(item.get("quantity"), 1, 10000, "수량"),
            "priority": integer(item.get("priority", 50), 0, 100, "우선순위"),
            "release_at": date(item.get("release_at", horizon.isoformat()), "착수 가능").isoformat(),
            "due_at": None if item.get("due_at") is None else date(item["due_at"], "납기").isoformat(),
        })
    if len({item["order_id"] for item in orders}) != len(orders):
        raise ValueError("WP1 작업 ID가 중복됩니다.")
    if sum(item["quantity"] for item in orders) > 10000:
        raise ValueError("계획당 최대 시연 수량은 10000개입니다.")
    request = {"plan_id": plan_id, "revision": revision, "start_at": horizon.isoformat(), "orders": orders}
    key = f"{plan_id}:{revision}"
    try:
        previous = store.get("schedules", key)
    except KeyError:
        pass
    else:
        if previous["request"] != request:
            raise ValueError("이미 저장된 개정입니다. 변경하려면 새 개정 번호를 사용하세요.")
        return previous

    lot_size = CONFIG["preview_lot_size"]
    settings = CONFIG["mock_schedule"]
    routes = [{"step_code": code, "name": name,
               "skills": [{"id": sid, "name": SKILLS[sid]["name"], "version": SKILLS[sid]["version"]}
                          for sid in skills]}
              for code, name, skills in CONFIG["steps"]]
    needed = {skill["id"] for step in routes for skill in step["skills"]}
    cells = [{**cell, "available": horizon, "recipe": None} for cell in settings["cells"]]
    pending = []
    unassigned = []
    for order in orders:
        eligible = [cell for cell in cells if order["recipe_id"] in cell["recipes"]
                    and needed.issubset(cell["skills"])]
        if not eligible:
            unassigned.append({**order, "reason": "레시피와 전체 공정 Skill을 지원하는 셀이 없습니다."})
            continue
        for number, offset in enumerate(range(0, order["quantity"], lot_size), 1):
            pending.append({"order": order, "lot_number": number,
                            "quantity": min(lot_size, order["quantity"] - offset), "cells": eligible,
                            "release": date(order["release_at"], "착수 가능")})

    def earliest(lot):
        return max(lot["release"], min(cell["available"] for cell in lot["cells"]))

    slots = []
    while pending:

        lot = min(pending, key=lambda item: (
            earliest(item), -item["order"]["priority"], item["order"]["due_at"] or "9999",
            item["order"]["order_id"], item["lot_number"]))
        pending.remove(lot)
        start = earliest(lot)
        order = lot["order"]
        recipe = order["recipe_id"]
        def setup(cell):
            return settings["recipe_change_sec"] if cell["recipe"] not in (None, recipe) else 0

        cell = min((cell for cell in lot["cells"] if cell["available"] <= start),
                   key=lambda cell: (setup(cell), len(cell["recipes"]), cell["id"]))
        finish = start + timedelta(seconds=setup(cell) + settings["cycle_sec_per_unit"] * lot["quantity"])
        slots.append({
            "cell_id": cell["id"], "order_id": order["order_id"], "lot_number": lot["lot_number"],
            "quantity": lot["quantity"], "recipe_id": recipe, "start_at": start.isoformat(),
            "end_at": finish.isoformat(), "setup_sec": setup(cell), "due_at": order["due_at"],
            "late": bool(order["due_at"] and finish > date(order["due_at"], "납기")),
        })
        cell.update(available=finish, recipe=recipe)

    schedule = {"request": request, "created_at": now(), "mode": "MOCK_PREVIEW", "slots": slots,
                "routes": routes, "unassigned": unassigned,
                "settings": {"lot_size": lot_size, "recipe_change_sec": settings["recipe_change_sec"],
                             "cycle_sec_per_unit": settings["cycle_sec_per_unit"]}}
    with store.db() as db:
        db.execute("INSERT OR IGNORE INTO records VALUES ('schedules',?,?)", (key, dump(schedule)))
        existing = json.loads(db.execute("SELECT body FROM records WHERE kind='schedules' AND id=?",
                                         (key,)).fetchone()[0])
        if existing["request"] != request:
            raise ValueError("이미 저장된 개정입니다. 변경하려면 새 개정 번호를 사용하세요.")
    return existing
