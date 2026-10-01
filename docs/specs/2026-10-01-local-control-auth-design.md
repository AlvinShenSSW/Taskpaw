# Local control authentication

TaskPaw V3 separates LAN polling from local management. Browser CORS alone does
not prevent a form POST from changing backend state, so the local API checks
both the request origin and an independent control credential before it reads a
body, validates route parameters or invokes a handler.

## Listeners and compatibility

| Role | Read listener | Local control listener |
| --- | --- | --- |
| Agent | `bind_host:5680`, `/ping`, `/status`, `/events`, film views | `control_host:5681`, `/control/*` |
| Hub | `bind_host:5690`, `/ping`, `/status`, `/events`, film views | `control_host:5691`, the same read views and management routes |

Ports can be configured. Both control hosts accept only numeric `127.0.0.1` or
`::1`; IPv6 URLs use brackets. The Hub read listener no longer registers POST
`/servers`, PATCH/DELETE `/servers/{sid}` or PATCH `/config`: they return 404 or
405 there. Those URLs retain their behavior on the Hub control listener. Both
Hub applications share one service, poller and store.

The Agent network Bearer, Hub read Bearer and outbound polling Bearer retain
their existing contracts. A network token never authorizes local control.
Agent network `/events` preserves clear-on-read/ack behavior; local event views
are nondestructive. The offline Hub database CLI remains available under the
current OS account's file permissions. V2 behavior is unchanged.

## Request gate

Every control request except GET/HEAD ping and a valid CORS preflight requires
`Authorization: Bearer <current-control-credential>`. Empty credentials cannot
disable authentication. Duplicate Authorization or Origin headers are rejected.
The gate covers all methods and future routes, including command dispatch and
LLM connection tests. Invalid bodies and parameters do not bypass it.

Absent Origin supports authenticated local CLI clients. When Origin is present,
only these complete values are accepted:

- `tauri://localhost`
- `http://tauri.localhost`
- `https://tauri.localhost`
- `http://localhost:5173`
- `http://127.0.0.1:5173`
- `http://[::1]:5173`

No normalization or suffix matching is performed. `null`, empty, duplicate,
multiple, alternate-port, path, query, fragment and encoded origins are denied.
A valid Origin never substitutes for a credential. Illegal Origin returns 403
`control_origin_forbidden`; a missing, invalid or inactive credential returns
401 `control_unauthorized` with a Bearer challenge. Error bodies and logs do not
include request bodies, header values or secrets. Allowed preflights support
Authorization, Content-Type, PATCH and DELETE without executing a handler.

## Runtime credential and startup

After claiming both sockets, each runtime creates a new random token and
independent boot ID. With a config path it publishes `agent.control.json` or
`hub.control.json` beside the actual YAML, before starting monitors, task logs,
polling or emitting readiness. Embedded launchers without a config path keep a
memory-only credential and never resolve the user's default config directory.
Neither environment variables nor YAML can choose or reuse the runtime token.
Misconfigured control credential environment variables are stripped before
managed tasks start; ASR and LLM child environments also strip them.

The descriptor has exactly these fields:

```json
{"version":1,"role":"agent","base_url":"http://127.0.0.1:5681","boot_id":"<running-id>","control_token":"<runtime-secret>"}
```

The placeholders above are explanatory. Files are protected from creation:
POSIX checks actual file descriptors, owner, mode 0600 and trusted directory
handles; Windows checks actual HANDLEs, owner, protected DACL and reparse
attributes, with access only for the current user and SYSTEM. Directory handles
anchor publication and cleanup. Unsafe existing files fail closed. Writes use
an exclusive temporary file, durable flush and atomic replacement; readers
validate and read the same opened object, with bounded content and fixed schema.

