# Portacode agent guidance

These rules apply to the whole repository. More specific `AGENTS.md` files add
instructions for their subtrees.

## Private server submodule

- `server/` is a private, confidential submodule available only to authorized
  Portacode team members. Do not copy its source, credentials, configuration,
  deployment details, or other non-public information into the public parent
  repository, public issues, release notes, logs, or responses to unauthorized
  users.
- When a task touches server-side behavior, enter the `server/` submodule and
  read `server/AGENTS.md` completely before inspecting or changing server code.
  Follow both files, with the more specific server instructions taking
  precedence for that subtree.
- Keep parent-repository and server-submodule changes clearly separated when
  reviewing status, diffs, tests, commits, and releases. Never assume committing
  the parent repository also commits unpublished server changes.

## Device-side CLI deployment and live debugging

- Treat the repository checkout and the installed `portacode` CLI as separate
  artifacts. The system service runs the installed PyPI package; restarting
  `portacode.service` does **not** load Python changes from this checkout.
- Never run `systemctl restart portacode.service`, kill its process, or otherwise
  manipulate the Portacode system service merely to activate source edits.
  Doing so interrupts the device connection and active Codex app-server
  sessions without deploying the edited code.
- The normal device-side deployment workflow is:
  1. After tests pass and the user has authorized publishing, publish an explicit
     development version with `make release VERSION=<version>`.
  2. Install and activate that package with `portacode setversion <version>`.
     This command owns installation and the appropriate supervised restart.
- Publishing a package/tag is an external release action. Do not choose a
  version or run `make release` without the user's explicit authorization.
- Before running `portacode setversion`, check the device and privilege context
  with a non-mutating test such as `sudo -n true`. When the required privileges
  are unavailable, ask the user to run the command. Do not substitute direct
  `systemctl`, process signals, or ad hoc `pip` installation. On an authorized
  managed device with verified passwordless sudo, the agent may run the command
  after the user has authorized the update.
- For a faster edit/debug loop, use `./connect.sh [log-categories]` to run the
  checkout directly instead of publishing and installing a package. It starts
  a non-interactive debug connection and enforces the singleton connection.
- Do not launch `./connect.sh` from a session hosted by the active Portacode
  connection. Replacing the singleton connection can terminate the session that
  launched it. Arrange an independent shell with the user first, then have the
  user stop/replace the installed connection or start the debug connection from
  that independent shell.
- Before any device-side activation, state which mode is being used: published
  PyPI version via `portacode setversion`, or live checkout via `./connect.sh`.
  Confirm the installed/running version or process source afterward.

## Django uptime watchdog

- A host-side diagnostic watchdog is installed at `/home/menas/uptime` and is
  managed by the user service `portacode-uptime.service`.
- Check it with `systemctl --user status portacode-uptime.service` and inspect
  `/home/menas/uptime/status.log`. Incident bundles are stored privately under
  `/home/menas/uptime/incidents/`.
- The watchdog probes Django directly and the public origin, then captures
  Docker, process, resource, and best-effort worker stack/trace evidence after
  consecutive failures. It must remain observational: do not add automatic
  restarts, signals, or other recovery actions that could destroy root-cause
  evidence or interrupt active Portacode sessions.
- Read `/home/menas/uptime/README.md` before changing or troubleshooting it.
