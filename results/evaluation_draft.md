# Evaluation (draft, generated from measured testbed numbers)

> All numbers come from `results/*.csv` (5 seeds, run 2026-10-13, deterministic mock LLM, synthetic data).
> Re-run `run_experiments.py` and `make_charts.py` after any change and update this text.

## Setup
We implemented a governed memory service (SQLite, a 4,096-dimensional hashed TF-IDF vector index, a hash-chained append-only audit log) and an **ungoverned baseline** that reflects common prototype practice: a single global vector store, no ACL, no lineage, no TTL, no audit, and deletion by the user column only. Three harnesses with distinct permissions share the service: a ReAct tool agent, a long-running checkpointing autopilot, and a RAG assistant. The synthetic enterprise workload has 2 tenants and 8 users. Each user has documents with mixed sensitivity, LLM-derived summaries, episodes, a TTL'd scratch note, a procedural runbook, a fact rewritten three times by two harnesses, and an autopilot run. A tenant-wide digest is derived from multiple users' documents. A unique canary token per user allows exact leakage and residue detection. Results are aggregated over 5 seeds.

## RQ1 – Isolation
We issued 18,000 retrieved results per backend from adversarial queries that embed another user's canary. The governed service leaked **0** results (0.0%). The baseline leaked 86.9%, and 8,808 of those results crossed tenant boundaries. Namespace filtering happens in the storage query before vector scoring, so isolation does not depend on retrieval ranking.

## RQ2 – Cross-harness sharing correctness
For every (harness, user, record) triple, we compared actual visibility with visibility derived from the policy specification (namespace, scope, type ACL, sensitivity ceiling, supersession, TTL). Governed: precision = recall = 1.00 for all three harnesses, with 0 false exposures and 0 false denials. Baseline: recall 1.00 but precision 0.09, and every record that policy should hide (100%) was visible. The governed service also rejected 30/30 disallowed writes, for example the RAG harness writing working state or the autopilot writing tenant-scope data. Every denial was audited.

## RQ3 – Right-to-delete completeness
After `forget_user` on one user per tenant, the governed service left **no** residue in primary records, derived summaries (including the cross-user tenant digest, removed through lineage cascade), embeddings, or retrieval results visible to other users. The baseline kept 2.0 derived summaries per forgotten user, still returned 12.0 results containing that user's data to other users, and left 272 orphan embeddings per run (mean). **Finding:** logical completeness is not physical completeness. With SQLite `secure_delete` disabled, the governed service was logically clean, yet the deleted text remained in free pages of the database file in 100% of cases. With `secure_delete=ON` it remained in 0%. Right-to-delete therefore has to cover storage-engine behaviour (free pages, WAL, backups), not just the data model.

## RQ4 – Crash/resume fidelity
We ran 200 autopilot runs per backend with a crash injected at a random step. Final state matched an uninterrupted golden run in 100% of cases for both backends because the plan is deterministic. The governed autopilot resumed from its last checkpoint and re-executed **0** steps. The baseline restarted and re-executed a mean of 6.46 steps per crash. Each of those steps would be a duplicated, possibly non-idempotent side effect in a real deployment.

## RQ5 – Staleness and conflicts across harnesses
A preference fact was updated three times by two harnesses. Versioned keyed facts with supersession gave 0% stale reads across all three harnesses and 0% stale or conflicting top-k search hits. The baseline returned the first-written value in 100% of fact reads and surfaced conflicting versions in 100% of searches. Expired (TTL) scratch memory stayed visible in 100% of baseline checks and 0% of governed checks.

## RQ6 – Auditability
Audit rows equalled API calls exactly (coverage 1.00 over 8,659 calls), and the hash chain verified on every seed. In-database UPDATE of the audit table was blocked by triggers. Tampering after dropping the trigger, simulating an attacker with file access, was detected by chain verification in 5/5 seeds. Audit records hold IDs and types only, so the audit log does not reintroduce deleted content.

## RQ7 – Overhead
| N records | write p50 gov/naive (ms) | search p50 gov/naive (ms) | forget_user gov/naive (ms) |
|---|---|---|---|
| 1,000 | 0.46 / 1.11 | 1.12 / 3.03 | 6.9 / 2.1 |
| 4,000 | 0.52 / 1.11 | 3.42 / 12.20 | 52.4 / 5.6 |
| 10,000 | 0.46 / 1.07 | 8.42 / 34.37 | 120.5 / 12.4 |

Governance added no measurable write penalty. The naive path is slower here only because it autocommits twice, so this is not a governance speedup. Search was 3–4× faster because namespace pre-filtering shrinks the candidate set. The real cost is deletion: lineage cascade plus secure_delete makes `forget_user` about 10× slower, though it still completes in about 0.12 s at 10k records. Storage overhead is +2.2% per record (17.5 KB vs 17.1 KB, dominated by the dense embedding blob in both).

