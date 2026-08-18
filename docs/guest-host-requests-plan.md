# Guest Host Requests: additive migration plan

> Implementation status: Phase 0 protocol constants, fail-closed validators,
> default-off operation flags, and the unused direct singleton delivery
> primitive are implemented. No HTTP endpoint has migrated yet.

## Goal

Allow an authenticated owner of a managed guest to request a narrowly scoped
operation on its Proxmox host without creating a synthetic browser/client
session authenticated as the host owner.

The existing singleton WebSocket connection from the Portacode CLI to the
server remains the transport. The change is the message class and its
authorization identity: the server sends a **Guest Host Request** through the
existing host connection, and the CLI returns a correlated result.

**Guest Host Request** is the canonical name for this pattern. It is a
request—not an authoritative command—because the server authorizes routing and
the host CLI still validates the request scope before execution.

This plan is intentionally additive. Existing client-session messages,
`on_behalf_of_device` response routing, provisioning behavior, and ordinary
device commands remain supported throughout the migration.

## Invariants

1. The HTTP actor is authorized against the guest device, not the host owner.
2. The server chooses the host from the guest's recorded `proxmox_parent`.
3. The host CLI accepts only an explicit allowlist of Guest Host Request types.
4. Every operation carries a request ID, guest device ID, command name, and
   authorization metadata.
5. The CLI validates the guest/container relationship again before changing
   Proxmox state.
6. Results are correlated by request ID and identify the guest through
   `on_behalf_of_device` when routed back to the guest owner.
7. No request may name an arbitrary host, VMID, or host-owner identity.
8. Each phase has a feature flag or compatibility fallback and can be reverted
   without changing existing records or deleting durable state.

## Message contract

Use a distinct envelope instead of pretending that a client session sent the
request:

```json
{
  "type": "guest_host_request",
  "command": "configure_proxmox_container_expose_ports",
  "request_id": "...",
  "target_device_id": "guest-id",
  "authorization": {
    "principal_type": "user",
    "principal_id": "user-id",
    "principal_role": "owner",
    "principal_device_id": "guest-id",
    "operation": "expose_ports"
  },
  "payload": {
    "child_device_id": "guest-id",
    "expose_ports": []
  }
}
```

The CLI responds with a standard result envelope:

```json
{
  "type": "guest_host_result",
  "request_id": "...",
  "command": "configure_proxmox_container_expose_ports",
  "target_device_id": "guest-id",
  "success": true,
  "result": {}
}
```

The server may then route the result to the guest owner using the existing
response mechanism, setting `on_behalf_of_device` to the guest ID. That field
is a routing assertion, not an authorization substitute: the server must
validate the host/guest relationship before forwarding it.

Standard wire message names are:

- `guest_host_request` — validated request delivered to the host;
- `guest_host_ack` — host acknowledgement that the request was accepted for
  processing;
- `guest_host_progress` — optional correlated progress update;
- `guest_host_result` — terminal success or structured failure.

## Role-aware authorization and future collaboration

Guest ownership must not be hard-coded into the transport protocol. The first
policy grants the owner access, but the authorization layer should support
future limited sharing and collaboration roles without adding another message
type or weakening host isolation.

Model authorization as a tuple:

```text
(principal, guest_device, role, request_type, scope, decision)
```

Examples of future roles may include `owner`, `administrator`, `operator`,
`developer`, `viewer`, or project-specific roles. Role names alone must not
grant authority in CLI payloads. The server resolves the authenticated
principal's current assignment and evaluates explicit permissions such as:

- `guest_host.expose_ports.read`
- `guest_host.expose_ports.write`
- `guest_host.power.start`
- `guest_host.power.stop`
- `guest_host.delete`
- `guest_host.provision`

The request envelope may carry the server's evaluated role and permission for
audit/display purposes, but user-supplied role or permission fields are never
trusted. The CLI enforces operation and guest scope; it does not independently
interpret application collaboration roles because the server is authoritative
for users, memberships, revocations, and role policy.

