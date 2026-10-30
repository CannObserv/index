# Dependabot

What the bot proposes here, and how one of its PRs lands (#23). Config:
[.github/dependabot.yml](../.github/dependabot.yml). Guards:
[tests/ci/test_dependabot.py](../tests/ci/test_dependabot.py) (the
`adopting-dependabot` skill's asset, edited only in its ADAPT block) and
[test_dependabot_cooldown.py](../tests/ci/test_dependabot_cooldown.py).

## What's on

| Ecosystem | Moves | PR shape |
|---|---|---|
| `github-actions` | `uses:` tags in `.github/workflows/` | one PR per action, weekly, 7-day cooldown |
| `uv` | the `dev` group in `pyproject.toml` / `uv.lock` | one `dev-tools` PR, weekly, 7-day cooldown |
| security updates | any locked package with an advisory | one PR per advisory, **no cooldown** |

**Off, on purpose:** `docker`, because `deploy/ollama-slim`'s digest pin is an
engine change (#11, D19); and `pre-commit`, which has local hooks only. The uv
*binary* CI runs is #25, and `setup-uv` is frozen at `@v7`, which Dependabot
can't see (#26).

Nothing either ecosystem moves is installed on `co-index`. **No landing ever
restarts a service.**

## Landing a PR

`main` is linear, and moves by fast-forward pushes. This repo merges no PRs on
GitHub. A Dependabot PR lands by pushing its head commit, the exact commit CI
tested, to `main`. GitHub then marks the PR merged.

**Never into the deploy checkout's `main`.** The skills `SessionStart` hook
commits on top of that `main` once a UTC day and pushes everything under its
commit. A Dependabot commit fast-forwarded there, and not yet pushed, would go
out with the hook's push without `pre-ship.sh` ever running. So land from a
detached worktree, and push by SHA. Paste it a step at a time; a `STOP` line
means don't go on:

```bash
set -a; . ./.env; set +a
N=<pr>
B=$(gh pr view "$N" --json headRefName -q .headRefName)
pr_head() { gh pr view "$N" --json headRefOid -q .headRefOid; }
git fetch -q origin main "$B"
H=$(pr_head)

# 1. Based on today's main? If not, rebase on GitHub, and WAIT FOR THE NEW HEAD.
#    Reading checks straight after `@dependabot rebase` reads the old commit's green.
if ! git merge-base --is-ancestor origin/main "$H"; then
  gh pr comment "$N" --body "@dependabot rebase"
  for _ in $(seq 60); do sleep 10; [ "$(pr_head)" != "$H" ] && break; done
  [ "$(pr_head)" != "$H" ] || echo "STOP: head never moved; see 'Stuck PRs'"
  H=$(pr_head); git fetch -q origin main "$B"
fi

# 2. CI on that head, both jobs (lint, test). Then make sure it's still that head.
gh pr checks "$N" --watch || echo "STOP: red, or no checks on the new head yet; read again in a minute"
[ "$(pr_head)" = "$H" ] || echo "STOP: head moved under the watch; start again"

# 3. Read the release notes for every version in between, not the diff.

# 4. The ship gate, on the exact commit.
git worktree add -q --detach ".worktrees/dependabot-$N" "$H"
( cd ".worktrees/dependabot-$N" && scripts/pre-ship.sh )

# 5. Fast-forward main to it. The remote refuses a non-fast-forward, so if main
#    moved since step 1 (the hook's daily push, say), go back to step 1.
#    Never rebase locally: that lands a commit CI didn't test.
git push origin "$H:refs/heads/main"
git worktree remove ".worktrees/dependabot-$N"
```

Then read CI on `main` for `$H`, by job. The deploy checkout catches up with
`git pull --ff-only` on `main`, or the next push the hook makes.

- **One at a time.** Each landing puts every other open Dependabot PR behind
  `main`, so expect one rebase per PR.
- **A bump that needs a code change** (a ruff release with a new rule, say):
  commit the fix in the detached worktree on top of `$H`, run `pre-ship.sh`,
  and push the new tip by SHA. The fast-forward still marks the PR merged.
  Pushing to the bot's branch instead stops it rebasing that PR.
- **A major action bump:** compare it with upstream's newest release
  (`inventory.sh --upstream`). A gap is a frozen tag or a fresh major. File the
  gap, and don't hold the PR for it.

## Stuck PRs

A Dependabot PR older than the config's first commit can't be rebased: the bot
answers that its `dependabot.yml` entry was deleted. None was open when the
config landed (#14 and #15 were closed). If one turns up anyway, **don't close
it to get it re-created**: closing records an ignore for that version, and no
replacement opens (CannObserv/address-validator#357). Land the fix by hand
(`uv lock --upgrade-package <pkg>`, through `pre-ship.sh`), then close the PR
with a comment naming that commit.

## Is it working?

```bash
bash skills/adopting-dependabot/scripts/check-dependabot.sh --repo CannObserv/index
```

The check reads the config, PR-limit headroom, the latest update runs, the
dependency graph against `uv.lock`, alerts, and security updates. With this
repo's `GH_TOKEN`, `security-updates` reads `unread`, because the token lacks
Administration: read. Confirm that one in Settings → Code security.
