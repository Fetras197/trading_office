"""LLM-бэкенды. Системный промпт роли = тело файла .claude/agents/<role>.md. Ответ — один JSON-объект."""
import json
import os
import subprocess
import tempfile
import threading
import time

import requests

from .config import AGENTS_DIR, DEFAULT_CLAUDE_MODEL, ROLE_BACKEND, ROLE_MODELS


class LLMError(Exception):
    pass


def load_agent(role: str) -> str:
    text = (AGENTS_DIR / f"{role}.md").read_text()
    return text.split("---", 2)[2].strip()  # без frontmatter


def parse_json(text: str) -> dict:
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        raise LLMError(f"нет JSON в ответе: {text[:120]!r}")
    try:
        return json.loads(text[s:e + 1])
    except json.JSONDecodeError as ex:
        raise LLMError(f"битый JSON: {ex}") from ex


def _prompt(payload: dict) -> str:
    return "Input data (JSON). Reply with ONE JSON object only.\n" + json.dumps(payload, ensure_ascii=False, default=str)


class _Limited:
    """Ограничитель параллелизма (лимиты API)."""
    sem = threading.Semaphore(3)


class ClaudeBackend:
    def __init__(self, model: str = "", timeout: int = 240):
        self.model, self.timeout = model, timeout
        self.cwd = tempfile.mkdtemp(prefix="to_llm_")  # нейтральная папка: не подхватываем CLAUDE.md проектов

    def ask(self, role: str, payload: dict) -> dict:
        cmd = ["claude", "-p", _prompt(payload), "--system-prompt", load_agent(role), "--tools", "",
               "--output-format", "json", "--no-session-persistence", "--disable-slash-commands",
               "--setting-sources", "", "--strict-mcp-config"]  # без хуков/MCP/CLAUDE.md: 19с и $0.001 вместо 83с и $0.06
        model = self.model or ROLE_MODELS.get(role, DEFAULT_CLAUDE_MODEL)
        cmd += ["--model", model]
        with _Limited.sem:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, cwd=self.cwd)
            except subprocess.TimeoutExpired as ex:
                raise LLMError("таймаут claude") from ex
        if r.returncode != 0:
            raise LLMError(f"claude rc={r.returncode}: {r.stderr[:200]}")
        try:
            outer = json.loads(r.stdout)
        except json.JSONDecodeError as ex:
            raise LLMError(f"не JSON от claude: {r.stdout[:120]!r}") from ex
        if outer.get("is_error"):
            raise LLMError(f"claude error: {str(outer.get('result'))[:200]}")
        return parse_json(outer.get("result", ""))


class GeminiBackend:
    """Gemini REST (ключ GEMINI_API_KEY из .env). JSON-режим ответа."""
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
    MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.5-flash"]  # порядок = приоритет (free tier: дневные лимиты по моделям)

    _lock, _last = threading.Lock(), [0.0]

    def __init__(self, model: str = "gemini-3.5-flash", timeout: int = 120, session=None, min_interval: float | None = None):
        self.model, self.timeout, self.s = model, timeout, session or requests.Session()
        self.min_interval = float(os.environ.get("GEMINI_MIN_INTERVAL", 4)) if min_interval is None else min_interval

    def _pace(self):
        """Глобальный интервал между запросами (по умолч. 4с ≈ 15 RPM), чтобы не ловить 429."""
        with self._lock:
            wait = self._last[0] + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[0] = time.monotonic()

    def ask(self, role: str, payload: dict) -> dict:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError("нет GEMINI_API_KEY")
        body = {"systemInstruction": {"parts": [{"text": load_agent(role)}]},
                "contents": [{"role": "user", "parts": [{"text": _prompt(payload)}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
        r = None
        for model in self.MODELS:  # дневная free-tier квота модели исчерпана → следующая модель
            for delay in (0, 15, 30):  # поминутная квота/пик спроса → backoff
                time.sleep(delay)
                with _Limited.sem:
                    self._pace()
                    try:
                        r = self.s.post(self.URL.format(model), params={"key": key}, json=body, timeout=self.timeout)
                    except requests.RequestException as ex:
                        raise LLMError(f"gemini сеть: {type(ex).__name__}") from ex
                if r.status_code not in (429, 503) or "PerDay" in r.text:
                    break
            if r.status_code == 200:
                break
        if r.status_code != 200:
            raise LLMError(f"gemini {r.status_code}: {r.text[:150]}")
        try:
            return parse_json(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, ValueError) as ex:
            raise LLMError(f"gemini пустой/странный ответ: {r.text[:120]}") from ex


class RoleRouter:
    """Аналитики → Gemini, остальные роли → Claude (ROLE_BACKEND)."""
    def __init__(self, claude=None, gemini=None):
        self.claude, self.gemini = claude or ClaudeBackend(), gemini or GeminiBackend()

    def ask(self, role, payload):
        return (self.gemini if ROLE_BACKEND.get(role) == "gemini" else self.claude).ask(role, payload)


class FakeBackend:
    """Для тестов: handler(role, payload) -> dict | исключение."""
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def ask(self, role, payload):
        self.calls.append(role)
        return self.handler(role, payload)


def make_backend(name: str = "router"):
    return {"router": RoleRouter, "claude": ClaudeBackend, "gemini": GeminiBackend}[name]()