Authorization must be evaluated at dispatch time. Long-running or queued
requests should record the authorization decision and policy version; policy
must specify whether execution continues after a later role revocation.
High-impact requests such as deletion should revalidate authorization before
execution when practical.

Role-aware results and progress use the same `on_behalf_of_device` routing.
The server determines which current principals may observe them based on
guest membership plus message/request-type permissions. A collaborator must
not automatically receive every event merely because they can perform one
operation.

### Role and sharing tests

- Owner receives the initial full policy defined for owners.
- A collaborator can invoke only request types granted to their role.
- Viewer/read permissions cannot perform mutations.
- Role information injected into an HTTP or WebSocket payload is ignored.
- Revoked and expired memberships fail closed.
- A role on Guest A grants no access to Guest B or the Proxmox host.
- Result/progress visibility is independently enforced from invocation rights.
- Policy-version changes and queued-request revocation behavior are tested.
- The dashboard shows the principal, role, evaluated permission, policy
  version, and decision that applied to each request.

## Message provenance and dashboard representation

The new transport must not erase who or what initiated an operation. Every
request, acknowledgement, result, progress event, and failure should carry a
durable provenance record separate from the transport target.

Use an explicit origin classification, for example:

- `authenticated_user` — a logged-in user initiated the request;
- `user_client_session` — the request came from a browser/client session;
- `server_worker` — an expiry, deletion, provisioning, or reconciliation
  worker initiated it;
- `scheduled_automation` — a configured automation or recurring job initiated
  it;
- `device` — a device-originated event or response;
- `system` — an internal administrative/system action.

For user-originated requests, persist the authenticated actor and request
context where available:

- user ID and username/display name;
- authenticated session/API-token class (never the token itself);
- source IP and country/region derived by the existing privacy policy;
- user-agent, device type, operating system, and browser/client version;
- client session ID, if one exists;
- target guest device, selected Proxmox parent, command, request ID, and
  timestamp.

For automated requests, persist a structured automation identity instead of
inventing a user:

- `origin_type` and stable worker/job name;
- worker instance or run ID;
- trigger (`expiry`, `quota`, `reconciliation`, `provisioning`, etc.);
- parent task/request ID and schedule ID where applicable;
- acting service/account ID if the deployment has one.

Do not copy host-owner details into a guest operation merely because the host
connection is authenticated as that device. The transport host and the
originating actor must remain separate fields.

### Dashboard behavior

The server state dashboard and operation/detail views should show a compact
provenance badge and an expandable detail panel for every automated message
and operation result. Examples:

- `Requested by Alice (alice@example.com) · browser · Linux · Firefox · US`
- `Automated worker · expired-device-cleanup · run 1234`
- `Scheduled automation · nightly-provisioning · schedule 8`
- `Device response · Proxmox host pve2 · on behalf of guest 842`

The dashboard must distinguish:

1. the actor/origin that authorized or initiated the request;
2. the transport path used to deliver it;
3. the device that executed it;
4. the guest device represented by `on_behalf_of_device`;
5. the final result, retry count, and timestamps.

User personal data should be displayed according to existing account/privacy
rules, with privileged details restricted to authorized administrators. Logs
and dashboard payloads must redact credentials, tokens, private keys, and full
untrusted command payloads.

### Likely elements

- Existing request/event/audit models and serializers in the server.
- Server WebSocket request/result persistence and notification helpers.
- `server/portacode_django/analytics` event recording and dashboard views.
- Dashboard state/operation detail templates or React serializers.
- Worker commands such as expired-device cleanup and automation/provisioning
  workers.
- CLI result envelopes and device-originated event serializers.

### Provenance tests

- A browser request displays the authenticated username and approved request
  context, without exposing credentials.
- An API-token request identifies the token class/service, never the token.
- A deletion worker is labeled `server_worker` with its worker/run/trigger
  details and no fake human user.
