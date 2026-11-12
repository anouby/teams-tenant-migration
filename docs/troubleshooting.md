# Troubleshooting

First, look at the end of the activity log. Every run ends with a list of problems, and each one names the item and Microsoft's answer. Re-running the same step retries only what failed.

## Sign-in and permissions

| Message | Meaning | Fix |
|---|---|---|
| `sign-in to the OLD/NEW tenant failed` | Wrong tenant ID, client ID or secret | Copy the secret **Value**, not the Secret ID. The client ID is the *Application (client) ID* |
| `the app in the … tenant is missing these permissions` | Admin consent not granted, or a permission missing | Add it in API permissions and click **Grant admin consent** |
| 403 on chat or channel messages during export | Protected Teams APIs not approved yet | Submit the "Microsoft Teams protected APIs" request, see [setup.md](setup.md#teams-protected-apis) |
| `the fallback sender … does not exist in the NEW tenant` | Typo, or the sign-in name was changed | Use the account's current sign-in name |
| `these new users in the matching table don't exist` | A row in `users.csv` points at a missing or renamed account | Fix the row in **Match users** and save |

## Import

| Message | Meaning | Fix |
|---|---|---|
| 429 Too Many Requests | Throttled | Nothing. The tool waits and continues |
| `rejected, retrying with less detail` | Teams refused part of a message | Nothing. It is retried without reactions, mentions or images, or as text |
| 409 on file upload (`nameAlreadyExists`, `resourceModified`) | Two uploads raced | Re-run. Finished files are skipped |
| `already in migration mode` / `already completed migration` | A previous run stopped half-way, or Teams' state hasn't settled | Re-run after a few minutes |
| A chat refuses new messages from users | It was left in migration mode | Re-run the step that touched it; every run completes migration on what it started |
| Group chat looks empty in Teams | Members don't see history | Handled by `import` and `rebuild-chats`. For chats made by older versions, rebuild them |
| Network or DNS errors | Connection dropped | Nothing. The tool waits for the internet and continues |

## Files and links

| Message | Meaning | Fix |
|---|---|---|
| 423 Locked | The old OneDrive is locked (often a disabled user) | Unlock it in the old tenant, or accept it as not available |
| 404 on a linked file | Deleted in the old tenant | Nothing to copy. The message shows the plain file name |
| `old_sharepoint` missing | Extras need the old SharePoint name | Settings → Advanced → old tenant's SharePoint name |

## Old-link clean-up (`clean-delete`)

| Message | Meaning | Fix |
|---|---|---|
| **AADSTS530035** | Security defaults block device-code sign-in | Nothing to do. This tool uses a browser sign-in |
| **AADSTS500113** *No reply address is registered* | The redirect address is missing | Add `http://localhost:8799` under **Mobile and desktop applications** (not *Web*) |
| **AADSTS7000218** | Public client flows are off | App → Authentication → **Allow public client flows: Yes** |
| *You don't have access to this* (530035 on the page) | Same as the first row | — |
| *Failed to get license information for the user* | The signed-in person has no Teams license | Assign a license. It takes a few minutes, and the tool keeps retrying |
| *AclCheckFailed … is not a member* | Channel message, and the sender isn't in the team | Add them to the team temporarily, and remove them afterwards |
| 402 Payment Required | You tried the app-only hide (`policyViolation`) | Needs E5-level compliance licenses. Use the sender sign-in instead |
| 412 `not supported in application-only context` | App-only delete | Same: only the sender may delete |

## Still stuck?

Open an issue with:
- the command
- the last 30 lines of the log, **with names, addresses and IDs removed**
- your Python version

See [CONTRIBUTING.md](../CONTRIBUTING.md).
