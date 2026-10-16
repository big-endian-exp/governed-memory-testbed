"""Pluggable LLM backends. Default: deterministic MockLLM (no key needed).

GOVMEM_LLM=anthropic|openai|codex|xai|mock|auto
  anthropic: bulk=GOVMEM_BULK_MODEL (claude-haiku-4-5), quality=GOVMEM_QUALITY_MODEL (claude-sonnet-4-6)
  openai:    OPENAI_API_KEY HTTP API
  xai:       XAI_API_KEY → https://api.x.ai/v1 (bulk=grok-4.20-0309-non-reasoning, quality=grok-4.7)
  codex:     Codex CLI (`codex exec --skip-git-repo-check`), ChatGPT Pro plan; no key in env

Every real call is logged to USAGE and cached under ~/.cache/govmem_llm (outside the repo)."""
import os, json, hashlib, time, urllib.request, urllib.error, subprocess, tempfile, re

USAGE = []   # dicts: model, kind, in_tok, out_tok, cached, provider
PRICE = {    # USD / MTok list-price estimates (Codex is plan-billed; we still log tokens)
    "claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-6": (3.0, 15.0),
    "gpt-4o-mini": (0.15, 0.6), "codex": (0.0, 0.0),
    "grok-4.7": (2.0, 6.0), "grok-4.6": (2.0, 6.0), "grok-4.3": (2.0, 6.0),
    "grok-4.20-0309-non-reasoning": (0.20, 0.50), "grok-4-fast-non-reasoning": (0.20, 0.50),
}
CACHE = os.path.expanduser(os.environ.get("GOVMEM_LLM_CACHE", "~/.cache/govmem_llm"))
QUALITY_KINDS = {"summarize", "attack", "digest"}

SYSTEM = {
    "summarize": "You are the summarization component of an enterprise assistant's memory. Write a concise factual summary "
                 "(max 80 words) of the user's notes so it can replace them in long-term memory. Keep specific facts.",
    "agent": "You are an enterprise agent with tools. On every turn reply with exactly one JSON object {\"tool\":\"...\",\"args\":{...}} and no other text. Available tools are listed in the user message.",
    "react": "You are a ReAct agent. Tools: lookup_ticket, get_pref, run_deploy, finish. Reply with exactly two lines:\n"
             "Thought: <one sentence>\nAction: <tool name>",
    "digest": "You write short factual digests of multi-user enterprise notes. Keep names and concrete values. Max 100 words.",
    "answer": "You are an enterprise assistant. Answer the user's question using only the provided memory context. "
              "If the context lacks the answer, say \"I don't know\". Be brief.",
}

def cost_usd(provider=None):
    tot = 0.0
    for u in USAGE:
        if u["cached"]: continue
        if provider and u.get("provider") != provider: continue
        p = PRICE.get(u["model"])
        if p is None: p = (0.0, 0.0) if u.get("provider") == "codex" else (3.0, 15.0)
        tot += (u["in_tok"] * p[0] + u["out_tok"] * p[1]) / 1e6
    return tot

def usage_summary():
    by = {}
    for u in USAGE:
        k = (u.get("provider") or "?", u["model"])
        by.setdefault(k, {"calls": 0, "cached": 0, "in": 0, "out": 0})
        if u["cached"]: by[k]["cached"] += 1
        else:
            by[k]["calls"] += 1; by[k]["in"] += u["in_tok"]; by[k]["out"] += u["out_tok"]
    return by

class MockLLM:
    name = "mock"; provider = "mock"
    def complete(self, prompt, kind="generic"):
        if kind == "summarize":
            lines = [l.strip() for l in prompt.splitlines() if l.strip()][:6]
            return "SUMMARY: " + " | ".join(l[:80] for l in lines)
        if kind == "react":
            p = prompt.lower()
            for kw, tool in [("ticket", "lookup_ticket"), ("region", "get_pref"), ("deploy", "run_deploy")]:
                if kw in p: return f"Thought: need {tool}\nAction: {tool}"
            return "Thought: answer directly\nAction: finish"
        if kind == "answer": return "ANSWER grounded in: " + prompt[-200:]
        return "OK " + hashlib.sha1(prompt.encode()).hexdigest()[:8]
    def chat(self, system, messages, kind="generic", max_tokens=400):
        return self.complete(messages[-1]["content"], kind)

