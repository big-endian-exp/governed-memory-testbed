"""Aggregate results/eq9x/<provider>_*.csv into results/eq9x/summary.csv and summary.md (rates with Wilson 95% CIs)."""
import os, csv, json, math, collections
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "eq9x")
TAGS = ("anthropic", "codex", "xai", "anthropic_para", "codex_para", "xai_para", "anthropic_para_xj")
PROV = [p for p in TAGS if os.path.exists(os.path.join(R, f"{p}_invariants.csv"))]
LBL = {"anthropic": "Claude", "codex": "OpenAI/Codex", "xai": "Grok/xAI"}
LBL.update({k + "_para": v + " (paraphrasing summarizer)" for k, v in list(LBL.items())})
LBL["anthropic_para_xj"] = "Claude (paraphrasing summarizer), Claude judge + Grok cross-judge"
PRICE = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-6": (3.0, 15.0)}
def spend(tag):
    """Total spend over ALL runs of this tag (incl. runs that were interrupted and resumed from cache), from the raw call log."""
    fp = os.path.join(R, f"{tag}_calls.jsonl")
    if not os.path.exists(fp): return {}
    L = [json.loads(l) for l in open(fp)]; U = [x for x in L if x.get("cached") is False]
    usd = (sum((x["usd_ticks"] or 0) for x in U) / 1e10 if tag.startswith("xai") else
           sum(x["in_tok"] * PRICE.get(x["model"], (0, 0))[0] + x["out_tok"] * PRICE.get(x["model"], (0, 0))[1] for x in U) / 1e6)
    return dict(tag=tag, logged_calls=len(L), uncached_calls=len(U), in_tok=sum(x["in_tok"] or 0 for x in U), out_tok=sum(x["out_tok"] or 0 for x in U),
                usd=round(usd, 4), basis="xAI cost_in_usd_ticks" if tag.startswith("xai") else ("list price x tokens" if tag.startswith("anthropic") else "plan-billed (Codex CLI), $0 marginal"))
ld = lambda p, n: list(csv.DictReader(open(os.path.join(R, f"{p}_{n}.csv")))) if os.path.exists(os.path.join(R, f"{p}_{n}.csv")) else []
def wilson(k, n, z=1.96):
    if n == 0: return (float("nan"),) * 2
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0, (c - h) / d), min(1, (c + h) / d)
def fmt(k, n):
    lo, hi = wilson(k, n); return f"{k}/{n} ({k/n:.2f}; CI {lo:.2f}-{hi:.2f})" if n else "n/a"
