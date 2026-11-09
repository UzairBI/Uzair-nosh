"""Groq (or Gemini) client: JSON calls, plain completions and token streaming; plus the structured-intent parser.

Keys come from the environment or a local, git-ignored `.env` file:  GROQ_API_KEY (preferred) or GEMINI_API_KEY.
Plain HTTPS via urllib - no SDK needed.

Models (override with env vars):
  GROQ_MODEL       answers, reasoning-heavy work            default openai/gpt-oss-120b
  GROQ_FAST_MODEL  routing, query rewriting, SQL, parsing    default openai/gpt-oss-20b
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Iterator

from . import ingredients as ing
from . import nlu

log = logging.getLogger('nosh.llm')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FAST = os.environ.get("GROQ_FAST_MODEL", "openai/gpt-oss-20b")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

PROMPT = """You convert a user's message to a recipe chatbot into JSON. Output ONLY JSON with these keys:
intent: one of search|match|stats|substitute|view|refine|reset|help
text: list of dish words; include: list of required ingredients; exclude: list of forbidden ingredients;
restrictions: list from [dairy,egg,peanut,nuts,gluten,soy,sesame,fish,shellfish,pork,meat,vegetarian,vegan,alcohol];
max_steps: int|null; have: list of ingredient names the user owns; stat: list from [count,percent,mean,median,top_ingredients,rank_fewest,rank_most];
target: ingredient to replace|null; with: proposed substitute|null; ref_index: int|null.
Conversation mode so far: {mode}. Message: {msg}"""


def load_env():
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


class LLMError(Exception):
    pass


# The model likes typographic characters that look wrong in a chat UI or break exact matching:
# narrow/no-break spaces ("30 days"), non-breaking hyphens, full-width citation brackets.
_TYPO = str.maketrans({" ": " ", " ": " ", " ": " ", " ": " ", "‑": "-", "【": "[", "】": "]"})


def clean_text(text: str) -> str:
    return text.translate(_TYPO)


FALLBACK_MODELS = [m for m in os.environ.get("GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",") if m.strip()]


class LLM:
    def __init__(self, provider: str, key: str):
        self.provider, self.key = provider, key
        self.main = GROQ_MODEL if provider == "groq" else GEMINI_MODEL
        self.fast = GROQ_FAST if provider == "groq" else GEMINI_MODEL

    def _chain(self, model: str | None) -> list:
        """Requested model first, then fallbacks - a rate limit on one model must not make the assistant fail."""
        first = model or self.main
        return [first] + [m for m in FALLBACK_MODELS if m != first]

    # ------------------------------------------------------------------ low level
    def _groq_body(self, messages, model, temperature, max_tokens, json_mode, stream):
        body = {"model": model, "messages": messages, "temperature": temperature, "max_completion_tokens": max_tokens}
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"
            body["include_reasoning"] = False
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if stream:
            body["stream"] = True
        return body

    def _post(self, body: dict, stream=False, retries=2):
        req = urllib.request.Request(GROQ_URL, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.key}", "User-Agent": "nosh-assistant/1.0"})
        for attempt in range(retries + 1):
            try:
                return urllib.request.urlopen(req, timeout=60)
            except urllib.error.HTTPError as e:
                if e.code == 429:                       # rate limit: wait only if it is short, otherwise let the caller switch model
                    try:
                        wait = float(e.headers.get("retry-after") or 99)
                    except ValueError:
                        wait = 99
                    if wait <= 4 and attempt < retries:
                        time.sleep(wait + 0.2)
                        continue
                elif e.code in (500, 502, 503) and attempt < retries:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                detail = ""
                try:
                    detail = json.loads(e.read().decode())["error"]["message"]
                except Exception:
                    pass
                raise LLMError(f"HTTP {e.code}: {detail}"[:300])
            except Exception as e:
                if attempt < retries:
                    time.sleep(0.8)
                    continue
                raise LLMError(str(e))

    # ------------------------------------------------------------------ public API
    def complete(self, messages: list, model: str | None = None, temperature: float = 0.3, max_tokens: int = 1800, json_mode: bool = False) -> str:
        if self.provider != "groq":
            return clean_text(self._gemini(messages, json_mode))
        last = None
        for m in self._chain(model):
            try:
                with self._post(self._groq_body(messages, m, temperature, max_tokens, json_mode, False)) as r:
                    return clean_text((json.load(r)["choices"][0]["message"].get("content") or "").strip())
            except LLMError as e:
                last = e
                log.warning("LLM call failed on %s: %s", m, str(e)[:160])
                if not any(k in str(e) for k in ("HTTP 429", "HTTP 5", "model_not_found", "does not exist", "timed out")):
                    raise
        raise last

    def json(self, messages: list, model: str | None = None, max_tokens: int = 700) -> dict:
        txt = self.complete(messages, model or self.fast, 0.0, max_tokens, json_mode=True)
        try:
            return json.loads(txt)
        except Exception:
            a, b = txt.find("{"), txt.rfind("}")
            if a >= 0 and b > a:
                return json.loads(txt[a:b + 1])
            raise LLMError("model did not return JSON")

    def stream(self, messages: list, model: str | None = None, temperature: float = 0.4, max_tokens: int = 1800) -> Iterator[str]:
        if self.provider != "groq":
            yield self._gemini(messages, False)
            return
        resp, last = None, None
        for m in self._chain(model):
            try:
                resp = self._post(self._groq_body(messages, m, temperature, max_tokens, False, True))
                break
            except LLMError as e:
                last = e
                if not any(k in str(e) for k in ("HTTP 429", "HTTP 5", "model_not_found", "does not exist", "timed out")):
                    raise
        if resp is None:
            raise last
        with resp:
            for raw in resp:
                line = raw.decode("utf8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"]
                except Exception:
                    continue
                piece = delta.get("content")
                if piece:
                    piece = clean_text(piece)
                    yield piece

    def _gemini(self, messages, json_mode):
        text = "\n\n".join(f"{m['role']}: {m['content']}" for m in messages)
        body = {"contents": [{"parts": [{"text": text}]}], "generationConfig": {"temperature": 0.3, **({"responseMimeType": "application/json"} if json_mode else {})}}
        req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={self.key}",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)["candidates"][0]["content"]["parts"][0]["text"]

    # ------------------------------------------------------------------ structured intent (used by the recipe engine fallback)
    def parse(self, msg: str, state) -> nlu.Intent | None:
        try:
            j = self.json([{"role": "user", "content": PROMPT.format(mode=getattr(state, "mode", None), msg=msg)}])
        except Exception:
            return None
        it = nlu.Intent(kind=j.get("intent") or "unknown", raw=msg)
        it.text = [str(w).lower() for w in j.get("text") or []]
        it.include = [ing.normalize_name(x) for x in j.get("include") or []]
        it.exclude = [ing.normalize_name(x) for x in j.get("exclude") or []]
        for r in j.get("restrictions") or []:
            it.mask |= nlu.RESTRICT.get(str(r).lower(), 0)
        it.max_steps = j.get("max_steps")
        it.have = [(ing.normalize_name(x), None, None, x) for x in j.get("have") or []]
        it.stat = j.get("stat") or []
        if j.get("target"):
            it.target = ing.normalize_name(j["target"])
        if j.get("with"):
            it.with_ = ing.normalize_name(j["with"])
        it.ref_index = j.get("ref_index")
        return it


def make_llm():
    load_env()
    if os.environ.get("GROQ_API_KEY"):
        return LLM("groq", os.environ["GROQ_API_KEY"])
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return LLM("gemini", key) if key else None