class AnthropicLLM:
    name = "anthropic"; provider = "anthropic"
    def __init__(self, bulk=None, quality=None):
        self.bulk = bulk or os.environ.get("GOVMEM_BULK_MODEL", "claude-haiku-4-5")
        self.quality = quality or os.environ.get("GOVMEM_QUALITY_MODEL", "claude-sonnet-4-6")
    def model_for(self, kind): return self.quality if kind in QUALITY_KINDS else self.bulk
    def chat(self, system, messages, kind="generic", max_tokens=400, model=None):
        model = model or self.model_for(kind)
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0, "system": system, "messages": messages}
        key = hashlib.sha256(("anthropic|" + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        os.makedirs(CACHE, exist_ok=True); cp = os.path.join(CACHE, key + ".json")
        if os.path.exists(cp):
            r = json.load(open(cp)); USAGE.append(dict(model=model, kind=kind, in_tok=r["in"], out_tok=r["out"], cached=True, provider="anthropic"))
            return r["text"]
        for attempt in range(6):
            try:
                req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                    headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                             "content-type": "application/json"})
                r = json.load(urllib.request.urlopen(req, timeout=120)); break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 529) and attempt < 5: time.sleep(2 ** attempt); continue
                raise RuntimeError(f"anthropic HTTP {e.code}: {e.read()[:200]}") from None
        text = "".join(c.get("text", "") for c in r["content"] if c["type"] == "text")
        u = r["usage"]; USAGE.append(dict(model=model, kind=kind, in_tok=u["input_tokens"], out_tok=u["output_tokens"], cached=False, provider="anthropic"))
        json.dump({"text": text, "in": u["input_tokens"], "out": u["output_tokens"]}, open(cp, "w")); return text
    def complete(self, prompt, kind="generic"):
        return self.chat(SYSTEM.get(kind, "You are a helpful assistant."), [{"role": "user", "content": prompt}], kind=kind)

