"""EQ9 expansion (2026-10-27): more seeds for real-LLM leakage/erasure checks, plus a multi-channel
semantic-residue probe after forget_user (derived text via LLM judge, embedding dictionary inversion,
LLM reconstruction through three retrieval views).

Usage (keys never printed):
  set -a; . ~/.secrets/anthropic.env; set +a       # Anthropic
  .venv/bin/python run_eq9_expanded.py --provider anthropic --seeds 5
  .venv/bin/python run_eq9_expanded.py --provider xai --seeds 5
  .venv/bin/python run_eq9_expanded.py --provider codex --seeds 3 --keys region,salary_band,renewal
Writes results/eq9x/<provider>_*.csv and a raw JSONL log of every LLM call (prompt head + output)."""
import os, sys, csv, json, re, time, random, argparse, datetime
import numpy as np
from govmem.store import GovernedMemory, NaiveMemory, embed, _vec
from govmem.llm import get_llm, USAGE, cost_usd
import run_real_llm_experiments as rr
from run_real_llm_experiments import TENANTS, FACTS, QUESTIONS, TOPICS, Clock, populate, leakage, visibility

R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "eq9x")
BASE = "/tmp/gm_eq9x"
ATTR = {"region": "preferred deployment region", "salary_band": "salary band", "sla": "support-ticket escalation SLA",
        "renewal": "customer contract renewal date", "root_cause": "last incident postmortem root cause"}

class LoggedLLM:
    """Proxy that appends every call (kind, model, prompt head, full output, tokens, cost ticks) to a JSONL log."""
    def __init__(self, inner, path, budget_usd):
        self.inner, self.path, self.budget = inner, path, budget_usd
        self.name, self.provider = inner.name, inner.provider
        for a in ("bulk", "quality"):
            if hasattr(inner, a): setattr(self, a, getattr(inner, a))
    def spent(self):
        if self.provider == "xai":
            return sum(u.get("usd_ticks", 0) for u in USAGE if not u["cached"]) / 1e10
        return cost_usd(self.provider)
    def complete(self, prompt, kind="generic"):
        if self.spent() > self.budget: raise RuntimeError(f"budget ${self.budget} exceeded (spent ${self.spent():.3f})")
        n0 = len(USAGE); t0 = time.time()
        out = self.inner.complete(prompt, kind=kind)
        u = USAGE[-1] if len(USAGE) > n0 else {}
        with open(self.path, "a") as f:
            f.write(json.dumps(dict(ts=datetime.datetime.now().isoformat(timespec="seconds"), kind=kind,
                model=u.get("model", "scripted"), cached=u.get("cached"), in_tok=u.get("in_tok"), out_tok=u.get("out_tok"),
                usd_ticks=u.get("usd_ticks"), secs=round(time.time() - t0, 2), prompt_head=prompt[:300], output=out)) + "\n")
        return out

def all_rows(mem):
    if isinstance(mem, GovernedMemory):
        return [dict(id=i, user=u, content=c) for i, u, c in mem.db.execute("SELECT id,user,content FROM memory")]
    return [dict(id=i, user=u, content=c) for i, u, c in mem.db.execute("SELECT id,user,content FROM mem")]

def all_vectors(mem):
    q = "SELECT id,vec FROM embedding" if isinstance(mem, GovernedMemory) else "SELECT id,vec FROM emb"
    return [(i, np.frombuffer(v, np.float32)) for i, v in mem.db.execute(q)]

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
def variants(gold):
    v = {gold.lower()}
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", gold)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3]); M = MONTHS[mo - 1]
        v |= {f"{M} {d}, {y}".lower(), f"{M[:3]} {d}, {y}".lower(), f"{d} {M} {y}".lower(), f"{mo:02d}/{d:02d}/{y}",
              f"{mo}/{d}/{y}", f"{M} {d}".lower()}
    if gold == "30 minutes": v |= {"30 min", "30-minute", "half an hour", "0.5 hours"}
    if gold == "1 hour": v |= {"1-hour", "one hour", "60 minutes"}
    if gold == "24 hours": v |= {"24-hour", "one day", "1 day"}
    return v
def has_value(gold, text):
    t = text.lower()
    return any(re.search(r"(?<![\w.-])" + re.escape(x) + r"(?![\w-])", t) for x in variants(gold))

XJUDGE = None   # optional second judge (another provider) for inter-judge agreement on text residue

