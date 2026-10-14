"""Aggregates results/sbx_*_<backend>.csv (bwrap and/or docker) into summaries and fig_sbx_*.png."""
import os, csv, math, glob, statistics as st, collections as C
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
BACKENDS = [b for b in ("bwrap", "docker") if os.path.exists(os.path.join(R, f"sbx_crash_resume_{b}.csv"))]
ld = lambda f, b: list(csv.DictReader(open(os.path.join(R, f"sbx_{f}_{b}.csv"))))
def mean(xs):
    v = [x for x in xs if not math.isnan(x)]; return st.mean(v) if v else float("nan")
ORDER = ["none", "stateless", "snapshot", "tracked_snapshot", "commit_snapshot", "tracked_commit"]
LBL = {"none": "no persistence", "stateless": "(a) stateless+ckpt", "snapshot": "(b) naive tar snapshot",
       "tracked_snapshot": "(c) tracked tar snapshot", "commit_snapshot": "(b') naive docker commit", "tracked_commit": "(c') tracked docker commit"}
S, POOL, ESC = [], [], []
for b in BACKENDS:
    Cr, Ov, D, T, P, E = (ld(x, b) for x in ("crash_resume", "overhead", "deletion", "ttl", "pooling", "escape"))
    for m in [m for m in ORDER if any(r["mode"] == m for r in Cr)]:
        c = [r for r in Cr if r["mode"] == m]; o = [r for r in Ov if r["mode"] == m]; d = [r for r in D if r["mode"] == m]; t = [r for r in T if r["mode"] == m]
        crashed = [r for r in c if int(r["resumed_from"]) < int(r["plan_len"])]     # kill actually interrupted the run
        comp = [r for r in crashed if r["completed"] == "1"]
        S.append(dict(backend=b, mode=m, runs=len(c), kills_landed=len(crashed),
            resume_completed=mean([float(r["completed"]) for r in crashed]), state_fidelity=mean([float(r["state_match"]) for r in crashed]),
            redone_steps_mean=mean([float(r["redone_steps"]) for r in comp]), runs_with_duplicate_effects=mean([float(int(r["redone_steps"]) > 0) for r in comp]),
            torn_state_failures=sum("JSONDecodeError" in r["error"] for r in c),
            cold_start_ms=mean([float(r["sandbox_start_ms"]) for r in o]), snapshot_ms=mean([float(r["snapshot_ms"]) for r in o]),
            snapshot_kb=mean([float(r["snapshot_bytes"]) / 1024 for r in o]), resume_latency_ms=mean([float(r["resume_latency_ms"]) for r in o]),
            del_snapshots=mean([float(r["snapshots_with_data"]) for r in d]), del_image_layers=mean([float(r.get("image_layers_with_data", 0)) for r in d]),
            del_logs=mean([float(r["sandbox_logs_with_data"]) for r in d]), del_workdir=mean([float(r["leftover_workdir_files"]) for r in d]),
            del_db_records=mean([float(r["db_records_left"]) for r in d]), del_db_raw=mean([float(r["db_raw_bytes_hit"]) for r in d]),
            artifacts_before_ttl=sum(int(r["artifacts_before_ttl"]) for r in t), artifacts_after_ttl=sum(int(r["artifacts_after_ttl"]) for r in t)))
    for p in ("fresh", "pooled", "pooled_scrubbed"):
        x = [r for r in P if r["policy"] == p]
        POOL.append(dict(backend=b, policy=p, tasks=len(x), leak_rate=mean([float(r["leaked"]) for r in x]),
                         cross_tenant_rate=mean([float(r["cross_tenant"]) for r in x]), start_ms=mean([float(r["start_ms"]) for r in x])))
    agg = C.defaultdict(lambda: [0, 0])
    for r in E: agg[(r["config"], r["attack"])][0] += int(r["succeeded"]); agg[(r["config"], r["attack"])][1] += 1
    for (cfg, a), (s, n) in agg.items(): ESC.append(dict(backend=b, config=cfg, attack=a, successes=s, trials=n))
def dump(name, rows):
    with open(os.path.join(R, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
dump("sbx_summary.csv", S); dump("sbx_pooling_summary.csv", POOL); dump("sbx_escape_summary.csv", ESC)
for r in S: print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})
for r in POOL: print(r)

col = {"bwrap": "#1f77b4", "docker": "#ff7f0e"}
def grouped(ax, key, title, ylabel=""):
    modes = [m for m in ORDER if any(s["mode"] == m for s in S)]
    for i, b in enumerate(BACKENDS):
        vals = [next((s[key] for s in S if s["backend"] == b and s["mode"] == m), float("nan")) for m in modes]
        ax.bar([j + i * 0.4 for j in range(len(modes))], [0 if math.isnan(v) else v for v in vals], 0.4, color=col[b], label=b)
    ax.set_xticks([j + 0.2 for j in range(len(modes))]); ax.set_xticklabels([LBL[m] for m in modes], rotation=25, ha="right", fontsize=7)
    ax.set_title(title, fontsize=9); ax.set_ylabel(ylabel, fontsize=8)