- A scheduled automation is labeled with its automation and schedule identity.
- A device response shows both the executing host and validated
  `on_behalf_of_device` guest.
- Provenance cannot be changed by user-supplied payload fields.
- Cross-tenant users cannot view another tenant's provenance details.
- Old messages without provenance remain readable with an explicit
  `legacy/unknown origin` label.

### Reversibility

Add provenance fields as nullable/versioned fields and preserve existing event
payloads. Rollout can begin in write-only mode, then enable dashboard display;
disabling the display does not remove audit data. Existing message routing and
operation behavior remain unchanged if provenance persistence is unavailable.

## Phase 0 — freeze the contract and add a kill switch

### Changes

- Add protocol constants/schema helpers for the two new message types.
- Add a server-side feature flag, disabled by default, for each migrated
  operation.
- Document command allowlists, payload limits, deadlines, idempotency, and
  result/error categories.
- Keep the old dispatch path untouched as the fallback.

### Likely files/elements

- `portacode/connection/handlers/WEBSOCKET_PROTOCOL.md`
- Existing server WebSocket consumer/message constants.
- Existing CLI connection message dispatch/registration modules.
- New shared command-contract module if the repository convention supports it.

The implementation is not complete until the canonical protocol and reference
documentation are updated. At minimum, update:

- `portacode/connection/handlers/WEBSOCKET_PROTOCOL.md` with all four wire
  envelopes, field semantics, direction, acknowledgements, errors,
  `on_behalf_of_device`, provenance, role/permission metadata, replay rules,
  examples, and compatibility behavior;
- `portacode/connection/README.md` and handler documentation with the transport
  lifecycle and the distinction from client sessions;
- relevant server architecture/dashboard documentation with authorization,
  singleton routing, persistence, provenance, and visibility rules;
- API reference documentation for every endpoint migrated to a Guest Host
  Request;
- worker/automation references identifying which automated jobs may originate
  each request type;
- rollout/runbook documentation covering feature flags, mixed-version hosts,
  monitoring, and rollback.

Add a documentation test or CI check, where practical, that the registered
Guest Host Request types and protocol reference remain synchronized.

### Tests

- Envelope serialization and schema rejection tests.
- Unknown command, missing request ID, mismatched target, oversized payload,
  expired request, and replay tests.
- Feature flag tests proving disabled operations use the old path.

### Reversibility

No existing behavior changes. Remove the constants/schema helpers and disable
the flags if the phase is reverted.

## Phase 1 — deliver Guest Host Requests through the existing WebSocket singleton

### Changes

Add a low-level server-to-device delivery primitive beside the current
`DeviceService`/client-session dispatch. It should:

- locate the authenticated singleton connection for the selected host device;
- send the new envelope directly over that connection;
- generate or require a request ID;
- support acknowledgement, synchronous result, and queued delivery;
- never construct a client session using `parent_user_id`;
- never use the host owner as the apparent actor;
- reject offline devices, unknown devices, and wrong connection targets.

Do not remove or alter the existing client-session helper yet. Existing
commands continue using it.

### Likely files/elements

- `server/portacode_django/dashboard/views/device_views.py`
  (`_dispatch_proxmox_container_command` vicinity).
- The server WebSocket singleton/connection registry and request-correlation
  implementation.
- Existing device connection service used to access a live device socket.

### Tests

- Direct delivery reaches exactly the selected host connection.
- No synthetic client session or host-owner identity is created.
- Request/result correlation, timeout, offline host, duplicate request ID, and
  late result behavior.
- Existing client-session command tests remain green.

### Reversibility

The new helper is unused until a feature flag is enabled. Revert the helper
and protocol tests without changing the existing dispatch implementation.

## Phase 2 — add CLI recognition and standardized responses

### Changes

Add a separate CLI dispatch branch for `guest_host_request`; do not route it
through ordinary client-session command handling.