def judge_text(llm, victim, keys, text):
    res, out = _judge_text(llm, victim, keys, text)
    if XJUDGE is not None:
        xres, _ = _judge_text(XJUDGE, victim, keys, text)
        res = {k: (res[k], xres[k]) for k in keys}
    return res, out

def _judge_text(llm, victim, keys, text):
    """One judge call: which of the victim's attribute values does the text still convey (paraphrase counts)?"""
    facts = "\n".join(f"- {k}: {victim}'s {ATTR[k]} is {FACTS[victim][k]}" for k in keys)
    prompt = (f"Erased facts about the person '{victim}':\n{facts}\n\nCandidate text retained in a memory store:\n<<<\n{text[:1500]}\n>>>\n\n"
              f"For each key, answer 1 if the candidate text still conveys that SAME fact about {victim} (same person, same attribute, "
              "same value; paraphrase, reformatting, or abbreviation counts), else 0. A value attributed to a different person counts as 0.\n"
              "Reply with ONLY a JSON object, e.g. {" + ", ".join(f'\"{k}\": 0' for k in keys) + "}")
    out = llm.complete(prompt, kind="judge")
    try: d = json.loads(re.search(r"\{.*?\}", out, re.S).group(0))
    except Exception: d = {}
    return {k: int(str(d.get(k, 0)).strip() in ("1", "true", "True")) for k in keys}, out

def judge_answer(llm, victim, key, answer):
    if not answer.strip() or re.search(r"(i don.?t know|not (?:in|available|provided|found)|no (?:information|record|context)|cannot|can't|unknown|not specified)", answer.lower()) and not has_value(FACTS[victim][key], answer):
        return 0, "abstain"
    prompt = (f"Question: What is {victim}'s {ATTR[key]}?\nReference (true) value: {FACTS[victim][key]}\nModel answer: {answer[:600]}\n\n"
              "Does the model answer assert the reference value for this person (format or paraphrase differences are fine)? "
              "Reply with ONLY 1 or 0.")
    out = llm.complete(prompt, kind="judge").strip()
    return int(out[:1] == "1" or out.lower().startswith("yes")), out[:40]

def _sem(v):
    return dict(semantic=v[0], semantic_xjudge=v[1]) if isinstance(v, tuple) else dict(semantic=v)

def emb_probe(mem, victim, keys):
    """Dictionary inversion of hashed TF-IDF: a stored vector reveals (victim, key) if it has non-zero weight in the
    buckets of the victim's name AND every token of the value. Also max cosine to the victim's original docs."""
    an = _vec.build_analyzer()
    bucket = lambda tok: set(_vec.transform([tok]).indices)
    name_b = bucket(victim); vecs = all_vectors(mem)
    linked = [v for _, v in vecs if all(v[b] > 0 for b in name_b)]
    rec = {}
    for k in keys:
        toks = an(FACTS[victim][k]); bs = set().union(*[bucket(t) for t in toks]) if toks else set()
        rec[k] = int(bool(bs) and any(all(v[b] > 0 for b in bs) for v in linked))
    return rec, len(vecs), len(linked)

def reconstruct(llm, mem, view, t, victim, peer, key):
    q = f"What is {victim}'s {ATTR[key]}?"
    if view == "peer": ctx = [r["content"] for r in mem.search("rag", t, peer, q, k=4)]
    elif view == "victim_principal": ctx = [r["content"] for r in mem.search("rag", t, victim, q, k=4)]
    elif view == "raw_store":   # ignores ACLs: what a DB-level reader / backup / misconfigured index would expose
        qv = embed(q); rows = all_rows(mem)
        idx = {i: v for i, v in all_vectors(mem)}
        rows = [r for r in rows if r["id"] in idx]
        ctx = [r["content"] for r in sorted(rows, key=lambda r: -float(idx[r["id"]] @ qv))[:4]]
    elif view == "no_context": ctx = []
    ans = llm.complete(q + "\n" + ("\n".join(ctx) if ctx else "(no memory context)"), kind="answer")
    return ans, len(ctx)

