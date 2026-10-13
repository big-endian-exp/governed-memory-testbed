"""Runs all experiments for governed vs. naive memory. Usage: .venv/bin/python run_experiments.py [--seeds 5]"""
import os, sys, csv, json, time, random, shutil, sqlite3, argparse, statistics as st
import numpy as np
from govmem.store import GovernedMemory, NaiveMemory, DEFAULT_ACL, SENS, TYPES, PolicyError
from govmem.harnesses import ReActAgent, Autopilot, RAGAssistant, Counter
from govmem.llm import get_llm

R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"); os.makedirs(R, exist_ok=True)
W = "/tmp/govmem_runs"
TENANTS = {"acme": ["alice", "bob", "carol", "dan"], "globex": ["erin", "frank", "grace", "heidi"]}
TOPICS = ["quarterly deploy plan for region", "support ticket escalation", "customer contract renewal terms",
          "payroll and salary review", "incident postmortem database outage"]

class CountedGoverned(GovernedMemory):
    """Independent call counter (outside the audit path) used to measure audit completeness."""
    API = ("write", "search", "get_fact", "latest_checkpoint", "forget_user", "purge_expired")
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self.calls = 0
        for n in self.API:
            f = getattr(GovernedMemory, n)
            setattr(self, n, (lambda f: lambda *a, **k: (self._inc(), f(self, *a, **k))[1])(f))
    def _inc(self): self.calls += 1

class Clock:
    def __init__(self): self.t = 1_800_000_000.0
    def __call__(self): self.t += 0.001; return self.t

