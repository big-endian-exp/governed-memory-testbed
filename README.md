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