## Limitations / threats to validity
- **Mock LLM.** No API key was available, so a deterministic scripted LLM drove all three harnesses (keyword tool selection, extractive summaries). Governance guarantees are enforced below the LLM and do not depend on model quality. However, we did not measure task success, prompt-injection-induced exfiltration attempts, or whether real abstractive summaries paraphrase deleted data in ways canary matching would miss. Real-LLM runs require `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, and the backend is already pluggable.
- **Synthetic data.** There are 8 users, 2 tenants, about 150 records per run, and canary-based detection. Real enterprise memories are larger and more heterogeneous, and residue can be semantic rather than verbatim.
- **Strawman risk.** The baseline is deliberately ungoverned. Several gaps, such as leakage, would close with a simple metadata filter, so the more informative comparisons are deletion through derived artifacts, staleness, and the secure_delete ablation.
- **Correctness-by-construction.** The visibility oracle is an independent re-implementation of the same policy spec. It validates the implementation, not the policy itself.
- **Scale and environment.** The work runs single-process on SQLite with an in-memory brute-force vector index and no concurrency, distributed stores, backups, or external vector DBs. Latency comes from one box and one run per size.
- **Crash model.** Crashes are injected only between steps. Torn writes mid-checkpoint and non-deterministic plans were not tested.

## RQ8 – Governance across the sandbox boundary
**Setup.** Each agent task ran in a short-lived bubblewrap sandbox: no network, private namespaces, cleared environment, read-only system, per-task working directory, and no DB access. Docker and podman were not available on the test host. Memory was reachable only through a Unix-socket gateway that checks a per-(tenant, user, task) HMAC token with an operation scope and expiry. We compared four persistence modes: no persistence; (a) a stateless sandbox with governed checkpoints written through the gateway; (b) a naive filesystem tarball persisted between runs; and (c) the same tarballs, plus host logs, tracked as derived artifacts with TTL and cascading deletion. Results come from 3 seeds and the deterministic mock LLM.

**Crash/resume.** 72 runs per mode were SIGKILLed at a random step and resumed in a fresh sandbox. Mode (a) resumed 100% of runs with correct final state. Snapshot modes (b) and (c) failed to resume in 9/72 and 7/72 runs, because the kill landed while the agent was rewriting its state file and the snapshot captured a truncated, torn file. Tracking snapshots in lineage did not fix this, since the failure is about atomicity, not governance. Transactional checkpoints in the governed store avoid it. Without persistence, every resume re-executed 7.49 steps on average. With any persistence the figure was 0.22–0.27, but 21–27% of completed runs still duplicated one external side effect, because a kill can land between the effect and the checkpoint. Checkpointing alone therefore gives at-least-once behaviour. Exactly-once needs idempotency keys on effects.

**Right-to-delete.** After `forget_user`, mode (b) still held the deleted user's data in 3.0 snapshot tarballs and 5.33 host sandbox logs per user. Modes (a) and (c) held none, and no mode left DB records or raw DB bytes. Retention showed the same pattern. After the TTL elapsed and a purge ran, all 216 untracked snapshot and log files in (b) remained, while (c) removed 162/162 and (a) removed 108/108. Sandbox artifacts are derived data, and they escape governance unless they are registered with the memory service.

**Sandbox reuse.** With warm sandboxes pooled without scrubbing, 91.7% of tasks could read another user's data from leftover files, and 83.3% read data from another tenant. Fresh sandboxes and scrubbed pooled sandboxes both gave 0%. Governed memory does not protect data that the agent puts on its own disk.

**Escape and prompt injection.** A malicious or prompt-injected agent made 12 kinds of attempt, 24 trials each. Examples include spoofing identity fields, forging or replaying tokens, out-of-scope operations, following a poisoned memory instruction to fetch another user's data, opening or reading the DB file, and reading host env, network, processes, or another task's workdir. Against the sandbox with a scoped gateway, all 288 attempts failed and all 144 gateway-level denials were audited. A plain subprocess with an identity-trusting gateway allowed 240/288. The two attack types that failed there (forged token, other-task token replay) failed only because that gateway ignores tokens. The mock agent always "followed" the injected instruction, so the 0% here reflects containment, not model robustness.

**Overhead.** Sandbox start was about 26–27 ms in every mode. Snapshotting took about 2.3 ms for a 3.1 KB tarball. Resume latency (restore plus start until the first step) was 32–36 ms in every mode. At this scale, governance adds no measurable latency at the sandbox boundary.

**Additional limitations (sandbox).**
- bubblewrap is not a container or VM boundary.
- The sandboxes ran locally on a single host, using a toy 3 KB working directory. Real snapshots (dependencies, caches) are orders of magnitude larger, so snapshot and restore cost is understated.
- Kills land only at step boundaries detected by the host, with the race window set by a 10 ms step sleep.
- Attacks are scripted, not generated by a real LLM.
- Residue detection uses verbatim markers, so it misses paraphrased data.
- The unscoped baseline is deliberately weak.
