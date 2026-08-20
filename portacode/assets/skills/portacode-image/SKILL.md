---
name: portacode-image
description: Generate new raster images or edit existing images through the local Portacode device API with recorded token usage and cost. Use whenever Codex on a Portacode device is asked to generate, create, transform, or edit a photo or other bitmap image.
---

# Portacode Image

Use `portacode-image` exclusively. It calls the device-authenticated, usage-metered
`/v1/images/*` endpoints. Do not request the Responses API native
`image_generation` tool and do not call OpenAI image endpoints directly.

## Generate

```bash
portacode-image generate "<detailed prompt>" --out <new-file.png> \
  --size 1024x1024 --quality auto
```

## Edit

```bash
portacode-image edit "<precise edit instructions>" --image <source.png> \
  --out <new-file.png> --size 1024x1024 --quality auto
```

Repeat `--image` for multiple inputs. Preserve source files and write a new output
unless the user explicitly requests replacement; only then add `--force`.

After the command succeeds, inspect the output image and report its absolute path.
The command prints recorded upstream usage as JSON. If the local service is
unavailable, report that the installed Portacode device agent must be updated or
connected. Never fall back to an unmetered image path.
