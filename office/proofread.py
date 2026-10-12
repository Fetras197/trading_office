"""Вычитка текста для Telegram: детерминированная чистка + отдельный шаг через Gemini (запятые, числительные, ё, бык/медведь).
Результат Gemini принимается только если числа и структура не изменились и не появилось латиницы; иначе уходит исходный текст."""
import difflib
import hashlib
import re

from .llm import GeminiBackend, LLMError

ROLE_WORDS = {"risk_manager": "Риск-менеджер", "portfolio_manager": "Управляющий", "fundamentals": "Фундаментальный анализ",
              "market": "Технический анализ", "news": "Новости", "quant": "Квант-аналитик", "trader": "Трейдер", "bull": "Бык", "bear": "Медведь"}
SYSTEM = """Ты корректор и редактор русских уведомлений торговой фирмы. Получишь JSON {"text": "..."}.
Верни {"text": "..."} — тот же текст, но исправленный: орфография, запятые, буква ё (всё, ещё, её, неё...), согласование числительных с существительными
(1 стратегия, 3 стратегии, 5 стратегий, 94 стратегии, 21 сделка, 2 позиции), падежи, «бык/медведь» (не «покупатель/продавец»).
Ты НЕ переписываешь: исправляй только орфографию, запятые, ё, падежи и согласование. Не заменяй слова синонимами, не переставляй и не удаляй слова — особенно «не», «нет», «нельзя», «без», «покупка», «продажа», «стоп-лосс», «тейк-профит».
СТРОГО: не меняй и не добавляй цифры, знаки + − % $, переводы строк, эмодзи в начале строк, названия тикеров (BTC, ONDO). Не добавляй новых фактов, не сокращай.
Английские слова и коды ролей замени на понятные русские. Причины — не длиннее 10 слов (если длиннее — сократи, сохранив смысл). Без обрывков и многоточий."""
_NUM = re.compile(r"\d+")
_LAT = re.compile(r"[A-Za-z][A-Za-z_]+")
_CACHE: dict[str, str] = {}
_TOK = re.compile(r"[\w\-+−]+", re.U)
NEG = {"не", "нет", "ни", "нельзя", "без", "запрещены", "запрещено", "запрещён", "запрещен"}
KEY = {"покупка", "продажа", "покупать", "продавать", "купили", "продали", "стоп-лосс", "тейк-профит", "бык", "быка", "медведь", "медведя", "паника", "пауза"}


def sanitize(text: str) -> str:
    """Детерминированно: коды ролей -> русские названия, «#123» убрать, пробелы."""
    for k, v in ROLE_WORDS.items():
        text = re.sub(rf"\b{k}\b", v, text, flags=re.I)
    text = re.sub(r"\s*\(?#\d+\)?", "", text)
    text = re.sub(r"\b([A-Z0-9]{2,6})/USD\b", r"\1", text)      # «ONDO/USD» -> «ONDO»
    text = re.sub(r"\bSPY\b", "S&P 500", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _ok(src: str, out: str) -> bool:
    if not isinstance(out, str) or not out.strip():
        return False
    if _NUM.findall(src) != _NUM.findall(out):
        return False
    if not (0.6 <= len(out) / max(len(src), 1) <= 1.4) or src.count("\n") != out.count("\n"):
        return False
    a, b = [t.lower() for t in _TOK.findall(src)], [t.lower() for t in _TOK.findall(out)]
    if [t for t in a if t in NEG] != [t for t in b if t in NEG]:     # потеря «не» переворачивает смысл
        return False
    if sorted(t for t in a if t in KEY) != sorted(t for t in b if t in KEY):
        return False
    matched = sum(m.size for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    if len(a) - matched > max(3, 0.2 * len(a)):                      # вычитка, а не переписывание: не более 3 слов (или 20%) изменено
        return False
    new_lat = {w.lower() for w in _LAT.findall(out)} - {w.lower() for w in _LAT.findall(src)}
    return not new_lat and "#" not in out


def proofread(text: str, backend=None) -> str:
    text = sanitize(text)
    key = hashlib.md5(text.encode()).hexdigest()
    if key in _CACHE:
        return _CACHE[key]
    try:
        out = (backend or GeminiBackend()).ask("proofread", {"text": text}, system=SYSTEM).get("text")
    except (LLMError, AttributeError):
        return text  # Gemini недоступен: отправляем чистый шаблон (он уже по-русски)
    res = out.strip() if _ok(text, out) else text
    if len(_CACHE) > 500:
        _CACHE.clear()
    _CACHE[key] = res
    return res
