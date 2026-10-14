"""Aggregates results/sbx_*.csv into sbx_summary.csv and fig_sbx_*.png."""
import os, csv, math, statistics as st, collections as C
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
ld = lambda f: list(csv.DictReader(open(os.path.join(R, f))))
mean = lambda xs: (lambda v: st.mean(v) if v else float("nan"))([x for x in xs if not math.isnan(x)])
MODES = ["none", "stateless", "snapshot", "tracked_snapshot"]
LBL = {"none": "no persistence", "stateless": "(a) stateless+governed ckpt", "snapshot": "(b) naive FS snapshot", "tracked_snapshot": "(c) tracked snapshot"}
S = []
Cr, Ov, D, T, P, E, EA = (ld(f"sbx_{x}.csv") for x in ("crash_resume", "overhead", "deletion", "ttl", "pooling", "escape", "escape_audit"))
for m in MODES:
    c = [r for r in Cr if r["mode"] == m]; o = [r for r in Ov if r["mode"] == m]; d = [r for r in D if r["mode"] == m]; t = [r for r in T if r["mode"] == m]
    comp = [r for r in c if r["completed"] == "1"]
    S.append(dict(mode=m, runs=len(c), resume_completed=mean([float(r["completed"]) for r in c]),
        state_fidelity=mean([float(r["state_match"]) for r in c]),
        redone_steps_mean=mean([float(r["redone_steps"]) for r in comp]),
        runs_with_duplicate_effects=mean([float(int(r["redone_steps"]) > 0) for r in comp]),
        torn_state_failures=sum("JSONDecodeError" in r["error"] for r in c),
        sandbox_start_ms=mean([float(r["sandbox_start_ms"]) for r in o]),
        snapshot_ms=mean([float(r["snapshot_ms"]) for r in o]), snapshot_kb=mean([float(r["snapshot_bytes"]) / 1024 for r in o]),
        resume_latency_ms=mean([float(r["resume_latency_ms"]) for r in o]),
        del_snapshots_with_data=mean([float(r["snapshots_with_data"]) for r in d]),
        del_logs_with_data=mean([float(r["sandbox_logs_with_data"]) for r in d]),
        del_workdir_files=mean([float(r["leftover_workdir_files"]) for r in d]),
        del_db_records=mean([float(r["db_records_left"]) for r in d]), del_db_raw=mean([float(r["db_raw_bytes_hit"]) for r in d]),
        artifacts_before_ttl=sum(int(r["artifacts_before_ttl"]) for r in t), artifacts_after_ttl=sum(int(r["artifacts_after_ttl"]) for r in t)))
