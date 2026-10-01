"""
Rootdir conftest — exists to register the AMS HIL KPI plugin.

`pytest_plugins` is only honoured in the conftest at the rootdir; pytest
hard-errors ("Defining 'pytest_plugins' in a non-top-level conftest is no
longer supported") when it appears in a nested one. It used to live in
`tests/hil/ams/conftest.py`, where it worked only by accident: that conftest
is loaded before `pytest_configure` when the AMS directory is named on the
command line, and the check is gated on being configured. Any whole-tree
run (`pytest tests/`) loads it during collection instead, and failed.

Registering here means the plugin — and its `--no-kpi` / `--kpi-dir` /
`--bench-name` options — is available for every invocation, not just AMS
ones. See `tests/hil/ams/kpi_plugin.py` and
`docs/ams-hil/test-plan-v1.5.0.md` §4.
"""

pytest_plugins = ["tests.hil.ams.kpi_plugin"]
