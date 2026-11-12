# A clean break, and cleaning up afterwards

When the old and new tenants belong to **unrelated companies**, the goal is that nothing in the new tenant points back to the old one, and that nothing of the migration itself is left behind.

## 1. Before importing: avoid old links in the first place

1. In Settings → Advanced, enter the **old tenant's SharePoint name** (and a planned **SharePoint rename**, if any).
2. Run `export`, then `relink-files`, then `import`. Messages are then written with links to the new copies.

## 2. After importing: find and fix what's left

```text
rebuild-chats  →  clean-scan  →  clean-copy  →  clean-delete (once per sender)
```

- **`rebuild-chats`** fixes group chats automatically, with no sign-in needed.
- **One-to-one chats and channel messages** can't be rebuilt, so:
  - **`clean-scan`** lists every message that still shows an old address, grouped by sender.
  - **`clean-copy`** posts a clean copy right under each one: same sender, same time, no old address.
  - **`clean-delete`** prints a sign-in link. The sender signs in, and their originals (only those with a clean copy) are deleted. Each one then shows "This message has been deleted", followed by the clean copy.

### One-time settings for `clean-delete`

On the **new tenant's** app registration:

1. **Authentication → Add a platform → Mobile and desktop applications.** Custom redirect URI: `http://localhost:8799`.
2. **Authentication → Allow public client flows → Yes.**
3. **API permissions → Add → Microsoft Graph → Delegated permissions:** `Chat.ReadWrite`, `ChannelMessage.ReadWrite` → **Grant admin consent**.

Requirements for each person who signs in:
- **A Teams license.** A license assigned a moment ago takes a few minutes, and the tool retries automatically.
- **For channel messages, membership of that team.** Add them temporarily if needed.
- **Sign in on the same computer that runs the tool.** The sign-in returns to `localhost`.

Messages from people who can't sign in can't be deleted for free. The alternative is the app-only hide (`policyViolation`), which needs an E5-level compliance license on each sender.

## 3. Check that the tenants are unrelated

In **both** tenants:

| Check | Where |
|---|---|
| No guest accounts from the other company | Entra → Users → filter *User type = Guest* |
| No organization-specific cross-tenant settings | Entra → External Identities → Cross-tenant access settings → *Organizational settings* is empty (or has no entry for the other tenant) |
| No multi-tenant organization | Entra → Multi-tenant organization |
| No apps owned by the other tenant | Entra → Enterprise applications (filter by the other tenant's apps) |
| No old company's domains | Settings → Domains |

## 4. Remove every trace of the migration

**In the new tenant:**
- [ ] Delete the migration app registration (this removes all its permissions).
- [ ] Remove temporary licenses and temporary team memberships given for `clean-delete`.
- [ ] Delete any test teams or test chats you created. Deleted chats are kept by Microsoft for a while, then purged.

**In the old tenant:**
- [ ] Delete the migration app registration.

**On your computer:**
- [ ] Keep `export/archive/` somewhere safe if you want a readable backup.
- [ ] Delete `config.json` (it holds the app secrets) and the whole `export/` folder (it holds every message and file).
