"""LLM-бэкенды. Системный промпт роли = тело файла .claude/agents/<role>.md. Ответ — один JSON-объект."""
import hashlib
import json
import os
import threading
import time

import requests

from . import config
from .config import AGENTS_DIR, GEMINI_BG, GEMINI_HALL


class LLMError(Exception):
    pass


_EVENTS: dict[str, list] = {"gemini": []}   # (время, ok) — давление лимитов
_EV_LOCK = threading.Lock()


def mark(backend: str, ok: bool) -> None:
    with _EV_LOCK:
        ev = _EVENTS.setdefault(backend, [])
        ev.append((time.time(), ok))
        del ev[:-200]


def pressure(window_s: float = 1800, now: float | None = None) -> dict[str, bool]:
    """Лимит почти исчерпан: за последние 30 минут не меньше 3 отказов по лимитам и отказов больше, чем успехов."""
    now = now or time.time()
    out = {}
    with _EV_LOCK:
        for b, ev in _EVENTS.items():
            recent = [ok for t, ok in ev if now - t <= window_s]
            fails = recent.count(False)
            out[b] = fails >= 3 and fails > recent.count(True)
    return out


class LLMUnavailable(LLMError):
    """Лимит/квота исчерпаны или сервис недоступен: роль пропускается (abstain), цикл не падает."""


# ---------- бюджет Gemini на сутки (UTC) и «пауза» после 429 ----------
DAILY_BUDGET = int(os.environ.get("GEMINI_DAILY_BUDGET", 450))   # HTTP-запросов в сутки на все роли
BG_SHARE, MISC_SHARE = 0.25, 0.12                                # фоновым отделам и мелочи (пересказы/вычитка сообщений) — не больше этих долей; остальное (≥63%) — залу
BUDGET_FILE = config.STATE_DIR / "logs" / "gemini_budget.json"
_BUD_LOCK = threading.Lock()
_DOWN = {"until": 0.0, "since": 0.0}      # Gemini ушёл на паузу после 429: пока она идёт, запросы не отправляются


def role_group(role: str) -> str:
    return "hall" if role in GEMINI_HALL else "bg" if role in GEMINI_BG else "misc"


def _budget_load() -> dict:
    today = time.strftime("%F", time.gmtime())
    try:
        b = json.loads(BUDGET_FILE.read_text())
        if b.get("day") == today:
            return b
    except (OSError, ValueError):
        pass
    return {"day": today, "hall": 0, "bg": 0, "misc": 0}


def budget_allow(role: str) -> bool:
    with _BUD_LOCK:
        b = _budget_load()
    group = role_group(role)
    if sum(b[k] for k in ("hall", "bg", "misc")) >= DAILY_BUDGET:
        return False
    return group == "hall" or b[group] < DAILY_BUDGET * (BG_SHARE if group == "bg" else MISC_SHARE)


def budget_spend(role: str) -> None:
    with _BUD_LOCK:
        b = _budget_load()
        b[role_group(role)] += 1
        try:
            BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
            BUDGET_FILE.write_text(json.dumps(b))
        except OSError:
            pass


def gemini_down() -> bool:
    return time.time() < _DOWN["until"]


def gemini_pause(seconds: float) -> None:
    now = time.time()
    if now >= _DOWN["until"]:
        _DOWN["since"] = now
    _DOWN["until"] = max(_DOWN["until"], now + seconds)


def status() -> dict:
    """Gemini на паузе / сколько запросов бюджета потрачено сегодня."""
    with _BUD_LOCK:
        b = _budget_load()
    return {"gemini_down": gemini_down(), "since": _DOWN["since"], "fallback_used": dict(_FALLBACK_USED),
            "budget_used": sum(b[k] for k in ("hall", "bg", "misc")), "budget": DAILY_BUDGET}


def reset_state() -> None:
    """Для тестов."""
    _DOWN.update(until=0.0, since=0.0)


