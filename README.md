# Governed Memory Testbed (CAIN 2027 Industry Track)

Reproducible testbed for "Governed memory for long-running agents". Three agent harnesses share one governed
memory service. The service is compared against an ungoverned baseline that represents a typical prototype agent
memory: a single global vector store with no ACL, lineage, TTL, or audit, and delete-by-user-column only.

## Architecture
```
 ReActAgent ─┐                       ┌─ SQLite: memory (namespaced records + provenance + versions + TTL)
 Autopilot  ─┼─► GovernedMemory API ─┼─ lineage(child,parent)   → cascading delete of derived summaries
 RAGAssistant┘   (policy check →     ├─ embedding (hashed TF-IDF, 4096-d) + in-proc vector index
                  op → audit, 1 txn) └─ audit (hash-chained, append-only via triggers)
```
- **Namespaces:** `(tenant, user)` for user scope, `(tenant, "*")` for tenant-shared records. Every read is pre-filtered by namespace in SQL before any vector scoring.
- **Memory types:** `episodic`, `semantic` (with versioned keyed facts that supersede older versions), `procedural`, `working` (autopilot checkpoints).
- **Policy (`govmem/store.py: DEFAULT_ACL`):**

| harness | read | write | scopes | max read sensitivity |
|---|---|---|---|---|
| react | episodic, semantic, procedural | episodic, semantic | user, tenant | confidential |
| autopilot | working, procedural, semantic | working, episodic, procedural | user | restricted |
| rag | semantic, episodic | semantic | user, tenant | internal |

- **Retention:** per-record TTL. Expired records are filtered at read time and removed by `purge_expired()`.
- **Right-to-delete:** `forget_user()` deletes the user's records, then follows lineage transitively to delete derived summaries in any namespace (including tenant digests) and superseded versions. Embeddings and the vector cache are removed in the same transaction. SQLite `secure_delete=ON` zeroes freed pages.
- **Provenance:** every record stores the source harness, source ref, SHA-256 of the source content, timestamp, and version.
- **Audit:** every API call, including denials, appends one row to a SHA-256 hash chain. Triggers block UPDATE/DELETE, and `verify_audit()` detects tampering. Audit rows hold IDs and types, never content, so the audit log itself carries no deletion residue.
- **LLM:** `govmem/llm.py` provides a pluggable backend. With no `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`, it uses a deterministic `MockLLM` (scripted ReAct tool choice, extractive summarizer). Set `GOVMEM_LLM=openai|anthropic` and a key to use a real model.
- **Harnesses (`govmem/harnesses.py`):** ReAct tool loop (retrieves → acts → writes an episode); Autopilot (multi-step plan, checkpoint per step, resumes from the latest checkpoint after a crash); RAG (ingests docs, summarizes them into derived memory with lineage, answers grounded queries).

