# -*- coding: utf-8 -*-
"""
Локализация интерфейса (язык окон, кнопок, редактора и журнала).

Как это устроено. В коде все видимые пользователю тексты записаны по-русски
и обёрнуты в tr("..."): русский текст служит ключом. Переводы лежат в
папке lang/ рядом с программой — по файлу на язык: lang/en.json, lang/zh.json,
lang/es.json, lang/pt.json (русский — язык «по умолчанию», файла не требует).
Формат файла — обычный JSON-словарь {"русский текст": "перевод"}. Пустое
значение ("") означает «ещё не переведено»: для такой строки программа берёт
английский вариант, а если и его нет — исходный русский текст.

Как добавить или поправить перевод: откройте lang/<код>.json в любом
текстовом редакторе, впишите перевод справа от нужной строки (части вида
{0}, {1}, \n и «кавычки» оставляйте как есть) и перезапустите программу
(или переключите язык в окне ещё раз). Как добавить новый язык: создайте
lang/<код>.json (скопируйте en.json и переведите значения) и добавьте язык в
UI_LANGUAGES ниже.

mark("...") — пометка строки для переводчиков без перевода «на месте»: так
помечены тексты, которые хранятся в списках/константах и переводятся в момент
показа через tr(...).
"""

import json
import locale
import os

LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")

# (название на самом языке, код). Русский — язык исходных текстов.
UI_LANGUAGES = [
    ("Русский", "ru"),
    ("English", "en"),
    ("中文（简体）", "zh"),
    ("Español", "es"),
    ("Português (Brasil)", "pt"),
]
SOURCE_LANGUAGE = "ru"

_current = SOURCE_LANGUAGE
_tables = {}


def available_codes():
    return [code for _name, code in UI_LANGUAGES]


def detect_system_language():
    """Язык системы, если он среди поддерживаемых, иначе английский."""
    # Windows: язык интерфейса системы (младшие 10 бит LANGID — основной язык).
    try:
        import ctypes
        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        code = {0x19: "ru", 0x09: "en", 0x04: "zh", 0x0A: "es", 0x16: "pt"}.get(primary)
        if code:
            return code
    except Exception:
        pass
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            loc = (locale.getdefaultlocale()[0] or "").lower()
    except Exception:
        loc = ""
    for code in available_codes():
        if loc.startswith(code):
            return code
    return "en"


def _load_table(code):
    path = os.path.join(LANG_DIR, code + ".json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and v.strip()}


def set_language(code):
    """Переключает язык и заново читает файлы переводов с диска."""
    global _current
    _tables.clear()
    _current = code if code in available_codes() else SOURCE_LANGUAGE


def get_language():
    return _current


def _table(code):
    if code not in _tables:
        _tables[code] = _load_table(code)
    return _tables[code]


def tr(text):
    """Перевод строки на текущий язык интерфейса. Нет перевода — берётся
    английский, нет и его — остаётся исходный русский текст."""
    if _current == SOURCE_LANGUAGE or not text:
        return text
    value = _table(_current).get(text)
    if value:
        return value
    if _current != "en":
        value = _table("en").get(text)
        if value:
            return value
    return text


def mark(text):
    """Пометка строки для перевода (сам перевод — через tr() при показе)."""
    return text