class OpenAILLM:
    name = "openai"; provider = "openai"
    def __init__(self, model=None): self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    def chat(self, system, messages, kind="generic", max_tokens=400):
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions",
            data=json.dumps({"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                             "messages": [{"role": "system", "content": system}] + messages}).encode(),
            headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"]
    def complete(self, prompt, kind="generic"):
        return self.chat(SYSTEM.get(kind, "You are a helpful assistant."), [{"role": "user", "content": prompt}], kind)

class CodexLLM:
    """OpenAI models via the Codex CLI (ChatGPT Pro). Non-interactive: prompt as argv, stdin=/dev/null,
    temp workdir, read-only sandbox. Parses --json agent_message items for the final reply."""
    name = "codex"; provider = "codex"
    def __init__(self, model=None):
        self.model = model or os.environ.get("GOVMEM_CODEX_MODEL", "")  # empty = CLI default
        self.bin = os.environ.get("GOVMEM_CODEX_BIN", "codex")
        self.timeout = int(os.environ.get("GOVMEM_CODEX_TIMEOUT", "180"))
        # Soft rate-limit: sleep between calls
        self.pause = float(os.environ.get("GOVMEM_CODEX_PAUSE", "1.5"))
        self._last = 0.0
    def model_for(self, kind): return self.model or "codex-default"
    def chat(self, system, messages, kind="generic", max_tokens=400, model=None):
        # Flatten to a single prompt (Codex CLI is a single-turn exec for our purposes)
        parts = [f"[system]\n{system}"]
        for m in messages: parts.append(f"[{m['role']}]\n{m['content']}")
        parts.append(f"[instruction]\nReply with only the answer content. No preamble. Max ~{max_tokens} tokens.")
        prompt = "\n\n".join(parts)
        key = hashlib.sha256(("codex|" + (model or self.model) + "|" + prompt).encode()).hexdigest()
        os.makedirs(CACHE, exist_ok=True); cp = os.path.join(CACHE, key + ".json")
        if os.path.exists(cp):
            r = json.load(open(cp)); USAGE.append(dict(model=r.get("model", "codex"), kind=kind, in_tok=r["in"], out_tok=r["out"], cached=True, provider="codex"))
            return r["text"]
        # rate limit
        dt = time.time() - self._last
        if dt < self.pause: time.sleep(self.pause - dt)
        cmd = [self.bin, "exec", "--skip-git-repo-check", "-s", "read-only", "--json"]
        if self.model: cmd += ["-m", self.model]
        cmd += ["--", prompt]
        with tempfile.TemporaryDirectory(prefix="govmem-codex-") as td:
            for attempt in range(4):
                try:
                    p = subprocess.run(cmd, cwd=td, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=self.timeout)
                    break
                except subprocess.TimeoutExpired:
                    if attempt == 3: raise
                    time.sleep(2 ** attempt)
        self._last = time.time()
        text, in_tok, out_tok, model_name = "", 0, 0, self.model or "codex-default"
        for line in p.stdout.splitlines():
            try: ev = json.loads(line)
            except Exception: continue
            if ev.get("type") == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
                text = ev["item"].get("text", text)
            if ev.get("type") == "turn.completed":
                u = ev.get("usage") or {}
                in_tok = int(u.get("input_tokens") or 0); out_tok = int(u.get("output_tokens") or 0)
        if not text:
            # fallback: last non-empty stdout line that isn't JSON
            for line in reversed(p.stdout.splitlines()):
                if line.strip() and not line.startswith("{"): text = line.strip(); break
        if p.returncode != 0 and not text:
            raise RuntimeError(f"codex exit {p.returncode}: {(p.stderr or '')[-300:]}")
        USAGE.append(dict(model=model_name, kind=kind, in_tok=in_tok, out_tok=out_tok, cached=False, provider="codex"))
        json.dump({"text": text, "in": in_tok, "out": out_tok, "model": model_name}, open(cp, "w"))
        return text
    def complete(self, prompt, kind="generic"):
        # Tool-routing is scripted for cost/rate-limit; real model used for summarize/answer/attack/judge.
        if kind == "react":
            return MockLLM().complete(prompt, kind)
        return self.chat(SYSTEM.get(kind, "You are a helpful assistant."), [{"role": "user", "content": prompt}], kind=kind)


class XAILLM:
    """xAI Grok via OpenAI-compatible Chat Completions at https://api.x.ai/v1."""
    name = "xai"; provider = "xai"
    def __init__(self, bulk=None, quality=None):
        self.bulk = bulk or os.environ.get("GOVMEM_BULK_MODEL", "grok-4.20-0309-non-reasoning")
        self.quality = quality or os.environ.get("GOVMEM_QUALITY_MODEL", "grok-4.7")
        self.base = os.environ.get("GOVMEM_XAI_BASE", "https://api.x.ai/v1")
    def model_for(self, kind): return self.quality if kind in QUALITY_KINDS else self.bulk
    def chat(self, system, messages, kind="generic", max_tokens=400, model=None):
        model = model or self.model_for(kind)
        body = {"model": model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}] + messages}
        key = hashlib.sha256(("xai|" + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        os.makedirs(CACHE, exist_ok=True); cp = os.path.join(CACHE, key + ".json")
        if os.path.exists(cp):
            r = json.load(open(cp)); USAGE.append(dict(model=model, kind=kind, in_tok=r["in"], out_tok=r["out"],
                                                       cached=True, provider="xai", usd_ticks=r.get("ticks", 0)))
            return r["text"]
        for attempt in range(6):
            try:
                req = urllib.request.Request(self.base + "/chat/completions", data=json.dumps(body).encode(),
                    headers={"Authorization": "Bearer " + os.environ["XAI_API_KEY"], "Content-Type": "application/json"})
                r = json.load(urllib.request.urlopen(req, timeout=180)); break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 529) and attempt < 5: time.sleep(2 ** attempt); continue
                raise RuntimeError(f"xai HTTP {e.code}: {e.read()[:200]}") from None
        text = r["choices"][0]["message"]["content"] or ""
        u = r.get("usage") or {}
        inn = int(u.get("prompt_tokens") or 0); out = int(u.get("completion_tokens") or 0)
        # reasoning tokens (if any) are often inside completion_tokens_details; bill as output for our estimate
        details = u.get("completion_tokens_details") or {}
        reason = int(details.get("reasoning_tokens") or 0)
        ticks = int(u.get("cost_in_usd_ticks") or 0)
        USAGE.append(dict(model=model, kind=kind, in_tok=inn, out_tok=out + reason, cached=False, provider="xai", usd_ticks=ticks))
        json.dump({"text": text, "in": inn, "out": out + reason, "ticks": ticks}, open(cp, "w")); return text
    def complete(self, prompt, kind="generic"):
        if kind == "react":
            return MockLLM().complete(prompt, kind)   # scripted tool routing; real model for summarize/answer/attack/judge
        return self.chat(SYSTEM.get(kind, "You are a helpful assistant."), [{"role": "user", "content": prompt}], kind=kind)


def get_llm(pref=None):
    pref = pref or os.environ.get("GOVMEM_LLM", "auto")
    if pref == "codex": return CodexLLM()
    if pref in ("xai", "auto") and os.environ.get("XAI_API_KEY"): return XAILLM()
    if pref in ("anthropic", "auto") and os.environ.get("ANTHROPIC_API_KEY"): return AnthropicLLM()
    if pref in ("openai", "auto") and os.environ.get("OPENAI_API_KEY"): return OpenAILLM()
    if pref == "anthropic": raise RuntimeError("GOVMEM_LLM=anthropic but ANTHROPIC_API_KEY not set")
    if pref == "xai": raise RuntimeError("GOVMEM_LLM=xai but XAI_API_KEY not set")
    if pref == "openai": raise RuntimeError("GOVMEM_LLM=openai but OPENAI_API_KEY not set")
    return MockLLM()
