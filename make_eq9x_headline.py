"""Headline tables for the paper from results/eq9x/*.csv (+ legacy results/real_semantic_residue*.csv).
Writes results/eq9x/headline.csv, results/eq9x/headline.md. Only counts what is in the CSVs."""
import os, csv, json, math, collections
H = os.path.dirname(os.path.abspath(__file__)); R = os.path.join(H, "results", "eq9x")
TAGS = ("anthropic", "codex", "xai", "anthropic_para", "codex_para", "xai_para")
ld = lambda t, n: list(csv.DictReader(open(os.path.join(R, f"{t}_{n}.csv")))) if os.path.exists(os.path.join(R, f"{t}_{n}.csv")) else []
def wilson(k, n, z=1.96):
    p = k / n; d = 1 + z*z/n; c = p + z*z/(2*n); h = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)); return max(0, (c-h)/d), min(1, (c+h)/d)
f = lambda k, n: f"{k}/{n} [{wilson(k,n)[0]:.2f},{wilson(k,n)[1]:.2f}]" if n else "n/a"
def pairs(txt, emb, b, ph, col="semantic"):
    allp = {(r["seed"], r["victim"], r["key"]) for r in emb if r["backend"] == b and r["phase"] == ph}
    s, v = collections.defaultdict(int), collections.defaultdict(int)
    for r in txt:
        if r["backend"] == b and r["phase"] == ph:
            k = (r["seed"], r["victim"], r["key"]); s[k] |= int(r[col]); v[k] |= int(r["verbatim"])
    return len(allp), sum(s[k] for k in allp), sum(v[k] for k in allp)
rows = []
for t in TAGS:
    inv = ld(t, "invariants")
    if not inv: rows.append(dict(cell=t, status="MISSING")); continue
    txt, emb, rec = ld(t, "residue_text"), ld(t, "residue_embedding"), ld(t, "residue_reconstruct")
    cost = json.load(open(os.path.join(R, f"{t}_cost.json"))) if os.path.exists(os.path.join(R, f"{t}_cost.json")) else {}
    I = {b: [r for r in inv if r["backend"] == b] for b in ("governed", "naive")}
    d = dict(cell=t, status="complete" if cost.get("finished") else "INCOMPLETE", seeds=len({r["seed"] for r in inv}), keys=len(cost.get("keys", [])) or "?")
    for b, a in (("governed", "gov"), ("naive", "nai")):
        d[f"{a}_leak"] = f(sum(int(r["leaked"]) for r in I[b]), sum(int(r["results"]) for r in I[b]))
        d[f"{a}_canary_rows_left"] = sum(int(r["rows_left_with_victim_canary"]) for r in I[b])
        n, s, v = pairs(txt, emb, b, "pre"); d[f"{a}_pre_text_judge"] = f(s, n); d[f"{a}_pre_text_string"] = f(v, n)
        n, s, v = pairs(txt, emb, b, "post"); d[f"{a}_post_text_judge"] = f(s, n); d[f"{a}_post_text_string"] = f(v, n)
        e = [r for r in emb if r["backend"] == b and r["phase"] == "post"]; d[f"{a}_post_emb"] = f(sum(int(r["recovered"]) for r in e), len(e))
        for view in ("peer", "raw_store"):
            x = [r for r in rec if r["backend"] == b and r["phase"] == "post" and r["view"] == view]
            d[f"{a}_post_recon_{view}"] = f(sum(int(r["reconstructed"]) for r in x), len(x))
    x = [r for r in rec if r["backend"] == "governed" and r["phase"] == "pre" and r["view"] == "peer"]
    d["gov_pre_recon_peer"] = f(sum(int(r["reconstructed"]) for r in x), len(x))
    x = [r for r in rec if r["backend"] == "governed" and r["phase"] == "post" and r["view"] == "no_context"]
    d["gov_post_recon_nocontext"] = f(sum(int(r["reconstructed"]) for r in x), len(x))
    rows.append(d)
# inter-judge agreement (Claude judge vs Grok cross-judge) on the paraphrase cell, row-level and pair-level
xt = ld("anthropic_para_xj", "residue_text"); kap = {}
if xt:
    a = [(int(r["semantic"]), int(r["semantic_xjudge"])) for r in xt]
    n = len(a); po = sum(x == y for x, y in a) / n; p1 = sum(x for x, _ in a) / n; p2 = sum(y for _, y in a) / n
    pe = p1*p2 + (1-p1)*(1-p2); kap = dict(n_row_key=n, agree=round(po, 4), kappa=round((po-pe)/(1-pe), 4) if pe < 1 else float("nan"),
        claude_pos=sum(x for x, _ in a), grok_pos=sum(y for _, y in a), disagree=sum(x != y for x, y in a))
    for ph in ("pre", "post"):
        for b in ("governed", "naive"):
            sub = [(int(r["semantic"]), int(r["semantic_xjudge"])) for r in xt if r["phase"] == ph and r["backend"] == b]
            kap[f"{ph}_{b}"] = f"{sum(x==y for x,y in sub)}/{len(sub)} agree; claude+={sum(x for x,_ in sub)} grok+={sum(y for _,y in sub)}"
    emb = ld("anthropic_para_xj", "residue_embedding")
    for b in ("governed", "naive"):
        n, s, _ = pairs(xt, emb, b, "post", "semantic_xjudge"); kap[f"post_{b}_grok_pair"] = f(s, n)
