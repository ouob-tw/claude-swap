# Issue tracker: GitHub (separate private repo)

Issues and specs for this project live as GitHub issues in the **private** repo `ouob-tw/claude-swap-work`. The code repo `ouob-tw/claude-swap` is a public fork, so issues are kept apart to avoid exposing handoffs, evidence, internal hosts, local paths, or account details.

Use the `gh` CLI for all operations and **always pass `-R ouob-tw/claude-swap-work`**. Do not infer the repo from `git remote -v`: that points at the public code repos.

## Conventions

- **Create an issue**: `gh issue create -R ouob-tw/claude-swap-work --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> -R ouob-tw/claude-swap-work --json number,title,body,labels,comments`, filtering with `--jq`.
- **List issues**: `gh issue list -R ouob-tw/claude-swap-work --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> -R ouob-tw/claude-swap-work --body "..."`
- **Apply / remove labels**: `gh issue edit <number> -R ouob-tw/claude-swap-work --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> -R ouob-tw/claude-swap-work --comment "..."`

## Cross-repo references

- Pull requests and commits stay in the code repo `ouob-tw/claude-swap`.
- In PRs, commits, and code-repo docs, reference issues with the full form `ouob-tw/claude-swap-work#N`. A bare `#N` resolves to the wrong repo.
- Issue bodies may contain internal hosts, local paths, and account details. Do not copy those into PR descriptions, commit messages, or files in the code repo.

## When a skill says "publish to the issue tracker"

Create a GitHub issue in `ouob-tw/claude-swap-work`. A spec (from `/to-spec`) carries the `spec` label and no triage state label.

## Pull requests as a triage surface

**PRs as a request surface: no.**
