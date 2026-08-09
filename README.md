# Portacode

**Build it. Own it. Take it anywhere.**

Portacode is an AI-powered builder and mobile-first development environment that works on real Linux machines. Describe what you want to build, let Portacode work through the implementation on a machine you control, and keep the result as ordinary source code, data, containers, and configuration.

It combines an AI coding agent with the tools developers expect from a real machine: a terminal, files, Git, diffs, processes, and deploys. Use hardware you already have, connect a VPS or homelab, deploy to supported infrastructure, and move the project when your needs change.

[Get started](https://portacode.com/accounts/signup/) · [Open Portacode](https://portacode.com/) · [Android app](https://play.google.com/store/apps/details?id=com.portacode.app) · [Report an issue](https://github.com/portacode/portacode/issues)

## Why Portacode

- **You own the result.** Projects remain ordinary files on an ordinary machine, not artifacts trapped in a proprietary runtime.
- **Real tools, not a preview sandbox.** Work with a real terminal, filesystem, Git repository, services, containers, databases, and deploys.
- **Your infrastructure is a choice.** Connect an existing machine, use managed capacity, or run projects on your own supported infrastructure.
- **Leaving is supported.** Back up, transfer, or move a project instead of rebuilding it around a platform-specific format.
- **AI changes stay reviewable.** Inspect diffs and project state while the agent works, and decide what it is allowed to change.
- **Built for mobile as well as desktop.** Browse a project, review changes, use a terminal, and respond to work without waiting to get back to a desk.
- **No approved-stack list.** If your language, framework, database, or tool runs on the underlying Linux machine, it can be part of your project.

## What You Can Do

Portacode supports more than remote terminal access. From the same browser-based workspace you can:

- ask an AI agent to create, change, debug, and deploy software;
- inspect files, branches, Git status, and line-by-line diffs;
- work directly in persistent terminal sessions;
- connect and switch between your own machines;
- provision clean environments from reusable deployment templates;
- run repeatable build, test, deployment, and runbook workflows from YAML;
- expose services through HTTPS and custom domains on supported infrastructure;
- manage projects from a phone, tablet, or desktop.

The product evolves quickly. The live [Portacode website](https://portacode.com/) and [technical guides](#guides) are the source of truth for currently available integrations, templates, limits, and infrastructure options.

## Start Building

You do not need to prepare a machine before trying Portacode. Start at [portacode.com](https://portacode.com/), describe the outcome you want, and choose where the project should run.

To bring an existing machine into Portacode, pair it once using the dashboard and the CLI below.

## Connect Your Own Machine

Portacode's Python package is the device-side CLI and agent. It connects an existing Linux/Python-capable machine to your Portacode account so the web workspace can work with its projects and developer tools.

### Recommended Linux setup

1. Sign in to [Portacode](https://portacode.com/) and select **Pair Device**.
2. Run the activation command shown there on the target machine. It installs Portacode in a dedicated virtual environment, pairs the device, and configures its persistent service.
3. Approve the pairing request in the dashboard.

The command has this form; use the short-lived code displayed in your own dashboard:

```bash
curl -fsSL https://portacode.com/static/activate_portacode.sh \
  | bash -s -- --pairing-code YOUR_CODE
```

Review the downloaded script before running it if that is required by your environment. See the [pairing guide](https://portacode.com/docs/pair-device/) for supported distributions, a fully manual installation, and Proxmox-specific setup.

### Manual CLI setup

If you already have a suitable Python environment:

```bash
python -m pip install --upgrade portacode
portacode connect --pairing-code YOUR_CODE \
  --device-name "My Linux Device" \
  --project-path /absolute/path/to/project
```

Approve the request in the dashboard. The device stores its identity locally and reuses it for later connections. For an always-on connection, install the supervised service after pairing:

```bash
portacode service install
portacode service status
```

System-wide service installation may require elevated privileges. Run `portacode --help`, `portacode connect --help`, or `portacode service --help` for the options supported by the installed version.

## Automation and Deployments

A `portafile.yaml` can describe a repeatable workflow for a new disposable environment or an existing device. Depending on the workflow, it can select a base environment, request resources, create project paths, collect inputs, run ordered build/test/deploy steps, expose services, and define success or failure behavior.

This keeps automation portable and reviewable alongside the project while Portacode handles orchestration and status reporting. The schema is intentionally documented on the live site because it gains capabilities more frequently than the CLI README changes.

See the [CI/CD and `portafile.yaml` reference](https://portacode.com/portacode-cicd-intro/) or browse [one-click deployment templates](https://portacode.com/one-click-deployment-templates/).

## Your Infrastructure

Existing Linux machines can be paired directly. For self-hosted provisioning, a supported Proxmox node can become Portacode infrastructure, allowing projects and automation to create isolated environments against the capacity you control. Services can be connected to custom domains through the supported Cloudflare Tunnel flow.

- [Set up a Proxmox infrastructure node](https://portacode.com/docs/proxmox-infra-node-setup/)
- [Connect a domain](https://portacode.com/docs/cloudflare-domain-tunnel-setup/)
- [Read current usage limits](https://portacode.com/usage-limits/)

## Security and Control

Each paired device has its own cryptographic identity. The private key is stored on the device, connections are encrypted in transit, and access can be revoked from Portacode. Pairing codes are short-lived and a new device still requires approval in the dashboard.

AI work is designed around visible project state and reviewable diffs. As with any remote administration or coding-agent tool, connect only machines and project paths you intend Portacode to access, review proposed changes, and use appropriate operating-system permissions and backups.

## Identity Storage and Containers

The CLI uses the operating system's application-data directory (through [`platformdirs`](https://pypi.org/project/platformdirs/)) and stores its device identity under `portacode/keys/`. Typical locations are:

- Linux: `~/.local/share/portacode/keys/`
- macOS: `~/Library/Application Support/portacode/keys/`
- Windows: `%APPDATA%\portacode\keys\`

When running the CLI in a container, persist the relevant application-data directory so the container retains its device identity across rebuilds. The examples demonstrate the pattern:

- [`examples/simple_device`](https://github.com/portacode/portacode/tree/master/examples/simple_device) — one container with a persistent workspace and identity
- [`examples/workshop_fleet`](https://github.com/portacode/portacode/tree/master/examples/workshop_fleet) — a multi-seat lab with separate persistent workspaces

## Guides

- [Pair an existing device](https://portacode.com/docs/pair-device/)
- [CI/CD and `portafile.yaml`](https://portacode.com/portacode-cicd-intro/)
- [One-click deployment templates](https://portacode.com/one-click-deployment-templates/)
- [Self-host on Proxmox](https://portacode.com/docs/proxmox-infra-node-setup/)
- [Connect a custom domain](https://portacode.com/docs/cloudflare-domain-tunnel-setup/)
- [Current usage limits](https://portacode.com/usage-limits/)

## Contributing and Support

Bug reports, focused fixes, and documentation improvements are welcome. Open an [issue](https://github.com/portacode/portacode/issues) or a pull request in this repository.

For product support, email [support@portacode.com](mailto:support@portacode.com).

## License

