# Permissions

All permissions are **Microsoft Graph application permissions** with admin consent, unless marked *delegated*. Grant only what the steps you use need, and delete both apps when you are done.

## Old tenant app (read-only)

| Permission | Used by | Why |
|---|---|---|
| `User.Read.All` | export, map | List users to match them |
| `Group.Read.All` | export | List teams |
| `TeamSettings.Read.All` | export | Team settings |
| `Channel.ReadBasic.All` | export | List channels |
| `ChannelMember.Read.All` | export | Private and shared channel members |
| `TeamMember.Read.All` | export | Team members and owners |
| `ChannelMessage.Read.All` | export | Channel messages. *Protected API: request access first* |
| `Chat.Read.All` | export | One-to-one, group and meeting chats. *Protected API* |
| `Files.Read.All` | export, relink-files | Files, OneDrive, and files that messages link to |
| `Sites.Read.All` | export | Channel file libraries |
| `ProfilePhoto.Read.All` | copy-photos | Profile photos |

## New tenant app (write)

| Permission | Used by | Why |
|---|---|---|
| `User.Read.All` | all | Look up the matched users |
| `Team.Create`, `Group.ReadWrite.All`, `TeamSettings.ReadWrite.All` | import | Create teams and apply settings |
| `Channel.Create`, `ChannelMember.ReadWrite.All` | import | Channels and their members |
| `TeamMember.ReadWrite.All` | import | Team members and owners |
| `Teamwork.Migrate.All` | import, clean-copy | **Migration mode**: post messages with the original sender and date |
| `Chat.Create`, `Chat.ReadWrite.All`, `ChatMember.ReadWrite.All` | import, rebuild-chats, clean-scan | Create chats, read them back, and give members the full history |
| `Files.ReadWrite.All`, `Sites.ReadWrite.All` | import, relink-files | Upload files and create organization-only share links |
| `Chat.ManageDeletion.All` | rebuild-chats | Delete a group chat before rebuilding it |
| `ProfilePhoto.ReadWrite.All` | copy-photos | Set profile photos |

## Old-link clean-up (`clean-delete`)

Microsoft only lets the **sender** delete a message, so this step signs a person in through the browser:

| Setting on the new-tenant app | Value |
|---|---|
| Delegated permission | `Chat.ReadWrite` |
| Delegated permission | `ChannelMessage.ReadWrite` |
| Authentication → Mobile and desktop applications → redirect URI | `http://localhost:8799` |
| Authentication → Allow public client flows | Yes |

Remove these when you are done. See [cleanup.md](cleanup.md).

## Permissions you might be tempted to add, and why they don't help

| Permission | What happened |
|---|---|
| `Chat.UpdatePolicyViolation.All`, `ChannelMessage.UpdatePolicyViolation.All` | They can hide a message app-only, but Microsoft answers **402 Payment Required** unless the *sender* has an E5-level compliance license |
| `OnlineMeetingTranscript.Read.All`, `OnlineMeetings.Read.All` | Transcripts also need a Teams *application access policy* created in PowerShell. Not used by this tool |
| `Policy.Read.All` | Useful only to verify cross-tenant access settings at the end. See [cleanup.md](cleanup.md) |
