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
$ git clone https://github.com/isc-fs/IFS08_HIL.git
$ cd IFS08_HIL
$ python3 -m venv .venv
$ . .venv/bin/activate
$ pip install -e '.[dev]' 2>/dev/null || pip install -e .
$ pip install pytest pytest-html
```

`RPi.GPIO` / `spidev` / `smbus2` will fail to install on non-Pi
hosts — that's fine. `tools/__init__.py` wraps its driver re-exports
in a `try/except ImportError` so the package imports cleanly
without them. The broker's fake backend + unit tests work without
any of the Pi-only packages.

```sh
$ python3 -m pytest tests/broker/ -v
# 15 passed in ...
```

HIL tests under `tests/hil/` need a reachable broker socket —
they auto-skip if the broker isn't there. Running them off-bench
is a no-op (everything skipped); real validation happens on the
Pi.

### Editing against a live bench

If your workstation can SSH into a running bench, a typical inner
loop is:

```sh
# from the workstation:
$ rsync -avz --delete broker/ tools/ dashboard/ tests/ \
        isc@<pi-ip>:/home/isc/IFS08_HIL/
$ sshpass -p isc ssh isc@<pi-ip> "
    sudo systemctl restart hil-broker &&
    pytest /home/isc/IFS08_HIL/tests/broker/ -q"
```

or just commit + push + `git pull` on the Pi.

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
  [isc-fs/can-flasher](https://github.com/isc-fs/can-flasher)'s
  `release.yml`.

---

## Local pre-commit checks

Nothing enforced yet. Recommended manual checks before pushing:

```sh
$ python3 -m pytest tests/broker/              # unit tests
$ python3 -c "from broker import server, bus, rpc, fake_bus; print('imports ok')"
$ python3 -c "from tools import hil_client, hw_config; print('imports ok')"
$ python3 -c "import json; json.load(open('dashboard/index.html' if False else 'pyproject.toml'))" 2>/dev/null || true
```

For on-bench verification:

```sh
pi$ pytest tests/hil/ -v
pi$ curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/status
```

---

## Filesystem layout quick reference

```
broker/        daemon source
dashboard/     Flask UI
tools/         register-level chip drivers + hil_client proxies
tests/broker/  unit tests (fake backend, off-bench safe)
tests/hil/    HIL tests (need the bench)
infra/
  devicetree/             custom DT overlay
  kernel-module/          out-of-tree mcp251x patches + build script
  systemd/                unit files
  sudoers.d/              narrow privilege drop-ins
  udev/                   stable device naming
docs/          every documentation file
configs/       per-ECU YAML
docker/        firmware build toolchain
scripts/       launch.sh, build_stm32_binaries.sh
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