# xjudge spend
xs = 0.0; fp = os.path.join(R, "anthropic_para_xjudge_xai_calls.jsonl")
if os.path.exists(fp): xs = sum((json.loads(l).get("usd_ticks") or 0) for l in open(fp) if json.loads(l).get("cached") is False) / 1e10
spend = json.load(open(os.path.join(R, "spend.json")))
# legacy (pre-expansion) single-seed semantic residue
leg = []
for fn in ("real_semantic_residue_anthropic.csv", "real_semantic_residue_codex.csv", "real_semantic_residue_xai.csv"):
    p = os.path.join(H, "results", fn)
    if not os.path.exists(p): continue
    L = list(csv.DictReader(open(p)))
    for b in ("governed", "naive"):
        s = [r for r in L if r["backend"] == b]
        leg.append(dict(file=fn, backend=b, n=len(s), verbatim=sum(int(r["verbatim_hit"]) for r in s), semantic=sum(int(r["semantic_hit"]) for r in s),
                        summary_row_left=sum(int(r["summary_row_left"]) for r in s)))
cols = list(dict.fromkeys(k for r in rows for k in r))
with open(os.path.join(R, "headline.csv"), "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols); w.writeheader(); w.writerows(rows)
with open(os.path.join(R, "legacy_semantic_residue_summary.csv"), "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(leg[0])); w.writeheader(); w.writerows(leg)
json.dump(dict(kappa=kap, xjudge_usd=round(xs, 4)), open(os.path.join(R, "interjudge.json"), "w"), indent=2)
M = ["# EQ9 expansion - headline tables (generated by make_eq9x_headline.py; counts are k/n [Wilson 95% CI])\n",
     "Unit for text/embedding/reconstruction = (seed, victim, attribute) pair; 2 victims per seed (one per tenant).",
     "Leak unit = adversarial cross-principal retrieval result. 'judge' = same-provider LLM judge; 'string' = value/format regex.\n",
     "## A. Isolation + erasure (all cells)\n", "| cell | status | seeds | keys | governed leak | naive leak | canary rows left (gov/naive) |", "|---|---|---|---|---|---|---|"]
for d in rows:
    if d.get("status") == "MISSING": M.append(f"| {d['cell']} | MISSING | | | | | |"); continue
    M.append(f"| {d['cell']} | {d['status']} | {d['seeds']} | {d['keys']} | {d['gov_leak']} | {d['nai_leak']} | {d['gov_canary_rows_left']}/{d['nai_canary_rows_left']} |")
M += ["\n## B. Semantic residue after forget_user\n",
      "| cell | pre: derived text conveys fact (judge, gov) | pre: string match (gov) | post text judge gov | post text judge naive | post string naive | post embedding gov | post embedding naive | post recon peer gov | post recon peer naive | post recon raw_store naive | post recon no-context gov |",
      "|---|---|---|---|---|---|---|---|---|---|---|---|"]
for d in rows:
    if d.get("status") == "MISSING": continue
    M.append(f"| {d['cell']} | {d['gov_pre_text_judge']} | {d['gov_pre_text_string']} | {d['gov_post_text_judge']} | {d['nai_post_text_judge']} | {d['nai_post_text_string']} | {d['gov_post_emb']} | {d['nai_post_emb']} | {d['gov_post_recon_peer']} | {d['nai_post_recon_peer']} | {d['nai_post_recon_raw_store']} | {d['gov_post_recon_nocontext']} |")
M += ["\n## C. Inter-judge agreement (anthropic_para cell re-judged by Grok grok-4.7; row x key level)\n", "```", json.dumps(kap, indent=1), "```",
      f"Cross-judge spend: ${xs:.4f} (xAI cost ticks).\n", "## D. Spend (uncached calls, all runs incl. resumed)\n", "| tag | uncached calls | in/out tokens | USD | basis |", "|---|---|---|---|---|"]
for s in spend: M.append(f"| {s['tag']} | {s['uncached_calls']} | {s['in_tok']}/{s['out_tok']} | {s['usd']} | {s['basis']} |")
M.append(f"| anthropic_para_xj (Grok judge) | - | - | {xs:.4f} | xAI cost_in_usd_ticks |")
M.append(f"\nTotal metered USD (Anthropic list-price estimate + xAI exact): ${sum(s['usd'] for s in spend) + xs:.2f}; Codex is plan-billed ($0 marginal).\n")
M += ["## E. Legacy single-run semantic residue (results/real_semantic_residue_*.csv, pre-expansion)\n", "| file | backend | probes | verbatim hits | semantic hits | summary rows left |", "|---|---|---|---|---|---|"]
for l in leg: M.append(f"| {l['file']} | {l['backend']} | {l['n']} | {l['verbatim']} | {l['semantic']} | {l['summary_row_left']} |")
open(os.path.join(R, "headline.md"), "w").write("\n".join(M) + "\n"); print("\n".join(M))
