---
# Weekly changelog PR bot (GitHub Agentic Workflow).
#
# This is the gh-aw SOURCE file. After editing, regenerate the compiled
# GitHub Actions workflow with:
#
#     gh aw compile changelog-bot
#
# and commit the generated `.github/workflows/changelog-bot.lock.yml` alongside it.
#
# The shared plumbing (engine, runner setup, gh/node/compose installs, open bash
# tool, Copilot-pinned threat detection, docs push handler) comes from the shared
# component imported below. What stays here is only what differs for this repo:
# the trigger, the runners, the changelog PR's allowed-files, and the prompt body.

on:
  schedule: weekly on saturday around 12:00 UTC
  # Allow maintainers to trigger a run on demand.
  workflow_dispatch:

permissions:
  contents: read
  # The agent must list open pull requests to enforce "never open a second
  # changelog PR". Without this the token is Contents:read only and every
  # list_pull_requests call returns 403 "Resource not accessible by integration".
  pull-requests: read

# Run on the Canonical self-hosted fleet. The agent and threat-detection jobs
# need Docker/AWF and so target the noble/amd64 fleet; the framework and
# safe_outputs jobs use the bare self-hosted label. `runs-on`/`runs-on-slim`
# cannot live in a shared component, so they are declared here.
runs-on: [self-hosted, noble, amd64]
runs-on-slim: self-hosted

imports:
  - canonical/landscape-internal-docs/.github/workflows/shared/changelog-bot-infra.md@d627562cfa37544175061045d201f4304154a62e

# The agent never pushes directly; its file edits are turned into a pull request.
# gh-aw binds the whole safe_outputs job to ONE credential for its git
# checkouts and pushes (github/gh-aw#77580), and the default GITHUB_TOKEN
# cannot write to canonical/landscape-documentation. LANDSCAPE_CHANGELOG_TOKEN
# is a fine-grained PAT with contents/pull-requests read+write on both this
# repo and canonical/landscape-documentation, so it covers the own-repo
# changelog PR and the cross-repo docs push with a single credential.
safe-outputs:
  create-pull-request:
    title-prefix: "build: "
    labels: [changelog, automation]
    team-reviewers: [canonical/landscape]
    draft: false
    if-no-changes: ignore
    # Push with a plain git push rather than the createCommitOnBranch GraphQL
    # path: that path produces commits GitHub does not mark verified for this
    # bot token, and pushSignedCommits then refuses to publish the branch.
    signed-commits: false
    # Exactly one own-repo PR per run; docs edits go through the push handler.
    max: 1
    preserve-branch-name: true
    recreate-ref: true
    # Own-repo PRs only ever edit CHANGELOG.md; the handler rejects any other
    # path outright. (gh-aw allowed-files filters by path only, not diff hunks,
    # so "prepend one dated section, never rewrite existing sections" stays
    # prompt-enforced, not handler-enforced.) Docs release-notes pages go
    # through the push-to-pull-request-branch handler below, never through
    # create-pull-request.
    allowed-files: ["CHANGELOG.md"]
---

# Weekly changelog update

You maintain the changelog for the landscape-task-handler charm. Your job is to draft a new changelog section summarizing recent user-facing changes and open a pull request with it.

## Gather the commits

This charm has no versioned releases in the repository: charm releases are Charmhub revisions assigned at publish time, so changelog sections are dated, not versioned. There are no git tags to anchor against. The changelog lives at `CHANGELOG.md` in the repository root; this bot creates it on its first run that finds user-facing commits.