out, md = [], []
for p in PROV:
    inv = ld(p, "invariants"); txt = ld(p, "residue_text"); emb = ld(p, "residue_embedding"); rec = ld(p, "residue_reconstruct")
    cost = json.load(open(os.path.join(R, f"{p}_cost.json"))) if os.path.exists(os.path.join(R, f"{p}_cost.json")) else {}
    sp = spend(p)
    md.append(f"\n## {LBL[p]} ({p})  seeds={len({r['seed'] for r in inv})}  total spend=${sp.get('usd','?')} [{sp.get('basis','')}]  uncached calls={sp.get('uncached_calls','?')} in/out tok={sp.get('in_tok','?')}/{sp.get('out_tok','?')}  models={cost.get('models','?')}  finished={cost.get('finished','INCOMPLETE')}\n")
    md.append("| metric | governed | naive |\n|---|---|---|")
    def add(metric, g, n): out.append(dict(provider=p, metric=metric, governed=g, naive=n)); md.append(f"| {metric} | {g} | {n} |")
    for b in ("governed",):
        pass
    I = {b: [r for r in inv if r["backend"] == b] for b in ("governed", "naive")}
    add("leaked / adversarial results (all seeds)", fmt(sum(int(r["leaked"]) for r in I["governed"]), sum(int(r["results"]) for r in I["governed"])),
        fmt(sum(int(r["leaked"]) for r in I["naive"]), sum(int(r["results"]) for r in I["naive"])))
    add("leak rate range across seeds", f"{min(float(r['leak_rate']) for r in I['governed']):.3f}-{max(float(r['leak_rate']) for r in I['governed']):.3f}" if I["governed"] else "",
        f"{min(float(r['leak_rate']) for r in I['naive']):.3f}-{max(float(r['leak_rate']) for r in I['naive']):.3f}" if I["naive"] else "")
    add("post-forget retrievable canary results (sum)", sum(int(r["post_forget_retrievable"]) for r in I["governed"]), sum(int(r["post_forget_retrievable"]) for r in I["naive"]))
    add("rows left holding victim canary (sum)", sum(int(r["rows_left_with_victim_canary"]) for r in I["governed"]), sum(int(r["rows_left_with_victim_canary"]) for r in I["naive"]))
    for phase in ("pre", "post"):
        g = {}
        for b in ("governed", "naive"):
            pairs = collections.defaultdict(lambda: [0, 0])
            for r in txt:
                if r["backend"] == b and r["phase"] == phase:
                    k = (r["seed"], r["victim"], r["key"]); pairs[k][0] |= int(r["semantic"]); pairs[k][1] |= int(r["verbatim"])
            # denominator: all (seed, victim, key) pairs probed, including those with no candidate rows
            allp = {(r["seed"], r["victim"], r["key"]) for r in emb if r["backend"] == b and r["phase"] == phase}
            n = len(allp); s = sum(pairs[k][0] for k in allp if k in pairs); v = sum(pairs[k][1] for k in allp if k in pairs)
            so = sum(1 for k in allp if k in pairs and pairs[k][0] and not pairs[k][1])
            g[b] = (fmt(s, n), fmt(v, n), fmt(so, n))
        lab = "derived rows (summary/digest) convey fact [power check]" if phase == "pre" else "surviving rows convey fact"
        add(f"text/{phase}: {lab} - LLM judge", g["governed"][0], g["naive"][0])
        add(f"text/{phase}: string/format match", g["governed"][1], g["naive"][1])
        add(f"text/{phase}: judge-only (missed by string match)", g["governed"][2], g["naive"][2])
    for phase in ("pre", "post"):
        e = {b: [r for r in emb if r["backend"] == b and r["phase"] == phase] for b in ("governed", "naive")}
        add(f"embedding/{phase}: name+value recoverable from stored vectors", fmt(sum(int(r["recovered"]) for r in e["governed"]), len(e["governed"])),
            fmt(sum(int(r["recovered"]) for r in e["naive"]), len(e["naive"])))
    for phase in ("pre", "post"):
        for view in ("peer", "victim_principal", "raw_store", "no_context"):
            x = {b: [r for r in rec if r["backend"] == b and r["phase"] == phase and r["view"] == view] for b in ("governed", "naive")}
            if not (x["governed"] or x["naive"]): continue
            add(f"reconstruct/{phase}/{view}: LLM answer reveals value (judge)", fmt(sum(int(r["reconstructed"]) for r in x["governed"]), len(x["governed"])),
                fmt(sum(int(r["reconstructed"]) for r in x["naive"]), len(x["naive"])))
            add(f"reconstruct/{phase}/{view}: string/format match", fmt(sum(int(r["string_match"]) for r in x["governed"]), len(x["governed"])),
                fmt(sum(int(r["string_match"]) for r in x["naive"]), len(x["naive"])))
with open(os.path.join(R, "summary.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["provider", "metric", "governed", "naive"]); w.writeheader(); w.writerows(out)
json.dump([spend(t) for t in TAGS if spend(t)], open(os.path.join(R, "spend.json"), "w"), indent=2)
open(os.path.join(R, "summary.md"), "w").write("# EQ9 expansion: results (generated by make_eq9x_tables.py)\n" + "\n".join(md) + "\n")
print("\n".join(md))
