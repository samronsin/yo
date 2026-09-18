# Contributing to yo

Thanks for helping out. yo is small on purpose: a runner, an installer, and a
handful of utilities. These notes keep it that way.

## Getting set up

Clone the repo and run the tests from its root:

```sh
git clone https://github.com/samronsin/yo.git && cd yo
python3 -m unittest
```

Python 3.10 or newer is the only requirement for development. You do not need
`codex` or `claude` installed to work on the code: the tests never call them.

## Before you open a pull request

- Run `python3 -m unittest` and `git diff --check`. Both must pass.
- Keep each change focused on one thing. Small pull requests with a clear
  title are easier to review and to revert.
- Update the README when behaviour visible to users changes: flags, settings
  keys, log formats, or the installer's output.

## Tests must stay free of side effects

yo touches two things that are expensive or hard to undo: the crontab, and paid
inference calls against a real account. Tests must mock both. A test that
writes a real crontab or sends a real ping is a bug, even if it passes.

## Keep examples generic

Documentation, test fixtures, and commit messages should not carry private
hostnames, paths, account identifiers, credentials, or log excerpts. Use
placeholders such as `/home/me` or `codex-pro`. If you need to quote real
output, redact it first.

## Working with an agent

If you use a coding agent on this repo, [AGENTS.md](AGENTS.md) holds the
instructions it follows, including the preview-then-apply procedure for
changing a real installation. The rules on this page apply to agents too.
