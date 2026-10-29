"""Verify current inference source against the GAT-GRU v05 golden predictions.

This is an inference-only compatibility test. It never trains or updates a
checkpoint. With ``--write-fixtures`` it exports one small raw input fixture and
the corresponding stored outputs for future smoke tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

import forecast_data as fd
from gatgru_inference import GATGRUPredictor


RTOL = 1e-5
ATOL = 1e-6


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalize_info(rows):
    return [(str(run), str(scenario), int(step)) for run, scenario, step in rows]


def verify(repo_root, write_fixtures=False):
    root = Path(repo_root).resolve()
    artifact_dir = root / "artifacts" / "gatgru" / "v05_selected_features_final"
    raw_path = root / "dataset" / "processed" / "gatgru" / "runs_raw.npz"
    graph_meta_path = root / "configs" / "gatgru" / "graph_metadata.json"
    training_meta_path = artifact_dir / "gatgru_selected_training_meta.json"
    golden_path = artifact_dir / "gatgru_selected_predictions_test.npz"

    training_meta = json.loads(training_meta_path.read_text(encoding="utf-8"))
    graph_meta = json.loads(graph_meta_path.read_text(encoding="utf-8"))
    runs, raw_meta = fd.load_runs_npz(raw_path)
    splits = raw_meta["splits"]
    split_audit = fd.validate_splits(splits, runs)
    if split_audit["dataset_fingerprint"] != training_meta["data_contract"]["dataset_fingerprint"]:
        raise AssertionError("Dataset fingerprint differs from training metadata")
    if split_audit["split_fingerprint"] != training_meta["data_contract"]["split_fingerprint"]:
        raise AssertionError("Split fingerprint differs from training metadata")

    raw_windows = fd.make_windows(
        runs=runs,
        splits=splits,
        split="test",
        window=int(training_meta["config"]["window"]),
        horizon=int(training_meta["data_contract"]["horizon"]),
        node_feats=training_meta["node_features"],
        edge_feats=training_meta["edge_features"],
        scalers=None,
    )
    with np.load(golden_path, allow_pickle=False) as stored:
        golden = stored["Y_pred"]
        golden_true = stored["Y_true"]
        golden_last = stored["Y_last"]
        golden_info = stored["info"]

    if normalize_info(raw_windows["info"]) != normalize_info(golden_info):
        raise AssertionError("Rebuilt and stored sample identities differ")
    np.testing.assert_array_equal(raw_windows["Y_raw"], golden_true)
    np.testing.assert_array_equal(raw_windows["Y_last"], golden_last)

    results = []
    for seed_index, seed in enumerate(training_meta["seeds"]):
        checkpoint_path = artifact_dir / f"gatgru_selected_model_seed_{seed}.pt"
        predictor = GATGRUPredictor(
            checkpoint_path=checkpoint_path,
            edges_named=graph_meta["edges"],
            device="cpu",
        )
        current = predictor.predict(raw_windows["X"], raw_windows["E"])
        expected = golden[seed_index]
        close = np.isclose(current, expected, rtol=RTOL, atol=ATOL)
        result = {
            "seed": int(seed),
            "checkpoint": checkpoint_path.name,
            "checkpoint_sha256": sha256(checkpoint_path),
            "strict_load": True,
            "allclose": bool(close.all()),
            "max_abs_diff": float(np.max(np.abs(current - expected))),
            "mean_abs_diff": float(np.mean(np.abs(current - expected))),
            "mismatch_count": int(np.count_nonzero(~close)),
        }
        results.append(result)
        if not result["allclose"]:
            raise AssertionError(f"Golden-output mismatch for seed {seed}: {result}")

    if write_fixtures:
        np.savez_compressed(
            artifact_dir / "reference_input.npz",
            X_raw=raw_windows["X"][:1],
            E_raw=raw_windows["E"][:1],
            info=np.asarray(normalize_info(raw_windows["info"][:1]), dtype=str),
        )
        np.savez_compressed(
            artifact_dir / "reference_output.npz",
            Y_pred=golden[:, :1],
            Y_true=golden_true[:1],
            Y_last=golden_last[:1],
            seeds=np.asarray(training_meta["seeds"], dtype=np.int64),
        )

    return {
        "status": "PASSED",
        "test_windows": int(len(raw_windows["X"])),
        "input_shape": list(raw_windows["X"].shape),
        "edge_shape": list(raw_windows["E"].shape),
        "golden_shape": list(golden.shape),
        "info_identity": "equal_after_normalizing_timestep_to_int",
        "rtol": RTOL,
        "atol": ATOL,
        "golden_sha256": sha256(golden_path),
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--write-fixtures", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.repo_root, args.write_fixtures), indent=2))


if __name__ == "__main__":
    main()
