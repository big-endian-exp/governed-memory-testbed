"""Aggregates results/*.csv into summary.csv and PNG charts."""
import os, csv, json, statistics as st
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
ld = lambda f: list(csv.DictReader(open(os.path.join(R, f))))
fl = lambda rows, k: [float(r[k]) for r in rows]
by = lambda rows, k, v: [r for r in rows if r[k] == v]
S = []
def add(metric, g, n, note=""): S.append(dict(metric=metric, governed=g, naive_baseline=n, note=note))

L = ld("leakage.csv")
add("cross-user leak rate (adversarial queries)", st.mean(fl(by(L,"backend","governed"),"leak_rate")), st.mean(fl(by(L,"backend","naive"),"leak_rate")),
    f"{sum(fl(by(L,'backend','governed'),'results')):.0f} results/backend")
add("cross-tenant leaked results", sum(fl(by(L,"backend","governed"),"cross_tenant")), sum(fl(by(L,"backend","naive"),"cross_tenant")))
V = ld("visibility.csv")
for b in ("governed", "naive"):
    pass
vb = {b: {k: sum(fl(by(V,"backend",b),k)) for k in ("tp","fp","fn","tn")} for b in ("governed","naive")}
pr = lambda d: d["tp"]/max(d["tp"]+d["fp"],1); rc = lambda d: d["tp"]/max(d["tp"]+d["fn"],1); ox = lambda d: d["fp"]/max(d["fp"]+d["tn"],1)
add("visibility precision vs policy", pr(vb["governed"]), pr(vb["naive"])); add("visibility recall vs policy", rc(vb["governed"]), rc(vb["naive"]))
add("over-exposure (FP / should-be-hidden)", ox(vb["governed"]), ox(vb["naive"]))
D = ld("deletion.csv")
for k in ("derived_residue", "retrievable_by_others", "raw_file_bytes_hit", "orphan_embeddings"):
    add(f"deletion residue: {k} (mean per forgotten user)", st.mean(fl(by(D,"backend","governed"),k)), st.mean(fl(by(D,"backend","naive"),k)),
        f"governed w/o secure_delete: {st.mean(fl(by(D,'backend','governed_no_secure_delete'),k)):.2f}")
C = ld("crash_resume.csv")
add("crash-resume state fidelity", st.mean(fl(by(C,"backend","governed"),"state_match")), st.mean(fl(by(C,"backend","naive"),"state_match")))
add("redone steps per crash (mean)", st.mean(fl(by(C,"backend","governed"),"redone_steps")), st.mean(fl(by(C,"backend","naive"),"redone_steps")),
    f"{len(by(C,'backend','governed'))} crashes/backend")
Sx = ld("staleness.csv")
for k in ("stale_fact_rate", "stale_search_rate", "expired_visible_rate"):
    add(k, st.mean(fl(by(Sx,"backend","governed"),k)), st.mean(fl(by(Sx,"backend","naive"),k)))
A = ld("audit.csv")
add("audit coverage (audit rows / API calls)", st.mean(fl(A,"coverage")), "n/a (no audit)", f"{sum(fl(A,'api_calls')):.0f} calls")
add("audit tamper detected", st.mean(fl(A,"tamper_detected")), "n/a")
O = ld("policy_denials.csv"); add("disallowed writes denied", f"{sum(fl(O,'denied')):.0f}/{sum(fl(O,'attempted')):.0f}", "0 (no ACL)")
B = ld("overhead.csv")
for n in sorted({r["n_records"] for r in B}, key=int):
    g = [r for r in B if r["n_records"] == n and r["backend"] == "governed"][0]; b = [r for r in B if r["n_records"] == n and r["backend"] == "naive"][0]
    for k in ("write_p50_ms", "search_p50_ms", "forget_user_ms", "bytes_per_record"):
        add(f"{k} @N={n}", float(g[k]), float(b[k]))
