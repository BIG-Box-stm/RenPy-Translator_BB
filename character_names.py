# -*- coding: utf-8 -*-
"""
Определение отображаемых имён персонажей по их коду (тому же, что стоит
перед репликой в .rpy, например "sm") — нужно движку Translate Gemma,
чтобы передавать модели настоящее имя персонажа (для контекста/пола
говорящего), а не короткий внутренний код, который разработчик игры мог
придумать как угодно.

Ren'Py обычно объявляет персонажей строкой вида:
    define sm = Character("Summer", color="#FFCC1F")
но НЕ всегда в одном и том же файле (часто game/script.rpy, но единого
стандарта нет). Поэтому вместо того чтобы полагаться на конкретное имя
файла, программа один раз перед началом перевода сканирует ВЕСЬ game/
(кроме game/tl — там уже переводы, не объявления) и строит словарь
{код_персонажа: настоящее_имя}.

Особые случаи:
- "Character(None)" — имени нет (рассказчик/системный голос) — такой
  код в словарь не попадает.
- имя со вставкой переменной вида "[player_name]" — раскрыть в реальное
  значение нельзя (оно вычисляется во время игры), такие имена
  заменяются на литеральное "Player" (по опыту пользователя, это почти
  всегда протагонист с именем, которое задаёт сам игрок).
- реплика без говорящего вообще (пустой "who", описания/литературные
  отступления) — получает "Narrator".
"""

import os
import re

_RE_CHARACTER_DEF = re.compile(
    r'^[ \t]*(?:define|default)?[ \t]*(\w+)[ \t]*=[ \t]*Character[ \t]*\([ \t]*'
    r'(?:(?P<q>["\'])(?P<name>(?:\\.|(?!(?P=q)).)*)(?P=q)|None\b)',
    re.MULTILINE,
)

PLAYER_PLACEHOLDER = "Player"
NARRATOR_PLACEHOLDER = "Narrator"


def _iter_source_rpy_files(game_dir):
    for root, dirs, files in os.walk(game_dir):
        # game/tl — это уже переводы, не объявления персонажей.
        dirs[:] = [d for d in dirs if d.lower() != "tl"]
        for name in files:
            if name.lower().endswith(".rpy"):
                yield os.path.join(root, name)


def scan_character_names(game_dir):
    """Сканирует все *.rpy в game_dir (кроме game/tl) на определения
    персонажей вида 'define код = Character("Имя", ...)'. Возвращает
    dict {код: имя}. Первое найденное определение кода побеждает (если
    он почему-то объявлен в нескольких местах)."""
    result = {}
    for path in _iter_source_rpy_files(game_dir):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        for m in _RE_CHARACTER_DEF.finditer(text):
            code = m.group(1)
            name = m.group("name")
            if name is None:
                continue  # Character(None) — сознательно без имени
            if "[" in name:
                name = PLAYER_PLACEHOLDER
            result.setdefault(code, name)
    return result


def resolve_speaker_name(who, names):
    """who — как его возвращает core.find_translatable() (пустая строка,
    один код, несколько токенов через пробел — код плюс атрибуты спрайта
    вида "k happy -glasses", либо целая строка в кавычках для
    динамического имени). names — словарь из scan_character_names().
    Возвращает имя для поля RULES запроса к Translate Gemma."""
    who = (who or "").strip()
    if not who:
        return NARRATOR_PLACEHOLDER
    if who[0] in "\"'":
        # Динамическое имя — это уже сама строка-говорящий, буквально;
        # снимаем внешние кавычки и возвращаем как есть.
        return who[1:-1] if len(who) >= 2 and who[-1] == who[0] else who
    code = who.split()[0]
    return names.get(code, code)