def load_agent(role: str) -> str:
    text = (AGENTS_DIR / f"{role}.md").read_text()
    parts = text.split("---", 2)
    return (parts[2] if len(parts) == 3 else text).strip()  # без frontmatter


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

    def ask(self, role: str, payload: dict, system: str | None = None) -> dict:
        if os.environ.get("GEMINI_DISABLED") == "1":   # проверка запасных моделей: Gemini выключен принудительно
            raise LLMUnavailable("gemini отключён (GEMINI_DISABLED=1)")
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError("нет GEMINI_API_KEY")
        if gemini_down():
            raise LLMUnavailable("gemini на паузе после 429")
        if not budget_allow(role):
            raise LLMUnavailable(f"gemini: суточный бюджет ({role_group(role)}) исчерпан")
        body = {"systemInstruction": {"parts": [{"text": system or load_agent(role)}]},
                "contents": [{"role": "user", "parts": [{"text": _prompt(payload)}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
        r = None
        for model in self.MODELS:  # дневная free-tier квота модели исчерпана → следующая модель
            for delay in (0, 15, 30):  # поминутная квота/пик спроса → backoff
                time.sleep(delay)
                with _Limited.sem:
                    self._pace()
                    if not budget_allow(role):   # ретраи тоже тратят бюджет — проверяем на каждый запрос
                        raise LLMUnavailable(f"gemini: суточный бюджет ({role_group(role)}) исчерпан")
                    budget_spend(role)
                    try:
                        r = self.s.post(self.URL.format(model), params={"key": key}, json=body, timeout=self.timeout)
                    except requests.RequestException as ex:
                        raise LLMError(f"gemini сеть: {type(ex).__name__}") from ex
                if r.status_code not in (429, 503) or "PerDay" in r.text:
                    break
            if r.status_code == 200:
                break
        if r.status_code != 200:
            err = LLMUnavailable if r.status_code in (429, 503) else LLMError
            if err is LLMUnavailable:
                mark("gemini", False)
                gemini_pause(3 * 3600 if "PerDay" in r.text else 600 if r.status_code == 429 else 120)
            raise err(f"gemini {r.status_code}: {r.text[:150]}")
        mark("gemini", True)
        try:
            return parse_json(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, ValueError) as ex:
            raise LLMError(f"gemini пустой/странный ответ: {r.text[:120]}") from ex


UNAVAILABLE_TEXT = "аналитики недоступны: лимит моделей"


class OpenAICompatBackend:
    """Бесплатные запасные модели с OpenAI-совместимым API (GitHub Models, Groq, OpenRouter free, Mistral, Cerebras). Ключ — переменная окружения (секрет GitHub); нет ключа — провайдер молча пропускается.
    429/5xx/сеть → LLMUnavailable и пауза провайдера (Retry-After или 10 минут); модель не найдена/отклонена → следующая модель того же провайдера."""
    PROVIDERS = {
        "github": ("https://models.github.ai/inference/chat/completions", "GITHUB_MODELS_TOKEN", "GITHUB_MODELS", "openai/gpt-4o-mini,meta/Llama-3.3-70B-Instruct", True),
        "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", "GROQ_MODELS", "llama-3.3-70b-versatile,llama-3.1-8b-instant", True),
        "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY", "OPENROUTER_MODELS", "meta-llama/llama-3.3-70b-instruct:free,google/gemma-3-27b-it:free", False),
        "mistral": ("https://api.mistral.ai/v1/chat/completions", "MISTRAL_API_KEY", "MISTRAL_MODELS", "mistral-small-latest,open-mistral-nemo", True),
        "cerebras": ("https://api.cerebras.ai/v1/chat/completions", "CEREBRAS_API_KEY", "CEREBRAS_MODELS", "llama-3.3-70b,llama3.1-8b", True),
    }
    _lock, _last = threading.Lock(), {}
    _pause: dict[str, float] = {}

    def __init__(self, name: str, session=None, timeout: int = 60, min_interval: float | None = None):
        self.name, self.s, self.timeout = name, session or requests.Session(), timeout
        self.url, self.key_env, self.models_env, self.default_models, self.json_mode = self.PROVIDERS[name]
        self.min_interval = float(os.environ.get("FALLBACK_MIN_INTERVAL", 2)) if min_interval is None else min_interval

    @property
    def key(self) -> str:
        k = os.environ.get(self.key_env, "")
        return k or (os.environ.get("GITHUB_TOKEN", "") if self.name == "github" else "")   # GitHub Models: встроенный токен Actions (permissions: models: read)

    def models(self) -> list[str]:
        return [m.strip() for m in os.environ.get(self.models_env, self.default_models).split(",") if m.strip()]

    def available(self) -> bool:
        return bool(self.key) and time.time() >= self._pause.get(self.name, 0.0)

    def ask(self, role: str, payload: dict, system: str | None = None) -> dict:
        if not self.key:
            raise LLMUnavailable(f"{self.name}: нет ключа {self.key_env}")
        if time.time() < self._pause.get(self.name, 0.0):
            raise LLMUnavailable(f"{self.name}: на паузе после 429")
        sysmsg = system or load_agent(role)
        last = None
        for model in self.models():
            body = {"model": model, "temperature": 0.2, "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": _prompt(payload)}]}
            if self.json_mode:
                body["response_format"] = {"type": "json_object"}
            for attempt in (0, 1):
                with self._lock:
                    wait = self._last.get(self.name, 0.0) + self.min_interval - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    self._last[self.name] = time.monotonic()
                try:
                    r = self.s.post(self.url, headers={"Authorization": f"Bearer {self.key}"}, json=body, timeout=self.timeout)
                except requests.RequestException as ex:
                    self._pause[self.name] = time.time() + 120
                    raise LLMUnavailable(f"{self.name}: сеть {type(ex).__name__}") from ex
                if r.status_code == 400 and attempt == 0 and "response_format" in body:   # модель не умеет JSON-режим — повтор без него
                    body.pop("response_format")
                    continue
                break
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    ra = float(r.headers.get("retry-after", 0))
                except (TypeError, ValueError, AttributeError):
                    ra = 0.0
                self._pause[self.name] = time.time() + (min(ra, 3600) if ra else 600)
                mark(self.name, False)
                raise LLMUnavailable(f"{self.name} {r.status_code}: {r.text[:100]}")
            if r.status_code != 200:     # неизвестная модель / отказ — пробуем следующую модель провайдера
                bad = f"{self.name}/{model} {r.status_code}: {r.text[:100]}"
                last = f"{last}; {bad}" if last else bad
                continue
            try:
                text = r.json()["choices"][0]["message"]["content"]
                out = parse_json(text)
            except (KeyError, IndexError, ValueError, LLMError) as ex:
                bad = f"{self.name}/{model}: странный ответ ({type(ex).__name__}: {str(locals().get('text', r.text))[:60]!r})"
                last = f"{last}; {bad}" if last else bad
                continue
            mark(self.name, True)
            _FALLBACK_USED[self.name] = _FALLBACK_USED.get(self.name, 0) + 1
            return out
        raise LLMError(last or f"{self.name}: нет моделей")


