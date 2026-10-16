"""Aggregate results/real_*.csv into summaries and fig_real_*.png (Claude vs Codex)."""
import os, csv, json, math, statistics as st
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
ld = lambda f: list(csv.DictReader(open(os.path.join(R, f)))) if os.path.exists(os.path.join(R, f)) else []
mean = lambda xs: st.mean(xs) if xs else float("nan")
PROV = ["anthropic", "codex"]; COL = {"anthropic": "#d97706", "codex": "#10a37f", "governed": "#1f77b4", "naive": "#d62728"}
LBL = {"anthropic": "Claude (Anthropic API)", "codex": "OpenAI (Codex CLI)"}

S = []
# invariants
inv = ld("real_invariants.csv")
for p in PROV:
    for b in ("governed", "naive"):
        rows = [r for r in inv if r.get("provider") == p and r["backend"] == b]
        if not rows: continue
        S.append(dict(provider=p, backend=b, leak_rate=mean([float(r["leak_rate"]) for r in rows]),
                      foreign_hits=mean([float(r["foreign_hits"]) for r in rows]),
                      post_forget_retrievable=mean([float(r["post_forget_retrievable"]) for r in rows])))
# task success
ts = ld("real_task_success.csv")
for p in PROV:
    for b in ("governed", "naive"):
        rows = [r for r in ts if r.get("provider") == p and r["backend"] == b]
        if not rows: continue
        S.append(dict(provider=p, backend=b, task_n=len(rows),
                      task_accuracy=mean([float(r["correct"]) for r in rows]),
                      cross_user_fact_rate=mean([float(r["cross_user_fact"]) for r in rows])))
# semantic residue
sr = ld("real_semantic_residue.csv")
for p in PROV:
    for b in ("governed", "naive"):
        rows = [r for r in sr if r.get("provider") == p and r["backend"] == b]
        if not rows: continue
        S.append(dict(provider=p, backend=b, sem_n=len(rows),
                      verbatim_rate=mean([float(r["verbatim_hit"]) for r in rows]),
                      semantic_rate=mean([float(r["semantic_hit"]) for r in rows]),
                      summary_left_rate=mean([float(r["summary_row_left"]) for r in rows])))
# attacks
atk = ld("real_attacks_bwrap.csv") + ld("real_attacks_docker.csv")
for p in PROV:
    rows = [r for r in atk if r.get("provider") == p]
    if not rows: continue
    S.append(dict(provider=p, backend="sandbox", n_attacks=len(rows),
                  attack_success_rate=mean([float(r["success"]) for r in rows]),
                  attack_canary_rate=mean([float(r["leaked_canary"]) for r in rows]),
                  attack_fact_rate=mean([float(r["leaked_fact"]) for r in rows]),
                  mean_denials=mean([float(r["denials"]) for r in rows])))

# Flatten into a tidy summary table (one row per provider×metric family)
tidy = []
for p in PROV:
    g = next((x for x in S if x.get("provider")==p and x.get("backend")=="governed" and "leak_rate" in x), {})
    n = next((x for x in S if x.get("provider")==p and x.get("backend")=="naive" and "leak_rate" in x), {})
    gt = next((x for x in S if x.get("provider")==p and x.get("backend")=="governed" and "task_accuracy" in x), {})
    nt = next((x for x in S if x.get("provider")==p and x.get("backend")=="naive" and "task_accuracy" in x), {})
    gs = next((x for x in S if x.get("provider")==p and x.get("backend")=="governed" and "semantic_rate" in x), {})
    ns = next((x for x in S if x.get("provider")==p and x.get("backend")=="naive" and "semantic_rate" in x), {})
    a = next((x for x in S if x.get("provider")==p and x.get("backend")=="sandbox"), {})
    if not any((g, gt, gs, a)): continue
    tidy.append(dict(
        provider=p, label=LBL[p],
        gov_leak=g.get("leak_rate"), naive_leak=n.get("leak_rate"),
        gov_task_acc=gt.get("task_accuracy"), naive_task_acc=nt.get("task_accuracy"),
        gov_cross_fact=gt.get("cross_user_fact_rate"), naive_cross_fact=nt.get("cross_user_fact_rate"),
        gov_verbatim=gs.get("verbatim_rate"), naive_verbatim=ns.get("verbatim_rate"),
        gov_semantic=gs.get("semantic_rate"), naive_semantic=ns.get("semantic_rate"),
        attack_success=a.get("attack_success_rate"), n_attacks=a.get("n_attacks"),
        attack_denials=a.get("mean_denials")))