1. The scan window is time-based: list the commits from the last four weeks with `git log --since="4 weeks ago" --no-merges --pretty=%H%x09%s`, run against the repository's default branch, not the ref this run was dispatched on: `git log --since="4 weeks ago" --no-merges --pretty=%H%x09%s origin/main`. Always use `origin/main` for the range — a `workflow_dispatch` run may check out a bot or feature branch whose own commits are automation churn, and those must never be classified as product changes. If `origin/main` is not present locally, use the checked-out HEAD and say so in the run output. `main` is squash-merged, so each merged PR is a single commit on this list. Do NOT anchor on the last commit that touched `CHANGELOG.md`: a bot changelog PR that is still open (or was merged late) makes that anchor unreliable, silently double-filing or skipping commits. Four weeks against a weekly schedule gives three weeks of overlap with the previous run, so omissions leapfrogged by later merges are still caught; the deduplication rules below prevent double-filing.
2. Skip any commit whose subject has no trailing pull request reference ` (#1234)`. Such commits cannot carry the `(#<N>)` marker the deduplication relies on, so they can never be filed safely; if one looks user-facing, mention it in the PR body under `Changelog entries to review` instead of adding it to the changelog.
3. Deduplicate the rest by pull request number. For each commit, extract its PR number `<N>` from the trailing ` (#1234)` reference in the subject. Match the FULL number wherever you search — `#123` must not match `#1234`. Skip the commit if EITHER of the following holds:
   a. Its own commit documented itself: run `git show --name-only --format= <commit>`; if `CHANGELOG.md` is among the paths, run `git show <commit> -- CHANGELOG.md` and check whether the diff adds a line containing the exact reference `#<N>`. A contributor who wrote their own changelog bullet counts as covered — do not add a second one.
   b. The changelog already covers it: run `git grep -n '#<N>' -- CHANGELOG.md` against the working tree. Every bullet this bot writes ends with ` (#<N>)` (see "Compose the new section"), so any match — a bullet from an earlier bot run or a hand-written entry — means this PR is already covered.

## Select user-facing commits only

Do not decide from memory or by skimming. Work from the exact `git log` output you collected and classify every commit, one line at a time.

First, write out a classification table with one row per commit, in this exact format:

```
| # | type | user-facing? | subject |
|---|------|--------------|---------|
| 1 | feat | yes | add hash-id component to snap |
```

Rules for the `user-facing?` column:

- **yes**: `feat:`, `fix:`, `perf:`, and any breaking change (a `!` before the colon, e.g. `feat!:` / `fix!:`).
- **maybe**: `refactor:` commits, only if they are user-facing.
- **no**: `chore:`, `ci:`, `test:`, `build:`, `docs:`, and `style:` commits, plus anything that only touches tooling, tests, or CI, and any dependency bumps.

Commit subjects in this repo are usually conventional commits, but the type prefix is sometimes capitalized (e.g. `Fix:`) and occasionally missing entirely — match prefixes case-insensitively and use your judgment for non-conforming subjects.

After the table, write the count: `Included: N of M`. `N` is the number of rows marked `yes` (count a `maybe` as included only if you marked it yes). If the table has fewer rows than the number of lines in your `git log` output, you dropped commits — redo the table until every commit appears.

**Only if `N` is 0** may you skip drafting a new changelog section and opening a pull request. In that case do not stay silent: find the open changelog pull request in this repository (label `changelog`) and post a comment on it containing the full classification table and the line `Included: 0 of M`, so a human can see exactly which commits were considered and why nothing was drafted. If no open changelog PR exists, report the table in your run summary instead.

If `N` is 1 or more, you MUST continue and open a pull request. Never stop just because most commits are chores; a single `feat:` or `fix:` means proceed.

Add each included commit's subject line, including its type prefix and any `(LP: #NNNN)` Launchpad references, but excluding any `(scope)` after the type prefix. Proofread and correct any obvious spelling or grammar errors. Remove Jira issue references such as `LNDENG-5678` wherever they occur in the subject. KEEP the trailing GitHub pull request reference ` (#1234)`: it becomes the deduplication marker every changelog bullet must carry (see "Compose the new section"). Do **not** strip `(LP: #NNNN)` references.

Breaking changes are not always flagged in the subject. For every selected commit, also inspect the full commit message with `git show -s --format=%B <sha>` and look for `BREAKING CHANGE:` or `BREAKING-CHANGE:` footers. A commit carrying such a footer is a breaking change even when its subject has no `!` — classify it as breaking and note the breaking nature in the docs island bullet.

## Sort the commits

Group the commits by their type. Adhere to the following order.

- Breaking changes (a `!` in the subject or a `BREAKING CHANGE:`/`BREAKING-CHANGE:` footer)
- `feat`
- `fix`
- `perf`
- `refactor`

Within each group, maintain the relative ordering of the commits.

## Compose the new section

1. The changelog is `CHANGELOG.md` in the repository root. If it does not exist yet, create it with this exact header:

   ```
   # Changelog

   All notable user-facing changes to this charm are documented here, newest first.
   ```

