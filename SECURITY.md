# Security

## What this tool handles

It works with a full copy of a company's Teams messages and files, and with app secrets that can read or write a whole tenant. Treat a migration machine like a production server:

- `config.json` (app secrets) and `export/` (all data) never leave the machine and are git-ignored.
- The browser interface listens only on `127.0.0.1`, checks the `Host` header and requires a custom header on every change, so other websites can't drive it.
- Grant only the permissions the steps you use need. Delete both app registrations when you're done (see [docs/cleanup.md](docs/cleanup.md)).

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Use GitHub's **"Report a vulnerability"** button on the repository's *Security* tab to send a private report. Include the steps to reproduce, but no real tenant data.
