"""Guards on the verdict comment posted back to a firmware PR.

A bare red tick plus a link means opening Actions, finding the job and
scrolling past 500 lines of candump to learn which case failed. Developers
re-run it or stop reading it. The comment carries the failures itself.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WF = yaml.safe_load((ROOT / ".github" / "workflows" / "hil-test.yml").read_text())
STEPS = WF["jobs"]["test"]["steps"]


def _step(name):
    return [s for s in STEPS if s.get("name") == name][0]


def _run_summariser(tmp_path, junit_xml):
    (tmp_path / "artifacts").mkdir()
    if junit_xml is not None:
        (tmp_path / "artifacts" / "junit.xml").write_text(junit_xml)
    script = tmp_path / "run.sh"
    script.write_text(_step("Summarise the run")["run"])
    subprocess.run(["bash", str(script)], cwd=tmp_path, check=True,
                   capture_output=True)
    return (tmp_path / "artifacts" / "summary.md").read_text()


ONE_FAILURE = textwrap.dedent("""\
    <testsuites><testsuite tests="4" failures="1" errors="0" skipped="0">
      <testcase classname="c" name="test_c001"/>
      <testcase classname="c" name="test_c002"/>
      <testcase classname="c" name="test_c004">
        <failure message="AssertionError: FSM reached WAIT_START_BRAKE, not ACTIVE"/>
      </testcase>
      <testcase classname="c" name="test_c005"/>
    </testsuite></testsuites>
""")


def test_the_summary_names_the_failed_case_and_why(tmp_path):
    out = _run_summariser(tmp_path, ONE_FAILURE)
    assert "test_c004" in out
    assert "WAIT_START_BRAKE" in out
    assert "3 passed, 1 failed, 0 skipped" in out


def test_passing_cases_are_not_listed(tmp_path):
    """Only failures earn a row; a green run should stay short."""
    out = _run_summariser(tmp_path, ONE_FAILURE)
    assert "test_c001" not in out and "test_c005" not in out


def test_a_run_that_died_before_pytest_produces_no_table(tmp_path):
    """A failed preflight or flash leaves no junit.xml. The comment must still
    post -- with the counts omitted rather than the step erroring."""
    assert _run_summariser(tmp_path, None) == ""


def test_a_long_failure_list_is_truncated(tmp_path):
    """GitHub caps a comment at 65536 chars, and nobody reads 60 rows anyway."""
    cases = "".join(
        f'<testcase classname="c" name="t{i:02d}"><failure message="boom {i}"/></testcase>'
        for i in range(20))
    out = _run_summariser(
        tmp_path,
        f'<testsuites><testsuite tests="20" failures="20" errors="0" skipped="0">{cases}</testsuite></testsuites>')
    assert "and 5 more" in out
    assert out.count("| `t") == 15


def test_errors_count_as_failures(tmp_path):
    """A collection error is not a pass. It must show, or a broken import reads
    as a green run with fewer tests."""
    out = _run_summariser(
        tmp_path,
        '<testsuites><testsuite tests="1" failures="0" errors="1" skipped="0">'
        '<testcase classname="c" name="t_broken"><error message="ImportError: no module named foo"/></testcase>'
        '</testsuite></testsuites>')
    assert "t_broken" in out and "ImportError" in out
    assert "1 failed" in out


def test_the_comment_includes_the_summary():
    body = _step("Comment result")["with"]["script"]
    assert "summary.md" in body
    assert "summary," in body, "the summary must be spliced into the comment body"