_FALLBACK_USED: dict[str, int] = {}   # сколько ответов дали запасные провайдеры за жизнь процесса (для отчётов)


def fallback_chain(session=None) -> list:
    """Запасные провайдеры в порядке приоритета; без ключа в окружении пропускаются при вызове."""
    return [OpenAICompatBackend(n, session=session) for n in ("github", "groq", "cerebras", "mistral", "openrouter")]


CACHE_TTL = 5400   # 1,5 ч: одинаковый запрос (новости/макро без изменений) повторно не отправляем


class RoleRouter:
    """Все роли → Gemini; при лимите/сбое → запасные бесплатные модели (GitHub Models → Groq → Cerebras → Mistral → OpenRouter free) с автопереключением при 429.
    Не ответил никто → LLMUnavailable('аналитики недоступны: лимит моделей'). Кэш одинаковых запросов в памяти процесса."""
    def __init__(self, gemini=None, now_fn=time.time, fallbacks=None):
        self.gemini, self.now = gemini or GeminiBackend(), now_fn
        self.fallbacks = fallback_chain() if fallbacks is None else fallbacks
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _key(self, role, payload, system):
        return hashlib.sha1(json.dumps([role, payload, system], sort_keys=True, default=str).encode()).hexdigest()

    def ask(self, role, payload, system=None):
        k, t = self._key(role, payload, system), self.now()
        with self._lock:
            hit = self._cache.get(k)
        if hit and t - hit[0] < CACHE_TTL:
            return hit[1]
        try:
            out = self.gemini.ask(role, payload, system=system)
        except LLMError as first:   # лимит Gemini (429 / бюджет / пауза) или сбой — идём по запасным моделям
            out, errs = None, [str(first)[:80]]
            for fb in self.fallbacks:
                try:
                    out = fb.ask(role, payload, system=system)
                    break
                except LLMError as ex:
                    errs.append(str(ex)[:80])
            if out is None:
                raise LLMUnavailable(f"{UNAVAILABLE_TEXT} ({'; '.join(errs)})"[:300]) from first
        with self._lock:
            self._cache[k] = (t, out)
            if len(self._cache) > 400:
                for old in sorted(self._cache, key=lambda x: self._cache[x][0])[:100]:
                    del self._cache[old]
        return out


class FakeBackend:
    """Для тестов: handler(role, payload) -> dict | исключение."""
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def ask(self, role, payload, system=None):
        self.calls.append(role)
        return self.handler(role, payload)


def make_backend(name: str = "router"):
    return {"router": RoleRouter, "gemini": GeminiBackend}[name]()
