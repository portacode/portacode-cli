# Portacode

[![License: PolyForm Shield](https://img.shields.io/badge/license-PolyForm%20Shield-blue.svg)](https://polyformproject.org/licenses/shield/1.0.0)

**One prompt. The whole software lifecycle.**

Portacode is an AI software builder and operator. Describe the outcome you want and its AI can provision a real computer, build the frontend and backend, test the application in a browser, deploy it, monitor it, and help repair failures. **No coding or server setup is required to start.**

Unlike builders that stop at a preview or exported repository, Portacode gives you the complete working system: source, data, services, containers, configuration, and runtime. Start on Portacode-hosted infrastructure, or connect your own machine when you want to. The result remains portable either way.

[Start building](https://portacode.com/) · [Deployment templates](https://portacode.com/one-click-deployment-templates/) · [Android app](https://play.google.com/store/apps/details?id=com.portacode.app) · [Report an issue](https://github.com/portacode/portacode-cli/issues)

> [!NOTE]
> This repository contains Portacode's open-source device agent and CLI. Installing it is optional: new users can start with a Portacode-hosted workspace without preparing a machine.

## From Idea to Running Software

Portacode's AI can carry work across the application lifecycle:

- **Build:** provision a clean workspace and create the application, backend, database, authentication, storage, dependencies, and services your project needs.
- **Test:** run builds and tests, open the real application in a browser, inspect results, and capture screenshots.
- **Deploy:** expose services through HTTPS and custom domains, using reusable workflows or a stack assembled for your project.
- **Operate:** inspect files, Git, terminals, processes, services, and resource usage; receive runtime alerts and completion notifications.
- **Fix:** return build, test, or deployment failures to the AI so it can investigate, change the system, and try again.
- **Move:** transfer the project and its working environment between Portacode-hosted and user-controlled machines.

## Start Simple. Go as Deep as You Want.

You can begin with an ordinary-language request and let Portacode choose the environment and handle the engineering. If you want more control later, the real files, Git repository, terminal, browser, processes, services, and infrastructure remain available. There is no approved-stack list: if a language, framework, database, or binary runs on the underlying Linux machine, it can be part of the project.

Portacode itself is mobile-first. Direct the AI, inspect files and diffs, use terminals, deploy services, monitor machines, and respond to alerts from a phone, tablet, or desktop. This is separate from the kind of application you build: Portacode can work with whichever web, desktop, or mobile stack the project requires.

## Own the Working System

Ownership in Portacode means more than downloading a code repository. Source, application data, containers, services, and configuration remain ordinary assets on a real machine. Use Portacode-hosted capacity for a zero-setup start, connect a computer or VPS you already have, or provision against supported self-hosted infrastructure. Back up or move the complete project when your needs change.

AI access remains controllable and reviewable. Inspect project state and line-by-line diffs, grant access only to selected devices or repositories, and decide what the agent may change.

## Start Building

Start at [portacode.com](https://portacode.com/), describe the outcome you want, and let Portacode prepare the workspace. You do not need to install this package or supply a server first.

The rest of this README documents the optional device agent for bringing an existing machine into Portacode.

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

- [`examples/simple_device`](https://github.com/portacode/portacode-cli/tree/master/examples/simple_device) — one container with a persistent workspace and identity
- [`examples/workshop_fleet`](https://github.com/portacode/portacode-cli/tree/master/examples/workshop_fleet) — a multi-seat lab with separate persistent workspaces

## Guides

- [Pair an existing device](https://portacode.com/docs/pair-device/)
- [CI/CD and `portafile.yaml`](https://portacode.com/portacode-cicd-intro/)
- [One-click deployment templates](https://portacode.com/one-click-deployment-templates/)
- [Self-host on Proxmox](https://portacode.com/docs/proxmox-infra-node-setup/)
- [Connect a custom domain](https://portacode.com/docs/cloudflare-domain-tunnel-setup/)
- [Current usage limits](https://portacode.com/usage-limits/)

## Contributing and Support

Bug reports, focused fixes, and documentation improvements are welcome. Open an [issue](https://github.com/portacode/portacode-cli/issues) or a pull request in this repository.

For product support, email [support@portacode.com](mailto:support@portacode.com).

## License

Portacode's device-side client is available under the [PolyForm Shield License 1.0.0](LICENSE).
You may view, modify, self-host, and use it for non-competing purposes. You may
not use it to build or offer a competing product or service.

Third-party components remain under their own licenses. See their accompanying
notices and license files where provided.