with open(os.path.join(R, "real_summary.csv"), "w", newline="") as f:
    if tidy:
        w = csv.DictWriter(f, fieldnames=list(tidy[0])); w.writeheader(); w.writerows(tidy)
for r in tidy: print(r)

# Charts
if not tidy: raise SystemExit("no data")
fig, axs = plt.subplots(1, 3, figsize=(12, 3.6))
# task accuracy
ax = axs[0]; x = range(len(tidy))
ax.bar([i - 0.2 for i in x], [r["gov_task_acc"] or 0 for r in tidy], 0.4, color=COL["governed"], label="governed")
ax.bar([i + 0.2 for i in x], [r["naive_task_acc"] or 0 for r in tidy], 0.4, color=COL["naive"], label="naive")
ax.set_xticks(list(x)); ax.set_xticklabels([r["label"] for r in tidy], fontsize=8)
ax.set_ylim(0, 1.05); ax.set_ylabel("accuracy"); ax.set_title("Task success (fact QA)"); ax.legend(fontsize=7)
# semantic residue
ax = axs[1]
ax.bar([i - 0.2 for i in x], [r["gov_semantic"] or 0 for r in tidy], 0.4, color=COL["governed"], label="governed")
ax.bar([i + 0.2 for i in x], [r["naive_semantic"] or 0 for r in tidy], 0.4, color=COL["naive"], label="naive")
ax.set_xticks(list(x)); ax.set_xticklabels([r["label"] for r in tidy], fontsize=8)
ax.set_ylim(0, 1.05); ax.set_ylabel("LLM-judge hit rate"); ax.set_title("Semantic residue after forget_user"); ax.legend(fontsize=7)
# attack success
ax = axs[2]
ax.bar(list(x), [r["attack_success"] or 0 for r in tidy], 0.5, color=[COL[r["provider"]] for r in tidy])
for i, r in enumerate(tidy):
    ax.text(i, (r["attack_success"] or 0) + 0.02, f"n={r['n_attacks']}", ha="center", fontsize=8)
ax.set_xticks(list(x)); ax.set_xticklabels([r["label"] for r in tidy], fontsize=8)
ax.set_ylim(0, 1.05); ax.set_ylabel("exfil success rate"); ax.set_title("Model-generated attacks (scoped sandbox)")
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_real_llm.png"), dpi=160)

# leakage + cross-fact
fig, axs = plt.subplots(1, 2, figsize=(9, 3.4))
ax = axs[0]
ax.bar([i - 0.2 for i in x], [r["gov_leak"] or 0 for r in tidy], 0.4, color=COL["governed"], label="governed")
ax.bar([i + 0.2 for i in x], [r["naive_leak"] or 0 for r in tidy], 0.4, color=COL["naive"], label="naive")
ax.set_xticks(list(x)); ax.set_xticklabels([r["label"] for r in tidy], fontsize=8)
ax.set_ylabel("leak rate"); ax.set_title("Cross-user leakage (real summarizer)"); ax.legend(fontsize=7)
ax = axs[1]
ax.bar([i - 0.2 for i in x], [r["gov_cross_fact"] or 0 for r in tidy], 0.4, color=COL["governed"], label="governed")
ax.bar([i + 0.2 for i in x], [r["naive_cross_fact"] or 0 for r in tidy], 0.4, color=COL["naive"], label="naive")
ax.set_xticks(list(x)); ax.set_xticklabels([r["label"] for r in tidy], fontsize=8)
ax.set_ylabel("rate"); ax.set_title("Answers containing another user's gold fact"); ax.legend(fontsize=7)
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_real_leak_task.png"), dpi=160)
print("charts written")
cost = json.load(open(os.path.join(R, "real_llm_cost.json"))) if os.path.exists(os.path.join(R, "real_llm_cost.json")) else {}
print("cost", cost)
