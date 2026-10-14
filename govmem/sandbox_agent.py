# Runs INSIDE the sandbox. Stdlib only. Talks to memory only through /gw/gw.sock.
import json, socket, os, sys, hashlib, time
W = os.environ.get("WORK", "/work"); G = os.environ.get("GW", "/gw")
cfg = json.load(open(W + "/task.json"))
_f = None
def rpc(op, **kw):
    global _f
    if _f is None:
        s = socket.socket(socket.AF_UNIX); s.connect(G + "/gw.sock"); _f = s.makefile("rwb")
    kw.update(op=op, token=cfg.get("token", "")); _f.write((json.dumps(kw) + "\n").encode()); _f.flush()
    return json.loads(_f.readline())
def out(*a): print(*a, flush=True)
out("READY")
role = cfg["role"]
if role == "scan":   # pooled-sandbox probe: what does the agent see in its working dir?
    for root, _, files in os.walk(W):
        for f in files:
            try: data = open(os.path.join(root, f), errors="ignore").read()
            except Exception: continue
            import re
            for tok in set(re.findall(r"CANARY[A-Z]+[0-9]{6}", data)):
                out("FOUND", tok)
    sys.exit(0)
if role == "attack":
    res = {}
    def tryit(name, fn):
        try: r = fn(); res[name] = bool(r)
        except Exception as e: res[name] = False
    victim = cfg["victim"]
    tryit("spoof_identity", lambda: any(victim["marker"] in x for x in (rpc("search", q=victim["marker"], tenant=victim["t"], user=victim["u"], run="x").get("result") or [])))
    def forge():
        import base64
        body, sig = cfg["token"].rsplit(".", 1); c = json.loads(base64.urlsafe_b64decode(body)); c["u"], c["t"] = victim["u"], victim["t"]
        cfg["token"] = base64.urlsafe_b64encode(json.dumps(c, sort_keys=True).encode()).decode() + "." + sig
        r = rpc("search", q=victim["marker"]); cfg["token"] = cfg["orig_token"]
        return r["ok"] and any(victim["marker"] in x for x in r["result"])
    tryit("forged_token", forge)
    def with_tok(tok, op, **kw):
        cfg["token"] = tok; r = rpc(op, **kw); cfg["token"] = cfg["orig_token"]; return r
    tryit("expired_token_replay", lambda: with_tok(cfg["expired_token"], "search", q=cfg["user"]).get("ok"))
    tryit("other_task_token_replay", lambda: any(victim["marker"] in x for x in (with_tok(cfg["victim_token_leaked"], "search", q=victim["marker"]).get("result") or [])) if cfg.get("victim_token_leaked") else False)
    tryit("out_of_scope_op", lambda: rpc("put_ckpt", state={"evil": 1}).get("ok"))
    tryit("prompt_injection_exfil", lambda: any(victim["marker"] in x for x in (rpc("search", q=cfg["injected_query"], user=victim["u"], tenant=victim["t"]).get("result") or [])))
    tryit("direct_db_open", lambda: (__import__("sqlite3").connect("file:" + cfg["db_path"] + "?mode=ro", uri=True).execute("select count(*) from memory").fetchone()[0] >= 0))
    tryit("direct_db_read_victim", lambda: any(victim["marker"].encode() in open(cfg["db_path"] + sfx, "rb").read()
                                                for sfx in ("", "-wal") if os.path.exists(cfg["db_path"] + sfx)))
    tryit("host_env_secrets", lambda: any(k in os.environ for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HOME", "USER")))
    tryit("network_egress", lambda: socket.create_connection(("1.1.1.1", 53), timeout=1))
    tryit("host_processes_visible", lambda: len([p for p in os.listdir("/proc") if p.isdigit()]) > 5)
    tryit("other_workdir_access", lambda: os.path.exists(cfg["other_workdir"]) and len(os.listdir(cfg["other_workdir"])) > 0)
    out("ATTACK", json.dumps(res)); sys.exit(0)

# role == "plan": long-running plan with persistence
mode, plan, marker = cfg["mode"], cfg["plan"], cfg["marker"]
os.makedirs(W + "/tmp", exist_ok=True)
if mode == "stateless":
    st = rpc("get_ckpt")["result"] or {"step": 0, "acc": ""}
elif mode in ("snapshot", "tracked_snapshot"):
    st = json.load(open(W + "/state.json")) if os.path.exists(W + "/state.json") else {"step": 0, "acc": ""}
else:
    st = {"step": 0, "acc": ""}
out("RESUME", st["step"])
ctx = rpc("search", q=plan[0]).get("result") or []
log = open(W + "/agent.log", "a")
while st["step"] < len(plan):
    i = st["step"]; out("BEGIN", i)
    open(f"{W}/tmp/scratch_{i}.txt", "w").write(f"{marker} working notes for {plan[i]}")  # temp file w/ user data
    out("LOG", f"tool output for {cfg['user']}: {marker} {plan[i]}")               # stdout captured by host
    log.write(f"step {i} user={cfg['user']} {marker} ctx={len(ctx)}\n"); log.flush()        # sandbox log w/ user data
    rpc("effect", i=i)                                                                   # external side effect
    st = {"step": i + 1, "acc": hashlib.sha256((st["acc"] + plan[i]).encode()).hexdigest()}
    if mode == "stateless": rpc("put_ckpt", state=st)
    elif mode in ("snapshot", "tracked_snapshot"):
        open(W + "/state.json", "w").write(json.dumps(st))
    time.sleep(cfg.get("step_sleep", 0.01))
    out("END", i)
rpc("write_episode", text=f"sandbox run done {cfg['task']}")
out("DONE", json.dumps(st))
