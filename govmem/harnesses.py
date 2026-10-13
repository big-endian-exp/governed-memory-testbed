"""Three agent harnesses sharing one memory service, each with different permissions (see store.DEFAULT_ACL)."""
import json, hashlib
from .store import PolicyError

class Counter:   # independent op counter used to check audit completeness
    def __init__(self): self.n = 0

class ReActAgent:
    name = "react"
    TOOLS = {"lookup_ticket": lambda u, q: f"ticket status for {u}: open",
             "get_pref": lambda u, q: "pref", "run_deploy": lambda u, q: "deployed", "finish": lambda u, q: ""}
    def __init__(self, mem, llm, ctr): self.mem, self.llm, self.ctr = mem, llm, ctr
    def run(self, tenant, user, task, max_steps=3):
        ctx = self.mem.search(self.name, tenant, user, task, k=4); self.ctr.n += 1
        trace = []
        for _ in range(max_steps):
            out = self.llm.complete(task + "\n" + "\n".join(c["content"] for c in ctx), kind="react")
            action = out.split("Action:")[-1].strip()
            obs = self.TOOLS.get(action, self.TOOLS["finish"])(user, task)
            trace.append(f"{action}->{obs}")
            if action == "finish": break
            task = task + " done"
            if action == "finish" or len(trace) > 1: break
        self.mem.write(self.name, tenant, user, "episodic", f"[react] task={task} trace={trace}", src_ref="react-run"); self.ctr.n += 1
        return ctx, trace

class Autopilot:
    """Long-running plan executor; checkpoints working state after each step; resumes after crash."""
    name = "autopilot"
    def __init__(self, mem, llm, ctr): self.mem, self.llm, self.ctr = mem, llm, ctr
    def run(self, tenant, user, run_id, plan, crash_at=None):
        st = self.mem.latest_checkpoint(self.name, tenant, user, run_id); self.ctr.n += 1
        if st is None: st = {"step": 0, "acc": "", "effects": []}
        resumed_from = st["step"]
        while st["step"] < len(plan):
            i = st["step"]
            if crash_at is not None and i == crash_at: raise RuntimeError(f"crash@{i}")
            st["acc"] = hashlib.sha256((st["acc"] + plan[i]).encode()).hexdigest()   # deterministic state evolution
            st["effects"].append(i); st["step"] = i + 1
            self.mem.write(self.name, tenant, user, "working", json.dumps(st), key=f"ckpt:{run_id}",
                           sensitivity="restricted", ttl=7 * 86400, src_ref=f"{run_id}:{i}"); self.ctr.n += 1
        self.mem.write(self.name, tenant, user, "episodic", f"[autopilot] completed {run_id} ({len(plan)} steps)",
                       src_ref=run_id); self.ctr.n += 1
        return st, resumed_from

class RAGAssistant:
    """Content-grounded assistant: ingests user documents as semantic memory, summarizes, answers."""
    name = "rag"
    def __init__(self, mem, llm, ctr): self.mem, self.llm, self.ctr = mem, llm, ctr
    def ingest(self, tenant, user, doc, sensitivity="internal", scope="user"):
        rid = self.mem.write(self.name, tenant, user, "semantic", doc, scope=scope, sensitivity=sensitivity,
                             src_ref="doc"); self.ctr.n += 1
        return rid
    def summarize(self, tenant, user, ids, texts):
        s = self.llm.complete("\n".join(texts), kind="summarize")
        rid = self.mem.write(self.name, tenant, user, "semantic", s, derived_from=ids, src_ref="summary"); self.ctr.n += 1
        return rid
    def answer(self, tenant, user, q):
        ctx = self.mem.search(self.name, tenant, user, q, k=4); self.ctr.n += 1
        return ctx, self.llm.complete(q + "\n" + "\n".join(c["content"] for c in ctx), kind="answer")
