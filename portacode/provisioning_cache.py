"""Versioned contract for node-local provisioning images.

Bump ``PROVISIONING_CACHE_ID`` before publishing a Portacode release whenever
the cacheable package/tool layer changes.  It deliberately does not follow the
package version: most CLI releases do not require rebuilding large LXC images.
"""

PROVISIONING_CACHE_ID = "2026-08-21.1"
