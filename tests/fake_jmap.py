"""Small fake of the plMail JMAP API (session, Email/query, Email/get, Email/changes, download).

Mimics the documented pitfalls: accountId must be a string, Email/get answers in database order,
request-level errors come as application/problem+json, no Email/queryChanges.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "plmail_" + "ab" * 32
PROJECT_CAP = "urn:plmail:params:jmap:projektakte"
PROJECT_PROP = "plmail:projektakte"


class FakePlMail:
    def __init__(self):
        self.accounts: dict[str, dict[str, dict]] = {}  # accountId → emailId → email
        self.blobs: dict[str, tuple[bytes, str]] = {}
        self.state: dict[str, int] = {}
        self.log: dict[str, list[tuple[int, str, str]]] = {}  # accountId → (state, kind, id)
        self.methods: list[str] = []
        self.well_known = True
        self.forget_history = False  # simulate cannotCalculateChanges
        self.project_extension = True  # advertise the Projektakte capability + Email property
        self.usings: list[list[str]] = []
        self.using: list[str] = []
        self._next = 0
        self.lock = threading.Lock()

    # -- data helpers --------------------------------------------------------------------------------
    def add(self, account: str, subject: str, body: str, *, sender=("Bauamt", "bauamt@x.example"),
            received="2025-01-10T09:00:00Z", attachments=(), html=False) -> str:
        with self.lock:
            self._next += 1
            eid = f"M{self._next:04d}"
            atts = []
            for i, (name, data, mime) in enumerate(attachments):
                blob = f"B{self._next:04d}{i}"
                self.blobs[blob] = (data, mime)
                atts.append({"partId": str(10 + i), "blobId": blob, "size": len(data), "name": name,
                             "type": mime})
            part = {"partId": "1", "type": "text/html" if html else "text/plain"}
            self.accounts.setdefault(account, {})[eid] = {
                "id": eid, "threadId": "T" + eid, "subject": subject,
                "from": [{"name": sender[0], "email": sender[1]}], "to": [{"name": "Büro", "email": "b@x.example"}],
                "cc": [], "receivedAt": received, "preview": body[:60], "hasAttachment": bool(atts),
                "attachments": atts, "bodyValues": {"1": {"value": body, "isTruncated": False}},
                "textBody": [] if html else [part], "htmlBody": [part], "keywords": {}, PROJECT_PROP: None}
            self._bump(account, "created", eid)
            return eid

    def assign(self, account: str, eid: str, project: str | None, *, status: str = "assigned",
               user: str = "anna", name: str = "") -> None:
        """What plMail's UI does when a human picks a project (or 'kein Projekt'); None = remove."""
        with self.lock:
            e = self.accounts[account][eid]
            if project is None and status == "assigned":
                e[PROJECT_PROP] = None
            else:
                e[PROJECT_PROP] = {"status": status, "project": project, "projectName": name,
                                   "assignedBy": user, "assignedAt": "2025-03-10T09:30:00Z"}
            self._bump(account, "updated", eid)  # the assignment changes the Email state

    def flag(self, account: str, eid: str) -> None:
        with self.lock:
            self.accounts[account][eid]["keywords"]["$seen"] = True
            self._bump(account, "updated", eid)

    def destroy(self, account: str, eid: str) -> None:
        with self.lock:
            del self.accounts[account][eid]
            self._bump(account, "destroyed", eid)

    def _bump(self, account: str, kind: str, eid: str) -> None:
        self.state[account] = self.state.get(account, 0) + 1
        self.log.setdefault(account, []).append((self.state[account], kind, eid))

    # -- JMAP methods --------------------------------------------------------------------------------
    def handle(self, name: str, args: dict, results: dict) -> list:
        self.methods.append(name)
        acc = args.get("accountId")
        if not isinstance(acc, str):
            return ["error", {"type": "invalidArguments", "description": "accountId must be a string"}]
        if acc not in self.accounts:
            return ["error", {"type": "accountNotFound"}]
        emails = self.accounts[acc]
        if "#ids" in args:
            ref = args["#ids"]
            args = {**args, "ids": results[ref["resultOf"]]["ids"]}
        if name == "Email/query":
            ordered = sorted(emails.values(), key=lambda e: e["receivedAt"],
                             reverse=not args.get("sort", [{}])[0].get("isAscending", False))
            pos, lim = int(args.get("position", 0)), int(args.get("limit", 100))
            return ["Email/query", {"accountId": acc, "ids": [e["id"] for e in ordered[pos:pos + lim]],
                                    "position": pos, "queryState": str(self.state.get(acc, 0))}]
        if name == "Email/get":
            ids = args.get("ids") or []
            found = [emails[i] for i in sorted(ids) if i in emails]  # database order, not request order
            props = args.get("properties")
            if props and PROJECT_PROP in props and (not self.project_extension or PROJECT_CAP not in self.using):
                return ["error", {"type": "invalidArguments", "description": f"unknown property {PROJECT_PROP}"}]
            if not props or PROJECT_PROP not in props:
                found = [{k: v for k, v in e.items() if k != PROJECT_PROP} for e in found]
            if props:
                found = [{k: v for k, v in e.items() if k in props} for e in found]
            return ["Email/get", {"accountId": acc, "state": str(self.state.get(acc, 0)), "list": found,
                                  "notFound": [i for i in ids if i not in emails]}]
        if name == "Email/changes":
            if self.forget_history:
                return ["error", {"type": "cannotCalculateChanges"}]
            since = int(args["sinceState"])
            entries = [e for e in self.log.get(acc, []) if e[0] > since]
            limit = int(args.get("maxChanges") or 1000)
            chunk, more = entries[:limit], len(entries) > limit
            created = [i for _, k, i in chunk if k == "created"]
            updated = [i for _, k, i in chunk if k == "updated" and i not in created]
            destroyed = [i for _, k, i in chunk if k == "destroyed"]
            created = [i for i in created if i not in destroyed]
            new_state = chunk[-1][0] if chunk else since
            return ["Email/changes", {"accountId": acc, "oldState": str(since), "newState": str(new_state),
                                      "hasMoreChanges": more, "created": created, "updated": updated,
                                      "destroyed": destroyed}]
        return ["error", {"type": "unknownMethod"}]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"type": "about:blank", "title": "Unauthorized", "status": 401},
                       "application/problem+json")
            return False
        return True

    def do_GET(self):  # noqa: N802
        fake: FakePlMail = self.server.fake
        base = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
        path = urllib.parse.urlparse(self.path).path
        if path in ("/.well-known/jmap", "/jmap/session"):
            if path == "/.well-known/jmap" and not fake.well_known:
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return
            return self._send(200, {
                "capabilities": {"urn:ietf:params:jmap:core": {}, "urn:ietf:params:jmap:mail": {},
                                 **({PROJECT_CAP: {}} if fake.project_extension else {})},
                "accounts": {a: {"name": f"Konto {a}", "isPersonal": True,
                                 "accountCapabilities": {"urn:ietf:params:jmap:mail": {}}}
                             for a in fake.accounts},
                "primaryAccounts": {"urn:ietf:params:jmap:mail": next(iter(fake.accounts), None)},
                "apiUrl": f"{base}/jmap/api/",
                "downloadUrl": f"{base}/jmap/download/{{accountId}}/{{blobId}}/{{name}}?accept={{type}}",
                "uploadUrl": f"{base}/jmap/upload/{{accountId}}/", "eventSourceUrl": f"{base}/jmap/events/",
                "state": "s1"})
        if path.startswith("/jmap/download/"):
            if not self._authorized():
                return
            _, _, _, acc, blob, name = path.split("/", 5)
            if blob not in fake.blobs:
                return self._send(404, {"error": "blob not found"})
            data, mime = fake.blobs[blob]
            return self._send(200, data, mime)
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        fake: FakePlMail = self.server.fake
        if urllib.parse.urlparse(self.path).path != "/jmap/api/":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            req = json.loads(raw)
            assert isinstance(req.get("using"), list) and isinstance(req.get("methodCalls"), list)
        except (ValueError, AssertionError, AttributeError):
            return self._send(400, {"type": "urn:ietf:params:jmap:error:notRequest", "status": 400,
                                    "detail": "Request does not match the JMAP Request object"},
                              "application/problem+json")
        results, responses = {}, []
        fake.using = req["using"]
        fake.usings.append(req["using"])
        for name, args, call_id in req["methodCalls"]:
            resp_name, resp_args = fake.handle(name, args, results)
            results[call_id] = resp_args
            responses.append([resp_name, resp_args, call_id])
        self._send(200, {"methodResponses": responses, "sessionState": "s1"})


def start_fake_plmail() -> tuple[ThreadingHTTPServer, FakePlMail]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.fake = FakePlMail()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, server.fake
