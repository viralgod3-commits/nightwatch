# Working instructions

- Do not narrate every command.
- Do not dump full diffs unless debugging a failure.
- Prefer compact file lists and `git diff --stat` over full diff review.
- Do not inspect git history, remotes, Cloudflare status, deployment status, or browser state unless the task requires it.
- Do not rediscover the whole repo. Read only files directly relevant to the request.
- Do not run tests or builds by default unless the change might affect performance or is risky.
- Do not make changes that negatively impact performance
- in code reviews focus first on UI & performance optimization. Also the quality of the application from trader perspective and trading execution integrity.

## Commits and pushes

Use direct main pushes. This repo has standing user approval to push task commits to `origin/main` after validation; do not skip pushing because the current prompt did not repeat approval. Do not create feature branches or PRs unless the user explicitly asks.

Stop after pushing to `origin/main`.
