"""Отправить сообщение в Telegram через общий путь офиса (вычитка Gemini + Bot API): python scripts/tg_send.py "текст" [--raw]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from office import config, report  # noqa: E402

config.load_env()
report.notify(sys.argv[1], proof="--raw" not in sys.argv)