## Run
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run_experiments.py --seeds 5   # ~1 min; writes results/*.csv
.venv/bin/python make_charts.py                 # results/summary.csv + fig_*.png
```
Workload: 2 tenants × 4 users = 8 users. Per user: 5 docs with mixed sensitivity, a summary, ReAct episodes, a TTL'd scratch note, a runbook, a fact updated 3× by 2 harnesses, and an autopilot run. There is also one tenant-wide digest derived from several users. Each user's data carries a unique canary token for leakage and residue detection. Five seeds.

## Results (5 seeds, mock LLM): see `results/summary.csv`
| metric | governed | naive |
|---|---|---|
| cross-user leak rate (18,000 retrieved results with adversarial queries) | **0.000** | 0.869 |
| cross-tenant leaked results | 0 | 8,808 |
| visibility precision / recall vs policy | 1.00 / 1.00 | 0.09 / 1.00 |
| derived summaries still holding a forgotten user's data | 0 | 2.0 per user |
| forgotten-user results retrievable by others | 0 | 12.0 per user |
| orphan embeddings after delete | 0 | 272 per run (mean) |
| raw DB file still contains deleted text | 0 (1 if secure_delete OFF) | 1 |
| crash-resume state fidelity / redone steps per crash | 1.00 / 0 | 1.00 / 6.46 |
| stale fact reads / stale search / expired still visible | 0 / 0 / 0 | 1.0 / 1.0 / 1.0 |
| audit coverage (rows/API calls), tamper detected | 1.00 (8,659 calls), yes | none |
| disallowed writes denied | 30/30 | 0 |
| write p50 @10k | 0.46 ms | 1.07 ms |
| search p50 @10k | 8.4 ms | 34.4 ms |
| forget_user @10k | 121 ms | 12 ms |
| storage per record | 17.5 KB (+2.2%) | 17.1 KB |

Charts: `results/fig_governance_rates.png`, `fig_deletion_residue.png`, `fig_visibility.png`, `fig_crash_resume.png`, `fig_latency.png`.
The evaluation section draft is in `results/evaluation_draft.md`.

## Caveats
The mock LLM and synthetic data are described in the evaluation draft's limitations. The naive write path is slower only because it autocommits twice per record while the governed path uses one transaction. Treat the write-latency comparison as "no measurable governance penalty", not as a speedup.

## Sandbox experiment: governance across the sandbox boundary
**Runtime.** This host has no Docker or podman, so each task runs in a short-lived **bubblewrap** sandbox (`govmem/sandbox.py`) with:
- no network (`--unshare-all`) and its own PID, IPC, UTS and user namespaces
- a cleared environment and read-only `/usr`
- a per-task `/work` directory and the gateway socket directory as its only writable mounts
- no access to the DB file

The in-sandbox agent (`govmem/sandbox_agent.py`, stdlib only) reaches memory only through the **gateway** (`govmem/gateway.py`). The gateway is JSON-lines RPC over a Unix socket. Each call carries an HMAC token scoped to (tenant, user, task, operations, expiry). Identity comes only from the verified token, never from request fields. Every denial is written to the hash-chained audit log, and the gateway records an external side-effect ledger per task.

**Persistence modes compared:**
- `none`: no persistence (reference).
- `stateless` (a): an ephemeral sandbox plus governed checkpoints written through the gateway.
- `snapshot` (b): the naive approach. The sandbox filesystem is tarballed after each run and restored on resume, and nothing tracks it.
- `tracked_snapshot` (c): the same tarballs plus host logs, registered with `register_artifact()` as derived records. They inherit TTL and are unlinked by the `forget_user` cascade. Mode (a) registers its host logs the same way.

**Run:** `.venv/bin/python run_sandbox_experiments.py --seeds 3 && .venv/bin/python make_sandbox_charts.py` (~2 min).

**Experiments:**
- 72 runs per mode, each killed with SIGKILL at a random step and then resumed in a fresh sandbox.
- `forget_user` on 2 users, followed by a residue scan of snapshots (contents included), host logs, workdirs, the DB and raw DB bytes, plus a TTL purge.
- Pooled vs. fresh vs. pooled-and-scrubbed sandboxes, 72 tasks each, with a probe that greps the working directory for other users' markers.
- 12 escape and exfiltration attacks × 24 trials, comparing the sandbox with a scoped gateway against a plain subprocess with a gateway that trusts caller-supplied identity.

**Results (3 seeds, mock LLM):** `results/sbx_summary.csv`, `sbx_pooling_summary.csv`, `sbx_escape_summary.csv`

| | none | (a) stateless+ckpt | (b) naive snapshot | (c) tracked snapshot |
|---|---|---|---|---|
| resume completed / final state correct | 100% / 100% | 100% / 100% | 87.5% / 87.5% | 90.3% / 90.3% |
| torn-state resume failures (of 72) | 0 | 0 | 9 | 7 |
| steps re-executed per crash (completed runs) | 7.49 | 0.26 | 0.27 | 0.22 |
| snapshot files with deleted user's data (per user) | 0 | 0 | 3.0 | 0 |
| host sandbox logs with deleted user's data (per user) | 0 | 0 | 5.33 | 0 |
| snapshot/log files left after TTL purge | 0/108 | 0/108 | 216/216 | 0/162 |
| sandbox start / snapshot / resume latency (ms) | 26.8 / – / 32.4 | 26.8 / – / 35.6 | 26.2 / 2.3 / 32.2 | 26.5 / 2.4 / 33.4 |
| mean snapshot size | – | – | 3.1 KB | 3.1 KB |

Other results:
- **Sandbox reuse:** tasks that saw another user's data: fresh 0%, pooled **91.7%** (83.3% cross-tenant), pooled+scrubbed 0%.
- **Escape attempts:** the scoped sandbox had 0 successes in 288 attempts (12 attack types × 24 trials). The plain, unscoped setup was breached in 240 of 288. Its two 0/24 results (forged token, other-task token) mean "not applicable" because that gateway ignores tokens. All 144 gateway-level denials were audited.

Charts: `results/fig_sbx_crash_resume.png`, `fig_sbx_deletion_ttl.png`, `fig_sbx_pooling.png`, `fig_sbx_escape.png`, `fig_sbx_overhead.png`.