fig, axs = plt.subplots(1, 3, figsize=(13, 3.8))
grouped(axs[0], "state_fidelity", "final-state fidelity (interrupted runs)"); grouped(axs[1], "redone_steps_mean", "steps re-executed per crash")
grouped(axs[2], "torn_state_failures", "torn-state resume failures (count)"); axs[0].legend(fontsize=8)
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_crash_resume.png"), dpi=160)
fig, axs = plt.subplots(1, 3, figsize=(13, 3.8))
grouped(axs[0], "del_snapshots", "tar snapshots w/ deleted user's data", "per forgotten user")
grouped(axs[1], "del_image_layers", "committed images w/ deleted user's data", "per forgotten user")
grouped(axs[2], "del_logs", "host sandbox logs w/ deleted user's data", "per forgotten user"); axs[0].legend(fontsize=8)
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_deletion.png"), dpi=160)
fig, ax = plt.subplots(figsize=(8, 3.6))
modes = [m for m in ORDER if any(s["mode"] == m for s in S)]
for i, b in enumerate(BACKENDS):
    for j, m in enumerate(modes):
        s = next((s for s in S if s["backend"] == b and s["mode"] == m), None)
        if not s: continue
        x = j + i * 0.4
        ax.bar(x, s["artifacts_before_ttl"], 0.38, color="#cccccc"); ax.bar(x, s["artifacts_after_ttl"], 0.38, color=col[b])
ax.set_xticks([j + 0.2 for j in range(len(modes))]); ax.set_xticklabels([LBL[m] for m in modes], rotation=25, ha="right", fontsize=7)
ax.set_title("Snapshot/log/image artifacts: before (grey) vs. after TTL purge (colour)", fontsize=9)
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_ttl.png"), dpi=160)
fig, ax = plt.subplots(figsize=(7, 3.6)); pols = ["fresh", "pooled", "pooled_scrubbed"]
for i, b in enumerate(BACKENDS):
    ax.bar([j + i * 0.4 for j in range(3)], [next(p["leak_rate"] for p in POOL if p["backend"] == b and p["policy"] == q) for q in pols], 0.4, color=col[b], label=b)
ax.set_xticks([j + 0.2 for j in range(3)]); ax.set_xticklabels(pols); ax.set_ylabel("tasks seeing another user's data"); ax.legend(); ax.set_title("Sandbox reuse leakage")
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_pooling.png"), dpi=160)
attacks = list(dict.fromkeys(e["attack"] for e in ESC))
fig, ax = plt.subplots(figsize=(9, 5)); series = [(b, "sandbox_scoped") for b in BACKENDS] + [(BACKENDS[0], "plain_unscoped")]
cc = [col[b] for b in BACKENDS] + ["#d62728"]; h = 0.8 / len(series)
for i, (b, cfg) in enumerate(series):
    vals = [next((e["successes"] / e["trials"] for e in ESC if e["backend"] == b and e["config"] == cfg and e["attack"] == a), float("nan")) for a in attacks]
    ax.barh([j + i * h for j in range(len(attacks))], [0 if math.isnan(v) else v for v in vals], h, color=cc[i],
            label=f"{b} sandbox + scoped token" if cfg == "sandbox_scoped" else "plain subprocess + unscoped gateway")
ax.set_yticks([j + 0.3 for j in range(len(attacks))]); ax.set_yticklabels(attacks, fontsize=7); ax.set_xlabel("success rate"); ax.legend(fontsize=7)
ax.set_title("Escape / exfiltration attempts"); fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_escape.png"), dpi=160)
fig, axs = plt.subplots(1, 3, figsize=(13, 3.6))
for i, b in enumerate(BACKENDS):
    cold = mean([s["cold_start_ms"] for s in S if s["backend"] == b]); warm = next(p["start_ms"] for p in POOL if p["backend"] == b and p["policy"] == "pooled")
    axs[0].bar([i * 0.4, 1 + i * 0.4], [cold, warm], 0.4, color=col[b], label=b)
axs[0].set_xticks([0.2, 1.2]); axs[0].set_xticklabels(["cold start", "warm (pooled) start"]); axs[0].set_ylabel("ms to READY"); axs[0].legend()
grouped(axs[1], "snapshot_kb", "snapshot size (KB)"); axs[1].set_yscale("symlog")
grouped(axs[2], "resume_latency_ms", "resume latency (ms)")
fig.tight_layout(); fig.savefig(os.path.join(R, "fig_sbx_overhead.png"), dpi=160)
