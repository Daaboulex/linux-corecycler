# Contributing

These hold for everyone working on this project, people and AI assistants alike. This project's own
rules follow at the end.

## Report a bug or ask for something

Open an issue with the bug, request or task form.

## Track work

- An open task is an issue in the repo it belongs to. Nothing else is a task list: no notes file,
  no assistant memory or queue, no session.
- Before opening an issue, search the open ones. What an existing issue covers is added to it: one
  matter is one issue.
- An assistant opens an issue in two cases only: the person it works for asked for something that
  is not finished when work ends, or it verified a defect.
- An idea, a maybe or a question is said once and dropped. It becomes an issue, with `later`, only
  when that person says keep it.
- An issue has one type, which its form sets. Add `later` to an idea nobody owes and `waiting` to
  one blocked on a person, a delivery or a decision. An issue that is not ours to solve, or no
  longer true, is closed with one sentence saying why.
- An issue's goal stays in the words it was opened with. Later work ticks its "Done when" list and
  comments; it does not rewrite the goal.
- Never record state, such as what was exported, what runs where or what was done: that is read at
  its source. Record only what someone must still act on. A one-off, such as a file exported for a
  meeting, is not recorded.
- Before starting work, read the repo's open issues. When work ends, close what is done, and open
  an issue for what was asked and is not finished.

## Make a change

- Read the code the change touches first.
- Run `nix flake check` before you commit. A red `main` is fixed before anything else.
- The commit message says why the change was made. A decision that shapes the project also gets
  one short file in `docs/`.
- A doc says only what the code cannot: a decision, a fact about an outside system, or a trap no
  check catches. It never repeats what the code, a check's message or a command's usage says, and
  never copies state such as counts, versions or what runs where: that is read at its source. A
  wrong line is deleted or fixed when seen.
- An open point is written as the question that is open, never as an errand: no line about a person
  asking, waiting on or confirming something with someone else.
- Nothing that exists only on your own machine goes into the project: no personal names, addresses,
  machines, hardware, paths, tools or habits.
- Never hide an error. Read a result from the check's own exit code.

## Improve these rules

These rules grow with the work. A rule for this project goes under This project below. A rule for
every repo goes to `Daaboulex/nix-packaging-standard` as an issue or a change, and reaches each
repo with the next release.

## This project

The dev shell, the test suite, the code style and the steps for a new backend, CPU or sensor chip
are in [docs/development.md](docs/development.md).

### Workflow

1. Fork the repo and create a feature branch (`feat/my-feature` or `fix/my-bug`)
2. Make your changes
3. Run the full test suite (see [Testing](docs/development.md#testing)) -- coverage must stay at 100%
4. Run linting: `ruff check src/`
5. Run flake check: `nix flake check` (needs Nix)
6. Submit a PR against `main`

### Hardware code

Hardware-touching code (SMU driver, hwmon, topology) has higher review standards:

- **Test with mock sysfs.** Unit tests never need real hardware. Only the Ring B contract
  tests (`-m contract`, above) touch it
- **Range-check all SMU arguments** — CO values, PBO limits
- **Log every SMU write** — debug logging for hardware operations
- **Document the source** — cite ZenStates-Core, kernel driver docs, or AMD documentation
- **Safety: all SMU writes are volatile** — never modify BIOS/UEFI

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```text
feat(hwmon): add NCT6687 Super I/O support
fix(smu): correct slot mapping for harvested cores
docs: update hardware support table
test(tuner): add crash-during-hardening gap test
```

The scope names the area touched -- e.g. `tuner`, `smu`, `gui`, `engine`,
`backends`, `history`, `topology`, `tests`, `ci`, `nix`.

### Review Process

- Maintainer reviews all PRs
- Pre-commit hooks must pass (format, lint, markdown, spelling)
- All tests must pass in CI
- Hardware changes need mock sysfs tests — never "works on my machine"
- Safety-critical code (SMU writes, process management) gets extra scrutiny
