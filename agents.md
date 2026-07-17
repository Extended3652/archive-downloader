# agents.md

This repository contains Python wrappers around the Internet Archive `ia` CLI, plus a curses TUI.

## What matters

- Keep changes small and behavior-focused.
- Prefer patching the existing flow over introducing new modes or parallel systems.
- Preserve the staging model: downloads land in `.ia_staging` first, then get imported into the media root.
- Preserve user state when possible. The app already remembers:
  - the last search query
  - the current local result filter
  - file-view state per item
  - pending download state

## Main entry points

- `ia_minotaur.py` - full-screen TUI
- `ia_dl.py` - CLI download/search helper
- `ia_easy.py` - simple interactive downloader
- `ia_audit.py` - library audit tool
- `ia_downloads.py` - download command and progress helpers
- `ia_paths.py` - media/staging path helpers and safety checks
- `ia_state.py` - JSON persistence helpers

## Useful tests

Run the focused suites first:

```bash
pytest -q tests/test_ia_minotaur_pure.py -q
pytest -q tests/test_ia_downloads.py -q
```

If you touch CLI behavior, also check:

```bash
pytest -q tests/test_ia_dl.py -q
pytest -q tests/test_fake_binaries_integration.py -q
```

## Working conventions

- Use `apply_patch` for edits.
- Avoid destructive git commands.
- Prefer `rg` for search and `rg --files` for file listing.
- Keep ASCII unless the file already uses Unicode for UI text.
- When changing a TUI shortcut or action, update:
  - the hotkey handler
  - the menu/action palette
  - the hint/help text
  - tests for the new behavior

## Download and selection model

- FILES mode uses marked files for bulk actions.
- Marked files now have an explicit order, and downloads should follow that order.
- `Space` is used as a fast mark-and-advance shortcut in FILES mode.
- `m` is used for range marking.
- Local result filters should remain easy to discover and should persist through session save/restore.

## Before wrapping up

- Run the relevant tests.
- If behavior changed, mention the specific shortcut/path affected.
- If you could not run tests, say so directly.
