# 0017. CI: a lockfile runs the groups of its cache, and a file in no list runs each group

- Status: Accepted
- Date: 2026-11-01

## Context

[ADR 0016](0016-ci-runs-by-the-files-of-a-change-and-main-trusts-a-proof-by-tree.md) split CI into four groups, each with its own list of files, and added a gate on main. After the merge of #125, three checks found problems that the tests did not find:

- **A run of the gate with the real GitHub API.** One question got a made-up answer: the last green run of `main.yml` was at `9b55560`. The gate found the green pull request runs of #125 by their tree id. But it also ran Web and the example site, because `apps/api/pyproject.toml` changed. The rule of ADR 0016 ran each group with a cache after any lockfile change, and the pnpm cache of those groups does not depend on that file. The mistake fails open: it costs about 5 billed minutes for each such push, and it hides no failure.
- **An audit of the lists**, by one agent for each group and one agent that tried to refute each finding, found two files that a job reads and that no list selects:
  - `.gitignore`. Ruff and the build of the package (hatchling) skip each file that the `.gitignore` of the root names. With one line `db/` in it, ruff checked 0 of the 6 files of `src/unshelve/db/` and exited with 0.
  - `exports/README.md`. The drill of the Stack copies a made-up export into `exports/`. Only this file keeps the folder in a clone. Without the folder, Docker makes it for root when it mounts it, and the copy fails.
- **The same audit found files that do not exist yet**, but that a tool reads when they appear: `.npmrc` and `.pnpmfile.cjs` (pnpm, in each install), `uv.toml` or a `pyproject.toml` at the root (uv), `.gitattributes` (the line endings of the checkout) and `compose.override.yaml` (Docker Compose). A list names only files that exist, and a test checks this. So a pull request that adds one of these files starts no run.

## Decision

- **A file that keys a cache runs only the groups that restore that cache**, also after a proof. The gate reads the cache of each group from its steps: the files of uv key the cache of `setup-uv` (unless `enable-cache: false`), and `pnpm-lock.yaml` keys the cache of `setup-node` with `cache: pnpm`. This replaces the rule for lockfiles of ADR 0016.
- **The list of the API names `.gitignore`, and the list of the Stack names `exports/**`.**
- **A file in no list runs each group on main**, unless the group has a proof on that tree. `NOT_READ_BY_CI` in the gate names the files that no job reads: `docs/**`, `README.md`, `CONTRIBUTING.md`, `LICENSE`, `.editorconfig` and `.pre-commit-config.yaml`. A changed file that no list selects, and that is not in `NOT_READ_BY_CI`, is a doubt. This adds one doubt to the doubts of ADR 0016.
- **A test checks that each tracked file is in a list or in `NOT_READ_BY_CI`.**

## Consequences

- A pull request that changes only a file in no list still runs no group, because the `paths` of GitHub cannot say "each other file". The run on main after the merge runs each group, and the test of the files names the new file.
- A note, an ADR or the README still starts no run. If a job starts to read one of them, the file must move to a list. The table of reads in `test_ci_workflows.py` is the guard for that, as before.
- The replay of October gives the same minutes with the new rule for lockfiles: no push of October changed a file of uv together with a file of Web. With the measured 4 s of actionlint, the replay gives 943 minutes instead of 1,687.
- The rest of ADR 0016 stays as it is.

## Alternatives considered

- **Name the files that do not exist yet in each list.** Each tool has its own files, so that list has no end, and the test that each entry exists would need exceptions.
- **Run each group for a file in no list, also with a proof.** A green run of the group on the same tree tested that file too, so the run adds minutes and no check.
- **`paths: ["**", "!docs/**", ...]` in each group.** Then each change of code runs each group on a pull request, and most of the saving of ADR 0016 is lost.
