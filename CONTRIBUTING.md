# Contributing

Use Python 3.11+ and `uv`:

```bash
uv sync --dev
uv run ruff check .
uv run pytest
uv build
```

Keep the public boundary focused on Linux + Kitty + Codex/Claude Hooks. Add tests for configuration migrations and preserve unrelated user configuration. Never include real credentials, OpenIDs, personal paths, screenshots, transcripts, or live Hook payloads in commits or fixtures.

Open an issue before a large behavior or protocol change. Pull requests should explain the user-visible change, security implications, and verification performed.
