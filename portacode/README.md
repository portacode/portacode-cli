# Portacode Python package

This directory contains Portacode's optional device-side agent and the
`portacode` command-line tool. It connects an existing machine to the Portacode
AI software builder and operations workspace. Users who start on
Portacode-hosted infrastructure do not need to install this package.

See the [repository README](../README.md) for the product overview and the
[pairing guide](https://portacode.com/docs/pair-device/) for installation.

This package is distributed under the [PolyForm Shield License 1.0.0](../LICENSE).
Third-party dependencies and bundled components remain under their own licenses.

## Important modules

| Module | Responsibility |
|--------|---------------|
| `cli.py` | Implements the Click-based command-line interface. |
| `data.py` | Determines and manages the cross-platform user-data directory. |
| `keypair.py` | Generates, stores and fingerprints the RSA key-pair. |
| `connection.client` | Maintains a resilient WebSocket connection to the Portacode gateway. |
| `connection.multiplex` | A tiny multiplexer that lets you open unlimited virtual channels over the single WebSocket connection. |

Each sub-package contains its own README for easier discovery.
