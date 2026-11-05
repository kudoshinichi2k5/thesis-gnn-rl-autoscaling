"""Offline regression tests for digital-twin calibration; no live cluster/network."""
import copy
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("profiling_runner", HERE / "run-profiling.py")
profiling = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profiling)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.main = profiling.load_plan(HERE / "profiling-plan.json", "main")

    def test_main_is_one_calibration_run_per_workload_cell(self):
        cells = profiling.plan_cells(self.main)
        self.assertEqual(self.main["protocol"], profiling.TWIN_PROTOCOL)
        self.assertEqual(len(cells), 4 * 3 * 3)
        self.assertEqual({cell["arm"] for cell in cells}, {"calibrate"})
        self.assertEqual(len({cell["cell_id"] for cell in cells}), len(cells))
        self.assertEqual({cell["split"] for cell in cells}, {"train", "validation", "test"})
        baseline = self.main["configurations"][0]
        train_pairs = set()
        for cell in (c for c in cells if c["split"] == "train"):
            for event in cell["schedule"]:
                changed = tuple(sorted(s for s, value in event["replicas"].items()
                                       if value != baseline[s]))
                if len(changed) == 2:
                    train_pairs.add(changed)
        self.assertGreater(len(train_pairs), 1)

    def test_schedule_sweeps_each_service_and_returns_to_baseline(self):
        events = profiling.schedule_for(self.main, 42, 0, "calibrate")
        baseline = self.main["configurations"][0]
        self.assertEqual(len(events), 17)
        self.assertEqual(events[-1]["replicas"], baseline)
        for service in self.main["controlled_services"]:
            single_service = [event for event in events if all(
                replicas == (event["replicas"][service] if name == service else baseline[name])
                for name, replicas in event["replicas"].items()
            )]
            self.assertTrue(any(event["replicas"][service] == 2 for event in single_service))
            self.assertTrue(any(event["replicas"][service] == 3 for event in single_service))
        self.assertTrue(any(sum(event["replicas"][s] != baseline[s] for s in baseline) == 2
                            for event in events))
        self.assertEqual(events, profiling.schedule_for(self.main, 42, 0, "calibrate"))
        self.assertNotEqual(events, profiling.schedule_for(self.main, 43, 0, "calibrate"))

    def test_smoke_is_short_and_checks_measured_slow_service(self):
        smoke = profiling.load_plan(HERE / "profiling-plan.json", "smoke")
        cells = profiling.plan_cells(smoke)
        self.assertEqual(len(cells), 3)
        self.assertEqual(smoke["duration_sec"], 600)
        self.assertEqual(smoke["controlled_services"], ["recommendationservice"])
        self.assertEqual([event["at_sec"] for event in cells[0]["schedule"]], [180, 300, 420])

    def test_invalid_domains_fail_closed(self):
        mutations = {
            "sampling": ("sampling_percentage", 100),
            "duration": ("duration_sec", 600),
            "pod_budget": ("max_total_app_pods", 10),
            "clock": ("capacity_poll_sec", 60),
            "levels": ("replica_levels", [1, 3]),
        }
        for name, (key, value) in mutations.items():
            plan = copy.deepcopy(self.main)
            plan[key] = value
            if name == "levels":
                plan["replica_limits"] = {s: 3 for s in plan["controlled_services"]}
            with self.subTest(name=name), self.assertRaises(ValueError):
                profiling.plan_cells(plan)
        overlap = copy.deepcopy(self.main)
        overlap["seeds_by_split"]["test"] = overlap["seeds_by_split"]["train"]
        with self.assertRaises(ValueError):
            profiling.plan_cells(overlap)

    def test_profile_inheritance_is_independent(self):
        smoke = profiling.load_plan(HERE / "profiling-plan.json", "smoke")
        smoke["configurations"][0]["frontend"] = 99
        self.assertEqual(self.main["configurations"][0]["frontend"], 1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plan.json"
            path.write_text(json.dumps({"profiles": {"main": {"extends": "smoke"},
                                                       "smoke": {"extends": "main"}}}))
            with self.assertRaisesRegex(ValueError, "cycle"):
                profiling.load_plan(path)


class CliTests(unittest.TestCase):
    def test_preview_is_read_only_and_defaults_are_separate(self):
        main_args = profiling.parse_args(["--profile", "main"])
        smoke_args = profiling.parse_args(["--profile", "smoke"])
        self.assertNotEqual(main_args.output, smoke_args.output)
        self.assertEqual(main_args.max_new, 1)
        with patch.object(profiling, "command", side_effect=AssertionError("cluster call")), \
             patch.object(profiling.subprocess, "run", side_effect=AssertionError("process call")), \
             redirect_stdout(io.StringIO()):
            profiling.run(smoke_args)

    def test_filters_do_not_mutate_the_study(self):
        args = profiling.parse_args(["--scenario", "mmpp", "--split", "train",
                                     "--load-multiplier", "0.5", "--repeat", "0"])
        with patch.object(profiling, "command", side_effect=AssertionError("cluster call")), \
             redirect_stdout(io.StringIO()):
            profiling.run(args)
        export_args = profiling.parse_args(["--scenario", "mmpp", "--export", "manifest.json"])
        with self.assertRaisesRegex(ValueError, "filters cannot be used with --export"):
            profiling.run(export_args)

    def test_confirm_and_export_are_mutually_exclusive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            profiling.parse_args(["--confirm", "--export", "manifest.json"])


class CollectorTests(unittest.TestCase):
    def test_jaeger_empty_response_differs_from_failed_query(self):
        fake = types.SimpleNamespace()
        with patch.dict(sys.modules, {"requests": fake}):
            spec = importlib.util.spec_from_file_location("collector_test", HERE / "collect_metrics.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        fake.get = lambda *a, **k: types.SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"data": []})
        report = {"jaeger_truncated_windows": 0, "jaeger_failed_queries": []}
        self.assertEqual(module.fetch_traces("http://fixture", "frontend", 0, 100, 100, report), [])
        self.assertEqual(report["jaeger_failed_queries"], [])
        fake.get = lambda *a, **k: types.SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"errors": ["timeout"]})
        module.fetch_traces("http://fixture", "frontend", 0, 100, 100, report)
        self.assertEqual(len(report["jaeger_failed_queries"]), 1)


if __name__ == "__main__":
    unittest.main()