2. Prepend a new dated section directly under the header (newest first), using `date +%F` for the date:

   ```
   ## YYYY-MM-DD

   ### Features
   - <subject>

   ### Fixes
   - <subject>
   ```

   - Section headings, in order: `### Breaking changes`, `### Features`, `### Fixes`, `### Performance`, `### Refactors`.
   - Map commit types to headings: breaking changes (! or footer) -> `### Breaking changes`, `feat` -> `### Features`, `fix` -> `### Fixes`, `perf` -> `### Performance`, `refactor` -> `### Refactors`.
   - Omit any section that has no commits.
   - Each bullet begins with `- `.
   - Every bullet ends with the pull request reference marker ` (#<N>)` naming the PR the entry covers (e.g. `- feat: add support for jammy and resolute bases (#14)`). Later runs rely on this marker to recognize already-covered PRs; a bullet without it will be double-filed by the next run.

## Review existing changelog entries

`CHANGELOG.md` is maintained by this bot, but contributors can also add entries by hand in their own PRs. For each commit in the window that touched `CHANGELOG.md`, read the added lines and flag mismatches. Do NOT edit or delete flagged entries — only list them.

- **Entry on a change with no user impact**: the commit added a changelog bullet, but every other path in `git show --name-only --format= <commit>` is a test (`tests/`), CI/tooling (`.github/`, `tox.ini`, root config files), or docs file.
- **Fix for an unreleased regression**: for a `fix:` commit F that added a changelog bullet, take the hunks F modified outside the changelog (`git show -U0 F -- . ':!CHANGELOG.md'`) and run `git blame --porcelain -L <start>,<end> F^ -- <file>` to find the commits that introduced those lines. If they all fall inside the window, the bug never shipped and the entry should be removed. A PR title or body that references another PR merged in the same window is supporting evidence. If blame results are mixed, mark the entry "possible", not certain.

Exclude flagged entries from the docs release-notes island. If a pull request is opened, list the flagged entries under a `Changelog entries to review` heading in its body, one bullet per PR with the reason and evidence (commit/PR links). If no pull request is opened (nothing to add), report the flagged entries in your run summary instead — do not open a pull request that contains no changes.

## Open the pull request

Before making any edit, create your working branch from the default branch, not from the ref this run was dispatched on: `git checkout -b <branch> origin/main`. The dispatch ref may be a bot or feature branch carrying automation commits; if you commit on top of it, the resulting PR contains all of those commits. The PR must contain ONLY the changelog commit on top of `origin/main`.

First check for an existing open changelog pull request in this repository (label `changelog`). Exactly one should ever be open at a time.

- If one is already open, do NOT open a second. Check whether its changelog section already covers the commits you selected (the listed subjects and their `(#<N>)` markers). If it does, make no changes and note that the existing PR is already current. If it is missing any of your selected commits, post the missing entries as a comment on that PR with `add_comment`, then carry on with the docs release-notes work below — that work is independent of this repo's PR state. No handler in this workflow can push to an existing own-repo PR branch (`push_to_pull_request_branch` only targets the docs repo), so do not claim you updated the PR and do not open a second PR.
- Only if no changelog PR is open, open one with the change. Use a title like `changelog update for <date>` (the `build: ` prefix is added automatically) and a body that lists the included commits, notes that only user-facing changes were selected, and includes the `Changelog entries to review` heading when any entries were flagged. If no new section was needed, do not open an empty pull request — but still do the docs release-notes work below for the commits you selected.

Never open a second changelog PR while one is open.

## Docs release notes (shared PR)

In addition to this repo's changelog, you maintain ONE SHARED pull request against `canonical/landscape-documentation` that drafts the user-facing release notes for the in-development Landscape release. You own exactly one marked section (island) in that PR. NEVER open a separate per-repo docs PR, and NEVER edit outside your own island.

Your island content derives from ALL user-facing commits selected in the window above — not only the ones missing a changelog entry: a PR that brought its own entry still belongs in the release notes. Rewrite those selected commits into docs-appropriate bullets, excluding any entries flagged in `Changelog entries to review`.

