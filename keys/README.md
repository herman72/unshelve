# Keys of the stack

The stack shows this folder to the `worker` container as `/keys`, and the container can only read it. The git target of the public feed takes its deploy key and the host keys of the server from here. [Note 43](../docs/learn/43-git-target.md) explains how the target uses them.

Git ignores everything in this folder, except this file, and `docker build` does not send the folder. So a key stays on this computer.

A new clone has no keys, and `git clean -fdx` deletes them. To keep the keys outside the clone, set `UNSHELVE_KEYS_DIR=<an absolute path>` in `.env`. Then make that folder with `mkdir -p` before the first `docker compose up`, and use that path in place of `keys/` in steps 1, 2, 3 and 6. If Docker already made the folder for root, run `sudo chown "$(id -u)"` of that path.

## A deploy key for a repository on GitHub

1. Make a key with no passphrase. The worker cannot type one. In the root of the clone:

   ```bash
   ssh-keygen -t ed25519 -N "" -C "unshelve feed" -f keys/deploy_key
   ```

2. Write the host keys that GitHub publishes. With them, ssh talks only to GitHub. `gh` must be signed in (`gh auth login`):

   ```bash
   gh api meta --jq '.ssh_keys[] | "github.com " + .' > keys/known_hosts
   ```

   Then show their fingerprints, and compare them with [GitHub's SSH key fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints). An empty output means that the file is empty: do step 2 again.

   ```bash
   ssh-keygen -lf keys/known_hosts
   ```

3. Add `keys/deploy_key.pub` to the repository of the site as a deploy key, and select "Allow write access". Use the page of the repository: "Settings", "Deploy keys", "Add deploy key". The command `gh repo deploy-key add keys/deploy_key.pub --repo <owner>/<site> --allow-write --title "unshelve feed"` does the same, but GitHub deletes a key that gh adds when the token of gh is revoked.

4. Set these lines in `.env`. The paths are the paths in the container, not on this computer:

   ```bash
   UNSHELVE_PUBLISH_TARGET=git
   UNSHELVE_PUBLISH_GIT_URL=git@github.com:<owner>/<site>.git
   UNSHELVE_PUBLISH_GIT_SSH_KEY=/keys/deploy_key
   UNSHELVE_PUBLISH_GIT_KNOWN_HOSTS=/keys/known_hosts
   ```

5. Start the stack with the new settings:

   ```bash
   docker compose up -d --wait
   ```

6. Check that the worker can read the key. The worker runs as the user 10001, and the key has the mode 600, so only its owner can read it:

   ```bash
   docker compose exec worker test -r /keys/deploy_key && echo readable
   ```

   On macOS, Docker Desktop shows each file of this folder to the container as a file of that user, so the check passes. On Linux, the container sees the owner of the host, so the check fails. On another system, the check shows the result. If the check prints nothing, give the key to that user, and do the check again:

   ```bash
   sudo chown 10001 keys/deploy_key
   ```

7. Check that GitHub accepts the key, with the same ssh options as the worker. This step pushes nothing:

   ```bash
   docker compose exec worker ssh -i /keys/deploy_key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/keys/known_hosts -T git@github.com
   ```

   GitHub answers `Hi <owner>/<site>! You've successfully authenticated, but GitHub does not provide shell access.`, and ssh ends with the exit code 1. The name of the repository in the answer shows that GitHub knows the key as a deploy key of that repository. "Host key verification failed" means that `keys/known_hosts` is wrong: do step 2 again.

The next publish of an item pushes the feed, or run `docker compose exec worker unshelve publish run`. A push to the branch of the site starts a build of the site. `.env.example` lists the other settings of the target, for example the author of the commits.