The CLI must verify:

- the message arrived on the authenticated server/device channel;
- the command is allowlisted;
- `request_id`, `target_device_id`, and `child_device_id` are present and
  consistent;
- the command is not expired or already processed;
- the operation is scoped to the recorded managed child.

Return `guest_host_result` for both success and structured failure, with
`guest_host_ack` and `guest_host_progress` where applicable.
For guest-facing results, use the existing response path and set
`on_behalf_of_device` to the guest ID. Preserve the current host-to-guest
routing guard and add tests proving cross-host results are rejected.

### Likely files/elements

- `portacode/connection/client.py`
- `portacode/connection/terminal.py`
- Existing CLI message dispatch/handler registration.
- `portacode/connection/handlers/proxmox_infra.py`
- Existing `on_behalf_of_device` response construction and routing.

### Tests

- Valid Guest Host Request is accepted and acknowledged.
- Ordinary client messages cannot invoke server-only commands.
- Wrong child ID, wrong VMID, unmanaged container, missing marker, unknown
  command, malformed payload, expired request, and replay are rejected.
- Result includes the same request ID and guest identity.
- Host A cannot emit a result on behalf of a child belonging to Host B.

### Reversibility

Leave the new branch disabled unless the feature flag is enabled. The old
handler remains available and no existing message type is reinterpreted.

## Phase 3 — create a server-side guest-operation service

### Changes

Create one explicit service/registry for guest-to-host operations. HTTP views
must call this service rather than constructing command names and payloads
directly.

Each operation performs, in order:

1. Load the child with its recorded `proxmox_parent`.
2. Authorize the authenticated principal against the guest, role, request
   type, and required permission (initially the guest owner policy).
3. Validate that the parent is the selected Proxmox host.
4. Validate the operation-specific payload and limits.
5. Generate an audit/request record and idempotency key.
6. Dispatch through the direct server-device primitive.

The service should expose operation objects such as `expose_ports`,
`start_container`, `stop_container`, and later `remove_container`.

### Likely files/elements

- New `server/portacode_django/dashboard/services/guest_host_operations.py`.
- `server/portacode_django/dashboard/views/device_container_views.py`.
- Existing managed-device deletion and provisioning services.
- Existing port normalization and ownership helpers.

### Tests

- Owner success; another guest receives the existing privacy-preserving error.
- Unmanaged child, missing parent, changed parent, and invalid payload cases.
- Per-operation limits and idempotency.
- Authorization/dispatch race tests using a transaction or locked snapshot.
- Audit record contains actor, child, host, operation, and request ID without
  claiming the host owner initiated the request.

### Reversibility

Keep the old HTTP view path behind the inverse feature flag. New request/audit
rows are additive and can be marked cancelled/rolled back; do not delete
existing device or forwarding records during this phase.

## Phase 4 — migrate expose ports

### Changes

Switch both synchronous and fire-and-forget expose-port APIs to the new service
and direct Guest Host Request. Preserve the existing CLI forwarding implementation:

- target-child rules are replaced atomically;
- other tenants' rules remain preserved;
- cached IPs continue to allow updates while another guest is offline;
- the guest container receives its existing exposure metadata;
- results contain verified canonical URLs and structured errors.

### Likely files/elements

- `server/portacode_django/dashboard/views/device_container_views.py`.
- New guest-operation service and direct-delivery helper.
- `portacode/connection/handlers/cloudflare_forwarding.py`.
- `portacode/connection/handlers/proxmox_infra.py` only where command
  registration/result handling is needed.

### Tests

- Guest A changes ports while Guest B is offline or has no current DHCP lease.
- Guest A cannot modify Guest B's rules.
- Empty list removes only Guest A's rules.
- Timeout followed by retry does not duplicate exposure rules.
- Container metadata sync failure produces a truthful failure/result.
- Host-owner session information is absent from the dispatched command.

