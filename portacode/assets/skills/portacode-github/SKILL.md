---
name: portacode-github
description: Create a new GitHub repository through a Portacode device's account-scoped permission and push a local project to it. Use when Codex on a Portacode device is asked to create, publish, or push to a new GitHub repository, including personal and organization repositories.
---

# Portacode GitHub

Use Portacode's device-authenticated command. Do not use `gh`, SSH authentication,
personal access tokens, or the GitHub REST API directly.

1. Accept the requested destination as `OWNER/REPOSITORY`. The owner selects the
   matching permitted personal or organization connection automatically.
2. Create a private repository unless the user explicitly requests public:

   ```bash
   python "$CODEX_HOME/skills/portacode-github/scripts/create_repository.py" OWNER/REPOSITORY
   ```

   Add `--public` only when requested and `--description "..."` when useful.
3. If the command returns an approval URL, give that link to the user and retry the
   same command after approval. Do not ask for an installation ID or a separately
   entered account name.
4. Configure scoped HTTPS credentials, set the remote, and push:

   ```bash
   portacode github-setup
   git remote add origin https://github.com/OWNER/REPOSITORY.git
   git push -u origin HEAD
   ```

   If `origin` already exists, inspect it. Change it only when needed for the user's
   requested destination. The new repository is already available to this device;
   do not announce an internal read/write grant unless asked.
