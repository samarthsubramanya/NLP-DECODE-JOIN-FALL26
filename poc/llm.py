"""Frozen LLM behind one call: complete_json(stage, system, user) -> dict.

Any OpenAI-compatible chat endpoint works (DeepSeek by default). Temperature 0,
responses cached on disk by prompt hash so reruns are free and identical, and
every call (cached or not) is appended to the run's llm_calls.jsonl.
"""

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

from poc.config import resolve


class LLM:
    def __init__(self, cfg, log_path, responder=None):
        self.cfg = cfg["llm"]
        self.cache_dir = resolve(cfg, cfg["output"]["dir"]) / "llm_cache"
        self.log_path = Path(log_path)
        self.responder = responder        # provider "fake": a function(stage, payload) -> dict
        self.calls = 0

    def complete_json(self, stage, system, user):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        key = hashlib.sha256(json.dumps([self.cfg["model"], messages], sort_keys=True).encode()).hexdigest()
        cached = self.cache_dir / f"{key}.json"

        start, usage = time.perf_counter(), None
        if self.cfg["provider"] == "fake":
            text, source = json.dumps(self.responder(stage, user) if self.responder else {}), "fake"
        elif self.cfg["cache"] and cached.exists():
            text, source = json.loads(cached.read_text())["text"], "cache"
        else:
            text, usage = self._post(messages)
            source = "api"
            if self.cfg["cache"]:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps({"model": self.cfg["model"], "text": text}))

        parsed = parse_json(text)
        self.calls += source == "api"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps({
                "stage": stage, "key": key, "source": source, "usage": usage,
                "seconds": round(time.perf_counter() - start, 2), "ok": parsed is not None,
            }) + "\n")
        return parsed

    def _post(self, messages):
        api_key = api_key_from(self.cfg["api_key_env"])
        body = {
            "model": self.cfg["model"], "messages": messages,
            "temperature": self.cfg["temperature"], "max_tokens": self.cfg["max_tokens"],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            self.cfg["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=self.cfg["timeout_s"]) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"], data.get("usage")


def parse_json(text):
    text = (text or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def api_key_from(env_name):
    if os.environ.get(env_name):
        return os.environ[env_name]
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == env_name and value.strip():
                return value.strip().strip("'\"")
    raise RuntimeError(f"{env_name} is not set; add it to decode-join/.env or the environment")
