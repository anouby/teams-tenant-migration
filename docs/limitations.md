# Limitations

These come from Microsoft's APIs, not from this tool. No free tool can get around them, and most paid tools can't either. Where a workaround exists, it is listed.

## Chats

| Limitation | What you see | Workaround |
|---|---|---|
| **The chat list date can't be set.** Teams dates and sorts each chat by its newest line, which is a system note ("… added …") from the migration day | Every migrated chat shows the migration date and sits together in the list | None. Inside the chat, and in search, every message has its real date. Chats re-sort naturally as people write again |
| **Meeting chats can't be an import destination** | — | Meeting chats are recreated as group chats titled after the meeting. Recordings are added as messages |
| **Chats hosted by another organization can't be read** (meeting chats of external meetings) | Listed in `export/unreadable_chats.txt` | Purview eDiscovery export in the old tenant |
| **Group chat members don't see imported history by default** | The chat looks empty | Handled automatically. Members are re-added with full history, which adds "removed / added" system lines |
| **A one-to-one chat can't be recreated after deletion** | — | The tool never deletes one-to-one chats |
| **Pinned chats, sections, hidden or muted chats, read/unread state** | Not migrated | None. This is per-user state with no app API |
| **"Edited" marks, edit history, read receipts** | Not migrated | — |
| **Reactions from people who didn't move** | Dropped | — |
| **Cards, polls, Loop components, app/bot messages** | Replaced by a readable summary or a "could not be migrated" note | The HTML backup shows the original |

## Messages that need changing later

Posted messages **can't be edited**, and an app **can't delete** them:

- App-only `softDelete` answers **412**: not supported in application context.
- The app-only "hide" (`policyViolation`) answers **402** unless the sender has an E5-level compliance license.

Plan links **before** importing: run `relink-files` before rebuilding, and set a planned SharePoint rename in Settings. For messages already posted, the [old-link clean-up](cleanup.md) posts clean copies, and each *sender* deletes their originals.

## Files

| Limitation | Notes |
|---|---|
| Only the latest version of each file is copied | Version history isn't available to copy |
| Sharing permissions are not copied | Copied linked files get organization-only view links |
| Locked OneDrives (HTTP 423) can't be read | Unlock them in the old tenant's admin center, or skip them |
| Files deleted in the old tenant | Their links become the plain file name |

## Meetings

| Limitation | Notes |
|---|---|
| Calendar meeting objects | Not created. Use the PST route in [setup.md](setup.md#e-mail-and-calendars) |
| Transcripts | Need a Teams application access policy (PowerShell) and are often inaccurate anyway. Not migrated |
| Recordings | The recording files are copied with OneDrive, and each meeting chat gets a message linking to its recording |

## Also not migrated

Tabs, apps and connectors, Planner, channel wiki and OneNote, call history, voicemail, e-mail and calendars (see the PST route in [setup.md](setup.md#e-mail-and-calendars)).
