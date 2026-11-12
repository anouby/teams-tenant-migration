# Contributing

Thanks for helping. Tenant migrations are painful, and every fix here saves someone else days.

## Ground rules

- **Standard library only.** No `pip install`, so anyone can run the tool on a locked-down admin machine. Python 3.9+.
- **Safe to re-run.** Every step must resume after a crash and never post anything twice. Record progress with `Importer.mark()` as soon as each item is done.
- **Never lose a message silently.** If Microsoft refuses something, retry with less detail, or report it in `imp.failed`.
- **Never put real data in issues, commits or tests.** No tenant IDs, names, e-mail addresses, message text or logs that contain them. Use `oldco`, `newco`, `Alice`, `Bob`.

## Development

```bash
python3 tests/test_teams_migrate.py   # offline self-check, no tenant needed
python3 ui.py                         # interface at http://127.0.0.1:8765
```

Use `DATA_DIR=/some/folder python3 ui.py` to keep test data out of the project folder.

The tests replace `Graph` with small fakes, so they run anywhere. Add a check for every behaviour you change, especially anything about message conversion, links or progress tracking.

## Adding a command

1. Write `cmd_<name>(cfg, out)` in `teams_migrate.py`, following the existing ones. Use `Importer` for the new tenant and `Graph(cfg["source"])` for the old one.
2. Add it to the `choices` and the dispatch in `main()`, and to the help text at the top of the file.
3. To expose it in the interface, add it to `COMMANDS` in `ui.py` and a button with `data-run="<name>"` in `ui.html`.
4. Document it in `docs/commands.md` and any new permission in `docs/permissions.md`.

## Pull requests

- Keep changes focused, and explain the problem you hit and how you tested the fix.
- If you learned something about Microsoft's APIs the hard way, add it to `docs/lessons-learned.md`.
- Run the self-check before opening the PR.

## Ideas that would help

- Migrating Planner plans, channel tabs and OneNote notebooks.
- Exchange mailboxes and calendars through Graph instead of PST.
- An app-only "hide" path for tenants that do have E5 compliance licenses.
- A dry-run mode that estimates time and volume before importing.
