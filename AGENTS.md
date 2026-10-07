# AGENTS.md

Guidance for AI coding assistants working on Kernel Patches Daemon (KPD). For setup and
contribution basics see [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md); this file
adds what maintainers expect from AI-assisted changes.

## What KPD is

KPD reconciles Patchwork with GitHub. In a loop that never ends, it finds the relevant patch
series in Patchwork, keeps one pull request per series version and target branch in sync with
them, reports CI results back to Patchwork as checks, and emails submitters.

- `kernel_patches_daemon/daemon.py`: the loop. Each iteration builds a fresh `GithubSync`, runs
  `sync_patches()` and sleeps.
- `github_sync.py`: one iteration. Refresh repositories and PRs, fetch relevant subjects from
  Patchwork, sync each subject to a target branch, handle PRs whose series dropped out, expire old
  branches and PRs.
- `branch_worker.py`: everything for one target branch. Applying series with `git am`, creating,
  updating and closing PRs, labels, CI results to Patchwork checks, emails, comment forwarding.
- `patchwork.py`: the Patchwork client and the `Series`/`Subject` model.
- `config.py`: the configuration schema; `configs/` has examples.
- `tests/`: `unittest`. Patchwork HTTP is mocked with `aioresponses`, GitHub objects with
  `unittest.mock`. Tests make no network calls.

## Easy to get wrong

These hold at the time of writing; check the code before relying on them.

- **Lifetimes.** `GithubSync` and every `BranchWorker` are recreated on each iteration, so their
  caches (`prs`, `all_prs`, the closed-PR cache, `branches`) live for one iteration. Find out an
  object's lifetime before reasoning about whether its data is stale.
- **PyGithub objects are snapshots.** `pr.labels`, `pr.state` and `pr.head.sha` reflect the last
  fetch. Label API calls do not update `pr.labels`; `pr.update()` refetches.
- **PR identity** is the head branch `series/<id>=><target>`, where `<id>` is the subject's latest
  series. A new version gets a new branch and PR, and the previous version's PR is closed
  (`close_existing_prs_for_series()`).
- **Labels carry state between iterations.** `merge-conflict` means the last apply failed.
  `V<n>-ci-pass`/`V<n>-ci-fail` mean the CI result for that version was reported; emails are
  deduplicated through them. People and CI workflows label PRs too.
- **Comment forwarding** is deduplicated through the `Forwarding comment [<id>]` comments KPD
  posts on the PR.
- **The local git checkout is shared state.** Many methods reset it or check out branches, and
  callers depend on what is checked out afterwards.
- **Target branches.** `tag_to_branch_mapping` maps a series tag to an ordered list of target
  branches; with more than one, KPD falls back to the next branch when a series does not apply and
  sticks to a branch whose PR applies cleanly.
- **Skips cross function boundaries.** `NewPRWithNoChangeException` is raised deep in
  `BranchWorker` and caught in `GithubSync.checkout_and_patch_safe()`; `None` from there means
  "skip this target branch".

## Keep the code easy to understand

Judge a change by what the next maintainer has to hold in their head to modify the code safely,
not by its line count. Each of these is a cost:

- an assumption about code elsewhere that is not stated or checked where it is relied on;
- state that means more than its name says (a label, a comment marker, a sentinel value);
- another writer of the same state;
- a decision or format that has to be kept identical in two places;
- a flag or parameter that selects between behaviours;
- a value that can come from several sources with different freshness;
- another handle (index, cache, alias) for the same thing;
- an exception or sentinel that callers in other functions have to handle;
- a comment, docstring or name that no longer matches the code.

A fix or feature may add some of these when it needs them; a refactor should remove them.
Prefer deleting code to adding it. Splitting a function or moving code removes none of them.
When code relies on something established elsewhere, state it where it is relied on, or better,
make it unnecessary.

## One logical change per commit

- Refactors a change needs go first, as their own commits, with no behaviour change.
- Keep renames, reformatting and unrelated fixes out of functional commits.
- If a commit grows the production code, its message should say why that is worth it.

## Tests: the least code that covers meaningful behaviour

Test code costs review time like any other code.

- List the behaviours the change adds or alters, as "given X, KPD does Y", observed at KPD's
  edges: calls on GitHub, Patchwork, git or email objects, return values, exceptions.
- For each behaviour, break the production code the way a plausible regression would (drop a
  condition, skip a call), check that a test fails, and restore the code. A behaviour no test
  catches needs a test; a new test that only fails together with other tests is redundant.
- A fix comes with a test that fails without it. A refactor adds no tests: adapt existing tests
  to new signatures and delete the tests of deleted code.
- Test through the method where the behaviour is observable, not private helpers. Extend an
  existing test or table (`subTest`) before adding a new test, and reuse the helpers and mocking
  style already in the test class.
- As a budget, aim for fewer than ten added test lines per behaviour.
- Do not test logging or metrics.

## Checks

KPD needs Python 3.11 or later and Poetry. CI runs these, and all must pass before a commit:

```
poetry install
poetry run black --check --diff .
poetry run pyrefly check
poetry run python -m unittest
```

Formatting is black's. Follow the typing and naming style of the module you are editing.

## Commit messages

- Subject: `component: lowercase imperative summary`, at most 72 characters. The component is the
  module (`branch_worker`, `github_sync`, `patchwork`, `daemon`, `config`), `tests` for test-only
  changes, or `kpd` for repository-wide changes (CI, packaging, dependencies).
- The body explains why; the diff shows what. Give the context, the problem and its visible
  impact, why it happens, then the fix, as short separate paragraphs. Leave out what adds
  nothing: the shorter message is usually the better one.
- Use the imperative mood, without "we", "I" or "this patch".
- Write functions as `name()`; put config keys, labels and branch names in backticks.
- Refer to other commits as `abcdef012345 ("subject line")`.
- End a refactor's message with `No functional change.`
- Mention tests in one sentence at most, and only when it adds something.
- Wrap the body at 72 to 75 columns.
- Trailers, in this order: `Assisted-by: <Tool>:<model>`, then `Signed-off-by:`.

## Sign-off

KPD requires the Developer Certificate of Origin (see CONTRIBUTING.md). `Signed-off-by:` is the
human contributor's certification: an AI assistant must never add it. Leave it to the human
(`git commit -s`).

## Working with maintainers

- Verify claims against the code before making them, and say what you checked.
- Do not push, open pull requests or post comments unless the human asks you to.
