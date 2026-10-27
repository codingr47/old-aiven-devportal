"""Loopback-only audit server. No credentials or arbitrary files are exposed."""

import copy
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote

from .reporting import report, select_audit
from .snapshot import verify_snapshot
from .storage import now


def decide(state, ident, action, notes="", edits=None):
    candidate = next((c for c in state["candidates"] if c["id"] == ident), None)
    if candidate is None:
        raise ValueError("Unknown candidate")
    if action not in {"approve", "reject", "edit"}:
        raise ValueError("Action must be approve, reject, or edit")
    if action == "approve" and candidate["status"] != "accepted":
        raise ValueError("Only automatically accepted records can be approved; revalidate edits first")
    previous = copy.deepcopy({k: candidate.get(k) for k in ("question", "paraphrase", "reference_answer", "required_facts", "evidence_groups")})
    original_category = [candidate["product"], candidate["question_type"]]
    if action == "edit":
        if not isinstance(edits, dict) or not edits:
            raise ValueError("Edits must be a nonempty object")
        allowed = {"question", "paraphrase", "reference_answer", "required_facts", "evidence_groups", "difficulty", "question_type", "persona"}
        if set(edits) - allowed:
            raise ValueError("Only question content and labels can be edited")
        candidate.update(edits)
        candidate["status"] = "pending_revalidation"
        candidate["human_status"] = "pending"
        candidate["validation"] = {"deterministic_errors": ["Edited; awaiting revalidation"]}
    else:
        candidate["human_status"] = "approved" if action == "approve" else "rejected"
    candidate["review_history"].append({"time": now(), "action": action, "notes": str(notes)[:10000],
                                        "previous": previous if action == "edit" else None})
    audit = state.get("audit")
    if audit and action in {"reject", "edit"}:
        if ident not in audit["failures"]:
            audit["failures"].append(ident)
        for category in (original_category, [candidate["product"], candidate["question_type"]]):
            if category not in audit["expanded_categories"]:
                audit["expanded_categories"].append(category)


def make_server(store, port=8765):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, value, status=200, html=False):
            data = value.encode("utf-8") if html else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.valid_host():
                return self.send({"error": "Loopback host required"}, 403)
            path = urlsplit(self.path).path
            state = store.load()
            if path == "/":
                source = Path(__file__).with_name("review.html").read_text(encoding="utf-8")
                return self.send(source.replace("__CSRF_TOKEN__", token), html=True)
            if path == "/api/status":
                result = report(state)
                try:
                    verify_snapshot(state)
                    result["snapshot_valid"] = True
                except ValueError as exc:
                    result.update(snapshot_valid=False, ready=False, browsing_complete=False, snapshot_error=str(exc))
                return self.send(result)
            if path == "/api/candidates":
                audit = state.get("audit") or {}
                required = set(report(state)["audit"].get("required", []))
                return self.send([{k: c.get(k) for k in ("id", "question", "product", "question_type", "persona", "difficulty", "status", "human_status")} |
                                 {"audit_required": c["id"] in required, "audit_sample": c["id"] in audit.get("members", [])}
                                 for c in state["candidates"]])
            if path.startswith("/api/candidates/"):
                ident = unquote(path.rsplit("/", 1)[1])
                c = next((c for c in state["candidates"] if c["id"] == ident), None)
                if not c:
                    return self.send({"error": "Unknown candidate"}, 404)
                evidence = []
                for group in c.get("evidence_groups") or []:
                    if not isinstance(group, dict):
                        continue
                    alternatives = group.get("alternatives")
                    if not isinstance(alternatives, list):
                        continue
                    for e in alternatives:
                        if not isinstance(e, dict):
                            continue
                        sid = e.get("section_id")
                        s = state["sections"].get(sid) if isinstance(sid, str) else None
                        obs = state.get('observations', {}).get(e.get('observation_id'), {})
                        evidence.append({**e, "text": obs.get('text', 'Missing browser observation'),
                                         "browser_url": obs.get('url'), "snapshot_id": obs.get('snapshot_id'),
                                         "accessibility_snapshot": state.get('accessibility_snapshots', {}).get(obs.get('snapshot_id'), {}).get('text'),
                                         "headings": s["headings"] if s else [],
                                         "source_section_id": s.get("source_section_id") if s else "Missing source",
                                         "document_path": s["document_path"] if s else None})
                return self.send({"candidate": c, "evidence": evidence})
            return self.send({"error": "Not found"}, 404)

        def do_POST(self):
            origin = self.headers.get("Origin")
            allowed_origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if not self.valid_host() or self.headers.get("X-Review-Token") != token or (origin and origin not in allowed_origins):
                return self.send({"error": "Invalid local review request"}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("Request must be 1 byte to 1 MiB")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected an object")
                with store.writer():
                    with store.transaction() as state:
                        verify_snapshot(state)
                        if self.path == "/api/audit":
                            select_audit(state)
                        elif self.path == "/api/decision":
                            decide(state, body["id"], body["action"], body.get("notes", ""), body.get("edits"))
                        else:
                            raise ValueError("Unknown action")
                return self.send({"ok": True})
            except (ValueError, KeyError, TypeError) as exc:
                return self.send({"error": str(exc)}, 400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(store, port):
    server = make_server(store, port)
    print(f"Review: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
