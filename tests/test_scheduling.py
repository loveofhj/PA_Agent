from datetime import datetime
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from pa.core import CONFIG, Store
from pa.scheduling import preview


class ScheduleTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name)
        self.payload = {
            "plan_id": "WP1-100",
            "start_at": "2026-11-24T09:00:00+09:00",
            "orders": [
                {"order_id": "LH-100", "recipe_id": "LH", "quantity": 100},
                {"order_id": "RH-13", "recipe_id": "RH", "quantity": 13, "priority": 90,
                 "due_at": "2026-11-24T09:05:00+09:00"},
            ],
        }

    def test_assignment_and_due_date(self):
        schedule = preview(self.store, self.payload)
        self.assertEqual(schedule["slots"][0]["order_id"], "RH-13")
        self.assertEqual([step["step_code"] for step in schedule["routes"]], [f"S{i}" for i in range(1, 9)])
        self.assertTrue(all(step["skills"] for step in schedule["routes"]))
        rh = [slot for slot in schedule["slots"] if slot["recipe_id"] == "RH"]
        self.assertEqual([slot["quantity"] for slot in rh], [10, 3])
        self.assertTrue(all(slot["cell_id"] == "CELL-01" and slot["late"] for slot in rh))
        self.assertEqual(sum(slot["quantity"] for slot in schedule["slots"]), 113)
        self.assertTrue(any(slot["setup_sec"] == 120 for slot in schedule["slots"]))
        for cell in ("CELL-01", "CELL-02"):
            slots = [slot for slot in schedule["slots"] if slot["cell_id"] == cell]
            self.assertTrue(all(datetime.fromisoformat(a["end_at"]) <= datetime.fromisoformat(b["start_at"])
                                for a, b in zip(slots, slots[1:])))

    def test_release_and_revision(self):
        self.payload["orders"][1]["release_at"] = "2026-11-24T10:00:00+09:00"
        original = preview(self.store, self.payload)
        self.assertEqual(original["slots"][0]["order_id"], "LH-100")
        rh = [slot for slot in original["slots"] if slot["recipe_id"] == "RH"]
        self.assertGreaterEqual(rh[0]["start_at"], "2026-11-24T01:00:00+00:00")
        self.assertEqual(preview(self.store, self.payload), original)
        self.payload["orders"][0]["quantity"] = 5
        with self.assertRaisesRegex(ValueError, "새 개정"):
            preview(self.store, self.payload)
        self.payload["revision"] = 2
        self.assertEqual(preview(self.store, self.payload)["request"]["revision"], 2)

    def test_missing_skill_blocks_assignment(self):
        settings = {**CONFIG["mock_schedule"], "cells": [
            {"id": "CELL-01", "recipes": ["LH", "RH"], "skills": ["SKL-013"]}
        ]}
        with patch.dict(CONFIG, {"mock_schedule": settings}):
            schedule = preview(self.store, self.payload)
        self.assertEqual(schedule["slots"], [])
        self.assertEqual(len(schedule["unassigned"]), 2)
