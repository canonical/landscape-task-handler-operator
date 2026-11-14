---
# Weekly changelog PR bot (GitHub Agentic Workflow).
#
# This is the gh-aw SOURCE file. After editing, regenerate the compiled
# GitHub Actions workflow with:
#
#     gh aw compile changelog-bot
#
# and commit the generated `.github/workflows/changelog-bot.lock.yml` alongside it.

on:
  schedule: weekly on saturday around 12:00 UTC
  # Allow maintainers to trigger a run on demand.
  workflow_dispatch:

# Run on the Canonical self-hosted fleet. The agent and threat-detection jobs
# need Docker/AWF and so target the noble/amd64 fleet; the framework and
# safe_outputs jobs use the bare self-hosted label.
runs-on: [self-hosted, noble, amd64]
runs-on-slim: self-hosted

# The self-hosted runner image ships a gh too old for the agent job's AWF
# install ('gh attestation verify' is missing), so install a modern,
# checksum-verified gh CLI before the agent job's setup scripts run.
pre-steps:
  - name: Install modern gh CLI
    run: |
      GH_VERSION="2.83.1"
      GH_SHA256="1c5252d4ce3db07b51c01ff0b909583da6364ff3fdc06d0c2e75e62dc0380a34"
      tmp="$(mktemp -d)"
      curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" -o "$tmp/gh.tgz"
      echo "${GH_SHA256}  $tmp/gh.tgz" | sha256sum -c -
      tar -xzf "$tmp/gh.tgz" -C "$tmp"
      install -m 0755 "$tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh
      rm -rf "$tmp"
      gh version
    env:
      GH_TOKEN: ${{ github.token }}

# The runner image also lacks the Docker Compose plugin, so the AWF sandbox's
# 'docker compose up' fails with "unknown shorthand flag: 'd' in -d" and the
# agent job exits 125. Install the compose plugin in the agent job before the
# AWF bring-up (same fix as the DISA STIG evaluator's test-airgap.md).
steps:
  - name: Install Docker Compose plugin
    run: |
      if docker compose version >/dev/null 2>&1; then
        echo "Docker Compose plugin already present, skipping install."
        docker compose version
        exit 0
      fi
      COMPOSE_VERSION="v5.5.0"
      mkdir -p ~/.docker/cli-plugins
      curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
        -o ~/.docker/cli-plugins/docker-compose
      chmod +x ~/.docker/cli-plugins/docker-compose
      docker compose version

permissions:
  contents: read

# The runner image also ships a Node too old for gh-aw's runtime scripts
# ('Cannot find module node:fs' during activation), so give the activation
# job a modern Node.
jobs:
  activation:
    pre-steps:
      - name: Setup modern Node for gh-aw runtime
        uses: actions/setup-node@v4
        with:
          node-version: "24"

# The default checkout is shallow (fetch-depth 1) with no tags, which makes the
# agent see only the latest commit. Fetch full history so it can scan the
# commit window and read the commit log. A second checkout fetches the
# documentation repo (all branches) for the shared docs release-notes PR (see
# the "Docs release notes (shared PR)" section below).
checkout:
  - fetch-depth: 0
  # canonical/landscape-documentation is public, so this checkout needs no
  # token (and setting one would leak it onto the primary-repo checkout in the
  # safe-output jobs). Pushing to the docs repo is handled by the credentialed
  # safe_outputs job below (LANDSCAPE_CHANGELOG_TOKEN).
  - repository: canonical/landscape-documentation
    path: ./landscape-documentation
    fetch: ["*"]

engine:
  id: gemini
  # The native Gemini engine uses GEMINI_API_KEY directly and takes an exact
  # model id, avoiding the pi engine's model-grammar rejection of vendored
  # OpenRouter ids (github/gh-aw#77514). Gemini does not resolve gh-aw model
  # aliases, so this must be an exact id.
  model: gemini-3.5-flash
  env:
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}

network:
  allowed: [defaults]

# Shell access for inspecting history and formatting dates. The pi engine
# cannot allowlist individual commands, so bash is open; the sandbox, network
# allowlist, and safe-output handlers still confine the run.
tools:
  bash: ["*"]
  edit:

