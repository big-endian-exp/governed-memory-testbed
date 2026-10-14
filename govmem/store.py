"""Governed memory service (SQLite + hashed TF-IDF vector index) and an ungoverned baseline."""
import sqlite3, time, json, hashlib, uuid
import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

TYPES = ("episodic", "semantic", "procedural", "working")
SENS = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
_vec = HashingVectorizer(n_features=2**12, alternate_sign=False, norm="l2")
def embed(text): return _vec.transform([text]).toarray()[0].astype(np.float32)

class PolicyError(PermissionError): pass

# ACL per harness: types it may read/write, scopes, max sensitivity it may read.
DEFAULT_ACL = {
    "react":     {"read": {"episodic", "semantic", "procedural"}, "write": {"episodic", "semantic"},
                  "scopes": {"user", "tenant"}, "max_sens": "confidential"},
    "autopilot": {"read": {"working", "procedural", "semantic"}, "write": {"working", "episodic", "procedural"},
                  "scopes": {"user"}, "max_sens": "restricted"},
    "rag":       {"read": {"semantic", "episodic"}, "write": {"semantic"},
                  "scopes": {"user", "tenant"}, "max_sens": "internal"},
    "sandbox":   {"read": {"episodic", "semantic", "working"}, "write": {"working", "episodic"},
                  "scopes": {"user"}, "max_sens": "confidential"},
    "admin":     {"read": set(TYPES), "write": set(), "scopes": {"user", "tenant"}, "max_sens": "restricted"},
}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS memory(id TEXT PRIMARY KEY, tenant TEXT, user TEXT, scope TEXT, mtype TEXT,
  key TEXT, content TEXT, sensitivity TEXT, created REAL, expires REAL, version INT, superseded_by TEXT,
  src_harness TEXT, src_ref TEXT, src_hash TEXT);
