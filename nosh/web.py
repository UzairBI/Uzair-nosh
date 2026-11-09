"""Web UI (standard library only):  python -m nosh.web  ->  http://127.0.0.1:8000

API
  POST /api/chat/stream  {"message"}        -> newline-delimited JSON events (route, cards, delta, sources, done)
  POST /api/chat         {"message"}        -> {"reply","lead","cards","sources"}   (non-streaming, used by tests)
  POST /api/upload       raw bytes + X-Filename header -> document info (parsed, chunked, embedded, indexed)
  GET  /api/docs                            -> documents attached to this session
  POST /api/docs/delete  {"id"}             -> remove a document
  POST /api/reset                           -> start a new conversation (documents stay attached)
"""
import json
import logging
import mimetypes
import os
import threading
import time
import urllib.parse
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .assistant import Assistant
from .llm import make_llm
from .rag.docstore import DocStore
from .rag.parse import MAX_BYTES, UnsupportedFile
from .session import HELP
from .store import Store

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
IMG = "/static/img/"

# Illustrative dish photos (cropped from the Nosh website). The dataset has no photos, so a card gets the closest-looking dish.
DISHES = {
    "f1l": ("prawn shrimp seafood fish lobster crab scallop salmon tuna", "dish_f1l.jpg"),
    "f1r": ("cake dessert pudding pie cookie brownie sweet halwa carrot muffin cupcake candy", "dish_f1r.jpg"),
    "f2l": ("chicken wings drumstick turkey", "dish_f2l.jpg"),
    "f2m": ("pasta penne spaghetti macaroni noodle lasagna fettuccine linguine ziti", "dish_f2m.jpg"),
    "f2r": ("butter masala tikka paneer korma gravy cream creamy alfredo", "dish_f2r.jpg"),
    "f3l": ("curry thai green coconut stew chili", "dish_f3l.jpg"),
    "f3m": ("beef pork meatball stir fry kung ribs steak lamb", "dish_f3m.jpg"),
    "f3r": ("soup rice risotto pulao pilaf broth chowder noodles", "dish_f3r.jpg"),
}
ORDER = list(DISHES)


def pick_image(title: str, ingredients: list, used: set | None = None) -> str:
    """Best-looking dish photo for a recipe; photos already used in the same answer are skipped so cards look varied."""
    used = used if used is not None else set()
    words = set((title + " " + " ".join(ingredients[:3])).lower().replace("-", " ").split())
    scored = []
    for k, (kw, f) in DISHES.items():
        s = sum(2 for w in kw.split() if w in words or w + "s" in words or w.rstrip("s") in words) \
            + sum(1 for w in kw.split() if any(w in x for x in ingredients[:6]))
        scored.append((s, f))
    scored.sort(key=lambda t: -t[0])
    neutral = ["f2m", "f2r", "f3r", "f3l", "f3m", "f2l"]   # savoury mains only; never a dessert or seafood photo by chance
    start = zlib.crc32(title.encode()) % len(neutral)
    rotation = [DISHES[neutral[(start + i) % len(neutral)]][1] for i in range(len(neutral))]
    for f in [f for s, f in scored if s > 0] + rotation:
        if f not in used:
            used.add(f)
            return IMG + f
    return IMG + rotation[0]


_STORE = None
_DOCS = None
_LLM = None
_ASSISTANTS: dict = {}
_ENGINE_LOCK = threading.Lock()   # the recipe engine shares one SQLite connection -> serialise engine calls only


def get_assistant(sid: str) -> Assistant:
    if sid not in _ASSISTANTS:
        _ASSISTANTS[sid] = Assistant(_STORE, _DOCS, _LLM, sid, _ENGINE_LOCK)
    return _ASSISTANTS[sid]


def decorate(cards, results):
    used: set = set()
    out = []
    for i, c in enumerate(cards or [], 1):
        c = dict(c)
        c["index"] = results.index(c["rid"]) + 1 if c["rid"] in results else i
        c["image"] = pick_image(c["title"], c["ingredients"], used)
        out.append(c)
    return out


