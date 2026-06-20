"""Baseline smoke test so the repo's pytest suite is green from commit 1 (the floor runs the whole
target-repo suite; a repo with zero tests would collect-error)."""


def test_repo_is_alive():
    assert True