Windows directory owners and modifying principals must be the current user,
SYSTEM, Administrators, or the exact fixed TrustedInstaller service SID.
Directory `OWNER_RIGHTS` refers to the already-validated owner; it cannot bypass
unknown ownership or an Everyone write grant. All ancestor HANDLEs stay pinned
without delete sharing. Credential files remain owned by the current user with
protected, non-inherited explicit current-user/SYSTEM access; TrustedInstaller
ownership and `OWNER_RIGHTS` are not accepted for files. These identities follow
[Windows Resource Protection](https://learn.microsoft.com/en-us/windows/win32/wfp/about-windows-file-protection)
and [Microsoft's Owner Rights semantics](https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/manage/understand-special-identities-groups#owner-rights).

On macOS, extended ACLs can grant access independently of permission bits. Both
Python and Rust inspect each opened directory and credential fd with the native
ACL API; Python also inspects its new temporary fd before writing any secret.
Empty ACLs and ordinary deny entries, including the default HOME deny-delete
entry, are accepted. The bounded policy rejects allow entries granting unsafe
data, replacement, metadata-write, security or owner rights, and directory
inheritance that would grant those rights to a new credential, even for
inherit-only entries. Harmless read-only directory/metadata grants remain
compatible. Unknown entries, permissions, flags or ACL query failures fail
closed. No existing user ACL is silently changed to make publication succeed.

Readiness contains role/base URL and the nonsecret actual descriptor path and
boot ID. It never contains a token. On shutdown the credential becomes inactive
first, then its descriptor is removed only if the current boot matches; all
monitor/poller/thread/socket cleanup continues if removal fails. Startup errors
roll back partial resources and do not announce ready. A new runtime always
gets a new key even if a prior crash left a descriptor.

## Desktop and development clients

Tauri spawn mode reads the protected descriptor named by readiness and checks
role, endpoint and boot ID exactly. Explicit attach mode reads the configured
`TASKPAW_CONTROL_CREDENTIAL_FILE` path, or the role's platform config file.
An optional `TASKPAW_UI_BASE` must agree with that descriptor. Missing or unsafe
credentials fail startup. `TASKPAW_UI_TOKEN`, `TASKPAW_CONTROL_TOKEN` and
`VITE_TASKPAW_TOKEN` are not control credential sources.

The native shell injects `{baseUrl, controlToken, role, bootId}` only into the
trusted top frame. It checks `window.top === window` before injecting and checks
the exact packaged origins; debug builds additionally allow the three port-5173
development origins. Subframes and unrelated loopback pages receive no key.

Direct Vite development asks for an endpoint and credential per role. Enter
the current descriptor's `base_url` (defaults: Agent 5681, Hub 5691), then its
credential in the password field. The endpoint must be a canonical numeric
loopback origin before a request can send the key. No generic
`VITE_TASKPAW_BASE` or Vite token fallback supplies either value. The UI validates
the credential with a protected status read and retains these values only in
that role's memory. No credential is stored in
URLs, local/session storage or build variables. All local reads, film views and
writes use the current role's key and canonical numeric-loopback endpoint.
On 401 the UI clears that role's key/cache and stops requests. Desktop users
reopen TaskPaw; development users enter the new descriptor's credential.
Neither client automatically replays a failed mutation.

## HTTP scripts without secret arguments

The following helper reads a fresh protected descriptor per operation, disables
proxies and redirects, and prints only status. Save it locally and edit the
nonsecret file paths and monitor/server identifiers for your installation.

```python
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from taskpaw_v3.core.control import ControlCredentialError, read_control_descriptor

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def control_call(file, method, path, body=None):
    try:
        descriptor = read_control_descriptor(Path(file))
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("invalid local path")
        data = json.dumps(body).encode() if body is not None else None
        request = Request(descriptor.base_url + path, data=data, method=method,
                          headers={"Authorization": "Bearer " + descriptor.control_token,
                                   "Content-Type": "application/json"})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
            print("HTTP", response.status)
    except HTTPError as error:
        if error.code == 401:
            print("Credential expired; read the current descriptor and run a new operation.")
        else:
            print("Control request rejected.")
        raise SystemExit(1) from None
    except (ControlCredentialError, URLError, OSError, ValueError):
        print("Local control unavailable.")
        raise SystemExit(1) from None

# Agent stop: the file is beside the actual agent.yaml.
control_call("config/agent.control.json", "POST",
             "/control/monitors/stop?name=" + quote("my-monitor", safe=""))

# Hub CRUD: the file is beside the actual hub.yaml; replace 7 with a real id.
control_call("config/hub.control.json", "POST", "/servers",
             {"name": "workstation", "ip": "192.168.1.50", "port": 5680})
control_call("config/hub.control.json", "PATCH", "/servers/7", {"enabled": False})
control_call("config/hub.control.json", "DELETE", "/servers/7")
```

Do not expand a credential into `curl -H`, a shell variable argument or a token
flag. Long-running scripts reread the descriptor for each new operation; 401 is
a failed operation, not permission to replay it automatically.

## Trust boundary and validation

File permissions and boot metadata establish trusted local credential transfer;
they do not prove the identity of an HTTP service occupying a loopback port.
An old client can send a stale key to a process that takes a stopped backend's
port, but that key never authorizes the next genuine runtime. Same-account
malicious programs, trusted-UI XSS, administrators and full native relay attacks
are outside this credential boundary.

Tests cover all twelve mutation routes and command dispatch, real configuration
and database state, rejection before validation, nondestructive local reads,
network ack compatibility, two-socket rollback, restart key changes, protected
file lifecycle, generated top-frame JavaScript and Windows Python-to-Rust file
interop. Real packaged macOS/Windows WebView behavior, installation and
cross-account/native attack checks require platform validation; simulated HTTP
and JavaScript tests alone do not establish those results. A WebView that sends
`Origin: null` must be investigated without broadening the whitelist.

Windows interop creates its own private root directly under Python's user Temp.
Native CI identified TrustedInstaller ownership on its C: root, and an unknown
owner on its D: runner-temp root. The latter remains rejected; tests select a
directory satisfying the production policy instead of changing a drive or
existing parent ACL. Mandatory fixtures exercise safe reads, broad-ACL rejection,
trusted-owner `OWNER_RIGHTS`, Everyone full control with `OWNER_RIGHTS`, and the
unchanged strict file ACL. Owner mutations unavailable without extra privileges
are explicit optional skips, not acceptance evidence.