with open(os.path.join(R, "sbx_summary.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(S[0])); w.writeheader(); w.writerows(S)
pool = {p: dict(tasks=len(x), leak_rate=mean([float(r["leaked"]) for r in x]), cross_tenant_rate=mean([float(r["cross_tenant"]) for r in x]))
        for p in ("fresh", "pooled", "pooled_scrubbed") for x in [[r for r in P if r["policy"] == p]]}
with open(os.path.join(R, "sbx_pooling_summary.csv"), "w", newline="") as f:
    w = csv.writer(f); w.writerow(["policy", "tasks", "leak_rate", "cross_tenant_rate"])
    for p, v in pool.items(): w.writerow([p, v["tasks"], v["leak_rate"], v["cross_tenant_rate"]])
esc = C.defaultdict(lambda: [0, 0])
for r in E: esc[(r["config"], r["attack"])][0] += int(r["succeeded"]); esc[(r["config"], r["attack"])][1] += 1
attacks = list(dict.fromkeys(r["attack"] for r in E))
with open(os.path.join(R, "sbx_escape_summary.csv"), "w", newline="") as f:
    w = csv.writer(f); w.writerow(["attack", "sandbox_scoped_success", "plain_unscoped_success", "trials_each"])
    for a in attacks: w.writerow([a, esc[("sandbox_scoped", a)][0], esc[("plain_unscoped", a)][0], esc[("sandbox_scoped", a)][1]])
for s in S: print(s)
print(pool); print({k: v for k, v in esc.items()}); print(EA)

cols = ["#7f7f7f", "#1f77b4", "#d62728", "#2ca02c"]
# crash/resume
fig, axs = plt.subplots(1, 3, figsize=(12, 3.4))
for ax, k, tl in zip(axs, ("state_fidelity", "redone_steps_mean", "torn_state_failures"),
                     ("final-state fidelity", "steps re-executed per crash", "torn-state resume failures (count)")):
    ax.bar(range(4), [s[k] for s in S], color=cols); ax.set_xticks(range(4)); ax.set_xticklabels([LBL[m] for m in MODES], rotation=20, ha="right", fontsize=8); ax.set_title(tl)
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_crash_resume.png"), dpi=160)
# deletion + TTL
fig, axs = plt.subplots(1, 2, figsize=(11, 3.6))
ks = ["del_snapshots_with_data", "del_logs_with_data", "del_workdir_files", "del_db_records"]
for i, m in enumerate(MODES):
    s = S[i]; axs[0].bar([j + i * 0.2 for j in range(4)], [s[k] for k in ks], 0.2, color=cols[i], label=LBL[m])
axs[0].set_xticks([j + 0.3 for j in range(4)]); axs[0].set_xticklabels(["snapshots", "host sandbox logs", "leftover workdirs", "DB records"])
axs[0].set_ylabel("artifacts with deleted user's data\n(mean per forgotten user)"); axs[0].legend(fontsize=7); axs[0].set_title("Residue after forget_user")
axs[1].bar([j - 0.2 for j in range(4)], [s["artifacts_before_ttl"] for s in S], 0.4, color="#bbbbbb", label="before TTL")
axs[1].bar([j + 0.2 for j in range(4)], [s["artifacts_after_ttl"] for s in S], 0.4, color=cols, label="after TTL+purge")
axs[1].set_xticks(range(4)); axs[1].set_xticklabels([LBL[m] for m in MODES], rotation=20, ha="right", fontsize=8); axs[1].legend(); axs[1].set_title("Snapshot/log files vs. retention TTL")
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_deletion_ttl.png"), dpi=160)
# pooling
fig, ax = plt.subplots(figsize=(5.5, 3.4)); ps = list(pool)
ax.bar([j - 0.2 for j in range(3)], [pool[p]["leak_rate"] for p in ps], 0.4, label="any cross-user", color="#d62728")
ax.bar([j + 0.2 for j in range(3)], [pool[p]["cross_tenant_rate"] for p in ps], 0.4, label="cross-tenant", color="#9467bd")
ax.set_xticks(range(3)); ax.set_xticklabels(ps); ax.set_ylabel("fraction of tasks seeing foreign data"); ax.legend(); ax.set_title("Sandbox reuse leakage")
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_pooling.png"), dpi=160)
# escape
fig, ax = plt.subplots(figsize=(9, 3.8)); n = esc[("sandbox_scoped", attacks[0])][1]
ax.barh([j + 0.2 for j in range(len(attacks))], [esc[("plain_unscoped", a)][0] / n for a in attacks], 0.4, color="#d62728", label="plain subprocess + unscoped gateway")
ax.barh([j - 0.2 for j in range(len(attacks))], [esc[("sandbox_scoped", a)][0] / n for a in attacks], 0.4, color="#1f77b4", label="bwrap sandbox + scoped token")
ax.set_yticks(range(len(attacks))); ax.set_yticklabels(attacks, fontsize=8); ax.set_xlabel("attack success rate"); ax.legend(fontsize=8); ax.set_title("Escape / exfiltration attempts")
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_escape.png"), dpi=160)
# overhead
fig, ax = plt.subplots(figsize=(7, 3.4))
for j, k in enumerate(("sandbox_start_ms", "snapshot_ms", "resume_latency_ms")):
    ax.bar([i + j * 0.27 for i in range(4)], [s[k] for s in S], 0.27, label=k)
ax.set_xticks([i + 0.27 for i in range(4)]); ax.set_xticklabels([LBL[m] for m in MODES], rotation=15, ha="right", fontsize=8); ax.set_ylabel("ms"); ax.legend(fontsize=8); ax.set_title("Sandbox overhead")
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_overhead.png"), dpi=160)