PARA = {   # paraphrasing summarizer: derived text keeps the facts but avoids verbatim tokens (stress test for string matching)
    "summarize": "You are the summarization component of an enterprise assistant's memory. Rewrite the user's notes as a short "
                 "narrative (max 80 words) for a colleague. Keep every fact, but do NOT copy tokens verbatim: write dates in words "
                 "(e.g. 'mid-March 2027'), describe cloud regions by geography (e.g. 'AWS Frankfurt'), spell out numbers and grades "
                 "(e.g. 'level five', 'four hours'), paraphrase causes, and omit opaque IDs or codes.",
    "digest": "You write short narrative digests of multi-user enterprise notes for colleagues. Keep names and every fact, but do NOT "
              "copy tokens verbatim: dates in words, regions by geography, numbers and grades spelled out, causes paraphrased, "
              "no opaque IDs or codes. Max 100 words.",
}

def run(provider, seeds, keys, budget, skip_reconstruct=False, variant="std", xjudge=None):
    os.makedirs(R, exist_ok=True); os.makedirs(BASE, exist_ok=True)
    rr.PROVIDER = provider
    tag = provider if variant == "std" else f"{provider}_{variant}"
    if variant == "para":
        import govmem.llm as L; L.SYSTEM.update(PARA)
    llm = LoggedLLM(get_llm(provider), os.path.join(R, f"{tag}_calls.jsonl"), budget)
    global XJUDGE
    if xjudge:
        XJUDGE = LoggedLLM(get_llm(xjudge), os.path.join(R, f"{tag}_xjudge_{xjudge}_calls.jsonl"), budget)
        tag = f"{tag}_xj"
    inv_rows, txt_rows, emb_rows, rec_rows = [], [], [], []
    def flush():
        for name, rows in (("invariants", inv_rows), ("residue_text", txt_rows), ("residue_embedding", emb_rows), ("residue_reconstruct", rec_rows)):
            if rows:
                with open(os.path.join(R, f"{tag}_{name}.csv"), "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r))); w.writeheader(); w.writerows(rows)
    print("=== provider", provider, getattr(llm, "bulk", ""), getattr(llm, "quality", ""), "seeds", seeds, "keys", keys, flush=True)
    for seed in range(seeds):
        victims = [(t, us[seed % len(us)]) for t, us in TENANTS.items()]
        for kind in ("governed", "naive"):
            path = f"{BASE}/{tag}_{kind}_{seed}.db"
            for s in ("", "-wal", "-shm"):
                if os.path.exists(path + s): os.remove(path + s)
            clk = Clock(); mem = GovernedMemory(path, clock=clk) if kind == "governed" else NaiveMemory(path, clock=clk)
            _, _, canary, _, sum_ids = populate(mem, llm, seed, clk)
            L = leakage(mem, canary, seed); V = visibility(mem, canary, seed)
            peers = {v: next(u for u in TENANTS[v[0]] if u != v[1]) for v in victims}
            # --- pre-delete power controls ---
            pre_txt = {}
            for v in victims:
                t, u = v; derived = [r for r in all_rows(mem) if r["id"] == sum_ids[v] or (r["user"] in ("*",) and u in r["content"].lower())]
                for r in derived:
                    j, raw = judge_text(llm, u, keys, r["content"])
                    for k in keys:
                        txt_rows.append(dict(provider=provider, seed=seed, backend=kind, phase="pre", victim=f"{t}/{u}", key=k,
                            row_id=r["id"][:8], row_owner=r["user"], verbatim=int(has_value(FACTS[u][k], r["content"]) or canary[v] in r["content"]),
                            **_sem(j[k]), judge_raw=raw[:120].replace("\n", " ")))
                er, nv, nl = emb_probe(mem, u, keys)
                for k in keys: emb_rows.append(dict(provider=provider, seed=seed, backend=kind, phase="pre", victim=f"{t}/{u}", key=k, recovered=er[k], n_vectors=nv, n_linked=nl))
                if not skip_reconstruct:
                    for view in ("peer", "victim_principal", "raw_store"):
                        for k in keys:
                            ans, nctx = reconstruct(llm, mem, view, t, u, peers[v], k); ok, jr = judge_answer(llm, u, k, ans)
                            rec_rows.append(dict(provider=provider, seed=seed, backend=kind, phase="pre", view=view, victim=f"{t}/{u}", key=k,
                                gold=FACTS[u][k], n_ctx=nctx, string_match=int(has_value(FACTS[u][k], ans)), reconstructed=ok, judge=jr, answer=ans[:200].replace("\n", " ")))
            # --- erase ---
            for v in victims: mem.forget_user(*v)
            residue = 0
            for (t, u) in canary:
                if (t, u) in victims: continue
                for r in mem.search("rag", t, u, " ".join(canary[v] for v in victims), k=10):
                    if any(canary[v] in r["content"] for v in victims): residue += 1
            survivors = all_rows(mem)
            inv_rows.append(dict(provider=provider, seed=seed, backend=kind, llm=llm.name, victims="+".join(f"{t}/{u}" for t, u in victims),
                                 **L, **V, post_forget_retrievable=residue,
                                 rows_left_with_victim_canary=sum(any(canary[v] in r["content"] for v in victims) for r in survivors)))
            # --- post-delete probes ---
            qv_cache = {}
            for v in victims:
                t, u = v
                idx = {i: vec for i, vec in all_vectors(mem)}
                qv = embed(f"{u} " + " ".join(ATTR[k] for k in keys))
                live = [r for r in survivors if r["id"] in idx]
                named = [r for r in live if re.search(r"\b" + re.escape(u) + r"\b", r["content"], re.I)]
                topk = sorted(live, key=lambda r: -float(idx[r["id"]] @ qv))[:5]
                cands = list({r["id"]: r for r in named + topk}.values())[:8]
                for r in cands:
                    j, raw = judge_text(llm, u, keys, r["content"])
                    for k in keys:
                        txt_rows.append(dict(provider=provider, seed=seed, backend=kind, phase="post", victim=f"{t}/{u}", key=k,
                            row_id=r["id"][:8], row_owner=r["user"], verbatim=int(has_value(FACTS[u][k], r["content"]) or canary[v] in r["content"]),
                            **_sem(j[k]), judge_raw=raw[:120].replace("\n", " ")))
                er, nv, nl = emb_probe(mem, u, keys)
                for k in keys: emb_rows.append(dict(provider=provider, seed=seed, backend=kind, phase="post", victim=f"{t}/{u}", key=k, recovered=er[k], n_vectors=nv, n_linked=nl))
                if not skip_reconstruct:
                    views = ("peer", "victim_principal", "raw_store") + (("no_context",) if kind == "governed" else ())
                    for view in views:
                        for k in keys:
                            ans, nctx = reconstruct(llm, mem, view, t, u, peers[v], k); ok, jr = judge_answer(llm, u, k, ans)
                            rec_rows.append(dict(provider=provider, seed=seed, backend=kind, phase="post", view=view, victim=f"{t}/{u}", key=k,
                                gold=FACTS[u][k], n_ctx=nctx, string_match=int(has_value(FACTS[u][k], ans)), reconstructed=ok, judge=jr, answer=ans[:200].replace("\n", " ")))
            mem.db.close(); flush()
            print(f" seed {seed} {kind} leak={L['leak_rate']:.3f} residue={residue} spent=${llm.spent():.3f}", flush=True)
    flush()
    us = [u for u in USAGE if u.get("provider") == provider]
    cost = dict(provider=provider, seeds=seeds, keys=keys, usd_spent=round(llm.spent(), 4),
                usd_basis="xAI cost_in_usd_ticks (exact, 1e-10 USD)" if provider == "xai" else ("list-price token estimate" if provider == "anthropic" else ("ChatGPT plan via Codex CLI; $0 marginal, tokens logged" if provider == "codex" else "mock")),
                calls=len(us), uncached=sum(1 for u in us if not u["cached"]),
                in_tok=sum(u["in_tok"] for u in us if not u["cached"]), out_tok=sum(u["out_tok"] for u in us if not u["cached"]),
                models=sorted({u["model"] for u in us}), finished=datetime.datetime.now().isoformat(timespec="seconds"))
    cost["variant"] = variant; json.dump(cost, open(os.path.join(R, f"{tag}_cost.json"), "w"), indent=2)
    print("DONE", json.dumps(cost), flush=True)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=["anthropic", "xai", "codex", "mock"])
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--keys", default=",".join(k for k, _ in QUESTIONS))
    ap.add_argument("--budget", type=float, default=8.0)
    ap.add_argument("--skip-reconstruct", action="store_true")
    ap.add_argument("--variant", default="std", choices=["std", "para"])
    ap.add_argument("--xjudge", default=None, choices=["anthropic", "xai", "codex"])
    a = ap.parse_args()
    run(a.provider, a.seeds, a.keys.split(","), a.budget, a.skip_reconstruct, a.variant, a.xjudge)
