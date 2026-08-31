"""Guards on the artifact-fallback path in hil-test.yml / hil-fw-build.yml.

The bench can rebuild firmware itself when the cloud artifact never reaches
storage (the org is on the free plan and a full quota fails the upload while
the build itself is fine). That path is load-bearing and easy to break in ways
that are invisible until a bench run either does nothing or does the wrong
thing, so the shape of it is asserted here rather than discovered on hardware.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _steps(workflow, job):
    d = yaml.safe_load((WF / workflow).read_text())
    return d["jobs"][job]["steps"], d


def _named(steps, prefix):
    return [s for s in steps if s.get("name", "").startswith(prefix)]


def test_upload_failure_is_not_reported_as_a_build_failure():
    """A failed upload must leave the job green: the firmware compiled and
    passed the layout gate, so the bench can still rebuild it. Only a failed
    COMPILE may stop the run."""
    steps, _ = _steps("hil-fw-build.yml", "build")
    up = _named(steps, "Upload")[0]
    assert up.get("continue-on-error") is True
    assert up.get("id") == "upload"


def test_build_reports_whether_the_artifact_actually_landed():
    _, d = _steps("hil-fw-build.yml", "build")
    assert "uploaded" in d["jobs"]["build"]["outputs"]
    assert "uploaded" in d[True]["workflow_call"]["outputs"]


def test_download_only_runs_when_the_artifact_exists():
    """Downloading an artifact that was never uploaded fails the run for a
    reason that has nothing to do with the firmware."""
    steps, _ = _steps("hil-test.yml", "test")
    dl = _named(steps, "Fetch the built firmware")[0]
    assert "uploaded == 'true'" in dl["if"]


def test_a_fallback_exists_and_is_the_exact_complement_of_the_download():
    """No gap and no overlap: exactly one of download / rebuild runs."""
    steps, _ = _steps("hil-test.yml", "test")
    fb = _named(steps, "Fallback")[0]
    assert "uploaded != 'true'" in fb["if"]
    assert "needs.build.result == 'success'" in fb["if"]


def test_bench_build_refuses_unreviewed_commits_by_default():
    """Building runs the commit's own CMakeLists next to the broker socket and
    the hardware groups. hil-fw-build.yml documents that firmware is never
    built on the bench; the fallback narrows that to reviewed code only."""
    steps, _ = _steps("hil-test.yml", "test")
    run = _named(steps, "Fallback")[0]["run"]
    assert "merge-base --is-ancestor" in run
    assert "Refusing to build" in run
    assert 'ALLOW" != "true"' in run


def test_the_fallback_applies_the_same_repo_pin_as_the_cloud_build():
    """The recipe pins the repo. Without this check the fallback would be a way
    to build a repo the DUT is not pinned to."""
    steps, _ = _steps("hil-test.yml", "test")
    run = _named(steps, "Fallback")[0]["run"]
    assert '"$RCP_REPO" != "$FW_REPO"' in run


def test_the_fallback_still_runs_the_layout_gate():
    """The image goes to a real carrier's app slot. Skipping the gate here
    would make the fallback the one path that can overwrite the bootloader."""
    steps, _ = _steps("hil-test.yml", "test")
    run = _named(steps, "Fallback")[0]["run"]
    assert "RCP_LAYOUT_CHECK" in run


def test_flash_step_does_not_override_the_image_it_was_given():
    """FW_SHA256 arrives via GITHUB_ENV from whichever path built the image. A
    step-level env of the same name would take precedence and pin the check to
    the CLOUD build's sha256, which a bench rebuild does not share -- so every
    fallback flash would fail its own integrity check."""
    steps, _ = _steps("hil-test.yml", "test")
    env = _named(steps, "Flash and run suite")[0].get("env", {})
    assert "FW_SHA256" not in env
    assert "FW_IMAGE" not in env


def test_missing_firmware_fails_instead_of_testing_the_wrong_image():
    """If firmware was under test but no image reached the bench, running the
    suite anyway would judge whatever the carrier happens to hold and report it
    as a verdict on the requested commit -- a false green."""
    steps, _ = _steps("hil-test.yml", "test")
    run = _named(steps, "Flash and run suite")[0]["run"]
    assert "WANTED_FW" in run
    assert "was under test but no image reached the bench" in run
