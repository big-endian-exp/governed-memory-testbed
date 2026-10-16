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
| 1,000 | 0.47 / 1.11 | 1.16 / 2.85 | 8.7 / 2.7 |
| 4,000 | 0.52 / 1.23 | 3.08 / 17.43 | 45.8 / 9.5 |
| 10,000 | 0.53 / 1.22 | 7.87 / 36.44 | 149.2 / 24.5 |

Governance added no measurable write penalty. The naive path is slower here only because it autocommits twice, so this is not a governance speedup. Search was 2.5–6× faster because namespace pre-filtering shrinks the candidate set. The real cost is deletion: lineage cascade plus secure_delete makes `forget_user` about 3–8× slower, though it still completes in about 0.15 s at 10k records. Storage overhead is +2.2% per record (17.5 KB vs 17.1 KB, dominated by the dense embedding blob in both).

## Limitations / threats to validity
- **Mock LLM.** No API key was available, so a deterministic scripted LLM drove all three harnesses (keyword tool selection, extractive summaries). Governance guarantees are enforced below the LLM and do not depend on model quality. However, we did not measure task success, prompt-injection-induced exfiltration attempts, or whether real abstractive summaries paraphrase deleted data in ways canary matching would miss. Real-LLM runs require `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, and the backend is already pluggable.
- **Synthetic data.** There are 8 users, 2 tenants, about 150 records per run, and canary-based detection. Real enterprise memories are larger and more heterogeneous, and residue can be semantic rather than verbatim.
- **Strawman risk.** The baseline is deliberately ungoverned. Several gaps, such as leakage, would close with a simple metadata filter, so the more informative comparisons are deletion through derived artifacts, staleness, and the secure_delete ablation.
- **Correctness-by-construction.** The visibility oracle is an independent re-implementation of the same policy spec. It validates the implementation, not the policy itself.
- **Scale and environment.** The work runs single-process on SQLite with an in-memory brute-force vector index and no concurrency, distributed stores, backups, or external vector DBs. Latency comes from one box and one run per size.
- **Crash model.** Crashes are injected only between steps. Torn writes mid-checkpoint and non-deterministic plans were not tested.

## RQ8 – Governance across the sandbox boundary
**Setup.** Each agent task ran in a short-lived sandbox under one of two pluggable backends.
- **bubblewrap:** no network, private namespaces, cleared environment, read-only system, per-task work directory.
- **Docker 26.1.5:** `--network none`, read-only rootfs with a tmpfs work directory, all capabilities dropped, `no-new-privileges`, CPU, memory and PID limits, and the host UID.

Neither backend could reach the database or the Docker socket. Memory was reachable only through a Unix-socket gateway that checks an HMAC token scoped to tenant, user, task, operations and expiry.

We compared:
- no persistence
- (a) a stateless sandbox with governed checkpoints
- (b) a naive tarball of the work directory
- (c) the same tarball, tracked as a derived artifact with TTL and cascading deletion
- Docker only: (b') a naive `docker commit` and (c') a tracked `docker commit`

bwrap results cover 3 seeds (72 interrupted runs per mode), Docker results 2 seeds (32 per mode). All runs used the deterministic mock LLM.

**Crash/resume.** Under bwrap, (a) resumed every interrupted run with correct final state. The tarball modes failed to resume in 8/72 (b) and 4/72 (c) runs, because the SIGKILL hit while the agent was rewriting its state file and the snapshot captured a torn file. Lineage tracking does not fix this. Transactional checkpoints do. Without persistence, every resume repeated 7.49 steps. With checkpoints, 0.29 steps were repeated on average, and 28.6% of completed runs duplicated one external effect. Checkpointing gives at-least-once semantics, and exactly-once needs idempotency keys. Under Docker, all modes, including both commit modes, reached 100% fidelity with 0 repeated steps. This is an artefact of the harness, not a Docker property. `docker kill` takes 0.1–0.2 s to land, so Docker steps were lengthened to 300 ms, and kills then landed during the idle part of the step, after the state was saved. A fair Docker crash comparison needs in-container fault injection.

**Right-to-delete across sandbox artifacts.** After `forget_user`, both backends showed the same pattern. Naive tarballs still held the deleted user's data (3.0 per user under bwrap, 2.0 under Docker), and so did untracked host sandbox logs (5.67 and 4.0 per user). **Naive `docker commit` snapshots left the user's data in 4.0 images per user.** Each resume commits a new image on top of the previous one, so the data persists in parent layers as well as in the tagged image. Tracked tarballs and tracked commits left 0. For tracked commits, `docker rmi` of the registered tag also removed the untagged parent images. After the retention TTL, untracked artifacts all remained: Docker tar 96/96, commit 128/128, bwrap 216/216. Tracked ones were all purged: Docker 72/72 and 96/96, bwrap 162/162. No mode left records or raw bytes in the governed database.

**Sandbox reuse.** Reusing pooled sandboxes without scrubbing exposed another user's data to 91.7% of tasks under both backends (cross-tenant: 83.3% bwrap, 89.6% Docker). Fresh sandboxes and scrubbed pooled sandboxes gave 0% on both. This held even with Docker's read-only rootfs, because the leak sits in the writable tmpfs `/work` that a warm container keeps. Hardening the container image does not replace per-tenant scrubbing.

**Escape and prompt injection.** 17 attack types were scripted. 12 target memory or host resources: identity spoofing, forged, expired or replayed tokens, out-of-scope operations, a poisoned-memory prompt injection that asks for another user's data, opening or reading the DB file, host env, network, host processes, and another task's workdir. 5 check the container boundary: Docker socket present, host filesystem reachable, non-zero effective capabilities, `no_new_privs` unset, and a writable rootfs. With the scoped gateway, the sandbox stopped every attempt under both backends (bwrap 0/408, Docker 0/272), and every gateway-level denial was audited (144/144 and 96/96). A plain subprocess with an identity-trusting gateway allowed 312/408 and 208/272. Its only failures were the token attacks, which do not apply to a gateway that ignores tokens, plus non-zero capabilities and a writable `/usr`, which an unprivileged host user also lacks. The mock agent always "followed" the injected instruction, so the 0% reflects containment, not model robustness.

**Overhead.**

| | bwrap | Docker |
|---|---|---|
| Cold start to agent-ready | 27–31 ms | 279–306 ms |
| Warm start in a pooled container | – | 214 ms |
| Tarball snapshot | 2.7 ms, 3.3 KB | 2.4 ms, 3.3 KB |
| `docker commit` | – | 58–64 ms, 6.8 KB layer |
| Resume latency | 32–38 ms | 292–342 ms |

Docker costs about 10× more per sandbox start. Warm pooling saves about 30%, but that is exactly the configuration that leaked in the reuse test unless scrubbed. The governance additions (gateway checks, artifact registration) did not measurably change latency compared with the untracked modes.

**Re-run check.** We re-ran the original experiments (RQ1–RQ7) after the sandbox changes to the store. All correctness metrics were bit-identical. Only latency changed, within run-to-run noise; the RQ7 table above shows the re-run numbers.

**Additional limitations (sandbox).**
- Single host, with a toy work directory of about 3 KB. Real images and snapshots carry dependencies and caches, so snapshot cost and the size of committed-layer residue are understated.
- Different kill-timing regimes per backend, as described above.
- Docker ran rootful (dockerd as root, containers as the host UID). Rootless Docker, gVisor and Kata were not tested.
- `docker save` was the only way residue in images was checked. Registries, build cache and exported image tarballs were not scanned.
- Attacks are scripted, not model-generated, and residue detection uses verbatim markers only.
- The Docker results come from fewer seeds and runs than the bwrap results.

## RQ9 – Real LLMs (Claude + OpenAI via Codex CLI)
**Setup.** We replaced the mock with two real backends: Anthropic HTTP (`claude-haiku-4-5` bulk, `claude-sonnet-4-6` quality) and OpenAI through the Codex CLI (`codex exec --skip-git-repo-check`, read-only sandbox, JSON event stream; ChatGPT Pro plan). Workload: 2 tenants × 3 users, five grounded facts per user. Anthropic: 2 seeds for invariants, 30 QA pairs/backend, 10 semantic-judge checks/backend, 18 sandbox attacks. Codex: 1 seed, 18 QA pairs/backend, 6 semantic checks/backend, 6 attacks. Docker attacks were skipped to keep spend/rate-limits modest.

**Invariants.** With real abstractive summaries, governed leak rate stayed 0.000 for both providers; naive leaked 0.200 (120/600 adversarial results) with 18 foreign hits and 8 post-forget retrievable results per seed. Namespace filtering still holds when the summarizer is a real model.

**Task success.** Governed vs naive accuracy: Claude 96.7%/96.7% (n=30); Codex 100%/100% (n=18). Cross-user gold-fact rate 0% for both after whole-token matching. Governance did not reduce usefulness on this fact-QA workload.

**Semantic residue.** After `forget_user`, an LLM judge (Sonnet for Claude runs; Codex for OpenAI runs) asked whether each deleted fact was still conveyed by surviving text. Governed: 0% verbatim and 0% semantic hits; summary rows gone. Naive: 100% verbatim and 100% semantic (summary rows owned by the agent survive delete-by-user-column). Lineage cascade removes paraphrased residue that canary matching would miss.

**Attacks.** A real agent inside bwrap, with tools routed through the scoped gateway, faced model-authored and handcrafted exfiltration instructions. Claude Sonnet refused to write attacks under a red-team framing; Haiku produced 8 under an authorized-evaluation framing; we also ran 10 handcrafted strategies. Codex authored 6. Success (victim canary or gold fact in the agent log): **0/18 Claude, 0/6 Codex**. Denials were audited when the agent attempted cross-identity search. Some models refused the malicious instruction without tools; others attempted `search_memory` with another user's id and were denied.

**Cost.** Anthropic ≈ $0.23 API. Codex ≈ 1.08M input tokens on the ChatGPT Pro plan (no per-token $ here).

**Limitations.** Modest n; Docker real-LLM attacks not re-run; Sonnet refusal means part of the Claude attack suite is handcrafted; Codex CLI wraps a proprietary agent runtime with a large system prompt (~14k tokens/call); judge is a single model without human adjudication.
