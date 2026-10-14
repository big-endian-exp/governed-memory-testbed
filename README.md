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
| write p50 @10k | 0.53 ms | 1.22 ms |
| search p50 @10k | 7.9 ms | 36.4 ms |
| forget_user @10k | 149 ms | 25 ms |
| storage per record | 17.5 KB (+2.2%) | 17.1 KB |

Charts: `results/fig_governance_rates.png`, `fig_deletion_residue.png`, `fig_visibility.png`, `fig_crash_resume.png`, `fig_latency.png`.
The evaluation section draft is in `results/evaluation_draft.md`.

## Caveats
The mock LLM and synthetic data are described in the evaluation draft's limitations. The naive write path is slower only because it autocommits twice per record while the governed path uses one transaction. Treat the write-latency comparison as "no measurable governance penalty", not as a speedup.

## Sandbox experiment: governance across the sandbox boundary
**Backends (pluggable).** Select one with `--backend bwrap|docker` or `GOVMEM_SANDBOX=bwrap|docker`. Override the Docker CLI with `GOVMEM_DOCKER` (for example `"sudo docker"`).
- **bwrap:** bubblewrap with no network, private namespaces, a cleared env, read-only `/usr`, and a per-task `/work`.
- **docker** (`govmem/sandbox.py: DockerRun`), image `govmem-sbx` built from `python:3.13-slim`:
  - `--network none --read-only --cap-drop ALL --security-opt no-new-privileges --cpus 1 --memory 256m --pids-limit 128`, running as the host UID
  - a tmpfs `/work` (or a bind-mounted `/work` for tarball snapshots)
  - the gateway socket directory as the only host mount; there is no DB mount and no Docker socket
  - agent code and the task are streamed in over stdin as a tar
  - pooling uses long-lived hardened containers plus `docker exec` (warm start)

In both backends, the sandbox reaches memory only through the gateway (`govmem/gateway.py`): JSON-lines over a Unix socket, with an HMAC token scoped to tenant, user, task, operations and expiry. Every denial is audited.

**Persistence modes:**
- `none`
- (a) `stateless` + governed checkpoints
- (b) naive tarball of `/work`
- (c) the same tarball, tracked (lineage + TTL + cascade delete)
- Docker only:
  - (b') naive `docker commit`
  - (c') tracked `docker commit`, where the image ref is registered as an artifact and `forget_user`/TTL purge run `docker rmi`. Commit snapshots need a **writable** rootfs, so they drop `--read-only`.

**Run**
```
.venv/bin/python run_sandbox_experiments.py --backend bwrap  --seeds 3                    # ~2 min
GOVMEM_DOCKER="sudo docker" .venv/bin/python run_sandbox_experiments.py --backend docker --seeds 2 --runs-per-user 2   # ~45 min
.venv/bin/python make_sandbox_charts.py
```
**CI.** `.github/workflows/experiments.yml` runs the original experiments plus the Docker-backend sandbox experiments on `ubuntu-latest` and uploads `results/`. *This file is not yet on GitHub* because the push token lacks the `workflow` scope; see the commit notes.

**Results (mock LLM).** bwrap: 3 seeds, 72 runs/mode. Docker: 2 seeds, 32 runs/mode, measured locally with Docker 26.1.5 on Debian 13, cgroup v2. Sources: `results/sbx_summary.csv`, `sbx_pooling_summary.csv`, `sbx_escape_summary.csv`.

| metric | bwrap | docker |
|---|---|---|
| cold start to READY (mean over modes) | 27–31 ms | 279–306 ms |
| warm start (pooled container, `docker exec`) | n/a (same as cold, 28.7 ms) | 214 ms |
| tar snapshot / `docker commit` time | 2.7 ms / – | 2.4 ms / 58–64 ms |
| snapshot size: tar / commit layer | 3.3 KB / – | 3.3 KB / 6.8 KB |
| resume latency, stateless / tar / commit | 32 / 34 ms | 292 / 318 / 295 ms |
| pooled-sandbox cross-user leak (fresh / pooled / scrubbed) | 0 / 91.7% / 0 | 0 / 91.7% / 0 |
| escape successes, scoped sandbox (17 attack types) | 0/408 | 0/272 |
| escape successes, plain subprocess + unscoped gateway | 312/408 | 208/272 |
| gateway denials audited | 144/144 | 96/96 |

| deletion residue per forgotten user | naive tar (b) | tracked tar (c) | naive commit (b') | tracked commit (c') |
|---|---|---|---|---|
| bwrap: snapshots / host logs | 3.0 / 5.67 | 0 / 0 | – | – |
| docker: snapshots or images / host logs | 2.0 / 4.0 | 0 / 0 | **4.0 images** / 4.0 | 0 / 0 |
| artifacts left after TTL purge (docker) | 96/96 | 0/72 | 128/128 | 0/96 |

| crash/resume (interrupted runs) | none | (a) | (b) | (c) |
|---|---|---|---|---|
| bwrap fidelity / torn-state failures / redone steps | 1.00 / 0 / 7.49 | 1.00 / 0 / 0.29 | 0.89 / 8 / 0.15 | 0.93 / 4 / 0.20 |
| docker fidelity / torn / redone (commit modes: 1.00 / 0 / 0) | 1.00 / 0 / 8.13 | 1.00 / 0 / 0 | 1.00 / 0 / 0 | 1.00 / 0 / 0 |

The Docker crash numbers are **not** comparable with bwrap. `docker kill` takes 0.1–0.2 s to land, so Docker steps sleep 300 ms (bwrap: 10 ms). Kills then almost always land during the sleep, after the checkpoint, which hides both torn writes and duplicated effects.

Charts: `results/fig_sbx_crash_resume.png`, `fig_sbx_deletion.png`, `fig_sbx_ttl.png`, `fig_sbx_pooling.png`, `fig_sbx_escape.png`, `fig_sbx_overhead.png`.
