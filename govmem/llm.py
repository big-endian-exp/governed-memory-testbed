"""Pluggable LLM backends. Default: deterministic MockLLM (no key needed)."""
import os, json, hashlib, urllib.request

class MockLLM:
    name = "mock"
    def complete(self, prompt: str, kind: str = "generic") -> str:
        if kind == "summarize":          # extractive: first clause of each line, deterministic
            lines = [l.strip() for l in prompt.splitlines() if l.strip()][:6]
            return "SUMMARY: " + " | ".join(l[:80] for l in lines)
        if kind == "react":              # scripted tool choice by keyword
            p = prompt.lower()
            for kw, tool in [("ticket", "lookup_ticket"), ("region", "get_pref"), ("deploy", "run_deploy")]:
                if kw in p: return f"Thought: need {tool}\nAction: {tool}"
            return "Thought: answer directly\nAction: finish"
        if kind == "answer":
            return "ANSWER grounded in: " + prompt[-200:]
        return "OK " + hashlib.sha1(prompt.encode()).hexdigest()[:8]

class OpenAILLM:
    name = "openai"
    def __init__(self, model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini")): self.model = model
    def complete(self, prompt, kind="generic"):
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions",
            data=json.dumps({"model": self.model, "temperature": 0,
                             "messages": [{"role": "user", "content": f"[{kind}] {prompt}"}]}).encode(),
            headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"]

class AnthropicLLM:
    name = "anthropic"
    def __init__(self, model=os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")): self.model = model
    def complete(self, prompt, kind="generic"):
        req = urllib.request.Request("https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": self.model, "max_tokens": 512, "temperature": 0,
                             "messages": [{"role": "user", "content": f"[{kind}] {prompt}"}]}).encode(),
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=60))["content"][0]["text"]

def get_llm(pref: str | None = None):
    pref = pref or os.environ.get("GOVMEM_LLM", "auto")
    if pref in ("openai", "auto") and os.environ.get("OPENAI_API_KEY"): return OpenAILLM()
    if pref in ("anthropic", "auto") and os.environ.get("ANTHROPIC_API_KEY"): return AnthropicLLM()
    return MockLLM()