### Reversibility

Per-operation flag switches back to the existing path. Keep request IDs and
audit records compatible with both paths. Do not change forwarding-state file
format until the migration is complete.

## Phase 5 — migrate start/stop power operations

### Changes

Move start/stop to the same service and message type. Keep the CLI's existing
`_ensure_container_managed(..., device_id=...)` check and idempotent behavior
(`already running`/`already stopped`).

Before implementation, explicitly decide whether a Proxmox host owner may
control another user's guest. If strict tenant isolation is required, remove
that implicit parent-owner authorization and introduce an explicit
infrastructure-admin capability instead.

### Likely files/elements

- `server/portacode_django/dashboard/views/device_views.py`.
- Guest-operation service and direct-delivery helper.
- `portacode/connection/handlers/proxmox_infra.py` start/stop handlers.

### Tests

- Guest owner can operate only their child.
- Other guest cannot.
- Host-owner behavior matches the chosen explicit policy.
- Wrong CTID cannot affect another child.
- Repeated start/stop is idempotent and Proxmox task errors are propagated.

### Reversibility

Use the per-operation feature flag and retain the existing endpoint contract.
Rollback means switching dispatch back, not changing device state or deleting
records.

## Phase 6 — migrate deletion and provisioning

### Deletion

Use the same direct command for removal, while retaining the existing server
deletion state machine, confirmation, forwarding cleanup, and already-gone
handling. Final database deletion must occur only after a verified result or a
durable reconciliation decision.

### Provisioning

Treat provisioning as a durable host operation rather than a simple guest
command. Keep quota/resource checks server-side; never accept arbitrary host
VMIDs or host resource identifiers from the guest. Persist progress and route
progress/result events to the requesting guest using `on_behalf_of_device`.

### Likely files/elements

- `server/portacode_django/dashboard/services/managed_device_delete.py`.
- Provisioning API/service and progress models/events.
- `portacode/connection/handlers/proxmox_infra.py` provisioning handlers.
- Existing server event persistence and consumer routing.

### Tests

- Failed operations release reservations and do not leave unmanaged guests.
- Already-deleted containers clean node metadata idempotently.
- Progress is durable while the guest is disconnected.
- Guest A cannot receive Guest B's progress/results.
- Host reconnect/retry does not duplicate provisioning or deletion.

### Reversibility

Migrate one operation at a time. Keep old command handlers and database state
transitions until the new path has passed production observation. Never make a
rollback depend on restoring a deleted Proxmox resource.

## Phase 7 — observability and cutover

Add structured logs and metrics for request ID, command, host, guest, outcome,
latency, retry, and transport path. Redact credentials and avoid logging
untrusted full payloads.

Run both paths in staging with shadow validation where safe, compare results,
then enable operations progressively. Monitor late responses, duplicate IDs,
cross-device routing denials, and host reconnect behavior.

Only after all operations are migrated and observed should the old
host-owner-session path be removed. Ordinary client-session messaging remains
unchanged.

## Rollback checklist

1. Disable the affected operation's feature flag.
2. Stop dispatching new commands through the new path.
3. Allow in-flight requests to reach a terminal timeout/result state.
4. Keep persisted audit/request records for diagnosis.
5. Resume the old endpoint dispatch path.
6. Do not undo successful Proxmox operations automatically; reconcile their
   durable state instead.
7. Review failed/ambiguous operations before re-enabling the new path.

## Definition of done

- No guest operation creates a synthetic client session authenticated as the
  host owner.
- The server performs role/request-type authorization and parent binding checks
  before send.
- The CLI independently validates managed-child identity before execution.
- Results are correlated, structured, durable where needed, and routed with
  validated `on_behalf_of_device` semantics.
- Existing client-session commands and provisioning compatibility remain intact.
- Every migrated operation has negative cross-tenant tests, retry/idempotency
  tests, disconnect tests, and a tested feature-flag rollback.
