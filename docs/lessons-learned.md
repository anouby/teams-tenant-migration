# Lessons learned

These are the surprises from a real migration of a company with about 20 teams, 800 chats, 75,000 messages and 50,000 OneDrive files, between two unrelated tenants. Each one cost hours to find. The tool now handles most of them. They are written down here so you can plan around the rest.

## Plan before you import

### Decide everything that affects links before importing
Imported messages **can't be edited**, and an app **can't delete** them. Anything that changes a link after import means rebuilding chats, and one-to-one chats can't be rebuilt. So before the import:
- Decide whether the new tenant's SharePoint will be renamed, and enter the future name (see [below](#sharepoint-domain-rename)).
- Run `export`, then `relink-files`, **then** `import`, so links already point at their new copies.

### Ask for the protected-API approval on day one
Reading chats and channel messages app-only needs Microsoft's approval for *protected APIs*. It is free but can take days. Request it first.

### Create users, licenses and OneDrives before importing
A message can only be posted as a person who exists and has a Teams license. A file can only be copied into a OneDrive that has been opened once.

### Choose a media owner who is staying
Voice notes, videos and copied linked files have to live in someone's OneDrive. If that person later loses their license, every one of those links breaks. Use an account that stays.

### Get the leavers' list early
People who are leaving keep their history (their messages are imported under their name while their account exists). Their **files**, though, are deleted some time after their license is removed. Move anything important to a staying person before removing licenses.

## Teams behaviour

### Group chats look empty
A group chat created by an app gives each member `visibleHistoryStartDateTime = now`. The Teams app then **hides all imported history**, and people report "empty" chats. The value can't be set when creating the chat (400: *cannot be set*). The fix is to remove each member and add them back with `0001-01-01T00:00:00Z`. Side effect: "X removed / X added" lines in each chat. The tool does this automatically.

### The chat list shows the migration date for every chat
Teams dates and sorts the chat list by the **newest line** in each chat. In a migrated chat that is a system note ("… added …") from the day of the migration, and an app can't remove system notes. "Touching" chats oldest-first (re-entering and completing migration mode) changes the chat's `lastUpdatedDateTime` but **not** what the Teams app shows. This tool once had such a command and it was removed because it doesn't work. Tell users in advance: inside each chat, and in search, every date is right. The list re-sorts itself as people write again.

### Never delete one-to-one chats
A one-to-one chat between two people has a fixed identity. After deleting it, creating it again returns the *deleted* chat and fails with **404 SoftDeleted-Thread**. Those two people may be unable to start a new one-to-one chat. Rebuild only group chats.

### Migration mode can report a contradictory state
Rarely, `startMigration` says "already importing" while `completeMigration` says "already completed", and the chat refuses new messages. It settles by itself within minutes. Always finish a run by calling `completeMigration` on every touched chat, so none stays locked.

### Small things Teams rejects
| Input | Error | Handling |
|---|---|---|
| `:` in a chat name | 400 | Replaced by " - " |
| A NUL character in the text | 400 *Unicode '' is null or empty* | Retried as clean text |
| Two messages in the same millisecond | 409 | Shifted by 1 ms |
| Images over about 3 MB, any video | 400/413 | Uploaded to OneDrive and linked |
| Very large rich-text messages | 413 | Styling stripped, then split into parts |
| A retried `completeMigration` | 400 *not in migration mode* | Treated as success |

### Counting messages: deduplicate by id
Paging through a chat's messages can return the same message twice. Our first count showed 10,000 "extra" messages. Always compare unique message ids.

## Links between the two companies

### Links hide inside other links
Teams meeting-recap links contain the old SharePoint address URL-encoded (`sitePath=https%3A%2F%2Fold-my.sharepoint.com…`). A search for `https://old.sharepoint.com` misses them. The tool's pattern matches the old name anywhere inside a link.

### OneDrive addresses are the owner's whole sign-in name
`/personal/jane_doe_company_com/` is `jane.doe@company.com` with every non-alphanumeric character turned into `_`. Match on the whole address, not just the part before the `@`, and never hard-code domain names.

### Sign-in names change during a project
An admin may rename sign-in names mid-migration (for example to a new domain). The old address usually stays as an alias, but steps that look users up by address then fail with "user doesn't exist". Re-check the user matching after any rename.

### SharePoint domain rename
- It must be scheduled **at least 24 hours ahead** (`Start-SPOTenantRename`), and a tenant can only be renamed **twice in its lifetime**.
- Sites are briefly unreachable while it runs. Don't run the migration during the window.
- Old addresses redirect to the new name for a limited time only, so links written before the rename eventually break. Enter the planned new name in Settings **before** importing.

### Removing messages for a clean break costs licenses, or needs the senders
To remove a message that still shows the old company:
- **App-only delete:** 412, not supported.
- **App-only hide** (`policyViolation`): 402, unless each *sender* has an E5-level compliance license.
- **The sender deleting it** (delegated): works for free. Each sender signs in once. The sender needs a Teams license (*"Failed to get license information"* otherwise), and for channel messages must be a team member (*AclCheckFailed* otherwise). A license assigned a minute ago takes a few minutes to reach Teams.
- **Device-code sign-in** ("enter this code at microsoft.com/devicelogin") is blocked by security defaults (**AADSTS530035**). Use a normal browser sign-in with a `localhost` redirect. That needs the redirect address registered on the app, otherwise **AADSTS500113**.

## Running a long migration

### Expect the network to drop
A multi-hour run will meet DNS failures and Wi-Fi drops. The tool waits for the internet to come back and continues, and the append-only journal makes restarts cheap.

### Parallelism helps, carefully
Microsoft's limit is per conversation, so many conversations can run at once. For files, create folders level by level **before** uploading files in parallel, or uploads race on folder creation (409 *nameAlreadyExists*).

### Some sources are simply unavailable
Expect a share of meeting chats hosted by other organizations to be unreadable (403), some OneDrives to be locked (423), and some linked files to be deleted (404). Report them and move on. Don't let them block the run.

## Ending cleanly

- Verify there are no guests, no cross-tenant partner settings and no apps from one tenant installed in the other. See [cleanup.md](cleanup.md).
- Delete both app registrations, remove temporary permissions and licenses, and delete the local `export/` folder and `config.json`.
- Leave **nothing** inside the new tenant that belongs to the migration: no helper files, no test chats, no marker messages.
