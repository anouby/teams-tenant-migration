# How it works

## The pieces

| File | Role |
|---|---|
| `teams_migrate.py` | The engine. Every step is a command. Standard library only |
| `ui.py` | A small local web server (127.0.0.1) that runs the commands and streams their log |
| `ui.html` | The browser interface |
| `tests/test_teams_migrate.py` | Offline self-check with fake Graph responses |

## Export

The old-tenant app reads everything over Microsoft Graph and stores it as JSON in `export/`:

```
export/
  users.json, users.csv          old users, and the old → new matching
  teams/<id>/team.json           team, channels, messages, replies, members
  teams/<id>/files/              channel files
  chats/<id>.json                one file per chat
  hosted/                        inline images, voice notes, videos
  onedrive/<user-id>/            OneDrive copies (optional)
  linked/                        files that messages linked to (relink-files)
  archive/                       readable HTML backup
  import_state.json(.journal)    progress of every import step
```

## Import: Teams migration mode

Normally Microsoft stamps every new message with "now" and the app's identity. In **migration mode** (`Teamwork.Migrate.All`) a channel or chat accepts messages with any sender and any past `createdDateTime`:

1. `POST …/startMigration` with a start date older than the first message.
2. `POST …/messages` with `createdDateTime`, `from`, body, mentions, reactions and inline images.
3. `POST …/completeMigration`. The conversation opens to members.

Rules the tool works with:

- **5 messages per second per conversation.** The tool runs up to 8 conversations at once, so throughput grows with the number of chats.
- **Timestamps must be unique to the millisecond** within a conversation. Duplicates are shifted by 1 ms.
- **Members can't be added to a channel in migration mode**, so they are added at the end.
- **A finished conversation can re-enter migration mode** if the new start date is older than its creation date. That is how recordings and clean copies are added later.

## Never twice: the progress journal

Every finished item (team, channel, chat, message, file, link) is appended as one line to `import_state.json.journal` and flushed to disk. On the next start the journal is folded into `import_state.json`. A crash, a network drop or Ctrl+C costs at most the one item in flight. Rewriting a big JSON file after each of 100,000 messages would take hours. Appending one line takes microseconds.

## Resilience

- **Throttling (429) and server errors (5xx)** are retried. The tool waits as long as Microsoft's `Retry-After` asks, otherwise with a growing pause of up to 60 seconds.
- **Network drops** pause the run until `login.microsoftonline.com` answers again, then continue.
- **A message Teams rejects** is retried with less detail: without reactions, then without mentions, then without images, then as plain text split into parts. A message is never dropped silently.
- **One failing team or chat never stops the run.** Problems are listed at the end, and a re-run retries only those.

## Links: making the new tenant self-contained

Messages contain links to the old tenant's SharePoint and OneDrive. The tool handles them in layers:

1. **Files that moved with their owner's OneDrive** are rewritten to the new address.
2. **`relink-files`** resolves every remaining old link through the old tenant's `/shares` API, or by OneDrive path when the share is gone, copies the file, and records `old link → new organization link`.
3. **`rebuild-chats`** and **`clean-copy`** write messages with those new links. An address that can't be copied becomes its plain file name, so no old address survives. This includes addresses hidden *inside* other links, such as Teams meeting-recap links.
4. **A planned SharePoint rename** is applied to every new link at write time, because posted messages can't be edited.

## Group chats and history

Group chat members created by an app only see messages from the moment they were added. The tool therefore removes and re-adds each member with `visibleHistoryStartDateTime = 0001-01-01`, which reveals the whole imported history. See [lessons learned](lessons-learned.md#group-chats-look-empty).

## Security model

- Secrets live only in `config.json` (mode 600) and are never sent back to the browser.
- The web server binds to `127.0.0.1`, checks the `Host` header against DNS rebinding, and requires a custom header on every write, so other websites can't drive it.
- The backup pages are served read-only. Nothing else in `export/` is reachable from the browser.