1. Determine the in-development Landscape release `YY.MM`: Landscape releases are `YY.04` and `YY.10`. From today's date, the in-development release is the next boundary strictly in the future (before April -> `YY.04`; before October -> `YY.10`; otherwise `(YY+1).04`). As of November 2026 this is **27.04** (betas are underway), so the target page is `docs/reference/release-notes/27.04-release-notes.md`.
2. Find the shared docs PR: use the GitHub tools to list open PRs in `canonical/landscape-documentation` with labels `changelog` and `automation` (title starts with `build: `). That PR is created weekly (Fridays 12:00 UTC) by the scheduled `release-notes-draft` workflow in the docs repo — it is idempotent, bases the PR on `develop`, and pre-seeds the release-notes page with all eight islands. If one exists, this is THE docs PR — use it regardless of which `YY.MM` page it targets. You only ever push to its head branch; the `develop` base needs no action from you.
3. If a docs PR exists:
   - In the `./landscape-documentation` checkout, check out that PR's head branch (branches were fetched via the wildcard fetch at setup: `git checkout <branch>`). If the branch is not among the fetched refs, skip the docs work for this run — the agent has no credentials to fetch it.
   - Update ONLY your own island (see below). If the release-notes page for the in-development version does not exist on that branch, create it from the template below and add it to the toctree in `docs/reference/release-notes/index.md` (newest non-LTS page goes directly under the LTS entries, matching existing ordering).
   - Other Landscape bots push to this same shared branch, so the checkout (fetched at setup time) may be stale and another bot may push while you work. The agent environment has NO git credentials — `git fetch` fails here, so NEVER attempt to fetch or rebase. Work from the PR branch ref as fetched at setup; keep every edit strictly inside your own island markers so it applies cleanly over the other bots' island updates. `push_to_pull_request_branch` applies your patch to the branch's live tip in a credentialed job; if a concurrent push made it conflict, the delivery fails safely — do not retry, the next run redoes the work. NEVER force-push over another bot's commits.
   - Commit in the docs checkout and deliver via `push_to_pull_request_branch` with `pull_request_number` set to the docs PR number.
4. If no docs PR exists: skip the docs release-notes work for this run entirely. NEVER create the shared docs PR yourself — no changelog bot ever creates it; the `release-notes-draft` workflow in `canonical/landscape-documentation` (step 2) is the only creator. (Your in-repo changelog work above is unaffected.)
5. If you have no user-facing changes this cycle, leave your island untouched; do not create or update the docs PR. (Your own-repo changelog PR logic above is unaffected.)

### Your island

Island markers are HTML comments, invisible in rendered docs. Your slug is `landscape-task-handler-operator`. The full island set, in this fixed order:

1. `landscape-server` — Landscape Server
2. `landscape-client` — Landscape Client
3. `landscape-ui` — Web portal
4. `landscape-go` — Snaps (debarchive, outbox, task-handler services)
5. `landscape-server-operator` — Landscape Server charm
6. `landscape-client-charm` — Landscape Client charm
7. `landscape-debarchive-operator` — Debarchive charm
8. `landscape-task-handler-operator` — Task handler charm

Island format inside `## Additional updates`:

```markdown
<!-- changelog-bot:landscape-task-handler-operator:start -->
### Task handler charm

#### Features
- <docs-appropriate bullet>

#### Fixes
- <docs-appropriate bullet>
<!-- changelog-bot:landscape-task-handler-operator:end -->
```

Rules:
- BEFORE writing, read `.github/instructions/release-notes.instructions.md` and `.github/instructions/documentation.instructions.md` in the docs checkout and follow them (applicability: state self-hosted vs SaaS and the component; factual, non-promotional; do not invent benefits, migration steps, or breaking-change claims).
- Rewrite your selected commits into user-facing docs bullets: no conventional-commit prefixes, no `(scope)`, no `(#1234)`, no Jira keys. Keep Launchpad references but format them as `[LP: #NNNN](https://bugs.launchpad.net/landscape/+bug/NNNN)`. Write in the style of existing release notes (see any `docs/reference/release-notes/*.md` page): complete sentences, no trailing period inconsistency, admin-focused.
- Omit `#### Features` / `#### Fixes` when empty. If your island has no entries yet, put `(pending)` as the island body.
- NEVER edit outside your own island markers. Other sections belong to other bots.
- When creating the page, include ALL eight islands in the fixed order above (yours filled, the rest with `(pending)`), so later bot runs find their markers.

### New-page template

File `docs/reference/release-notes/YY.MM-release-notes.md` (dots in the filename, matching `26.10-release-notes.md`; anchor uses dashes):

```markdown
---
myst:
  html_meta:
    description: "Release notes for Landscape YY.MM. Draft; updated automatically until release."
---

(reference-release-notes-YY-MM)=
# YY.MM release notes

> Landscape Server YY.MM runs on Ubuntu 26.04 LTS Resolute Raccoon, 24.04 LTS Noble Numbat, or 22.04 LTS Jammy Jellyfish.

```{note}
This page is a draft maintained automatically until release.
```

## Highlights

(pending - written at release time)

## Additional updates

(eight islands here)

## Upgrade

(pending - written at release time)

## Bug fixes

(pending - written at release time)
```