CREATE INDEX IF NOT EXISTS ix_ns ON memory(tenant,user,mtype);
CREATE TABLE IF NOT EXISTS artifact(record_id TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS lineage(child TEXT, parent TEXT);
CREATE TABLE IF NOT EXISTS embedding(id TEXT PRIMARY KEY, tenant TEXT, user TEXT, vec BLOB);
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, harness TEXT, tenant TEXT,
  user TEXT, op TEXT, target TEXT, decision TEXT, detail TEXT, prev TEXT, h TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_upd BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_del BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""

class GovernedMemory:
    def __init__(self, path, acl=None, secure_delete=True, clock=time.time):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute(f"PRAGMA secure_delete={'ON' if secure_delete else 'OFF'}")
        self.db.executescript(SCHEMA)
        self.acl, self.clock = acl or DEFAULT_ACL, clock
        self._index = {}   # in-memory vector cache: id -> (tenant,user,vec)
        for i, t, u, v in self.db.execute("SELECT id,tenant,user,vec FROM embedding"):
            self._index[i] = (t, u, np.frombuffer(v, np.float32))

    # ---------- audit (hash-chained, append-only) ----------
    def _audit(self, harness, tenant, user, op, target, decision, detail=""):
        prev = self.db.execute("SELECT h FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        prev = prev[0] if prev else "GENESIS"
        ts = self.clock()
        h = hashlib.sha256(f"{prev}|{ts}|{harness}|{tenant}|{user}|{op}|{target}|{decision}|{detail}".encode()).hexdigest()
        self.db.execute("INSERT INTO audit(ts,harness,tenant,user,op,target,decision,detail,prev,h) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (ts, harness, tenant, user, op, target, decision, detail, prev, h))

    def verify_audit(self):
        prev = "GENESIS"
        for ts, ha, t, u, op, tg, d, de, p, h in self.db.execute(
                "SELECT ts,harness,tenant,user,op,target,decision,detail,prev,h FROM audit ORDER BY seq"):
            if p != prev or h != hashlib.sha256(f"{p}|{ts}|{ha}|{t}|{u}|{op}|{tg}|{d}|{de}".encode()).hexdigest():
                return False
            prev = h
        return True

    # ---------- policy ----------
    def _check(self, harness, op, mtype, scope, sens="public"):
        a = self.acl.get(harness)
        if not a: return "unknown harness"
        if mtype not in a["read" if op == "read" else "write"]: return f"{op} {mtype} denied"
        if scope not in a["scopes"]: return f"scope {scope} denied"
        if op == "read" and SENS[sens] > SENS[a["max_sens"]]: return "sensitivity"
        return None

    # ---------- write ----------
    def write(self, harness, tenant, user, mtype, content, *, scope="user", key=None, sensitivity="internal",
              ttl=None, src_ref="", derived_from=()):
        why = self._check(harness, "write", mtype, scope)
        if why:
            self._audit(harness, tenant, user, "write", mtype, "deny", why); raise PolicyError(why)
        owner = user if scope == "user" else "*"
        now, rid = self.clock(), uuid.uuid4().hex
        ver = 1
        self.db.execute("BEGIN")
        if key:   # versioned fact: supersede prior live version in same namespace
            old = self.db.execute("SELECT id,version FROM memory WHERE tenant=? AND user=? AND key=? AND superseded_by IS NULL",
                                  (tenant, owner, key)).fetchone()
            if old:
                ver = old[1] + 1
                self.db.execute("UPDATE memory SET superseded_by=? WHERE id=?", (rid, old[0]))
        self.db.execute("INSERT INTO memory VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, tenant, owner, scope, mtype, key, content, sensitivity, now, now + ttl if ttl else None, ver, None,
             harness, src_ref, hashlib.sha256(content.encode()).hexdigest()))
        for p in derived_from: self.db.execute("INSERT INTO lineage VALUES(?,?)", (rid, p))
        v = embed(content)
        self.db.execute("INSERT INTO embedding VALUES(?,?,?,?)", (rid, tenant, owner, v.tobytes()))
        self._audit(harness, tenant, user, "write", rid, "allow", mtype)
        self.db.execute("COMMIT")
        self._index[rid] = (tenant, owner, v)
        return rid

    def register_artifact(self, harness, tenant, user, path, *, ttl=None, derived_from=(), kind="snapshot"):
        """Track an out-of-DB artifact (snapshot tarball, sandbox log) as a derived record: it inherits
        TTL and is removed (file unlinked) by cascading delete."""
        import os
        h = hashlib.sha256(open(path, "rb").read()).hexdigest()
        rid = self.write(harness, tenant, user, "working", f"artifact:{kind} sha256={h}", ttl=ttl,
                         sensitivity="restricted", src_ref=os.path.basename(path), derived_from=derived_from)
        self.db.execute("INSERT INTO artifact VALUES(?,?)", (rid, path))
        return rid

    # ---------- read ----------
    def _visible_rows(self, harness, tenant, user, mtypes):
        a = self.acl[harness]; now = self.clock()
        owners = [user] + (["*"] if "tenant" in a["scopes"] else [])
        mt = [m for m in mtypes if m in a["read"]]
        if not mt: return []
        q = (f"SELECT * FROM memory WHERE tenant=? AND user IN ({','.join('?'*len(owners))}) AND mtype IN ({','.join('?'*len(mt))})"
             " AND superseded_by IS NULL AND (expires IS NULL OR expires>?)")
        rows = self.db.execute(q, (tenant, *owners, *mt, now)).fetchall()
        return [r for r in rows if SENS[r[7]] <= SENS[a["max_sens"]]]

    def search(self, harness, tenant, user, query, k=5, mtypes=TYPES):
        if harness not in self.acl:
            self._audit(harness, tenant, user, "search", "", "deny", "unknown harness"); raise PolicyError("unknown")
        rows = self._visible_rows(harness, tenant, user, mtypes)
        if not rows:
            self._audit(harness, tenant, user, "search", "", "allow", "0 hits"); return []
        qv = embed(query)
        scored = sorted(rows, key=lambda r: -float(self._index[r[0]][2] @ qv))[:k]
        self._audit(harness, tenant, user, "search", ",".join(r[0] for r in scored), "allow", f"{len(scored)} hits")
        return [dict(id=r[0], tenant=r[1], user=r[2], mtype=r[4], key=r[5], content=r[6], sensitivity=r[7],
                     version=r[10], src_harness=r[12]) for r in scored]

    def get_fact(self, harness, tenant, user, key):
        for r in self._visible_rows(harness, tenant, user, ("semantic",)):
            if r[5] == key:
                self._audit(harness, tenant, user, "get", r[0], "allow"); return r[6]
        self._audit(harness, tenant, user, "get", key, "allow", "miss"); return None

    def latest_checkpoint(self, harness, tenant, user, run_id):
        r = self.db.execute("SELECT id,content FROM memory WHERE tenant=? AND user=? AND mtype='working' AND key=? "
                            "AND superseded_by IS NULL", (tenant, user, f"ckpt:{run_id}")).fetchone()
        ok = self._check(harness, "read", "working", "user") is None
        self._audit(harness, tenant, user, "get", r[0] if r else "", "allow" if ok else "deny", "checkpoint")
        if not ok: raise PolicyError("checkpoint read denied")
        return json.loads(r[1]) if r else None

    # ---------- retention & right-to-delete ----------
    def purge_expired(self):
        ids = [r[0] for r in self.db.execute("SELECT id FROM memory WHERE expires IS NOT NULL AND expires<=?", (self.clock(),))]
        return self._delete_ids(ids, "system", "*", "*", "ttl_purge")

    def forget_user(self, tenant, user, requested_by="admin"):
        ids = [r[0] for r in self.db.execute("SELECT id FROM memory WHERE tenant=? AND user=?", (tenant, user))]
        # tenant-shared records whose provenance is this user's content also go (derived from user's data)
        return self._delete_ids(ids, requested_by, tenant, user, "forget_user")

    def _delete_ids(self, ids, who, tenant, user, op):
        frontier, allids = list(ids), set(ids)
        while frontier:   # cascade over lineage to derived summaries (any namespace)
            kids = [c for (c,) in self.db.execute(
                f"SELECT child FROM lineage WHERE parent IN ({','.join('?'*len(frontier))})", frontier)]
            frontier = [k for k in kids if k not in allids]; allids.update(frontier)
        # also older versions superseded by deleted ids
        if allids:
            allids |= {r[0] for r in self.db.execute(
                f"SELECT id FROM memory WHERE superseded_by IN ({','.join('?'*len(allids))})", list(allids))}
        L = list(allids)
        import os
        paths = [p for ch in [L[i:i+500] for i in range(0, len(L), 500)] for (p,) in self.db.execute(
            f"SELECT path FROM artifact WHERE record_id IN ({','.join('?'*len(ch))})", ch)]
        self.db.execute("BEGIN")
        for chunk in [L[i:i+500] for i in range(0, len(L), 500)]:
            ph = ",".join("?"*len(chunk))
            self.db.execute(f"DELETE FROM artifact WHERE record_id IN ({ph})", chunk)
            for sql in (f"DELETE FROM memory WHERE id IN ({ph})", f"DELETE FROM embedding WHERE id IN ({ph})",
                        f"DELETE FROM lineage WHERE child IN ({ph}) OR parent IN ({ph})"):
                self.db.execute(sql, chunk * sql.count(ph))
        self._audit(who, tenant, user, op, f"{len(allids)} records", "allow")
        self.db.execute("COMMIT")
        for i in allids: self._index.pop(i, None)
        for p in paths:
            if os.path.exists(p): os.remove(p)
        return len(allids)


class NaiveMemory:
    """Ungoverned baseline typical of prototype agent memory: one global vector store, no ACL,
    no lineage, delete-by-user-column only, overwrite-free (append) facts, no audit."""
    def __init__(self, path, clock=time.time):
        self.db = sqlite3.connect(path, isolation_level=None); self.clock = clock
        self.db.executescript("""CREATE TABLE IF NOT EXISTS mem(id TEXT PRIMARY KEY, user TEXT, mtype TEXT, key TEXT,
            content TEXT, created REAL); CREATE TABLE IF NOT EXISTS emb(id TEXT PRIMARY KEY, vec BLOB);""")
        self._index = {}
    def write(self, harness, tenant, user, mtype, content, *, scope="user", key=None, derived_from=(), **kw):
        rid = uuid.uuid4().hex
        owner = user if not derived_from else "summarizer"   # derived artifacts owned by the agent, typical
        self.db.execute("INSERT INTO mem VALUES(?,?,?,?,?,?)", (rid, owner, mtype, key, content, self.clock()))
        v = embed(content); self.db.execute("INSERT INTO emb VALUES(?,?)", (rid, v.tobytes())); self._index[rid] = v
        return rid
    def search(self, harness, tenant, user, query, k=5, mtypes=TYPES):
        qv = embed(query)
        rows = self.db.execute("SELECT id,user,mtype,key,content FROM mem").fetchall()
        rows = [r for r in rows if r[2] in mtypes]
        rows.sort(key=lambda r: -float(self._index[r[0]] @ qv))
        return [dict(id=r[0], user=r[1], tenant=None, mtype=r[2], key=r[3], content=r[4]) for r in rows[:k]]
    def get_fact(self, harness, tenant, user, key):
        r = self.db.execute("SELECT content FROM mem WHERE key=? AND user=? ORDER BY created LIMIT 1", (key, user)).fetchone()
        return r[0] if r else None   # first-written wins -> stale
    def latest_checkpoint(self, harness, tenant, user, run_id): return None   # no checkpoints
    def forget_user(self, tenant, user, requested_by="admin"):
        ids = [r[0] for r in self.db.execute("SELECT id FROM mem WHERE user=?", (user,))]
        self.db.execute("DELETE FROM mem WHERE user=?", (user,))   # embeddings table and derived rows left behind
        for i in ids: self._index.pop(i, None)
        return len(ids)
    def purge_expired(self): return 0
