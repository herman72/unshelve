# 43. Publishing to a git repository

> Pull requests: [#110](https://github.com/herman72/unshelve/pull/110) (the target), [#140](https://github.com/herman72/unshelve/pull/140) (the key in the stack, the author) · Code: [`publish/git.py`](../../apps/api/src/unshelve/publish/git.py), [`publish/targets.py`](../../apps/api/src/unshelve/publish/targets.py), [`deploy/compose.yaml`](../../deploy/compose.yaml), [`keys/README.md`](../../keys/README.md) · Tests: [`tests/publish/test_git_target.py`](../../apps/api/tests/publish/test_git_target.py), [`tests/deploy/test_worker_keys.py`](../../apps/api/tests/deploy/test_worker_keys.py)

## In one minute

Your site lives in a git repository. When a new commit arrives, its host builds the site again. So the simplest way to give your site the public feed is a commit.

With `UNSHELVE_PUBLISH_TARGET=git`, the worker commits `feed.json` into that repository and pushes it. A test published the made-up items into a made-up site that already had one commit. `unshelve publish run` reports such a run in its first line, and the repository then shows the two commits:

```text
Published 2 items to /tmp/server/site.git main:public/feed.json at 08c9479f6fd0.

$ git log --format='%h  %an  %s' main
08c9479  Unshelve      Update the public feed (dcda07ac6dfb)
b8ea727  Someone Else  A page of the site

$ git ls-tree -r --name-only main
index.html
public/feed.json
```

(The folder of the test is shortened to `/tmp`.)

The commit of the site stays, and the feed goes on top of it. A second run with the same items makes no commit.

## 1. A working copy that follows the server

The worker keeps its own copy of the repository, in `UNSHELVE_PUBLISH_GIT_DIR`. Each publish runs these steps:

```mermaid
sequenceDiagram
    participant W as Worker (its working copy)
    participant S as Server (the repository of the site)
    W->>S: git fetch the branch
    W->>W: checkout -B the fetched commit, clean
    W->>W: write feed.json; same bytes? then stop
    W->>W: commit
    W->>S: git push
    alt someone pushed after our fetch
        S-->>W: refused (not a fast-forward)
        W->>S: fetch again, and repeat (at most 3 tries)
    else
        S-->>W: accepted
    end
```

**The server is the truth.** Each try starts from the state of the server, not from the state of the copy. A worker that stopped between its commit and its push left a commit that never reached the server; the next try simply starts again from the server, and nothing is lost.

**A comparison from daily life:** a shared whiteboard. Before you write, you look at the board as it is now, not at your photo of it from yesterday.

## 2. When two writers push at once

A push is refused when the server has a commit that the push would leave out. Git calls an accepted push a *fast-forward*: the new commit builds on top of the old one.

This is the race test, step by step:

| Time | The worker | Someone else |
|---|---|---|
| 1 | fetches `main`: no commit yet | |
| 2 | commits `public/feed.json` | |
| 3 | | pushes `about.html` |
| 4 | pushes: **refused**, not a fast-forward | |
| 5 | fetches again: `about.html` is there | |
| 6 | commits the feed on top, and pushes: accepted | |

Both commits are on the server. After 3 refused tries, the publish fails as a fault that can pass, and the job tries again 5 minutes later.

## 3. Same bytes, no commit

The feed bytes are stable ([note 42](42-public-feed.md)): the same items give the same bytes. So the target compares the new bytes with the file of the branch, and an unchanged feed makes no commit. Your site is not built again for nothing.

## 4. Git without the git config of this computer

Each git command runs with `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1` and `GIT_TERMINAL_PROMPT=0`. So the commands do the same on your Mac, in Docker and on CI. No password prompt can wait forever, and no helper of your computer can answer for the worker.

A test proves it. It writes a git config that breaks each commit: it asks for a signature from a program that always fails. The publish still works, because git never reads that config.

**The author comes from the settings.** `UNSHELVE_PUBLISH_GIT_AUTHOR_NAME` and `UNSHELVE_PUBLISH_GIT_AUTHOR_EMAIL` give the author and the committer of each commit. The default is "Unshelve <unshelve@localhost>". A host that builds the site can check this person before it builds:

| The host of the site | A commit of "Unshelve <unshelve@localhost>" | A commit with the email of the owner |
|---|---|---|
| Vercel, Hobby plan, private repository | no deployment: only the owner of the account can deploy | a production deployment |
| A host that builds each commit of the branch | a deployment | a deployment |

GitHub links a commit to an account by its email. Each account has a noreply address, `<id>+<login>@users.noreply.github.com`, so the feed can name the owner and still show no private address. A name of only spaces, or of only the signs `, : ; < > "`, makes git fail with "name consists only of disallowed characters". Git accepts other names, also `123`. That is a lasting fault: the job ends, and the message names the cause.

## 5. The deploy key and the host key

**The deploy key** is an SSH key that GitHub accepts for one repository only, read-only by default, with write access when you allow it. If the worker leaks it, an attacker can write to your site, and to nothing else.

The target gives git one key and no other: `GIT_SSH_COMMAND` runs `ssh -i <key> -o IdentitiesOnly=yes -o BatchMode=yes`. ssh refuses a key that other users can read, and on Linux a key mounted into a container keeps the owner and the mode of the host (see the table below). So the target first makes a private copy of the key (mode 600) in its own folder.

**The host key** proves that the server is GitHub, and not a machine in between:

| | With `UNSHELVE_PUBLISH_GIT_KNOWN_HOSTS` | Without it |
|---|---|---|
| ssh option | `StrictHostKeyChecking=yes` | `StrictHostKeyChecking=accept-new` |
| The first connection | checked against the keys that GitHub publishes | trusts the key that it gets ("trust on first use") |
| A later connection | checked | checked against that first key |

GitHub publishes ready lines for a `known_hosts` file. I recommend the file.

A test checks all of this without a network. A pretend `ssh`, first on the `PATH`, writes down how git called it: `-i …/ssh/deploy_key`, `StrictHostKeyChecking=yes`, `UserKnownHostsFile=` the file of the settings, and the host `git@example.invalid`.

**The key in the stack.** The stack shows the folder `keys/` of the clone to the worker as `/keys`, and the worker can only read it. So the settings are `UNSHELVE_PUBLISH_GIT_SSH_KEY=/keys/deploy_key` and `UNSHELVE_PUBLISH_GIT_KNOWN_HOSTS=/keys/known_hosts`. [keys/README.md](../../keys/README.md) has the steps. Git ignores the keys, and `.dockerignore` keeps them out of each build. The API, the bot and the backup get no mount: only the worker publishes.

The worker runs as the user 10001. ssh-keygen makes the key with the mode 600, so only its owner can read it. Who is the owner in the container?

| The computer | The key on the host | The key in the container | Can the worker read it? |
|---|---|---|---|
| macOS with Docker Desktop | user 501, mode 600 | user 10001, mode 600 | yes |
| Linux | user 1000, mode 600 | user 1000, mode 600 | no: "Permission denied" |
| Linux, after `sudo chown 10001 keys/deploy_key` | user 10001, mode 600 | user 10001, mode 600 | yes |

Docker Desktop shows each shared file as a file of the user in the container. Linux shows the real owner. A key that the worker cannot read is a lasting fault: "cannot read the deploy key /keys/deploy_key: Permission denied".

## 6. No secret in a message

An address can hold a password or a token: `https://someone:TOKEN@host/site.git`. Each message of the target shows `https://***@host/site.git` instead. A test checks that the token is in no error and in no "where". Still, use a deploy key, and keep tokens out of addresses.

## 7. Wrong settings stop only the feed

| The settings | What happens |
|---|---|
| `git`, but no `UNSHELVE_PUBLISH_GIT_URL` | each publish fails with "UNSHELVE_PUBLISH_GIT_URL is not set" |
| a path out of the repository (`../feed.json`, `/etc/x`, `.git/config`) | each publish fails with the reason |
| a repository that does not exist, a refused key | a lasting fault: the job ends |
| a server that does not answer, a race in each try | a fault that can pass: the job tries again later |

In each case, the other jobs of the worker go on: an optional feature cannot stop the capture of your posts.

## 8. The tests

The tests of the target use bare repositories on this computer: no network, no key of yours. They cover each section above: a commit on top of the site, no commit for the same bytes, a new branch, a branch deleted on the server, a new address, the race, two publishes at once, a stale lock of git, the git config of this computer, the author of the settings, the key and the host keys through a pretend `ssh`, and a token in an address. One test of the command line pushes the real feed of the made-up world into a bare repository. The tests of the stack read `docker compose config`: the mount of the keys, its folder, and the production stack.

**Can these tests fail?** I put 28 typical mistakes into the target and its settings, one at a time, and each one made a named test fail. A mistake in a race can be caught by luck, so I ran one of them more often: without the lock, the test of two publishes at once failed in each of 4 runs; with the lock, it passed in each of 5 runs.

## A choice that we made

**The git program, not a Python library or the API of GitHub.**

| | The git program (ours) | A Python library (dulwich) | The contents API of GitHub |
|---|---|---|---|
| The image of the API | +108 MB (git, ssh and Perl) | a smaller Python package | nothing more |
| Each git server | yes | yes | GitHub only |
| Known behaviour | the git that everyone uses | fewer users, more edge cases | one HTTP call for each file |

## What a senior engineer checks here

- **Does a stop between commit and push lose a commit?** No: each try starts from the server.
- **Can a stopped run block the next one?** No: a stale `index.lock` of git is removed, under the lock of the target.
- **Can two workers break the copy?** No: one publish at a time for each working copy (a file lock), and a test runs two at once.
- **Can a secret reach a log?** Not through the target: each message hides the user and the password of an address.
- **Does the key reach only the worker, and only to read?** Yes: a test reads the stack as Compose joins it, also the production stack.
- **What is not done:**
  - no push to GitHub in the tests: that needs a real repository and its deploy key;
  - no check of the key before the first publish: `ssh -T` in the worker (keys/README.md, step 7) shows only that GitHub accepts the key, not that it may write;
  - each change of the feed is one more build of the site (on the Hobby plan of Vercel, at most 100 deployments a day);
  - the image of the API is 108 MB larger;
  - the lock uses `fcntl`, so the worker runs on Linux and macOS, not Windows.

## Try it yourself

After the merge, run `git pull`. To try it without your site, use a bare repository on this computer. In the root of the repository:

```bash
git init --bare --initial-branch=main /tmp/site.git
```

```bash
cd apps/api && UNSHELVE_PUBLISH_TARGET=git UNSHELVE_PUBLISH_GIT_URL=/tmp/site.git UNSHELVE_PUBLISH_GIT_DIR=/tmp/unshelve-publish uv run unshelve publish run
```

```bash
git -C /tmp/site.git log --format='%h %an %s'
```

You have no public item yet, so the feed is empty, but the commit is real.

## Exercise (10 minutes)

1. In `apps/api/src/unshelve/publish/git.py`, change `TRIES = 3` to `TRIES = 1`.
2. Run the tests of the race in `apps/api`:

```bash
uv run pytest tests/publish/test_git_target.py -k race -q
```

Which test fails, and what would you see in the stack after such a race?

<details>
<summary>The answer</summary>

Two tests fail. The race test fails first:

```text
PublishError: the branch main changed during each of 1 tries (…/site.git main:public/feed.json)
```

With one try, the first push that loses a race is the end of the publish. The second test fails too, because it expects 3 tries in its message.

In the stack, the job of the feed would fail as a fault that can pass. The worker tries it again 5 minutes later, so your site gets the feed 5 minutes late. With 3 tries, a race costs only one more fetch and push.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Bare repository | A repository with no working files, as on a server |
| Working copy | The files of one commit, which you can change and commit |
| Fast-forward | A push whose new commit builds on top of the old one |
| Deploy key | An SSH key that a server accepts for one repository only |
| Host key | The key that proves which server you talk to |
| Trust on first use | Accept the key of the first connection, and check the later ones against it |
| Noreply address | An email address that GitHub gives each account, so that a commit can name the account and show no private address |

## To read more

- [git push](https://git-scm.com/docs/git-push), git documentation. Read "NOTE ABOUT FAST-FORWARDS": why the server refuses a push that would leave out a commit.
- [Managing deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys), GitHub documentation. Read "Deploy keys": one repository, read-only by default, write access when you allow it.
- [GitHub's SSH key fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints), GitHub documentation. It has the lines for a `known_hosts` file.
- [Why aren't commits triggering deployments on Vercel?](https://vercel.com/kb/guide/why-aren-t-commits-triggering-deployments-on-vercel), Vercel Knowledge Base. Read "The commit author has to clear the access boundary": the rules for a private repository and for the Hobby plan.

Previous: [42. The public feed](42-public-feed.md) · Next: [44. Publishing to object storage](44-object-storage-target.md) · Back to the [list of notes](README.md)
