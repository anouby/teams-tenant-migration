# Setup

You need admin rights in both tenants and about an hour, plus a few days of waiting for Microsoft to approve Teams API access (step 2).

## 1. Prepare the new tenant

1. **Create every user** who will exist in the new company, and give each one a license that includes **Teams and OneDrive**. Microsoft 365 Business Basic is enough.
2. **Open OneDrive once** for each user, or pre-provision OneDrive with the SharePoint PowerShell command `Request-SPOPersonalSite`. A user without a provisioned OneDrive can't receive their files.
3. **Choose a fallback sender.** This is a licensed account that stays, usually an admin. Messages from people who won't exist in the new tenant (leavers, guests, bots) are posted by this account, with "Originally sent by *name*" on top.
4. **Choose a media owner** (optional, defaults to the fallback sender). Voice notes, videos and files copied from other people's OneDrive are stored in this person's OneDrive and shared with the organization. **Pick someone who is staying.** If that license is removed, those files disappear.
5. **Planning a SharePoint domain rename** (for example after a company rename)? Decide the new name **before** importing. Posted messages can't be edited later, so the tool writes links with the future name. Enter it under *Advanced settings*. See [lessons learned](lessons-learned.md#sharepoint-domain-rename).

> Keep both tenants running until you have checked the result. Don't move the e-mail domain yet.

## 2. Register an app in the OLD tenant (read-only)

1. [Entra admin center](https://entra.microsoft.com): **App registrations → New registration**. Use any name and keep it single tenant.
2. **API permissions → Add a permission → Microsoft Graph → Application permissions.** Add:

   ```
   User.Read.All, Group.Read.All, TeamSettings.Read.All, Channel.ReadBasic.All,
   ChannelMember.Read.All, TeamMember.Read.All, ChannelMessage.Read.All, Chat.Read.All,
   Files.Read.All, Sites.Read.All, ProfilePhoto.Read.All
   ```

3. Click **Grant admin consent**.
4. **Certificates & secrets → New client secret.** Copy the secret **Value** (not the Secret ID). It is shown only once.
5. From **Overview**, copy the **Directory (tenant) ID** and the **Application (client) ID**.

### Teams "protected APIs"

`ChannelMessage.Read.All` and `Chat.Read.All` are *protected* when used by an app. If the export fails with **403** on messages, submit Microsoft's free request form: search for **"Microsoft Teams protected APIs"** on learn.microsoft.com. Approval can take a few days. The export of teams, files and users works without it.

## 3. Register an app in the NEW tenant (write)

Same steps, with these **application** permissions:

```
User.Read.All, Team.Create, Group.ReadWrite.All, TeamSettings.ReadWrite.All, Channel.Create,
ChannelMember.ReadWrite.All, TeamMember.ReadWrite.All, Teamwork.Migrate.All, Chat.Create,
Chat.ReadWrite.All, ChatMember.ReadWrite.All, Files.ReadWrite.All, Sites.ReadWrite.All
```

Optional extras, only if you use those steps:

| Step | Extra application permission |
|---|---|
| Rebuild group chats | `Chat.ManageDeletion.All` |
| Copy profile photos | `ProfilePhoto.ReadWrite.All` |
| Old-link clean-up | Delegated permissions plus two settings, see [cleanup.md](cleanup.md) |

Grant admin consent and create a secret.

## 4. Start the tool and connect

```bash
python3 ui.py
```

In **1. Connect**, paste the tenant ID, client ID and secret for each side, enter the fallback sender, and click **Save settings**. Everything is saved to `config.json` (readable only by you) in the project folder.

<details>
<summary>Editing <code>config.json</code> by hand instead</summary>

```bash
cp config.example.json config.json
```

| Key | Required | Meaning |
|---|---|---|
| `source` / `dest` | yes | `tenant_id`, `client_id`, `client_secret` of each app |
| `fallback_sender_upn` | yes | New-tenant account that posts messages of people who don't exist there |
| `media_owner_upn` | no | Owner of uploaded voice notes, videos and copied linked files. Defaults to the fallback sender |
| `old_sharepoint` | for extras | The old tenant's SharePoint name: `oldcompany` for `oldcompany.sharepoint.com` |
| `sharepoint_rename` | no | `{"from": "current", "to": "future"}` when the new tenant's SharePoint will be renamed |

</details>

## 5. Run the migration

Work down the page, or use the [commands](commands.md):

1. **Choose what to migrate.** Tick the parts you need. OneDrive is off by default because it can be very large.
2. **Export.** Open a few pages in **Backup** to see that it looks right.
3. **Match users.** Every row should have its new address. Leave a row empty only for people who won't exist in the new tenant. **Save matching.**
4. **Import one small team first.** Type its name, start the import, and check the result in Teams. Then import everything.
5. **Finishing touches** and **Old-link clean-up**, as needed.
6. When you are done, follow [cleanup.md](cleanup.md) to remove the apps and the local copy.

## E-mail and calendars

These are not handled by this tool. A free route:

- **Old tenant:** Purview → Content search → export the mailboxes as PST files.
- **New tenant:** import each PST with Outlook desktop (File → Open & Export → Import), or with Purview's free *PST import (network upload)*.
