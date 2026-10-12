# Working instructions

- Do not narrate every command.
- Do not dump full diffs unless debugging a failure.
- Prefer compact file lists and `git diff --stat` over full diff review.
- Do not inspect git history, remotes, Cloudflare status, deployment status, or browser state unless the task requires it.
- Do not rediscover the whole repo. Read only files directly relevant to the request.
- Do not run tests or builds by default unless the change might affect performance or is risky.
- Do not make changes that negatively impact performance
- in code reviews focus first on UI & performance optimization. Also the quality of the application from trader perspective and trading execution integrity.

## Trading UI preferences

- Keep the trading UI compact and useful to a crypto trader. Omit redundant titles, subtitles, explanatory labels and dedicated rows for them.
- Do not repeat the current ticker inside the orderbook; it is already shown in the market bar.
- Do not show routine status badges such as LIVE or headings such as ORDER BOOK and VISIBLE DEPTH. Reclaim their space for prices and trading data.
- Grouping controls must display the actual price step (exchange tick size times grouping multiplier) with its quote unit, rather than an opaque group number or multiplier.
- Reserve exception indicators for conditions that affect trading, such as stale data. Keep stale-price selection safeguards even when normal-state badges are omitted.

## Commits and pushes

- Always use the connected GitHub account route to publish changes directly to main. Local Git CLI credentials are not required; do not stop or ask for credentials when that connection is available.
- Keep repository work to pulling current source and pushing task changes directly to main. Do not add PR workflows or GitHub Actions integrations.
- Do not run integration tests unless the task involves a large code change. Do not add automated GitHub integration checks.

Use direct main pushes. This repo has standing user approval to push task commits to `origin/main` after validation; do not skip pushing because the current prompt did not repeat approval. Do not create feature branches or PRs unless the user explicitly asks.

Stop after pushing to `origin/main`.
