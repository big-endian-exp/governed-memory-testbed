"""Policy-checked memory gateway for sandboxes: JSON-lines RPC over a Unix socket, per-user/per-task HMAC tokens.
The sandbox never sees the DB; identity comes only from the verified token, never from request fields."""
import json, hmac, hashlib, os, socket, socketserver, threading, time, base64
from .store import PolicyError

class TokenError(Exception): pass

class Gateway:
    OPS = {"search", "get_ckpt", "put_ckpt", "effect", "write_episode", "llm"}
    def __init__(self, mem, sock_path, scoped=True, clock=time.time, llm=None):
        self.mem, self.sock, self.scoped, self.clock, self.llm = mem, sock_path, scoped, clock, llm
        self.secret = os.urandom(32); self.lock = threading.Lock()
        self.effects = {}   # run_id -> list of executed step indices (side-effect ledger)
        self.stats = {"allowed": 0, "denied": 0}; self.denials = []
        if os.path.exists(sock_path): os.remove(sock_path)
        gw = self
        class H(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    for line in self.rfile:
                        try: resp = gw.dispatch(json.loads(line))
                        except Exception as e: resp = {"ok": False, "err": f"{type(e).__name__}: {e}"}
                        self.wfile.write((json.dumps(resp) + "\n").encode()); self.wfile.flush()
                except (ConnectionError, BrokenPipeError): return   # sandbox was killed mid-call
        self.srv = socketserver.ThreadingUnixStreamServer(sock_path, H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def mint(self, tenant, user, task, ttl=300, ops=None):
        claims = {"t": tenant, "u": user, "task": task, "exp": self.clock() + ttl, "ops": sorted(ops or self.OPS)}
        body = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True).encode()).decode()
        return body + "." + hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()

    def verify(self, tok):
        try: body, sig = tok.rsplit(".", 1)
        except Exception: raise TokenError("malformed")
        if not hmac.compare_digest(sig, hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()):
            raise TokenError("bad signature")
        c = json.loads(base64.urlsafe_b64decode(body))
        if c["exp"] < self.clock(): raise TokenError("expired")
        return c

    def dispatch(self, req):
        with self.lock:
            op = req.get("op")
            try:
                if self.scoped:
                    c = self.verify(req.get("token", ""))
                    if op not in c["ops"]: raise PolicyError(f"op {op} not in token scope")
                    t, u, task = c["t"], c["u"], c["task"]
                    if req.get("tenant", t) != t or req.get("user", u) != u:   # spoof attempt: log and deny
                        raise PolicyError("identity mismatch with token")
                    run = task
                else:   # unscoped baseline: trusts caller-supplied identity
                    t, u, run = req.get("tenant"), req.get("user"), req.get("run")
                out = self._do(op, t, u, run, req)
                self.stats["allowed"] += 1
                return {"ok": True, "result": out}
            except (TokenError, PolicyError) as e:
                self.stats["denied"] += 1; self.denials.append((op, str(e)))
                self.mem._audit("sandbox", req.get("tenant", "?"), req.get("user", "?"), f"gw:{op}", "", "deny", str(e))
                return {"ok": False, "err": str(e)}

    def _do(self, op, t, u, run, req):
        m = self.mem
        if op == "search":
            return [r["content"] for r in m.search("sandbox", t, u, req["q"], k=req.get("k", 5))] if self.scoped else \
                   [r[0] for r in m.db.execute("SELECT content FROM memory WHERE tenant=? AND user=?", (t, u))][:req.get("k", 5)]
        if op == "get_ckpt": return m.latest_checkpoint("sandbox", t, u, run)
        if op == "put_ckpt":
            m.write("sandbox", t, u, "working", json.dumps(req["state"]), key=f"ckpt:{run}", sensitivity="restricted", ttl=7 * 86400)
            return True
        if op == "effect": self.effects.setdefault(run, []).append(req["i"]); return True
        if op == "write_episode": return m.write("sandbox", t, u, "episodic", req["text"], sensitivity="internal")
        if op == "llm":
            if self.llm is None: raise PolicyError("llm not configured")
            from .llm import SYSTEM
            kind = req.get("kind", "react")
            return self.llm.chat(SYSTEM.get(kind, "You are a helpful assistant."), req["messages"], kind=kind, max_tokens=req.get("max_tokens", 250))
        raise PolicyError(f"unknown op {op}")

    def close(self): self.srv.shutdown(); self.srv.server_close()
