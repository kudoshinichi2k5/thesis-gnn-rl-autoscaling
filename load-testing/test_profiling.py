"""Offline regression tests for digital-twin calibration; no live cluster/network."""
import copy
import ast
import csv
import importlib.util
import io
import json
import sys
import tempfile
import types
import time
from urllib.parse import urlsplit
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("profiling_runner", HERE / "run-profiling.py")
profiling = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profiling)
ARRIVAL_SPEC = importlib.util.spec_from_file_location("arrival_models_test", HERE / "arrival_models.py")
arrival = importlib.util.module_from_spec(ARRIVAL_SPEC)
ARRIVAL_SPEC.loader.exec_module(arrival)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.main = profiling.load_plan(HERE / "profiling-plan.json", "main")

    def test_main_is_one_calibration_run_per_workload_cell(self):
        cells = profiling.plan_cells(self.main)
        self.assertEqual(self.main["protocol"], profiling.TWIN_PROTOCOL)
        self.assertEqual(len(cells), 27)
        self.assertEqual({cell["arm"] for cell in cells}, {"calibrate"})
        self.assertEqual(len({cell["cell_id"] for cell in cells}), len(cells))
        self.assertEqual({cell["split"] for cell in cells}, {"train", "validation", "test"})
        self.assertEqual(
            {split: sum(c["split"] == split for c in cells)
             for split in ("train", "validation", "test")},
            {"train": 11, "validation": 8, "test": 8},
        )
        focus_counts = {}
        for cell in (c for c in cells if c["split"] == "train"):
            baseline = cell["replicas"]
            focus_counts[cell["focus_service"]] = focus_counts.get(cell["focus_service"], 0) + 1
            for event in cell["schedule"]:
                changed = tuple(sorted(s for s, value in event["replicas"].items()
                                       if value != baseline[s]))
                self.assertLessEqual(len(changed), 1)
                if changed:
                    self.assertEqual(changed, (cell["focus_service"],))
        self.assertEqual(focus_counts, {
            'frontend': 4, 'currencyservice': 4, 'cartservice': 1,
            'productcatalogservice': 1, 'recommendationservice': 1,
        })

        for cell in (c for c in cells if c["split"] != "train"):
            self.assertEqual(cell["transition_design"], "joint_constrained_random_walk")
            self.assertIsNone(cell["focus_service"])
            self.assertTrue(any(
                sum(value != cell["replicas"][service]
                    for service, value in event["replicas"].items()) >= 2
                for event in cell["schedule"][:-1]
            ))
            self.assertTrue(all(sum(event["replicas"].values()) <= self.main["max_total_app_pods"]
                                for event in cell["schedule"]))

    def test_schedule_isolates_focus_service_and_returns_to_baseline(self):
        focus = "cartservice"
        events = profiling.schedule_for(self.main, 42, 0, "calibrate", focus_service=focus)
        baseline = profiling.baseline_for_focus(self.main, focus)
        self.assertEqual(len(events), 3)
        self.assertEqual([event["at_sec"] for event in events], [300, 810, 1320])
        self.assertEqual(events[-1]["replicas"], baseline)
        self.assertEqual(events[0]["replicas"][focus], 2)
        self.assertEqual(events[1]["replicas"][focus], 3)
        for event in events:
            self.assertTrue(all(name == focus or value == baseline[name]
                                for name, value in event["replicas"].items()))
        self.assertEqual(events, profiling.schedule_for(
            self.main, 42, 0, "calibrate", focus_service=focus))

    def test_focus_baseline_guards_every_other_controlled_service(self):
        for focus in self.main['controlled_services']:
            baseline = profiling.baseline_for_focus(self.main, focus)
            self.assertEqual(baseline[focus], 1)
            for service in self.main['controlled_services']:
                if service != focus:
                    self.assertGreaterEqual(baseline[service], 2)
            self.assertLessEqual(sum(baseline.values()), self.main['max_total_app_pods'])

    def test_smoke_is_short_and_checks_measured_slow_service(self):
        smoke = profiling.load_plan(HERE / "profiling-plan.json", "smoke")
        cells = profiling.plan_cells(smoke)
        self.assertEqual(len(cells), 4)
        self.assertEqual(smoke["duration_sec"], 600)
        self.assertEqual(smoke["scenarios"], ["nhpp", "mmpp", "onoff", "pareto"])
        self.assertEqual(smoke["drain_sec"], 120)
        self.assertEqual(smoke["controlled_services"], ["recommendationservice"])
        self.assertEqual(smoke["max_concurrent"], 400)
        for cell in cells:
            self.assertEqual([event["at_sec"] for event in cell["schedule"]], [180, 300, 420])

    def test_concurrency_budget_scales_with_profile_load_envelope(self):
        diagnostic = profiling.load_plan(HERE / "profiling-plan.json", "diagnostic")
        self.assertEqual(self.main["max_concurrent"], 2000)
        self.assertEqual(diagnostic["max_concurrent"], 1000)
        self.assertEqual(self.main['safe_scenario_multipliers'], {
            'mmpp': 0.5, 'nhpp': 0.75, 'onoff': 1.25,
            'pareto': 2.4649420542587386,
        })
        self.assertEqual(diagnostic['scenario_load_multipliers'], {
            'mmpp': 0.5, 'nhpp': 0.75, 'onoff': 1.25,
        })
        self.assertEqual(
            {(cell['scenario'], cell['load_multiplier'])
             for cell in profiling.plan_cells(diagnostic)},
            {('mmpp', 0.5), ('nhpp', 0.75), ('onoff', 1.25)},
        )
        wrapper = (HERE / 'run-profile-loadgen.sh').read_text()
        self.assertIn('REQUIRED_NOFILE=$((MAX_CONCURRENT + 256))', wrapper)
        self.assertIn('ulimit -Sn "$REQUIRED_NOFILE"', wrapper)

    def test_v62_envelope_rejects_legacy_high_load_and_accepts_new_cells(self):
        cells = profiling.execution_cells(self.main)
        self.assertEqual(len(cells), 30)
        self.assertTrue(cells[0].get('safety_canary'))
        self.assertEqual(cells[0]['scenario'], 'pareto')
        self.assertEqual(self.main['pareto_peak_target_rps'], 50.0)
        for cell in cells:
            evidence = profiling.cell_envelope_evidence(self.main, cell)
            self.assertLessEqual(evidence['designed_peak_frontend_rps'], 60)
            self.assertLessEqual(evidence['worst_rho_peak'], 1.3)
            if (cell['scenario'] == 'pareto'
                    and cell['load_multiplier'] == self.main['safe_scenario_multipliers']['pareto']):
                self.assertAlmostEqual(
                    evidence['designed_peak_frontend_rps'],
                    self.main['pareto_peak_target_rps'], places=9,
                )
        template = next(cell for cell in cells if cell['scenario'] == 'nhpp')
        for multiplier in (2.0, 2.5):
            legacy = dict(template, load_multiplier=multiplier, cell_id=f'legacy-{multiplier}')
            with self.subTest(multiplier=multiplier), self.assertRaisesRegex(
                    ValueError, 'envelope rejected'):
                profiling.cell_envelope_evidence(self.main, legacy)

    def test_v62_canary_must_complete_as_a_one_cell_checkpoint(self):
        cells = profiling.execution_cells(self.main)
        canary_id = cells[0]['cell_id']
        profiling.validate_v62_canary_checkpoint(
            self.main, cells, cells, set(), 1
        )
        with self.assertRaisesRegex(ValueError, '--max-new 1'):
            profiling.validate_v62_canary_checkpoint(
                self.main, cells, cells, set(), 2
            )
        with self.assertRaisesRegex(ValueError, 'cannot be skipped'):
            profiling.validate_v62_canary_checkpoint(
                self.main, cells, cells[1:], set(), 1
            )
        profiling.validate_v62_canary_checkpoint(
            self.main, cells, cells[1:], {canary_id}, 29
        )

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

    def test_load_multiplier_changes_intensity_not_work_distribution(self):
        fields = {
            "nhpp": (("base", "amp1", "amp2", "floor"), ("period_sec", "mean_actions", "think_mean_sec")),
            "mmpp": ((), ("transitions", "initial", "mean_actions", "think_mean_sec")),
            "pareto": (("session_rate",), ("items", "think_mean_sec")),
            "onoff": (("rate_on",), ("n_sources", "on", "off", "warmup_sec")),
        }
        for name, (intensity, invariant) in fields.items():
            spec = importlib.util.spec_from_file_location(f"scenario_{name}", HERE / "scenarios" / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            original = copy.deepcopy(module.CONFIG)
            scaled = arrival.scaled_config(original, 1.5)
            self.assertEqual(module.CONFIG, original)
            for key in intensity:
                self.assertAlmostEqual(scaled[key], original[key] * 1.5)
            for key in invariant:
                self.assertEqual(scaled[key], original[key])
            if name == "mmpp":
                for state in original["states"]:
                    self.assertAlmostEqual(scaled["states"][state]["rate"],
                                           original["states"][state]["rate"] * 1.5)
                    self.assertEqual(scaled["states"][state]["mean_sojourn"],
                                     original["states"][state]["mean_sojourn"])


class CliTests(unittest.TestCase):
    def test_pod_validation_accepts_legacy_and_native_istio_sidecars(self):
        def pod(native):
            proxy = {"name": "istio-proxy", "ready": True}
            return {
                "metadata": {"name": "service-pod"},
                "spec": {
                    "containers": [{"name": "server"}] + ([] if native else [{"name": "istio-proxy"}]),
                    "initContainers": ([{"name": "istio-init"}, {"name": "istio-proxy"}]
                                       if native else [{"name": "istio-init"}]),
                },
                "status": {
                    "phase": "Running",
                    "containerStatuses": ([{"name": "server", "ready": True}]
                                          if native else [{"name": "server", "ready": True}, proxy]),
                    "initContainerStatuses": ([{"name": "istio-init", "ready": False}, proxy]
                                              if native else [{"name": "istio-init", "ready": False}]),
                },
            }
        profiling.validate_app_pod(pod(native=False))
        profiling.validate_app_pod(pod(native=True))
        missing = pod(native=True)
        missing["spec"]["initContainers"] = [{"name": "istio-init"}]
        missing["status"]["initContainerStatuses"] = [{"name": "istio-init", "ready": False}]
        with self.assertRaisesRegex(RuntimeError, "Istio proxy"):
            profiling.validate_app_pod(missing)

    def test_preview_is_read_only_and_defaults_are_separate(self):
        main_args = profiling.parse_args(["--profile", "main"])
        smoke_args = profiling.parse_args(["--profile", "smoke"])
        self.assertNotEqual(main_args.output, smoke_args.output)
        self.assertTrue(main_args.output.endswith(
            'digital-twin-calibration-v2-official-v6-2'
        ))
        self.assertEqual(main_args.max_new, 1)
        with patch.object(profiling, "command", side_effect=AssertionError("cluster call")), \
             patch.object(profiling.subprocess, "run", side_effect=AssertionError("process call")), \
             redirect_stdout(io.StringIO()):
            profiling.run(smoke_args)

    def test_cloud_controller_can_select_direct_worker_ssh(self):
        local = profiling.parse_args([])
        cloud = profiling.parse_args(["--direct-worker-ssh"])
        self.assertFalse(local.direct_worker_ssh)
        self.assertTrue(cloud.direct_worker_ssh)

    def test_live_run_rejects_nonpositive_concurrency_before_cluster_access(self):
        args = profiling.parse_args([
            "--preflight-only", "--max-concurrent", "0",
            "--ssh-target", "ubuntu@loadgen",
            "--observability-ssh-target", "ubuntu@observability",
            "--k3s-ssh-target", "ubuntu@control",
            "--ssh-key", "fixture",
        ])
        with patch.object(profiling, "command", side_effect=AssertionError("cluster call")), \
             redirect_stdout(io.StringIO()), \
             self.assertRaisesRegex(ValueError, "max-concurrent"):
            profiling.run(args)

    def test_early_loadgen_exit_reports_driver_abort_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'driver_report.json').write_text(json.dumps({
                'execution_logging_error': 'abort: dropped arrivals',
                'planned': 611, 'started': 505, 'dropped': 106,
                'max_in_flight': 400, 'max_concurrent': 400,
            }))
            failure = profiling.loadgen_failure_with_evidence(
                RuntimeError('loadgen exited before scheduled duration'), tmp)
            self.assertIn('abort: dropped arrivals', str(failure))
            self.assertIn('dropped=106', str(failure))

    def test_locust_http_error_exit_is_not_an_operational_failure(self):
        complete = {
            'execution_schema_version': 1,
            'planned': 10, 'started': 10, 'completed': 10,
            'trace_events_total': 10, 'dropped': 0, 'failed': 0, 'unfinished': 0,
            'http_error_responses': 2,
        }
        # Compatibility for runs collected before --exit-code-on-error 0:
        # HTTP failures made Locust exit 1 although the execution journal closed.
        profiling.validate_locust_process({
            'locust_exit_code': 1, 'trace_sampling_percentage': '10'
        }, complete)
        profiling.validate_locust_process({
            'locust_exit_code': 0, 'trace_sampling_percentage': 10
        }, complete)

        for mutation in (
            {'dropped': 1}, {'unfinished': 1}, {'execution_logging_error': 'OSError'}
        ):
            invalid = dict(complete, **mutation)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                    ValueError, 'without a complete execution journal'):
                profiling.validate_locust_process({
                    'locust_exit_code': 1, 'trace_sampling_percentage': 10
                }, invalid)
        with self.assertRaisesRegex(ValueError, 'sampling percentage'):
            profiling.validate_locust_process({
                'locust_exit_code': 0, 'trace_sampling_percentage': 'unknown'
            }, complete)

    def test_filters_do_not_mutate_the_study(self):
        args = profiling.parse_args(["--scenario", "nhpp", "--split", "train",
                                     "--load-multiplier", "0.75", "--repeat", "0"])
        with patch.object(profiling, "command", side_effect=AssertionError("cluster call")), \
             redirect_stdout(io.StringIO()):
            profiling.run(args)
        export_args = profiling.parse_args(["--scenario", "mmpp", "--export", "manifest.json"])
        with self.assertRaisesRegex(ValueError, "filters cannot be used with --export"):
            profiling.run(export_args)

    def test_confirm_and_export_are_mutually_exclusive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            profiling.parse_args(["--confirm", "--export", "manifest.json"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            profiling.parse_args(["--confirm", "--preflight-only"])

    def test_collection_lock_excludes_offline_analyzer_and_synthetic_code(self):
        hashes = profiling.collection_lock_hashes(HERE / "profiling-plan.json")
        self.assertEqual(set(hashes), {
            "run-profiling.py", "locustfile.py", "collect_metrics.py",
            "profiling-plan.json",
        })
        self.assertNotIn("calibration.py", hashes)
        self.assertNotIn("synthetic_recovery.py", hashes)
        self.assertNotIn("test_profiling.py", hashes)

    def test_registered_order_locks_trace_definitions_separately(self):
        trace_hashes = profiling.trace_definition_hashes()
        self.assertEqual(set(trace_hashes), {
            'arrival_models.py', 'scenarios/mmpp.py', 'scenarios/nhpp.py',
            'scenarios/onoff.py', 'scenarios/pareto.py',
        })
        plan = profiling.load_plan(HERE / 'profiling-plan.json', 'main')
        order = profiling.registered_order(
            plan, plan['max_concurrent'], HERE / 'profiling-plan.json'
        )
        self.assertEqual(order['trace_definition_sha256'], trace_hashes)
        with patch.object(profiling, 'trace_definition_hashes',
                          return_value=dict(trace_hashes, **{'arrival_models.py': 'changed'})):
            changed = profiling.registered_order(
                plan, plan['max_concurrent'], HERE / 'profiling-plan.json'
            )
        self.assertNotEqual(order['order_sha256'], changed['order_sha256'])

    def test_live_cpu_limits_must_match_registered_envelope(self):
        plan = profiling.load_plan(HERE / 'profiling-plan.json', 'main')
        self.assertEqual(profiling.cpu_quantity_cores('500m'), 0.5)
        self.assertEqual(profiling.cpu_quantity_cores('1000000u'), 1.0)
        deployments = {}
        for service, limit in plan['envelope_gate']['cpu_limit_cores_per_pod'].items():
            deployments[service] = {'spec': {'template': {'spec': {'containers': [{
                'name': 'server', 'resources': {'limits': {'cpu': f'{limit:g}'}},
            }]}}}}
        profiling.validate_envelope_cpu_limits(plan, deployments)
        deployments['frontend']['spec']['template']['spec']['containers'][0][
            'resources']['limits']['cpu'
        ] = '600m'
        with self.assertRaisesRegex(RuntimeError, 'differs from registered envelope'):
            profiling.validate_envelope_cpu_limits(plan, deployments)

    def test_analyzer_failure_preserves_collected_run_and_resume_identity(self):
        plan = profiling.load_plan(HERE / "profiling-plan.json", "main")
        cell = profiling.execution_cells(plan)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / cell["cell_id"]
            run.mkdir()
            (run / "meta.json").write_text(json.dumps({"profiling": cell}))
            with patch.object(profiling, "validate_run", return_value=({}, {})), \
                 patch("calibration.analyze", side_effect=RuntimeError("fixture analyzer crash")), \
                 redirect_stdout(io.StringIO()):
                result = profiling.analyze_collected(root, plan)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["safety_censored"], 1)
            self.assertTrue(any(row.get('exclude_from_fit') is True
                                for row in result['results']))
            self.assertTrue(run.is_dir())
            self.assertFalse((root / "_incomplete").exists())
            with patch.object(profiling, "validate_run", return_value=({}, {})):
                completed = profiling.collected_run_ids(
                    root, [cell], lambda _: cell, plan["max_unstarted_fraction"]
                )
            self.assertEqual(completed, {cell["cell_id"]})

    def test_main_collection_is_not_blocked_by_scientific_analysis_reports(self):
        args = profiling.parse_args(["--profile", "main", "--collect-only"])
        with patch.object(profiling, "campaign_lock", return_value=nullcontext()), \
             patch.object(profiling, "run_impl", return_value="collection-started"):
            self.assertEqual(profiling.run(args), "collection-started")

    def test_operational_helpers_are_fail_closed_and_atomic(self):
        self.assertEqual(profiling.parse_df_available_kib(
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/vda 100 20 80 20% /\n"
        ), 80)
        with self.assertRaises(ValueError):
            profiling.parse_df_available_kib("malformed")
        with patch.object(profiling.time, "time_ns", side_effect=(1_000_000_000, 1_020_000_000)), \
             patch.object(profiling, "command", return_value="1005000000"):
            bound, offset, uncertainty = profiling.remote_clock_error_bound(["ssh", "fixture"], samples=1)
        self.assertAlmostEqual(offset, -0.005)
        self.assertAlmostEqual(uncertainty, 0.01)
        self.assertAlmostEqual(bound, 0.015)
        with self.assertRaises(ValueError):
            profiling.remote_clock_error_bound(["ssh", "fixture"], samples=0)
        times = iter((
            0, 400_000_000,       # slow first sample: 200 ms uncertainty
            1_000_000_000, 1_020_000_000,  # fast second sample: 10 ms uncertainty
        ))
        remotes = iter(("200000000", "1005000000"))
        with patch.object(profiling.time, "time_ns", side_effect=lambda: next(times)), \
             patch.object(profiling, "command", side_effect=lambda _: next(remotes)):
            bound, offset, uncertainty = profiling.remote_clock_error_bound(
                ["ssh", "fixture"], samples=2
            )
        self.assertAlmostEqual(uncertainty, 0.01)
        self.assertAlmostEqual(offset, -0.005)
        self.assertAlmostEqual(bound, 0.015)
        # Choose the smallest complete error bound, not merely the lowest RTT.
        # Sample 1 has lower uncertainty but an offset-biased 100 ms bound;
        # sample 2 has a wider RTT interval but proves a tighter 20 ms bound.
        times = iter((0, 20_000_000, 1_000_000_000, 1_040_000_000))
        remotes = iter(("100000000", "1020000000"))
        with patch.object(profiling.time, "time_ns", side_effect=lambda: next(times)), \
             patch.object(profiling, "command", side_effect=lambda _: next(remotes)):
            bound, offset, uncertainty = profiling.remote_clock_error_bound(
                ["ssh", "fixture"], samples=2
            )
        self.assertAlmostEqual(bound, 0.02)
        self.assertAlmostEqual(offset, 0.0)
        self.assertAlmostEqual(uncertainty, 0.02)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profiling_progress.json"
            plan = profiling.load_plan(HERE / "profiling-plan.json", "smoke")
            profiling.write_progress(path, plan, "context", {"cell-a"}, 3, "running")
            saved = json.loads(path.read_text())
            self.assertEqual(saved["completed_cell_ids"], ["cell-a"])
            self.assertEqual(saved["completed_count"], 1)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_workload_completion_and_frontend_coverage_fail_closed(self):
        complete = dict(planned=10, started=10, completed=10, dropped=0, failed=0,
                        unfinished=0, trace_events_total=10)
        profiling.validate_driver_counters(complete, 10, 0)
        transport_outcome = dict(complete, failed=1, completed=9)
        profiling.validate_driver_counters(transport_outcome, 10, 0)
        for key, value in (("unfinished", 1), ("dropped", 1),
                           ("completed", 8), ("planned", 9)):
            invalid = dict(complete)
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                profiling.validate_driver_counters(invalid, 10, 0)

        rows = [
            {"timestamp": "129.9", "service": "frontend"},
            {"timestamp": "130", "service": "frontend"},
            {"timestamp": "131", "service": "frontend"},
            {"timestamp": "165", "service": "frontend"},
            {"timestamp": "190", "service": "cartservice"},
        ]
        total, empty, windows = profiling.frontend_span_coverage(
            rows, origin=100, duration=120, warmup=30, window=30)
        self.assertEqual((total, empty, windows), (3, 1, 3))

    def test_storage_estimate_uses_smoke_artifact_and_three_hour_ttl(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "cell"
            run.mkdir()
            (run / "meta.json").write_text(json.dumps({
                "duration_sec": 600,
                "profiling": {"scenario": "nhpp", "load_multiplier": 0.5},
            }))
            (run / "collect_report.json").write_text("{}")
            (run / "jaeger_server_spans.csv").write_bytes(b"x" * 600)
            (run / "node_metrics.csv").write_bytes(b"y" * 300)
            with patch.object(profiling, "validate_run", return_value=({}, {})):
                estimate = profiling.storage_estimate(
                    folder, profiling.load_plan(HERE / "profiling-plan.json", "main")
                )
            self.assertEqual(estimate["smoke_samples"], 1)
            self.assertEqual(estimate["covered_scenarios"], ["nhpp"])
            self.assertGreater(estimate["exported_bytes"], 0)
            max_multiplier = max(
                cell['load_multiplier'] for cell in profiling.execution_cells(
                    profiling.load_plan(HERE / 'profiling-plan.json', 'main'))
            )
            self.assertEqual(estimate["jaeger_peak_bytes"],
                             (600 / 600) / 0.5 * max_multiplier * 3 * 3600 * 3)

    def test_storage_estimate_scales_low_load_smoke_to_main_cells(self):
        with tempfile.TemporaryDirectory() as folder:
            main = profiling.load_plan(HERE / "profiling-plan.json", "main")
            for scenario in main["scenarios"]:
                run = Path(folder) / scenario
                run.mkdir()
                (run / "meta.json").write_text(json.dumps({
                    "duration_sec": 600,
                    "profiling": {"scenario": scenario, "load_multiplier": 0.5},
                }))
                (run / "collect_report.json").write_text("{}")
                (run / "jaeger_server_spans.csv").write_bytes(b"x" * 600)
                (run / "node_metrics.csv").write_bytes(b"y" * 300)
            with patch.object(profiling, "validate_run", return_value=({}, {})):
                estimate = profiling.storage_estimate(folder, main)
            self.assertEqual(estimate["covered_scenarios"], sorted(main["scenarios"]))
            # Mean main multiplier is 1.0, twice the smoke multiplier 0.5.
            unscaled = sum(profiling.directory_bytes(Path(folder) / scenario) / 600
                           for scenario in main["scenarios"])
            self.assertGreater(estimate["exported_bytes"], unscaled * main["duration_sec"])


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

    def test_calibration_queries_bounded_chunks_but_keeps_10s_grid(self):
        spec=importlib.util.spec_from_file_location('collector_chunks',HERE/'collect_metrics.py')
        with patch.dict(sys.modules,{'requests':types.SimpleNamespace()}):
            collector=importlib.util.module_from_spec(spec);spec.loader.exec_module(collector)
        calls=[]
        def fetch(*args):
            calls.append((args[2],args[3]));return []
        with patch.object(collector,'jaeger_service_names',return_value={'frontend':'frontend'}), \
             patch.object(collector,'fetch_traces',side_effect=fetch):
            report={'jaeger_truncated_windows':0,'jaeger_failed_queries':[]}
            collector.collect_jaeger('http://fixture','ns',1000,1120,10,1500,report,
                                     raw_spans=[],lookback_sec=120,capture_end=1180)
        self.assertEqual(len(calls),10)
        self.assertTrue(all(b-a<=30_000_000 for a,b in calls))


class ExecutionLoggingTests(unittest.TestCase):
    """Exercise production logger without importing Locust or contacting a host."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tree = ast.parse((HERE / 'locustfile.py').read_text(encoding='utf-8'))
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name in ('ExecutionLog', 'Client')]
        self.ns = dict(Path=Path, csv=csv, json=json, time=time, urlsplit=urlsplit,
                       CONFIG={'model': 'mmpp'})
        exec(compile(ast.Module(body=classes, type_ignores=[]), 'locustfile.py', 'exec'), self.ns)
        self.log = self.ns['ExecutionLog'](self.tmp.name)
        self.addCleanup(self.log.close)
        self.ns['RECORDER'] = self.log
        self.event = dict(t=2, size=5, kind='session', source=1)

    def rows(self, name):
        with (Path(self.tmp.name) / (name + '.csv')).open(newline='', encoding='utf-8') as f:
            return list(csv.DictReader(f))

    def test_runtime_abort_thresholds_are_enforced(self):
        rules=profiling.load_plan(HERE/'profiling-plan.json','main')['abort_rules']
        with patch.object(time,'monotonic',return_value=100), patch.object(Path,'read_text',return_value='MemAvailable: 1000000 kB\n'):
            self.log.check_abort(rules)
            self.log.dropped=1
            with self.assertRaisesRegex(RuntimeError,'dropped'): self.log.check_abort(rules)
            self.log.dropped=0
            self.log.recent_requests=[(100,20,True)]*100
            with self.assertRaisesRegex(RuntimeError,'error fraction'): self.log.check_abort(rules)
            self.log.recent_requests=[(100,10001,False)]*100
            with self.assertRaisesRegex(RuntimeError,'p95'): self.log.check_abort(rules)
            self.log.recent_requests=[]
            self.log.recent_lags=[(100,1001)]*20
            with self.assertRaisesRegex(RuntimeError,'lag'): self.log.check_abort(rules)
            self.log.recent_lags=[]; self.log.last_snapshot_mono=90
            with self.assertRaisesRegex(RuntimeError,'stalled'): self.log.check_abort(rules)
        with patch.object(Path,'read_text',return_value='MemAvailable: 100 kB\n'):
            with self.assertRaisesRegex(RuntimeError,'memory'): self.log.check_abort(rules)

    def start(self, sid='one'):
        with patch.object(time, 'time', return_value=102.25), \
             patch.object(time, 'monotonic', return_value=12.25):
            self.log.start_session(sid, self.event, 100, 10)

    def test_session_lag_size_and_http_errors_are_distinct(self):
        self.start()
        rid, mono = self.log.begin_request('one', 'GET', 'GET /')
        response = types.SimpleNamespace(status_code=503, history=[], url='http://fixture/',
                                         request=types.SimpleNamespace(method='GET'))
        self.log.finish_request(rid, mono, response=response)
        self.log.finish_session('one')
        self.log.close()
        row = self.rows('sessions')[0]
        self.assertEqual(float(row['lag_ms']), 250)
        self.assertEqual(int(row['planned_requests']), 6)
        self.assertEqual(int(row['actual_size']), 1)
        self.assertEqual(row['outcome'], 'completed')
        summary = self.log.summary()
        self.assertEqual(summary['http_error_responses'], 1)
        self.assertEqual(summary['driver_exceptions'], 0)
        self.assertEqual(summary['transport_errors'], 0)

    def test_redirects_are_one_operation_with_hop_evidence(self):
        self.start()
        rid, mono = self.log.begin_request('one', 'POST', 'POST /cart')
        hop = types.SimpleNamespace(status_code=302, url='http://fixture/cart?private=omit',
                                    request=types.SimpleNamespace(method='POST'))
        response = types.SimpleNamespace(status_code=200, history=[hop], url='http://fixture/',
                                         request=types.SimpleNamespace(method='GET'))
        self.log.finish_request(rid, mono, response=response)
        self.log.finish_session('one')
        self.log.close()
        row = self.rows('client_requests')[0]
        self.assertEqual(row['redirect_count'], '1')
        self.assertEqual([r['status'] for r in json.loads(row['redirect_chain'])], [302, 200])
        self.assertNotIn('private', row['redirect_chain'])
        self.assertEqual(self.log.summary()['requests_completed'], 1)

    def test_dropped_and_censored_work_are_not_completed(self):
        self.start('active')
        self.log.begin_request('active', 'GET', 'GET /')
        self.log.start_session('drop', self.event, 100, time.monotonic(), dropped=True)
        self.log.close()
        sessions = {r['session_id']: r for r in self.rows('sessions')}
        self.assertEqual(sessions['drop']['dropped'], '1')
        self.assertEqual(sessions['drop']['actual_start_ts'], '')
        self.assertEqual(sessions['active']['outcome'], 'right_censored')
        self.assertEqual(sessions['active']['end_ts'], '')
        request = self.rows('client_requests')[0]
        self.assertEqual(request['t_end'], '')
        self.assertEqual(request['error'], 'right_censored')
        self.assertEqual(self.log.summary()['requests_completed'], 0)
        self.assertEqual(self.log.summary()['request_censored'], 1)
        self.assertEqual(self.log.summary()['scheduling_lag_ms']['count'], 1)

    def test_transport_failure_and_driver_exception_have_separate_counters(self):
        self.start()
        rid, mono = self.log.begin_request('one', 'GET', 'GET /')
        self.log.finish_request(rid, mono, error=ConnectionError('fixture'))
        self.log.finish_session('one', error=ConnectionError('fixture'))
        self.assertEqual(self.log.summary()['transport_errors'], 1)
        self.assertEqual(self.log.summary()['driver_exceptions'], 1)
        self.assertEqual(self.log.summary()['http_error_responses'], 0)

    def test_transport_evidence_formatting_cannot_lose_request(self):
        self.start()
        rid, mono = self.log.begin_request('one', 'GET', 'GET /')
        response = types.SimpleNamespace(
            status_code=0,
            history=[],
            url=object(),
            request=types.SimpleNamespace(method='GET'),
            error=ConnectionError('fixture'),
        )
        self.log.finish_request(rid, mono, response=response)
        self.log.finish_session('one', error=ConnectionError('fixture'))
        self.log.close()
        requests = self.rows('client_requests')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['status'], '0')
        self.assertEqual(requests[0]['error'], 'ConnectionError')
        self.assertEqual(self.log.summary()['requests_completed'], 1)

    def test_execution_validator_accepts_logged_transport_outcome(self):
        self.start()
        rid, mono = self.log.begin_request('one', 'GET', 'GET /')
        exc = ConnectionError('fixture')
        self.log.finish_request(rid, mono, error=exc)
        self.log.finish_session('one', error=exc)
        self.log.writers['in_flight'].writerow(dict(timestamp=103, sessions_in_flight=0,
                                                   requests_in_flight=0))
        self.log.writers['loadgen_resources'].writerow(dict(timestamp=103,
            node_cpu_busy_pct=50, node_mem_available_bytes=1000000, process_rss_bytes=100000))
        self.log.close()
        driver = dict(self.log.summary(), planned=1, failed=1)
        profiling.validate_execution_artifacts(self.tmp.name, driver)

    def test_transport_outcome_does_not_require_driver_exception(self):
        self.start()
        rid, mono = self.log.begin_request('one', 'GET', 'GET /')
        response = types.SimpleNamespace(
            status_code=0, history=[], url='http://fixture/',
            request=types.SimpleNamespace(method='GET'),
            error=ConnectionError('connection closed'),
        )
        self.log.finish_request(rid, mono, response=response)
        # Locust may return a status=0 response object instead of raising it to
        # the session greenlet.  This is measured transport telemetry, while the
        # session itself completed normally.
        self.log.finish_session('one')
        self.log.writers['in_flight'].writerow(dict(timestamp=103, sessions_in_flight=0,
                                                   requests_in_flight=0))
        self.log.writers['loadgen_resources'].writerow(dict(timestamp=103,
            node_cpu_busy_pct=50, node_mem_available_bytes=1000000,
            process_rss_bytes=100000))
        self.log.close()
        driver = dict(self.log.summary(), planned=1, failed=0)
        profiling.validate_execution_artifacts(self.tmp.name, driver)

        sessions = self.rows('sessions')
        sessions[0]['outcome'] = 'driver_exception'
        sessions[0]['driver_error'] = 'TimeoutError'
        with (Path(self.tmp.name) / 'sessions.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=sessions[0])
            writer.writeheader(); writer.writerows(sessions)
        with self.assertRaisesRegex(ValueError, 'evidence mismatch'):
            profiling.validate_execution_artifacts(
                self.tmp.name, dict(driver, driver_exceptions=1, failed=1)
            )

    def test_onoff_ids_do_not_merge_events_from_same_source(self):
        self.ns['CONFIG'] = {'model': 'onoff'}
        self.event.update(kind='request', size=1)
        self.start('first')
        self.start('second')
        first, mono = self.log.begin_request('first', 'GET', 'GET /')
        second, mono2 = self.log.begin_request('second', 'GET', 'GET /cart')
        self.log.finish_request(second, mono2, error=TimeoutError())
        self.log.finish_session('second')
        self.log.close()
        rows = {r['request_id']: r for r in self.rows('client_requests')}
        self.assertEqual(rows[str(first)]['session_id'], 'first')
        self.assertEqual(rows[str(second)]['session_id'], 'second')

    def test_resource_sampling_and_inflight_conservation(self):
        self.start()
        self.log.begin_request('one', 'GET', 'GET /')
        proc = {'/proc/stat': 'cpu 100 0 0 100 0 0 0 0 0 0\n',
                '/proc/meminfo': 'MemTotal: 1024 kB\nMemAvailable: 512 kB\n',
                '/proc/self/statm': '10 2 0 0 0 0 0'}
        self.ns['Path'] = lambda path: types.SimpleNamespace(read_text=lambda: proc[path])
        self.ns['os'] = types.SimpleNamespace(sysconf=lambda _: 4096)
        self.log.snapshot()
        proc['/proc/stat'] = 'cpu 110 0 0 110 0 0 0 0 0 0\n'
        self.log.snapshot()
        self.log.close()
        rows = self.rows('loadgen_resources')
        self.assertEqual(rows[0]['node_cpu_busy_pct'], '')
        self.assertEqual(float(rows[1]['node_cpu_busy_pct']), 50)
        self.assertEqual(int(rows[1]['process_rss_bytes']), 8192)
        row = self.rows('in_flight')[0]
        self.assertEqual(int(row['requests_started']) - int(row['requests_completed']),
                         int(row['requests_in_flight']))
        self.assertEqual(row['sessions_in_flight'], '1')

    def test_execution_validator_accepts_http_errors_but_rejects_logging_failure(self):
        self.start()
        rid, mono = self.log.begin_request('one', 'GET', 'GET /')
        self.log.finish_request(rid, mono, response=types.SimpleNamespace(status_code=500))
        self.log.finish_session('one')
        self.log.writers['in_flight'].writerow(dict(timestamp=103, sessions_in_flight=0,
                                                   requests_in_flight=0))
        self.log.writers['loadgen_resources'].writerow(dict(timestamp=103,
            node_cpu_busy_pct=50, node_mem_available_bytes=1000000, process_rss_bytes=100000))
        self.log.close()
        driver = dict(self.log.summary(), planned=1, failed=0)
        profiling.validate_execution_artifacts(self.tmp.name, driver)
        with self.assertRaisesRegex(ValueError, 'logger'):
            profiling.validate_execution_artifacts(self.tmp.name,
                dict(driver, execution_logging_error='OSError'))
        with self.assertRaisesRegex(ValueError, 'request count'):
            profiling.validate_execution_artifacts(self.tmp.name, dict(driver, requests_started=2))


class CalibrationV2Tests(unittest.TestCase):
    def test_analyzer_accepts_zero_status_final_transport_hop(self):
        from calibration import parse_client_records
        base=dict(request_id='1',t_start='1',t_end='2',latency_ms='1000',
                  error='transport_error',endpoint='GET /',method='GET')
        transport=dict(base,status='0',redirect_count='0',
                       redirect_chain='[{"path":"/","method":"GET","status":0}]')
        record=parse_client_records([transport])[0]
        self.assertEqual(record['status'],0)
        self.assertEqual(record['redirect_count'],0)
        redirect=dict(base,status='200',redirect_count='1',error='',
                      redirect_chain=('[{"path":"/old","method":"GET","status":302},'
                                      '{"path":"/","method":"GET","status":200}]'))
        self.assertEqual(parse_client_records([redirect])[0]['redirect_count'],1)
        inconsistent=dict(transport,redirect_count='1')
        with self.assertRaisesRegex(ValueError,'redirect evidence mismatch'):
            parse_client_records([inconsistent])
        wrong_status=dict(transport,status='200')
        with self.assertRaisesRegex(ValueError,'final-hop status mismatch'):
            parse_client_records([wrong_status])

    def test_diagnostic_contract_is_scoped_to_its_focus_service(self):
        from calibration import observation_contract_services
        self.assertEqual(observation_contract_services(dict(
            role='diagnostic', focus_service='currencyservice')),
            {'currencyservice'})
        self.assertEqual(observation_contract_services(dict(
            role='study', controlled_services=['frontend','cartservice'])),
            {'frontend','cartservice'})

    def test_capture_contract_uses_interval_and_trace_propagation(self):
        from calibration import infer_capture_contract
        capture=[dict(record_type='summary',endpoint='GET /',client_hops=1000,
                      frontend_spans=100)]
        raw=([dict(service='frontend',trace_id=f't{i}') for i in range(100)] +
             [dict(service='cartservice',trace_id=f't{i}') for i in range(98)])
        result=infer_capture_contract(capture,raw,.10)
        self.assertTrue(result['frontend_valid'])
        self.assertTrue(result['services']['cartservice']['valid'])
        self.assertTrue(result['services']['cartservice']['propagation_observed'])
        self.assertEqual(result['services']['cartservice']['traces_shared_with_frontend'],98)
        self.assertEqual(result['services']['cartservice']['sampled_traces'],98)
        self.assertEqual(result['services']['cartservice']['shared_fraction_of_service_traces'],1)
        unrelated=[dict(service='frontend',trace_id=f't{i}') for i in range(100)] + [
            dict(service='cartservice',trace_id=f'x{i}') for i in range(100)]
        disconnected=infer_capture_contract(capture,unrelated,.10)['services']['cartservice']
        self.assertTrue(disconnected['valid'])
        self.assertFalse(disconnected['propagation_observed'])
        self.assertEqual(disconnected['shared_fraction_of_service_traces'],0)
        self.assertFalse(infer_capture_contract([
            dict(record_type='summary',endpoint='GET /',client_hops=1000,
                 frontend_spans=300)],raw,.10)['frontend_valid'])

    def test_resource_coverage_threshold_distinguishes_controlled_and_auxiliary(self):
        from calibration import required_resource_coverage
        self.assertEqual(required_resource_coverage('frontend',()),.90)
        self.assertEqual(required_resource_coverage('cartservice',('cartservice',)),.90)
        self.assertEqual(required_resource_coverage('checkoutservice',('cartservice',)),.75)

    def test_resource_window_fallback_requires_three_measured_10s_samples(self):
        from calibration import aggregate_resource_window
        rows=[dict(timestamp=str(timestamp),service='frontend',cpu_cores=str(cpu),
                   cpu_limit_cores='0.5',cpu_throttle_ratio=str(throttle))
              for timestamp,cpu,throttle in
              ((10,.1,.01),(20,.2,.02),(30,.3,.03))]
        result=aggregate_resource_window(rows,'frontend',0,30)
        self.assertAlmostEqual(result['cpu_cores'],.2)
        self.assertAlmostEqual(result['cpu_limit_cores'],.5)
        self.assertAlmostEqual(result['cpu_throttle_ratio'],.02)
        self.assertIsNone(aggregate_resource_window(rows[:2],'frontend',0,30))
        broken=[dict(row) for row in rows]
        broken[1]['cpu_cores']=''
        self.assertIsNone(aggregate_resource_window(broken,'frontend',0,30))
        gapped=[dict(row) for row in rows]
        gapped[2]['timestamp']='31.5'
        self.assertIsNone(aggregate_resource_window(gapped,'frontend',0,31.5))

    def test_drop_counter_parsing_never_assumes_absent_is_zero(self):
        self.assertEqual(profiling.span_drop_counters('unrelated_metric 3'),{})
        self.assertEqual(profiling.span_drop_counters('jaeger_collector_spans_dropped_total{svc="x"} 2'),
                         {'jaeger_collector_spans_dropped_total{svc="x"}':2})
        with self.assertRaises(ValueError):
            profiling.span_drop_counters('jaeger_collector_spans_dropped_total NaN')

    def test_registration_is_local_and_refuses_changed_order(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(profiling,'command',side_effect=AssertionError('network')):
            args=profiling.parse_args(['--profile','diagnostic','--register-order','--output',tmp])
            with redirect_stdout(io.StringIO()): profiling.run(args)
            self.assertEqual(len(json.loads((Path(tmp)/'campaign_order.json').read_text())['executions']),3)
            args.max_concurrent+=1
            with self.assertRaisesRegex(ValueError,'differs'): profiling.run(args)

    def test_v62_refuses_legacy_validator_only_order_compatibility(self):
        plan = profiling.load_plan(HERE / 'profiling-plan.json', 'main')
        candidate = profiling.registered_order(
            plan, plan['max_concurrent'], HERE / 'profiling-plan.json'
        )
        registered = copy.deepcopy(candidate)
        registered['collection_code_sha256']['run-profiling.py'] = (
            profiling.PRIOR_VALIDATOR_ONLY_RUNNER_SHA256
        )
        registered['collection_lock_sha256'] = profiling.digest(
            registered['collection_code_sha256']
        )
        registered['order_sha256'] = profiling.digest({
            key: value for key, value in registered.items() if key != 'order_sha256'
        })
        self.assertFalse(profiling.validator_only_order_compatible(
            registered, candidate
        ))
        changed_collector = copy.deepcopy(registered)
        changed_collector['collection_code_sha256']['collect_metrics.py'] = 'changed'
        self.assertFalse(profiling.validator_only_order_compatible(
            changed_collector, candidate
        ))
        changed_plan = copy.deepcopy(registered)
        changed_plan['executions'][0]['cell_id'] = 'changed'
        self.assertFalse(profiling.validator_only_order_compatible(
            changed_plan, candidate
        ))

    def test_analyzer_writes_fail_closed_estimates_and_preserves_inputs(self):
        from calibration import analyze,read_csv,write_csv
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            meta=dict(duration_sec=300,scenario='nhpp',profiling=dict(warmup_sec=0,post_ready_warmup_sec=0,split='train'))
            (root/'meta.json').write_text(json.dumps(meta))
            (root/'driver_report.json').write_text(json.dumps(dict(trace_start_epoch=100.0)))
            journal=[dict(kind='start',origin=100,profile=dict(replicas={'frontend':1}))]
            journal.extend(dict(kind='snapshot',epoch=t,state={'frontend':dict(available=1,desired=1)}) for t in range(100,401,5))
            (root/'capacity_events.jsonl').write_text('\n'.join(json.dumps(r) for r in journal))
            clients=[dict(t_start=101,t_end=102,latency_ms=1000,error='',endpoint='GET /',redirect_chain='[{"path":"/","method":"GET","status":200}]')]
            write_csv(root/'client_requests.csv',clients,list(clients[0]))
            write_csv(root/'sessions.csv',[dict(actual_start_ts=101,end_ts=102)],['actual_start_ts','end_ts'])
            raw=[dict(timestamp=101,end_timestamp=102,service='frontend',latency_ms=1000,error=0,endpoint='GET /',trace_id='t1')]
            write_csv(root/'jaeger_server_spans.csv',raw,list(raw[0]))
            # Prometheus deployment gauges may be absent at an aligned timestamp;
            # analyzer must use the recent controller journal, never a constant.
            nodes=[dict(window_end=t,service='frontend',replicas='',
                        cpu_cores='' if t==130 else .1,
                        cpu_limit_cores=1,cpu_throttle_ratio=0)
                   for t in range(130,401,30)]
            write_csv(root/'node_windows_30s.csv',nodes,list(nodes[0]))
            before=(root/'client_requests.csv').read_bytes()
            result=analyze(root)
            self.assertTrue(result['conservation_pass'])
            self.assertFalse(result['estimates']['frontend']['capacity']['capacity_identified'])
            self.assertFalse(result['estimates']['frontend']['cpu']['identified'])
            features=read_csv(root/'calibration_features_30s.csv')
            self.assertEqual({row['replicas'] for row in features},{'1.0'})
            self.assertEqual({row['replicas_source'] for row in features},
                             {'capacity_events_available'})
            self.assertEqual({row['replicas_prometheus'] for row in features},{''})
            self.assertEqual(len(features),9)
            self.assertEqual(result['resource_coverage']['frontend'],
                             {'total':10,'usable':9,'pressure_usable':9,'critical':True,
                              'required_fraction':0.9,'fraction':0.9})
            nodes[1]['cpu_cores']=''
            write_csv(root/'node_windows_30s.csv',nodes,list(nodes[0]))
            with self.assertRaisesRegex(ValueError,'registered service threshold'):
                analyze(root)
            self.assertEqual(before,(root/'client_requests.csv').read_bytes())

    def test_exclusive_union_clips_overlaps(self):
        from calibration import exclusive_ms
        self.assertEqual(exclusive_ms(1000,10000,[(0,4000),(3000,7000),(9000,20000)]),2)

    def test_boundary_cohorts_and_conservation(self):
        from calibration import window_rows
        records=[dict(t_start=-1,t_end=1,latency_ms=2000,error=False),
                 dict(t_start=2,t_end=30,latency_ms=28000,error=False),
                 dict(t_start=31,t_end=None,latency_ms=None,error=False)]
        rows=window_rows(records,0,60)
        self.assertEqual([r['completion_count'] for r in rows],[1,1])
        self.assertEqual([r['arrival_count'] for r in rows],[1,1])
        self.assertEqual([r['conservation_residual'] for r in rows],[0,0])
        self.assertEqual(rows[1]['inflight_right'],1)

    def test_capacity_requires_independent_evidence_not_latency_alone(self):
        from calibration import identify_capacity
        rows=[]
        for band in range(3):
            for _ in range(12):
                rows.append(dict(split='train',role='study',stable=True,replicas=1,
                    scenario='mmpp',mix_id='fixed',active_sessions=(2,10,30)[band],
                    completed_rps=(10,40,40)[band],
                    effective_service_latency_ms=(10,20,100)[band],
                    cpu_cores=(.1,.9,.95)[band],cpu_limit_cores=1,cpu_throttle_ratio=0,
                    effective_latency_valid=True,callee_normal=True,capture_valid=True))
        self.assertEqual(identify_capacity(rows)['mu'],40)
        for flag in ('effective_latency_valid','callee_normal','capture_valid'):
            self.assertFalse(identify_capacity([dict(r,**{flag:False}) for r in rows])['capacity_identified'])
        self.assertFalse(identify_capacity([dict(r,split='test') for r in rows])['capacity_identified'])

    def test_cpu_exact_count_and_split_exclusion(self):
        from calibration import fit_cpu
        rows=[dict(split='train',stable=True,capture_valid=True,exact_client_count=True,
                   replicas=1+i%3,completed_rps=float(i),cpu_cores=.02*(1+i%3)+.004*i)
              for i in range(60)]
        result=fit_cpu(rows)
        self.assertAlmostEqual(result['idle'],.02)
        self.assertAlmostEqual(result['cost'],.004)
        self.assertFalse(fit_cpu([dict(r,role='diagnostic') for r in rows])['identified'])
        self.assertFalse(fit_cpu([dict(r,capture_valid=False) for r in rows])['identified'])

    def test_root_cluster_variance_exceeds_independent_span_assumption(self):
        from calibration import rps_trace_variance
        # Two spans from one sampled trace have m^2=4, not 2 independent trials.
        self.assertEqual(rps_trace_variance(4),2*rps_trace_variance(2))
        with self.assertRaises(ValueError): rps_trace_variance(4,sampling=0)

    def test_order_controls_and_registration(self):
        plan=profiling.load_plan(HERE/'profiling-plan.json','main')
        cells=profiling.execution_cells(plan)
        self.assertEqual(len(cells),30)
        controls=[c for c in cells if c.get('role')=='drift_control']
        self.assertEqual([cells.index(c) for c in controls],[1,15,29])
        self.assertEqual(len({c['cell_id'] for c in cells}),30)
        self.assertTrue(cells[0].get('safety_canary'))
        self.assertEqual(cells[0]['scenario'],'pareto')
        self.assertEqual(controls[0]['schedule'],controls[2]['schedule'])
        self.assertEqual(controls[0]['seed'],controls[2]['seed'])
        self.assertEqual(cells,profiling.execution_cells(plan))
        a=profiling.registered_order(plan,400); b=profiling.registered_order(plan,401)
        self.assertNotEqual(a['order_sha256'],b['order_sha256'])
        for name in ('smoke','diagnostic'):
            self.assertTrue(all(c['role']=='diagnostic' for c in profiling.execution_cells(
                profiling.load_plan(HERE/'profiling-plan.json',name))))

    def test_remote_stop_confirmation_required(self):
        with patch.object(profiling,'command',return_value='still running'):
            with self.assertRaisesRegex(RuntimeError,'not confirmed'):
                profiling.confirm_remote_stopped(['ssh','fixture'],'/fixture',None)
        with patch.object(profiling,'command',return_value='REMOTE_STOP_CONFIRMED'):
            profiling.confirm_remote_stopped(['ssh','fixture'],'/fixture',None)


if __name__ == "__main__":
    unittest.main()