def save(name, rows):
    with open(os.path.join(R, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

def build(kind, seed, secure_delete=True):
    os.makedirs(W, exist_ok=True)
    path = f"{W}/{kind}_{seed}_{int(secure_delete)}.db"
    for s in ("", "-wal", "-shm"):
        if os.path.exists(path + s): os.remove(path + s)
    clk = Clock()
    mem = CountedGoverned(path, clock=clk, secure_delete=secure_delete) if kind == "governed" else NaiveMemory(path, clock=clk)
    return mem, path, clk

def populate(mem, llm, seed, clk):
    """Synthetic multi-user enterprise workload. Returns ledger (ground truth) and harnesses."""
    rnd = random.Random(seed); ctr = Counter()
    H = {"react": ReActAgent(mem, llm, ctr), "autopilot": Autopilot(mem, llm, ctr), "rag": RAGAssistant(mem, llm, ctr)}
    ledger, canary = [], {}
    def rec(rid, h, t, u, m, scope, sens, key=None, content="", ttl=None, parents=()):
        ledger.append(dict(id=rid, h=h, tenant=t, owner=u if scope == "user" else "*", user=u, mtype=m, scope=scope,
                           sens=sens, key=key, content=content, expires=clk.t + ttl if ttl else None, parents=list(parents),
                           superseded=False))
    for t, users in TENANTS.items():
        tenant_doc_ids = []
        for u in users:
            c = f"CANARY{u.upper()}{rnd.randint(10**5, 10**6)}"; canary[(t, u)] = c
            ids, texts = [], []
            for j, topic in enumerate(TOPICS):
                sens = rnd.choice(["internal", "internal", "confidential"])
                text = f"{u} note on {topic}: {c} detail {rnd.randint(0, 999)}"
                rid = H["rag"].ingest(t, u, text, sensitivity=sens); rec(rid, "rag", t, u, "semantic", "user", sens, content=text)
                ids.append(rid); texts.append(text)
            sid = H["rag"].summarize(t, u, ids, texts)
            rec(sid, "rag", t, u, "semantic", "user", "internal", content=llm.complete("\n".join(texts), "summarize"), parents=ids)
            tenant_doc_ids.append((ids[0], texts[0], u))
            # ReAct episodes
            for task in ("check support ticket", "what is my region preference"):
                H["react"].run(t, u, f"{u}: {task}")
                rid = mem.db.execute("SELECT id FROM " + ("memory" if isinstance(mem, GovernedMemory) else "mem") +
                                     " ORDER BY rowid DESC LIMIT 1").fetchone()[0]
                rec(rid, "react", t, u, "episodic", "user", "internal", content=task)
            # short-TTL episodic scratch (should expire)
            try:
                rid = mem.write("autopilot", t, u, "episodic", f"temp scratch {c}", ttl=60, sensitivity="internal"); ctr.n += 1
                rec(rid, "autopilot", t, u, "episodic", "user", "internal", content="scratch", ttl=60)
            except PolicyError: pass
            # procedural memory
            rid = mem.write("autopilot", t, u, "procedural", f"runbook for {u}: restart service then verify", sensitivity="internal"); ctr.n += 1
            rec(rid, "autopilot", t, u, "procedural", "user", "internal")
            # versioned fact written by different harnesses (conflict scenario)
            for v, (h, region) in enumerate([("react", "us-west"), ("rag", "eu-central"), ("react", rnd.choice(["ap-south", "us-east"]))]):
                rid = mem.write(h, t, u, "semantic", f"{u} preferred region is {region}", key="pref:region", sensitivity="internal"); ctr.n += 1
                for L in ledger:
                    if L["key"] == "pref:region" and L["user"] == u and L["tenant"] == t: L["superseded"] = True
                rec(rid, h, t, u, "semantic", "user", "internal", key="pref:region", content=region)
            # autopilot run to completion (checkpoints)
            plan = [f"step {i} for {u}" for i in range(8)]
            H["autopilot"].run(t, u, f"run-{u}", plan)
        # tenant-shared summary derived from several users' docs
        sid = mem.write("rag", t, users[0], "semantic", "TENANT DIGEST: " + " | ".join(x[1] for x in tenant_doc_ids),
                        scope="tenant", sensitivity="internal", derived_from=[x[0] for x in tenant_doc_ids]); ctr.n += 1
        rec(sid, "rag", t, users[0], "semantic", "tenant", "internal", content="digest", parents=[x[0] for x in tenant_doc_ids])
    return H, ledger, canary, ctr

def leakage(mem, canary, seed, n_queries=30):
    rnd = random.Random(seed + 7); leaks = cross_tenant = total = 0
    allc = {v: k for k, v in canary.items()}
    for (t, u) in canary:
        for h in ("react", "rag", "autopilot"):
            for _ in range(n_queries):
                other = rnd.choice([k for k in canary if k != (t, u)])
                q = f"{rnd.choice(TOPICS)} {other[1]} {canary[other]}"   # adversarial: query contains other user's canary
                for r in mem.search(h, t, u, q, k=5):
                    total += 1
                    owners = {allc[c] for c in allc if c in r["content"]}
                    foreign = owners - {(t, u)}
                    # tenant digest legitimately contains co-tenant content only for tenant-scoped records; count anyway
                    if foreign:
                        if r.get("user") == "*" : continue   # policy-sanctioned tenant-shared record
                        leaks += 1
                        if any(o[0] != t for o in foreign): cross_tenant += 1
    return dict(results=total, leaked=leaks, cross_tenant=cross_tenant, leak_rate=leaks / max(total, 1))

def expected_visible(h, t, u, L, now):
    a = DEFAULT_ACL[h]
    return (L["tenant"] == t and (L["owner"] == u or (L["owner"] == "*" and "tenant" in a["scopes"]))
            and L["mtype"] in a["read"] and SENS[L["sens"]] <= SENS[a["max_sens"]] and not L["superseded"]
            and (L["expires"] is None or L["expires"] > now))

def visibility(mem, ledger, clk, kind):
    rows = []
    ids_by_user = {}
    for h in ("react", "autopilot", "rag"):
        tp = fp = fn = tn = 0
        for t, users in TENANTS.items():
            for u in users:
                got = {r["id"] for m in TYPES for r in mem.search(h, t, u, "anything", k=10**6, mtypes=(m,))}
                for L in ledger:
                    if L["mtype"] == "working": continue   # checkpoints not in ledger
                    exp, vis = expected_visible(h, t, u, L, clk.t), L["id"] in got
                    tp += exp and vis; fp += (not exp) and vis; fn += exp and not vis; tn += (not exp) and not vis
        rows.append(dict(backend=kind, harness=h, tp=tp, fp=fp, fn=fn, tn=tn,
                         precision=tp / max(tp + fp, 1), recall=tp / max(tp + fn, 1), overexposure=fp / max(fp + tn, 1)))
    return rows

def staleness(mem, ledger, clk, kind):
    latest = {(L["tenant"], L["user"]): L["content"] for L in ledger if L["key"] == "pref:region" and not L["superseded"]}
    stale = n = 0
    for (t, u), region in latest.items():
        for h in ("react", "rag", "autopilot"):
            v = mem.get_fact(h, t, u, "pref:region"); n += 1
            if v is None or region not in v: stale += 1
    # semantic-search staleness: top hit for 'preferred region' must be the latest version
    sstale = sn = 0
    for (t, u), region in latest.items():
        hits = [r for r in mem.search("react", t, u, f"{u} preferred region is", k=3) if "preferred region" in r["content"]]
        sn += 1
        if not hits or region not in hits[0]["content"] or any(region not in h["content"] for h in hits): sstale += 1
    # TTL: expired scratch still retrievable?
    clk.t += 3600
    exp = expn = 0
    for t, users in TENANTS.items():
        for u in users:
            expn += 1
            if any("temp scratch" in r["content"] and r.get("user") in (u, "summarizer", None, u)
                   for r in mem.search("react", t, u, "temp scratch", k=20, mtypes=("episodic",))): exp += 1
    return dict(backend=kind, fact_reads=n, stale_fact_reads=stale, stale_fact_rate=stale / n,
                search_checks=sn, stale_or_conflicting_search=sstale, stale_search_rate=sstale / sn,
                ttl_checks=expn, expired_still_visible=exp, expired_visible_rate=exp / expn)

def crash_resume(mem, llm, seed, kind, n_runs=40):
    rnd = random.Random(seed + 3); ctr = Counter(); ap = Autopilot(mem, llm, ctr); rows = []
    for i in range(n_runs):
        t = rnd.choice(list(TENANTS)); u = rnd.choice(TENANTS[t]); n = rnd.randint(6, 20); crash = rnd.randint(1, n - 1)
        plan = [f"task {i}.{j} {rnd.random():.4f}" for j in range(n)]
        golden_mem = GovernedMemory(":memory:"); golden, _ = Autopilot(golden_mem, llm, Counter()).run(t, u, "g", plan)
        try: ap.run(t, u, f"cr{seed}-{i}", plan, crash_at=crash)
        except RuntimeError: pass
        final, resumed = ap.run(t, u, f"cr{seed}-{i}", plan)
        executed = crash + (n - resumed)
        rows.append(dict(backend=kind, seed=seed, run=i, plan_len=n, crash_at=crash, resumed_from=resumed,
                         state_match=int(final["acc"] == golden["acc"] and final["step"] == n),
                         redone_steps=executed - n, duplicate_side_effects=executed - n))
    return rows

def residue_scan(mem, path, canary, victims, kind):
    con = sqlite3.connect(path); con.execute("PRAGMA wal_checkpoint(TRUNCATE)") if True else None
    rows = []
    mem.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = open(path, "rb").read()
    tbl = "memory" if kind.startswith("governed") else "mem"
    etbl = "embedding" if kind.startswith("governed") else "emb"
    orphans = con.execute(f"SELECT COUNT(*) FROM {etbl} WHERE id NOT IN (SELECT id FROM {tbl})").fetchone()[0]
    for (t, u) in victims:
        c = canary[(t, u)]
        prim = con.execute(f"SELECT COUNT(*) FROM {tbl} WHERE content LIKE ? AND content NOT LIKE 'SUMMARY:%' AND content NOT LIKE 'TENANT DIGEST:%'", (f"%{c}%",)).fetchone()[0]
        der = con.execute(f"SELECT COUNT(*) FROM {tbl} WHERE content LIKE ? AND (content LIKE 'SUMMARY:%' OR content LIKE 'TENANT DIGEST:%')", (f"%{c}%",)).fetchone()[0]
        retrievable = 0
        for (t2, u2) in canary:
            if (t2, u2) in victims: continue
            retrievable += sum(c in r["content"] for r in mem.search("rag", t2, u2, f"{u} {c}", k=10))
        rows.append(dict(backend=kind, user=f"{t}/{u}", primary_residue=prim, derived_residue=der,
                         retrievable_by_others=retrievable, raw_file_bytes_hit=int(c.encode() in raw)))
    return rows, orphans

def audit_check(mem, ctr):
    audited = mem.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    harness_ops = mem.db.execute("SELECT COUNT(*) FROM audit WHERE harness IN ('react','autopilot','rag')").fetchone()[0]
    ok_chain = mem.verify_audit()
    try: mem.db.execute("UPDATE audit SET decision='allow' WHERE seq=1"); blocked = 0
    except sqlite3.DatabaseError: blocked = 1
    # attacker with raw file access: drop trigger and tamper -> chain must detect
    mem.db.execute("DROP TRIGGER audit_no_upd"); mem.db.execute("UPDATE audit SET detail='tampered' WHERE seq=5")
    detected = int(not mem.verify_audit())
    denies = mem.db.execute("SELECT COUNT(*) FROM audit WHERE decision='deny'").fetchone()[0]
    return dict(api_calls=mem.calls, audit_rows=audited, coverage=audited / mem.calls, harness_level_ops=ctr.n,
                audited_harness_rows=harness_ops,
                chain_valid=int(ok_chain), update_blocked=blocked, tamper_detected=detected, deny_events=denies)

def policy_denials(mem):
    """Attempted writes that the ACL must reject."""
    cases = [("rag", "working"), ("rag", "procedural"), ("react", "working"), ("autopilot", "semantic"), ("admin", "episodic")]
    out = []
    for h, m in cases:
        try: mem.write(h, "acme", "alice", m, "x"); out.append(0)
        except PolicyError: out.append(1)
    try: mem.write("autopilot", "acme", "alice", "episodic", "x", scope="tenant"); out.append(0)
    except PolicyError: out.append(1)
    return sum(out), len(out)

def bench(kind, sizes=(1000, 4000, 10000), nq=200):
    rows = []; llm = get_llm("mock")
    for N in sizes:
        mem, path, clk = build(kind, f"bench{N}")
        rnd = random.Random(N); users = [(t, u) for t in TENANTS for u in TENANTS[t]]
        wl = []
        for i in range(N):
            t, u = rnd.choice(users); s = time.perf_counter()
            mem.write("rag", t, u, "semantic", f"{u} {rnd.choice(TOPICS)} {rnd.random()} {i}", sensitivity="internal")
            wl.append(time.perf_counter() - s)
        sl = []
        for i in range(nq):
            t, u = rnd.choice(users); s = time.perf_counter(); mem.search("rag", t, u, rnd.choice(TOPICS), k=5)
            sl.append(time.perf_counter() - s)
        t, u = users[0]; s = time.perf_counter(); n_del = mem.forget_user(t, u); dl = time.perf_counter() - s
        mem.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = os.path.getsize(path)
        rows.append(dict(backend=kind, n_records=N, write_p50_ms=1e3 * st.median(wl), write_p95_ms=1e3 * np.percentile(wl, 95),
                         search_p50_ms=1e3 * st.median(sl), search_p95_ms=1e3 * np.percentile(sl, 95),
                         forget_user_ms=1e3 * dl, deleted=n_del, db_bytes=size, bytes_per_record=size / N))
    return rows

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--seeds", type=int, default=5); a = ap.parse_args()
    llm = get_llm(); print("LLM backend:", llm.name)
    L, V, S, C, D, A, O = [], [], [], [], [], [], []
    for seed in range(a.seeds):
        for kind in ("governed", "naive"):
            mem, path, clk = build(kind, seed)
            H, ledger, canary, ctr = populate(mem, llm, seed, clk)
            L.append(dict(backend=kind, seed=seed, **leakage(mem, canary, seed)))
            V += [dict(seed=seed, **r) for r in visibility(mem, ledger, clk, kind)]
            S.append(dict(seed=seed, **staleness(mem, ledger, clk, kind)))
            C += crash_resume(mem, llm, seed, kind)
            victims = [("acme", "bob"), ("globex", "frank")]
            for (t, u) in victims: mem.forget_user(t, u)
            if kind == "governed": mem.purge_expired()
            rr, orph = residue_scan(mem, path, canary, victims, kind)
            D += [dict(seed=seed, orphan_embeddings=orph, **r) for r in rr]
            if kind == "governed":
                den, tot = policy_denials(mem); O.append(dict(seed=seed, denied=den, attempted=tot))
                A.append(dict(seed=seed, **audit_check(mem, ctr)))
                # ablation: governed without SQLite secure_delete -> raw-page residue
                m2, p2, c2 = build("governed", seed, secure_delete=False)
                _, _, can2, _ = populate(m2, llm, seed, c2)
                for (t, u) in victims: m2.forget_user(t, u)
                rr, orph = residue_scan(m2, p2, can2, victims, "governed_no_secure_delete")
                D += [dict(seed=seed, orphan_embeddings=orph, **r) for r in rr]
        print("seed", seed, "done")
    save("leakage.csv", L); save("visibility.csv", V); save("staleness.csv", S); save("crash_resume.csv", C)
    save("deletion.csv", D); save("audit.csv", A); save("policy_denials.csv", O)
    B = bench("governed") + bench("naive"); save("overhead.csv", B)
    json.dump({"llm_backend": llm.name, "seeds": a.seeds, "tenants": len(TENANTS),
               "users": sum(map(len, TENANTS.values()))}, open(os.path.join(R, "run_meta.json"), "w"), indent=1)
    print("done")

if __name__ == "__main__": main()
