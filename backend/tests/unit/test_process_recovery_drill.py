import os

from scripts.verify_local_integration_recovery import verify


def test_numeric_runtime_threads_are_bounded_for_subprocess_drills():
    assert os.environ["OPENBLAS_NUM_THREADS"] == "1"
    assert os.environ["OMP_NUM_THREADS"] == "1"
    assert os.environ["MKL_NUM_THREADS"] == "1"


def test_actual_process_exit_preserves_queue_and_human_checkpoint():
    report = verify()
    assert report["different_processes"]
    assert report["model"] == "stub"
    assert all(report["checks"].values())
    assert report["checks"]["resumed_from_checkpoint"]
    assert not report["production_ready"]