with open(os.path.join(R, "summary.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(S[0])); w.writeheader(); w.writerows(S)

col = {"governed": "#1f77b4", "naive": "#d62728", "governed_no_secure_delete": "#9467bd"}
# 1 safety bars
fig, ax = plt.subplots(figsize=(8, 4))
labels = ["leak rate", "over-exposure", "stale facts", "expired visible", "redo / crash\n(norm.)"]
nr = max(st.mean(fl(by(C,"backend","naive"),"redone_steps")), 1e-9)
vals = {b: [st.mean(fl(by(L,"backend",b),"leak_rate")), ox(vb[b]), st.mean(fl(by(Sx,"backend",b),"stale_fact_rate")),
            st.mean(fl(by(Sx,"backend",b),"expired_visible_rate")), st.mean(fl(by(C,"backend",b),"redone_steps"))/nr] for b in ("governed","naive")}
x = range(len(labels))
for i, b in enumerate(("governed", "naive")):
    ax.bar([j + i*0.38 for j in x], vals[b], 0.38, label=b, color=col[b])
ax.set_xticks([j + 0.19 for j in x]); ax.set_xticklabels(labels); ax.set_ylabel("rate (lower is better)"); ax.legend(); ax.set_title("Governance failure rates")
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_governance_rates.png"), dpi=160)
# 2 deletion residue
fig, ax = plt.subplots(figsize=(8, 4)); ks = ["derived_residue", "retrievable_by_others", "raw_file_bytes_hit"]
bs = ["governed", "governed_no_secure_delete", "naive"]
for i, b in enumerate(bs):
    ax.bar([j + i*0.27 for j in range(3)], [st.mean(fl(by(D,"backend",b),k)) for k in ks], 0.27, label=b, color=col[b])
ax.set_xticks([j + 0.27 for j in range(3)]); ax.set_xticklabels(["derived summaries\nwith user data", "results retrievable\nby other users", "raw DB bytes\ncontain data (0/1)"])
ax.set_ylabel("mean per forgotten user"); ax.legend(); ax.set_title("Residue after right-to-delete")
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_deletion_residue.png"), dpi=160)
# 3 latency
fig, axs = plt.subplots(1, 3, figsize=(11, 3.4))
for ax, k, t in zip(axs, ("write_p50_ms", "search_p50_ms", "forget_user_ms"), ("write p50", "search p50 (k=5)", "forget_user")):
    for b in ("governed", "naive"):
        rr = sorted(by(B,"backend",b), key=lambda r: int(r["n_records"]))
        ax.plot([int(r["n_records"]) for r in rr], [float(r[k]) for r in rr], "o-", label=b, color=col[b])
    ax.set_title(t); ax.set_xlabel("records in store"); ax.set_ylabel("ms")
axs[0].legend(); fig.tight_layout(); fig.savefig(os.path.join(R, "fig_latency.png"), dpi=160)
# 4 visibility confusion
fig, ax = plt.subplots(figsize=(7, 3.6)); hs = ["react", "autopilot", "rag"]
for i, b in enumerate(("governed", "naive")):
    ax.bar([j + i*0.38 for j in range(3)], [sum(fl([r for r in V if r["backend"]==b and r["harness"]==h],"fp")) for h in hs], 0.38, label=f"{b}: FP (exposed, should be hidden)", color=col[b])
    ax.bar([j + i*0.38 for j in range(3)], [-sum(fl([r for r in V if r["backend"]==b and r["harness"]==h],"fn")) for h in hs], 0.38, color=col[b], alpha=.4)
ax.axhline(0, color="k", lw=.6); ax.set_xticks([j + 0.19 for j in range(3)]); ax.set_xticklabels(hs); ax.set_ylabel("records (FP up / FN down)")
ax.set_title("Cross-harness visibility vs. policy (all seeds)"); ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(os.path.join(R, "fig_visibility.png"), dpi=160)
# 5 crash
fig, ax = plt.subplots(figsize=(6, 3.6))
for b in ("governed", "naive"):
    rr = by(C,"backend",b); ax.scatter(fl(rr,"crash_at"), fl(rr,"redone_steps"), label=b, color=col[b], alpha=.6, s=14)
ax.set_xlabel("crash step"); ax.set_ylabel("steps re-executed (dup. side effects)"); ax.legend(); ax.set_title("Autopilot crash/resume")
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_crash_resume.png"), dpi=160)
for r in S: print(r)
