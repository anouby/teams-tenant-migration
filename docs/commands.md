# Commands

Every button in the browser interface runs one of these commands. You can also run them yourself:

```bash
python3 teams_migrate.py <command> [options]
```

| Option | Default | Meaning |
|---|---|---|
| `--config PATH` | `config.json` | Settings file |
| `--out PATH` | `export` | Working folder: exported data, progress, backup |
| `--parts LIST` | everything | `export`, `import`: only these parts, comma-separated: `teams`, `one-to-one`, `group-chats`, `meeting-chats`, `onedrive`. Export leaves out `onedrive` unless it is listed |
| `--onedrive` | off | `export`: also copy each matched user's OneDrive (same as adding `onedrive` to `--parts`) |
| `--chats-only` | off | `export`: only retry chats that were not saved yet |
| `--team NAME` | all | `import`: only this team, for a first test |
| `--port N` | `8799` | `clean-delete`: localhost port for the sign-in redirect |

Every command can be stopped (Ctrl+C or **Stop**) and run again. It continues where it stopped and never posts anything twice.

## Choosing what to migrate

Export and import cover five parts. Pick any combination in the page's **Choose what to migrate** card, or with `--parts`:

| Part | What it covers |
|---|---|
| `teams` | Teams, channels, posts and replies, channel files, members and owners |
| `one-to-one` | Private chats between two people |
| `group-chats` | Chats with three or more people |
| `meeting-chats` | Chats of scheduled meetings, recreated as group chats |
| `onedrive` | Each matched user's OneDrive |

```bash
python3 teams_migrate.py export --parts teams,one-to-one
python3 teams_migrate.py import --parts teams,one-to-one
```

You can add more parts later. Run export and import again with the extra parts, and what is already done is skipped.

## Main steps

### `export`
Reads users, teams, channels, messages, replies, chats, images, voice notes, videos, channel files and optionally OneDrive from the **old** tenant into `export/`. It also writes a readable HTML backup to `export/archive/`. Nothing in the old tenant changes.

Chats the app may not read (usually meeting chats hosted by another company) are listed in `export/unreadable_chats.txt`.

### `map`
Writes `export/users.csv` with one row per old user (`source_upn,dest_upn`), matched by address and display name. **Always check it.** A wrong match posts messages under the wrong person. An empty `dest_upn` means "won't exist", so their messages go to the fallback sender.

### `import`
Recreates teams, channels, chats, files and members in the **new** tenant using Teams migration mode, so every message keeps its original sender and timestamp. Group chat members are given the full history (see [lessons learned](lessons-learned.md#group-chats-look-empty)). Problems are collected and listed at the end, so one bad message never stops the run.

## Finishing touches

### `backfill`
Downloads images, voice notes and videos an earlier export missed, for example after a network drop or a throttled run.

### `relink-files`
Messages often link to files in **someone else's** OneDrive in the old tenant. After the move those links lead back to the old company. This command finds every such link in the export and copies each file:
- into its owner's new OneDrive, when the owner moved and the file was copied with their OneDrive, or
- into the media owner's OneDrive under `Files from chats/<owner>/`.

It then creates an organization-only link to each copy. It needs `old_sharepoint` in the settings. Files that are gone or locked in the old tenant are reported as "no longer available".

### `rebuild-chats`
Deletes and recreates **group chats** that still point at the old tenant or that had meeting recordings. Every old link is replaced by its new copy, or by the plain file name when no copy exists. Meeting recordings are added as messages at their original time. Run `relink-files` first.

One-to-one chats are **never** deleted. Teams can't recreate a one-to-one chat between the same two people (see [lessons learned](lessons-learned.md#never-delete-one-to-one-chats)). For those, recordings are added in place, and old links are handled by the clean-up below.

### `copy-photos`
Copies the profile photo of every matched user.

## Old-link clean-up

For a complete break between two unrelated companies. Run them in order. Details and required settings: [cleanup.md](cleanup.md).

### `clean-scan`
Reads every imported chat and the exported channel messages, and lists those that still show an old-tenant address, grouped by sender. It saves the list to `export/old_links.json`.

### `clean-copy`
Posts a clean copy right after each listed message: same sender, original time + 1 ms, no old address. Nothing is deleted yet.

### `clean-delete`
Prints a sign-in link. The person who opens it and signs in gets **their own** listed messages deleted, only those that already have a clean copy. Run it once per sender.
