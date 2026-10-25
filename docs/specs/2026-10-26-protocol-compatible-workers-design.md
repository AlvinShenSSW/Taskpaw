# Independent upgrades for compatible media workers

## Operator scope and problem

The operator permits Hub, Windows Agent and Mac Agent to use different builds.
The same rule explicitly includes the Windows media controller and GPU workers:
an unrelated application update must not interrupt running work or prevent the
next film from being assigned. Only an incompatible protocol or a concrete
feature requirement may require an upgrade.

Today `FleetLive.version_ok()` requires both the application version string and
the workflow protocol number to match. A 3.10.4 controller consequently marks a
3.10.2 worker incompatible despite an unchanged workflow wire contract. The
scheduler blocks new assignments and can withdraw unclaimed automatic routes.
It already preserves claimed work; that ownership rule must stay unchanged.

## Decision

Use the existing explicit workflow `PROTOCOL` as the compatibility boundary for
new work. Application versions remain visible diagnostic strings and do not
participate in eligibility. Keep the existing `version_ok` JSON field as a
compatibility alias so this correction does not itself break older readers.
Malformed heartbeats remain rejected and an unsupported protocol remains
ineligible. Existing hardware, approval, staging, model, admission, reconciliation
and task ownership checks remain in force.

The existing scheduler check alone leaves a window for an already routed film
to be claimed under an unsupported protocol. Add a controller-derived admission
snapshot to the remote claim path: a current-boot heartbeat with the supported
protocol is required for a new claim. Check its denial inside the store's claim
handler after authorization, exact idempotent replay and existing ownership
checks, before creating a fence or attempt. Denials roll back without caching a
request result, so the same request can succeed after a verified heartbeat.
Replay of a persisted claim still validates its original digest and returns its
result even if the controller has restarted and lost in-memory heartbeat state.
Renew, upload and finish are unchanged. The snapshot does not claim atomicity
with a future heartbeat; it is server-derived and never a worker request field.

Replace the misleading "version differs" reason with "workflow protocol
incompatible". Show the worker and controller protocol numbers in device details
when available, so an operator can identify the incompatible endpoints without
being told that all applications must have equal version numbers. Do not guess
that the worker always needs upgrading: a newer worker may require a newer
controller. This patch introduces neither forced installation nor automatic
restart, and does not create a general feature negotiation framework.

The monitoring Hub likewise retains each Agent's displayed build but removes
its blanket "Agent newer than Hub" upgrade warning. Actual unsupported monitor
and read-error feedback remains; version ordering alone is not a feature check.

Protocol changes must represent incompatible wire or workflow semantics. Future
features with narrower requirements must gate only that feature on a documented
capability or actual minimum version. A major version number alone does not make
two endpoints incompatible. No unsupported blanket promise is made about all
historical builds: old controllers retain their old checks and may reject newer
request fields before a heartbeat. In particular, pre-3.10.2 controllers reject
the later `agent_port` pair/boot field under their strict schema. Updating this
controller cannot change the behavior of those old controller binaries.

## Verification

1. RED: the existing equality check must reject a 3.10.2 worker against the new
   controller in a regression that uses a real heartbeat, routing and claim path.
2. GREEN: different application versions with the supported protocol can receive
   new work and retain an unclaimed route; the displayed version stays intact.
3. Unsupported protocol remains blocked for new work with a truthful reason;
   malformed protocol values stay rejected. A change in protocol triggers routing.
4. Claimed/running work remains owned by its worker and can renew, upload and
   settle despite application-version differences. No cancellation, retry or
   reassignment is introduced by this patch.
5. Exercise a full fake-media controller/worker flow using 3.10.2 as the worker
   build string and the current controller version, retaining byte hashes,
   single-attempt and process cleanup assertions. Separately compare the
   historical wire/client surface; do not label a version override alone as an
   actual older binary interoperability test.
6. UI regression shows differing application versions without an upgrade warning;
   an incompatible protocol displays the actual boundary and guidance.
7. Run canonical Python/UI/static/version checks, clean CI and the independent
   Claude outer then Kimi final gates. Advance the six V3 version sources to
   3.10.5; leave V2 frozen and the existing v3.10.4 tag unchanged.

## Delivery boundaries

Update deployment guidance to remove same-build instructions for unrelated Hub,
Agent and media-worker updates. Document that active machines are upgraded only
at an operator-selected idle point. No running Agent, service, real media, real
database or configured port is touched during this work. Root owns the final
merge, release publication and copying the installer to the desktop.
