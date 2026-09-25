# Development setup

How to set up a dev environment for contributing to the HIL bench,
and the repo conventions to follow.

---

## Working on a laptop without bench access

Nearly everything except actual on-bench verification can be done
off the Pi. Python tooling runs natively on macOS and Linux; only
the hardware drivers (`RPi.GPIO`, `spidev`) need the Pi. The broker
ships a full fake backend so broker RPC work and test development
happen entirely off-bench.

```sh
$ git clone https://github.com/isc-fs/IFS_HIL.git
$ cd IFS_HIL
$ python3 -m venv .venv
$ . .venv/bin/activate
$ pip install -e .
```

The hardware drivers live in a separate `[bench]` extra and are
installed only on the Pi. `spidev` compiles against
`<linux/spi/spidev.h>` and `RPi.GPIO` against `<sys/epoll.h>`, so
neither builds on macOS — and because a single failed build aborts the
whole install, keeping them out of the core set is what makes
`pip install -e .` succeed on a laptop at all.

`tools/__init__.py` wraps its driver re-exports in a
`try/except ImportError`, so the package imports cleanly without them,
and the broker's fake backend plus the unit tests need none of them.

On the bench itself, add the extra:

```sh
pi$ pip install -e '.[bench]'
```

```sh
$ python3 -m pytest tests/broker/ -v
# 15 passed in ...
```

HIL tests under `tests/hil/` need a reachable broker socket —
they auto-skip if the broker isn't there. Running them off-bench
is a no-op (everything skipped); real validation happens on the
Pi. To exercise a bench self-test against the fake backend instead:

```sh
$ python3 -m broker.server --fake --socket /tmp/hil-broker.sock &
$ HIL_BROKER_SOCKET=/tmp/hil-broker.sock python3 -m pytest tests/hil/test_i2c.py -v
```

### Editing against a live bench

`~/IFS_HIL` on the bench is updated with rsync from your
workstation, not with git — never `git pull` there (only a brand-new
bench's first install clones the repo). From the repo root on your
workstation, pick the bench (addresses are in `CLAUDE.md`, *Bench
hosts*) and sync with the script:

```sh
$ export HIL_BENCH_HOST=isc@<bench-ip>
$ scripts/sync_to_pi.sh              # rsync, no --delete
$ ssh "$HIL_BENCH_HOST" 'sudo systemctl restart hil-broker &&
    cd ~/IFS_HIL && python3 -m pytest tests/broker/ -q'
```

Use your own SSH key (`ssh-copy-id "$HIL_BENCH_HOST"`), not a shared
password. The sync never deletes, so a file you rename or remove
lingers on the bench until you clean it up there.

---

## Repo conventions

### Branching

`main` is the release branch. Feature work lands on `dev` first,
then `dev` merges to `main` when a release is cut.

Branch names follow `<type>/<kebab-slug>`:

- `feat/...` — new functionality.
- `fix/...` — bug fixes.
- `docs/...` — documentation changes (like this one).
- `chore/...`, `refactor/...`, `test/...` — sparingly, only if the
  change genuinely doesn't fit the first three.

Always branch **from `dev`**, not `main`. PRs target `dev`.

### Commits

Conventional-Commits-ish prefixes, split by concern:

- One commit per distinct concern. Don't bundle a kernel-module
  rebuild + a test-file update + a new doc — three commits.
- First line ≤ 72 chars, imperative mood, prefix the area:
  `feat(broker): …`, `docs: …`, `fix(mcp251x-patched): …`.
- Wrap body prose at 72. Explain the *why*, not just the *what*.
- **No AI co-author lines.** Keep the authorship clean.
- `Co-Authored-By:` for genuine paired work, not for tool use.

### Pull requests

- PR body has **Summary** and **Test plan** sections at a
  minimum. Test plan uses `- [ ]` / `- [x]` checkboxes for
  individual things to verify. Mark the done ones on-bench
  before requesting review.
- Keep PRs small and thematic. Four small reviewable PRs are
  better than one large one.
- Draft PRs are first-class — open a draft early when something
  is blocked or needs review on a specific question.
- Delete the branch on merge.

### Release process

- `dev` → `main` via a merge PR once a batch of work is ready and
  on-bench verified.
- Tag the merge commit on `main` with the release name (e.g.
  `v0.4.0`) if a released binary or shipped artefact is involved.
- For `can-flasher` releases, the tag on the upstream repo
  drives the release CI; see
  [isc-fs/MingoCAN](https://github.com/isc-fs/MingoCAN)'s
  `release.yml`.

---

## Local pre-commit checks

No hooks. CI's `host-tests.yml` runs a whole-tree collection plus
every suite outside `tests/hil/` — but only on PRs that touch
`tests/`, `tools/`, `configs/`, `conftest.py` or `pyproject.toml`
(and on pushes to `dev`); a change confined to `broker/` or
`dashboard/` does not trigger it. `bench-inventory.yml` validates the
bench descriptors when `configs/benches/` or `tools/bench.py` changes.
Run the same checks by hand before pushing:

```sh
$ python3 -m pytest tests/ --collect-only -q        # every test module still imports
$ python3 -m pytest tests/ --ignore=tests/hil -q    # host suites, incl. tests/broker/
$ python3 -c "from broker import server, bus, rpc, fake_bus; print('imports ok')"
$ python3 -c "from tools import hil_client, hw_config; print('imports ok')"
```

For on-bench verification — the bench self-tests only; `tests/hil/vcu/`
and `tests/hil/ams/` drive carriers and can reflash them (see
[`testing.md`](testing.md)):

```sh
pi$ pytest tests/hil/ --ignore=tests/hil/vcu --ignore=tests/hil/ams -v
pi$ curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/status
```

---

## Filesystem layout quick reference

```
broker/        daemon source
dashboard/     Flask UI (+ CAN trace)
tools/         register-level chip drivers + hil_client proxies,
               hw_config.py (pin/address source of truth), bench.py
               (fleet CLI), flash_dut.py (what CI flashes with);
               flash.py and mcp2515.py are legacy
tests/broker/  unit tests (fake backend, off-bench safe)
tests/*.py     host-only guards (descriptors, suites, flash_dut, …)
tests/hil/     on-bench: top-level = bench self-tests;
               vcu/ (the ECU) + ams/ = DUT suites
infra/
  devicetree/             SPI overlay (3× MCP2515 + kernel-owned CS)
  kernel-module/          out-of-tree mcp251x patches + build script
  systemd/                unit files + the runner restart drop-in
  sudoers.d/              narrow privilege drop-ins
  udev/                   stable device naming
docs/          every documentation file
configs/       benches/ (bench descriptors), firmware/ (CI build
               recipes), suites.yaml (named suites); ecu_*.yaml legacy
docker/        firmware build image of the dead /hil-build chain
scripts/       bench_setup.sh (new bench, start here), sync_to_pi.sh,
               build_stm32_binaries.sh
.github/workflows/  hil-test.yml + hil-fw-build.yml (firmware-PR
               chain), host-tests.yml, bench-inventory.yml;
               hil-build-*.yml / hil-flash.yml are the dead legacy chain
```

For deeper detail see the README's repo layout section and the
individual subfolder READMEs.

---

## Read these next

- [`testing.md`](testing.md) — how the test suites are
  structured and how to add tests.
- [`kernel-module.md`](kernel-module.md) — iterating on
  `mcp251x-patched`.
- [`../design/broker-migration.md`](../design/broker-migration.md) —
  the architectural intent.