class H(BaseHTTPRequestHandler):
    def _sid(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid" and v:
                self._new_sid = None
                return v, False
        sid = uuid.uuid4().hex
        self._new_sid = sid
        return sid, True

    def _send(self, code, body: bytes, ctype: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if getattr(self, "_new_sid", None) and "Set-Cookie" not in (extra or {}):
            self.send_header("Set-Cookie", f"sid={self._new_sid}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf8"), "application/json")

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        sid, new = self._sid()
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = open(os.path.join(STATIC, "index.html"), encoding="utf8").read()
            html = html.replace("%TOTAL%", f"{_STORE.total:,}").replace("%HELP%", json.dumps(HELP))
            return self._send(200, html.encode("utf8"), "text/html; charset=utf-8",
                              {"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Strict"} if new else None)
        if path == "/api/docs":
            return self._json({"docs": _DOCS.list(sid)})
        if path == "/favicon.ico":
            return self._send(200, open(os.path.join(STATIC, "favicon.ico"), "rb").read(), "image/x-icon")
        if path.startswith("/static/"):
            full = os.path.normpath(os.path.join(STATIC, path[len("/static/"):]))
            if full.startswith(STATIC) and os.path.isfile(full):
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                return self._send(200, open(full, "rb").read(), ctype, {"Cache-Control": "no-cache"})
        self._send(404, b"not found", "text/plain")

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        sid, _ = self._sid()
        n = int(self.headers.get("Content-Length") or 0)
        path = self.path.split("?")[0]
        if path == "/api/upload":
            return self._upload(sid, n)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad request"}, 400)
        if path == "/api/reset":
            get_assistant(sid).reset()
            return self._json({"ok": True})
        if path == "/api/docs/delete":
            return self._json({"ok": _DOCS.remove(sid, data.get("id", ""))})
        if path == "/api/chat/stream":
            return self._stream(sid, data.get("message", ""))
        if path == "/api/chat":
            reply, cards, lead, sources, details = "", [], "", [], ""
            a = get_assistant(sid)
            for ev in a.respond(data.get("message", "")):
                if ev["t"] == "delta":
                    reply += ev["x"]
                elif ev["t"] == "cards":
                    cards = decorate(ev["cards"], a.session.results)
                elif ev["t"] == "sources":
                    sources = ev["items"]
                elif ev["t"] == "done":
                    details = ev.get("details", "")
            return self._json({"reply": reply, "cards": cards, "sources": sources, "details": details, "lead": lead})
        self._json({"error": "not found"}, 404)

    def _stream(self, sid, message):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        if getattr(self, "_new_sid", None):
            self.send_header("Set-Cookie", f"sid={self._new_sid}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        a = get_assistant(sid)

        def emit(obj):
            self.wfile.write((json.dumps(obj) + "\n").encode("utf8"))
            self.wfile.flush()
        try:
            for ev in a.respond(message):
                if ev["t"] == "cards":
                    ev = {"t": "cards", "cards": decorate(ev["cards"], a.session.results)}
                emit(ev)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # keep the server alive
            try:
                emit({"t": "delta", "x": f"\n\nSorry, something went wrong: {e}"})
                emit({"t": "done", "answer": "", "details": ""})
            except Exception:
                pass

    def _upload(self, sid, n):
        name = urllib.parse.unquote(self.headers.get("X-Filename") or "upload.txt")
        name = os.path.basename(name.replace("\\", "/"))[:120] or "upload.txt"
        if n > MAX_BYTES:
            self.rfile.read(0)
            return self._json({"error": f"File is larger than {MAX_BYTES // (1024 * 1024)} MB."}, 413)
        body = self.rfile.read(n)
        t0 = time.time()
        try:
            info = _DOCS.add(sid, name, body)
        except UnsupportedFile as e:
            return self._json({"error": f"I couldn't read {name}: {e}."}, 400)
        except Exception as e:
            return self._json({"error": f"Something went wrong while reading {name}: {e}"}, 500)
        info["seconds"] = round(time.time() - t0, 1)
        self._json({"doc": info})

    def log_message(self, *a):
        pass


def main():
    global _STORE, _DOCS, _LLM
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    _STORE, _LLM, _DOCS = Store(), make_llm(), DocStore()
    _DOCS.purge()
    port = int(os.environ.get("PORT", "8000"))
    mode = f"{_LLM.provider} ({_LLM.main})" if _LLM else "offline (no API key)"
    print(f"Nosh Assistant on http://127.0.0.1:{port}  | {_STORE.total:,} recipes | LLM: {mode} | embeddings: {_DOCS.emb.name}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main()
