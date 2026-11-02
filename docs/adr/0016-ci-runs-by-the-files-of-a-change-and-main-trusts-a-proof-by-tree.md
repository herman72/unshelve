# 0016. CI runs by the files of a change, and main trusts a proof by tree

- Status: Accepted
- Date: 2026-11-01

## Context

The repository is private, so each minute of GitHub Actions counts against the 2,000 free minutes of the account each month. In October 2026, Unshelve used 1,687 minutes, and two other projects of the owner used 1,848 and 54. CI stopped at the limit.

Three facts of the October runs shaped the decision:

- **Each pull request ran all eight jobs on each push.** A change of a note ran the stack jobs, which build two images and start the whole stack.
- **Each push to main ran all eight jobs again.** The owner merges by rebase, and a branch usually contains main already. Then the new commits on main have the same tree as the head of the pull request, and 61 of the 70 pushes to main tested a tree that a green pull request run had tested. Those pushes cost 565 minutes.
- **GitHub rounds each job up to a whole minute.** Two short jobs bill more than one job that does both. The contract check (42 s) and the `next dev` check ran as their own jobs.

The repository has no branch protection, so a red run does not stop a merge. The owner reads the runs.

## Decision

- **Four groups, each with its own workflow and its own list of files**: API (`api.yml`), Web (`web.yml`), Example site (`site.yml`) and Stack (`stack.yml`). A pull request runs a group only when it changes a file of the group's `paths`. A list names only a file or a folder and `/**`.
- **A test keeps each list complete.** `test_ci_workflows.py` names the files outside each app that the jobs of a group read, and fails when the list of the group does not select one of them.
- **Fewer, longer jobs.** The contract check and the lint of the workflows run at the end of the API job, and `next dev` runs at the end of the end-to-end job. A step after a failed step still runs when its own setup worked, so one run still reports each failure.
- **A gate on main.** `main.yml` runs on each push to main. Its first job runs `.github/scripts/ci_gate.py`, and then calls each group as a reusable workflow, unless the gate said `false` for it. The gate compares HEAD with the commit of the last green run of `main.yml`. A group with no changed file skips. A group with a changed file skips when a green `pull_request` run of the group has the same tree id as HEAD.
- **A lockfile change runs the groups that restore a cache**, also after a proof: `setup-uv` and `setup-node` save a cache only in a run on main, and each new pull request restores the cache of main.
- **Each doubt runs the group**: a manual run, no green run of `main.yml`, a green commit that the clone does not have, a failed call of the API, or a failed gate (an empty output is not `false`).
- **Older runs of a pull request are cancelled.** A run on main is never cancelled while it runs.

## Consequences

- A replay of the October runs with these rules gives 951 minutes instead of 1,687 (44% less). This is an estimate: the lint step of the API job is not measured yet, and the replay used the full diff of each stacked pull request.
- A pull request that changes only prose (notes, ADRs, the README) starts no run.
- A skip on main can miss drift between the pull request run and the merge: a new runner image, a new version of an action, or a test that fails only sometimes. A manual run of Main checks all groups.
- A file that a job reads, but that no list names, lets a pull request break the job without a run. The test of the lists guards the files that the jobs read today. A new read needs a new line in the test and in the list.
- The first run of `main.yml` has no green run to compare with, so it runs each group.
- A diff of more than 3,000 files can skip a pull request run (a limit of GitHub). The gate of main reads the full diff with git, so main still runs the group.
- The gate needs `actions: read` to read the runs of the groups. No other job gets more than `contents: read`.
- Added on 2026-11-02, before the repository becomes public: a proof counts only when the pull request run comes from a branch of this repository (`head_repository`), and the base of the diff is the last green `push` run of `main.yml`. A pull request from a fork controls its own run, so its green run proves nothing.
- Added on 2026-11-02, when the repository became public: GitHub bills no minutes for standard runners in a public repository, so the monthly limit no longer stops CI. The groups and the gate stay. A pull request gets its answer sooner, and the minutes stay low if the repository becomes private again.

## Alternatives considered

- **No run on main.** It costs 0 minutes on main, but 9 trees on main in October had no green pull request run. With no branch protection, nothing would test them.
- **Skip a push only when its commit SHA has a green run.** Rebase merges give new SHAs, so this skips almost nothing. The tree id is the same for the same files.
- **A self-hosted runner on the laptop.** It costs no minutes. But the stack jobs run `docker compose down --volumes` for the project `unshelve`, and on the computer of the real stack this deletes the real database.
- **A public repository.** Standard runners are free for public repositories. On the day of this decision, the repository was private. It became public a day later (see the last consequence).