# The agent never pushes directly; its file edits are turned into a pull request.
# gh-aw binds the whole safe_outputs job to ONE credential for its git
# checkouts and pushes (github/gh-aw#77580), and the default GITHUB_TOKEN
# cannot write to canonical/landscape-documentation. LANDSCAPE_CHANGELOG_TOKEN
# is a fine-grained PAT with contents/pull-requests read+write on both this
# repo and canonical/landscape-documentation, so it covers the own-repo
# changelog PR and the cross-repo docs push with a single credential.
safe-outputs:
  github-token: ${{ secrets.LANDSCAPE_CHANGELOG_TOKEN }}
  # The safe_outputs job runs the framework's output handlers, not the agent,
  # so the bare self-hosted label is enough.
  runs-on: self-hosted
  # Threat detection runs the AWF sandbox and needs Docker, so it targets the
  # noble/amd64 fleet alongside the agent job.
  threat-detection:
    engine: copilot
    runs-on: [self-hosted, noble, amd64]
  create-pull-request:
    title-prefix: "build: "
    labels: [changelog, automation]
    team-reviewers: [canonical/landscape]
    draft: false
    if-no-changes: ignore
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
  push-to-pull-request-branch:
    target: "*"
    target-repo: canonical/landscape-documentation
    github-token: ${{ secrets.LANDSCAPE_CHANGELOG_TOKEN }}
    required-labels: [changelog, automation]
    # Docs PR updates may only touch release-notes pages (including index.md).
    allowed-files: ["docs/reference/release-notes/*.md"]
    max: 1
---

# Weekly changelog update

You maintain the changelog for the landscape-task-handler charm. Your job is to draft a new changelog section summarizing recent user-facing changes and open a pull request with it.

## Gather the commits

This charm has no versioned releases in the repository: charm releases are Charmhub revisions assigned at publish time, so changelog sections are dated, not versioned. There are no git tags to anchor against. The changelog lives at `CHANGELOG.md` in the repository root; this bot creates it on its first run that finds user-facing commits.

1. The scan window is time-based: list the commits from the last four weeks with `git log --since="4 weeks ago" --no-merges --pretty=%H%x09%s`. `main` is squash-merged, so each merged PR is a single commit on this list. Do NOT anchor on the last commit that touched `CHANGELOG.md`: a bot changelog PR that is still open (or was merged late) makes that anchor unreliable, silently double-filing or skipping commits. Four weeks against a weekly schedule gives three weeks of overlap with the previous run, so omissions leapfrogged by later merges are still caught; the deduplication rules below prevent double-filing.
2. Skip any commit whose subject has no trailing pull request reference ` (#1234)`. Such commits cannot carry the `(#<N>)` marker the deduplication relies on, so they can never be filed safely; if one looks user-facing, mention it in the PR body under `Changelog entries to review` instead of adding it to the changelog.
3. Deduplicate the rest by pull request number. For each commit, extract its PR number `<N>` from the trailing ` (#1234)` reference in the subject. Match the FULL number wherever you search — `#123` must not match `#1234`. Skip the commit if EITHER of the following holds:
   a. Its own commit documented itself: run `git show --name-only --format= <commit>`; if `CHANGELOG.md` is among the paths, run `git show <commit> -- CHANGELOG.md` and check whether the diff adds a line containing the exact reference `#<N>`. A contributor who wrote their own changelog bullet counts as covered — do not add a second one.
   b. The changelog already covers it: run `git grep -n '#<N>' -- CHANGELOG.md` against the working tree. Every bullet this bot writes ends with ` (#<N>)` (see "Compose the new section"), so any match — a bullet from an earlier bot run or a hand-written entry — means this PR is already covered.

## Select user-facing commits only

Include only conventional commits that a charm user or Juju operator would notice. Commit subjects in this repo are usually conventional commits, but the type prefix is sometimes capitalized (e.g. `Fix:`) and occasionally missing entirely — match prefixes case-insensitively and use your judgment for non-conforming subjects.

- **Include**: `feat:`, `fix:`, `perf:`, and any breaking change (a `!` before the colon, e.g. `feat!:` / `fix!:`).
- **Maybe Include**: `refactor: ` commits, only if they are user-facing.
- **Exclude**: `chore:`, `ci:`, `test:`, `build:`, `docs:`, and `style:`, commits,
  plus anything that only touches tooling, tests, or CI, and any dependency bumps.

Add each selected commit's subject line, including its type prefix and any `(LP: #NNNN)` Launchpad references, but excluding any `(scope)` after the type prefix. Proofread and correct any obvious spelling or grammar errors. Remove Jira issue references such as `LNDENG-5678` wherever they occur in the subject. KEEP the trailing GitHub pull request reference ` (#1234)`: it becomes the deduplication marker every changelog bullet must carry (see "Compose the new section"). Do **not** strip `(LP: #NNNN)` references.

Breaking changes are not always flagged in the subject. For every selected commit, also inspect the full commit message with `git show -s --format=%B <sha>` and look for `BREAKING CHANGE:` or `BREAKING-CHANGE:` footers. A commit carrying such a footer is a breaking change even when its subject has no `!` — classify it as breaking and note the breaking nature in the docs island bullet.

If there are no user-facing commits, stop and make no changes.

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

Open a pull request with the change. Use a title like `changelog update for <date>` (the `build: ` prefix is added automatically) and a body that lists the included commits, notes that only user-facing changes were selected, and includes the `Changelog entries to review` heading when any entries were flagged. If no new section was needed, do nothing — do not open an empty pull request.

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
