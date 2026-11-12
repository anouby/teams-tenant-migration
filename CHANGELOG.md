# Changelog

## 1.0.0

First public release, based on a complete real-world migration between two unrelated tenants.

- Choose what to migrate: any mix of teams, one-to-one chats, group chats, meeting chats and OneDrive (`--parts`).
- Export and import of teams, channels (standard, private, shared), one-to-one and group chats, with original senders and dates through Teams migration mode.
- Channel files, optional OneDrive, inline images; voice notes, videos and large images uploaded and linked.
- Group chat members get the full imported history.
- `relink-files`: copies files that messages link to in other people's OneDrive.
- `rebuild-chats`: rebuilds group chats with clean links, and adds meeting recordings at their original time.
- `copy-photos`: profile photos.
- `clean-scan`, `clean-copy`, `clean-delete`: remove messages that still show an old-tenant address, with the sender signing in through the browser.
- Planned SharePoint domain renames are applied to links at write time.
- Survives network drops; append-only progress journal; up to 8 conversations in parallel.
- Browser interface with a guided layout, live log, user matching table and readable backup.
