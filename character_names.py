# -*- coding: utf-8 -*-
"""
Определение отображаемых имён персонажей по их коду (тому же, что стоит
перед репликой в .rpy, например "sm") — используется в двух местах:

1. Движком Translate Gemma — чтобы передавать модели настоящее имя
   персонажа (для контекста/пола говорящего) вместо короткого
   внутреннего кода, который разработчик игры мог придумать как угодно
   (см. scan_character_names()/resolve_speaker_name()).

2. Галочкой «Применить локализованные имена» в GUI — имена персонажей (и, где
   переопределён свой шрифт для имени, шрифт тоже) можно временно
   подменить в самих исходниках игры на переведённые и вернуть обратно
   в любой момент (см. update_character_names_file()/
   parse_names_file()/apply_character_names()).

Ren'Py обычно объявляет персонажей строкой вида:
    define sm = Character("Summer", color="#FFCC1F")
но НЕ всегда в одном и том же файле (часто game/script.rpy, но единого
стандарта нет). Поэтому вместо того чтобы полагаться на конкретное имя
файла, программа сканирует ВЕСЬ game/ (кроме game/tl — там уже переводы,
не объявления).

Что распознаётся: define/default/"$ x = ..." и простое присваивание в
init python; Character, ADVCharacter, NVLCharacter; имя в виде "Имя",
'Имя' и _("Имя") (с переводом через _()); вызов на нескольких строках и с
комментарием после закрывающей скобки.

Особые случаи при извлечении имени:
- "Character(None)" — имени нет (рассказчик/системный голос) — такой
  код никуда не попадает, переводить нечего.
- имя со вставкой переменной вида "[player_name]" — раскрыть в реальное
  значение нельзя (оно вычисляется во время игры). Для RULES движка
  Translate Gemma такое имя заменяется на литеральное "Player" (почти
  всегда это протагонист с именем, которое задаёт сам игрок); для файла
  переводимых имён (пункт 2 выше) такой персонаж пропускается целиком —
  подставлять в игру фиксированный текст вместо динамического имени
  нельзя.
- реплика без говорящего вообще (пустой "who", описания/литературные
  отступления) — для RULES получает "Narrator".
"""

from i18n import tr
import os
import re

# Начало определения персонажа. Сам вызов (до закрывающей скобки) разбирается
# отдельно (_find_call_end): он может занимать несколько строк и иметь
# комментарий после себя.
_RE_CHARACTER_START = re.compile(
    r'^[ \t]*(?:(?:define|default)[ \t]+(?:-?\d+[ \t]+)?|\$[ \t]*)?(?P<code>\w+)[ \t]*=[ \t]*'
    r'(?:ADV|NVL)?Character[ \t]*\(',
    re.MULTILINE,
)
# Первый позиционный аргумент: "Имя", 'Имя' либо _("Имя").
_RE_ARG_NAME = re.compile(
    r'^\s*(?P<wrap>_\(\s*)?(?P<q>["\'])(?P<name>(?:\\.|(?!(?P=q)).)*)(?P=q)(?(wrap)\s*\)|)'
)
_RE_WHO_FONT = re.compile(r'\bwho_font\s*=\s*(?P<q>["\'])(?P<font>(?:\\.|(?!(?P=q)).)*)(?P=q)')


def _skip_string(text, i):
    """text[i] — открывающая кавычка строки. Возвращает индекс сразу после
    закрывающей кавычки или None, если строка не закрыта."""
    q = text[i]
    n = len(text)
    if text.startswith(q * 3, i):
        j = i + 3
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text.startswith(q * 3, j):
                return j + 3
            j += 1
        return None
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == q:
            return j + 1
        if c == "\n":
            return None
        j += 1
    return None


def _find_call_end(text, open_idx):
    """text[open_idx] == "(". Индекс парной ")" с учётом вложенных скобок,
    строк и комментариев "#" — либо None, если вызов не закрыт."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            i = _skip_string(text, i)
            if i is None:
                return None
            continue
        if c == "#":
            nl = text.find("\n", i)
            if nl == -1:
                return None
            i = nl
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
            if depth < 0:
                return None
        i += 1
    return None


def _parse_character_defs(text):
    """Все определения персонажей с ЗАДАННЫМ строкой именем в тексте файла.
    Элемент: code, name (как в файле, внутри кавычек), quote (какими кавычками
    записано имя), wrapped (имя обёрнуто в _()), name_span (начало, конец имени
    в text), who_font (внутри кавычек или None), font_span, line (номер строки,
    с 1). Character(None), имя не строкой (переменная, kind=...) и пустое имя
    пропускаются."""
    result = []
    for m in _RE_CHARACTER_START.finditer(text):
        open_idx = m.end() - 1
        close_idx = _find_call_end(text, open_idx)
        if close_idx is None:
            continue
        base = open_idx + 1
        args = text[base:close_idx]
        nm = _RE_ARG_NAME.match(args)
        if not nm or not nm.group("name"):
            continue
        rest = args[nm.end():].lstrip()
        if rest and rest[0] not in ",#":
            continue                      # например "A" + "B" — не просто строка
        fm = _RE_WHO_FONT.search(args, nm.end())
        result.append({
            "code": m.group("code"),
            "name": nm.group("name"),
            "quote": nm.group("q"),
            "wrapped": bool(nm.group("wrap")),
            "name_span": (base + nm.start("name"), base + nm.end("name")),
            "who_font": fm.group("font") if fm else None,
            "font_span": (base + fm.start("font"), base + fm.end("font")) if fm else None,
            "line": text.count("\n", 0, m.start()) + 1,
        })
    return result


def _plain_name(name):
    """Имя как обычный текст: снимает экранирование кавычек (D\\'Arcy -> D'Arcy)."""
    return re.sub(r'\\(["\'])', r'\1', name)


def _convert_quote(inner, from_q, to_q):
    """Переписывает содержимое строки, записанной кавычками from_q, под кавычки
    to_q (экранирует/снимает экранирование кавычек)."""
    if from_q == to_q:
        return inner
    plain = inner.replace("\\" + from_q, from_q)
    return re.sub(r'(?<!\\)' + re.escape(to_q), lambda m: "\\" + to_q, plain)


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
    персонажей вида 'define код = Character("Имя", ...)' (в том числе
    _("Имя"), NVLCharacter, вызов на нескольких строках). Возвращает
    dict {код: имя}. Первое найденное определение кода побеждает (если
    он почему-то объявлен в нескольких местах)."""
    result = {}
    for path in _iter_source_rpy_files(game_dir):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        for d in _parse_character_defs(text):
            name = _plain_name(d["name"])
            if "[" in name:
                name = PLAYER_PLACEHOLDER
            result.setdefault(d["code"], name)
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


# ---------------------------------------------------------------------------
# Часть 2: файл переводимых имён персонажей + подмена имени/шрифта в самих
# исходниках игры (чек-бокс «Переводить имена» в GUI).
# ---------------------------------------------------------------------------

NAMES_FILENAME = "_character_names.txt"
# Более ранняя версия программы использовала именно это имя (с .rpy) —
# см. migrate_legacy_names_file() ниже про то, почему это было ошибкой и
# как для уже существующих проектов сохраняются уже накопленные переводы.
LEGACY_NAMES_FILENAME = "_character_names.rpy"


def migrate_legacy_names_file(lang_dir, log=None):
    """Более ранняя версия программы клала файл имён прямо как
    "_character_names.rpy" внутрь game/tl/<язык>/ — но выяснилось, что
    Ren'Py парсит ЛЮБОЙ файл с расширением .rpy внутри game/ как часть
    сценария игры (даже в папке tl), а служебная строка "who_font=..."
    внутри него — не валидный синтаксис Ren'Py, поэтому игра падала с
    "Parsing the script failed.". Расширение поменяли на .txt — такие
    файлы Ren'Py вообще не трогает. У уже существующих проектов старый
    .rpy-файл мог быть частично переведён, поэтому вместо того чтобы
    просто начинать заново, эта функция один раз переносит его
    содержимое на новое имя (и тем самым убирает из game/tl/ — исправляя
    и сам краш игры). Возвращает True, если перенос произошёл."""
    log = log or (lambda msg: None)
    legacy_path = os.path.join(lang_dir, LEGACY_NAMES_FILENAME)
    new_path = os.path.join(lang_dir, NAMES_FILENAME)
    if not os.path.isfile(legacy_path):
        return False
    if os.path.isfile(new_path):
        # Оба файла почему-то есть — не угадываем, какой актуален,
        # просто предупреждаем и ничего не трогаем.
        log(
            tr("  Внимание: найдены оба файла имён — старый {0} и новый "
            "{1}. Оставляю как есть, разберитесь вручную, какой "
            "актуален (скорее всего можно удалить старый {0}).")
            .format(LEGACY_NAMES_FILENAME, NAMES_FILENAME)
        )
        return False
    try:
        os.rename(legacy_path, new_path)
    except Exception as e:
        log(tr("  ОШИБКА переноса {0} -> {1}: {2}").format(LEGACY_NAMES_FILENAME, NAMES_FILENAME, e))
        return False
    log(
        tr("  Старый {0} (Ren'Py пытался парсить его как часть сценария и "
        "падал с ошибкой \"Parsing the script failed.\") перенесён в "
        "{1} — уже переведённые имена сохранены, файла с расширением "
        ".rpy в tl/ больше нет.").format(LEGACY_NAMES_FILENAME, NAMES_FILENAME)
    )
    return True

# Заголовок одной записи в файле имён: "# путь/к/файлу.rpy:18  code=Hippo".
# Номер строки — только подсказка для человека, при подмене программа
# заново ищет определение персонажа по коду (см. _patch_character_line),
# а не доверяет слепо сохранённому номеру строки (файл-источник мог
# измениться между извлечением и применением).
_RE_NAME_ENTRY_HEADER = re.compile(
    r'^#\s*(?P<file>\S+):(?P<line>\d+)\s+code=(?P<code>\w+)\s*$'
)
_RE_NAME_OLD = re.compile(r'^\s*old\s+"((?:\\.|[^"\\])*)"\s*$')
_RE_NAME_NEW = re.compile(r'^\s*new\s+"((?:\\.|[^"\\])*)"\s*$')
_RE_NAME_WHO_FONT_LINE = re.compile(r'^\s*who_font=(.+?)\s*$')


def _scan_character_definitions(game_dir):
    """Как scan_character_names(), но для каждого персонажа возвращает
    полную запись: код, оригинальное имя (в виде, пригодном для строки в двойных
    кавычках), свой шрифт имени (who_font, если задан — иначе None), файл,
    номер строки и признак wrapped (имя записано как _("...")). Пропускает
    Character(None) и имена со вставкой переменной ("[...]") — там нечего
    подставлять в игру вместо фиксированного текста."""
    result = []
    for path in _iter_source_rpy_files(game_dir):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        rel = os.path.relpath(path, game_dir).replace(os.sep, "/")
        for d in _parse_character_defs(text):
            if "[" in d["name"]:
                continue  # динамическое имя — фиксированного текста для подмены нет
            result.append({
                "code": d["code"],
                "name": _convert_quote(d["name"], d["quote"], '"'),
                "who_font": d["who_font"],
                "file": rel,
                "line": d["line"],
                "wrapped": d["wrapped"],
            })
    return result


def update_character_names_file(game_dir, names_path, log=None):
    """Сканирует game_dir на определения персонажей и ДОПИСЫВАЕТ в
    names_path только те коды, которых там ещё нет — уже существующие
    записи не трогает вообще, даже если сейчас в исходниках лежит уже
    ПОДМЕНЁННОЕ имя/шрифт (после применения чек-бокса): иначе "old" в
    файле перестал бы быть настоящим оригиналом. Возвращает число
    добавленных новых персонажей."""
    log = log or (lambda msg: None)
    existing = parse_names_file(names_path)
    known_codes = {e["code"] for e in existing}

    found = _scan_character_definitions(game_dir)
    # Один код — одна запись (первое определение, как и при подмене).
    new_entries = []
    seen = set(known_codes)
    for d in found:
        if d["code"] in seen:
            continue
        seen.add(d["code"])
        new_entries.append(d)
    if not new_entries:
        log(tr("Новых персонажей не найдено — файл имён не изменился."))
        return 0

    chunk = []
    if existing:
        chunk.append("")
    for d in new_entries:
        chunk.append("# {0}:{1}  code={2}".format(d["file"], d["line"], d["code"]))
        chunk.append('old "{0}"'.format(d["name"]))
        chunk.append('new "{0}"'.format(d["name"]))
        if d["who_font"]:
            chunk.append("who_font={0}".format(d["who_font"]))
        chunk.append("")

    os.makedirs(os.path.dirname(names_path), exist_ok=True)
    mode = "a" if os.path.isfile(names_path) else "w"
    with open(names_path, mode, encoding="utf-8") as f:
        f.write("\n".join(chunk).rstrip("\n") + "\n")

    log(tr("Добавлено новых персонажей в файл имён: {0}.").format(len(new_entries)))
    return len(new_entries)


def parse_names_file(names_path):
    """Читает файл переводимых имён (NAMES_FILENAME). Возвращает список
    dict {code, file, line, old, new, who_font (или None)} в порядке
    появления в файле. Пустой список, если файла ещё нет."""
    if not os.path.isfile(names_path):
        return []
    with open(names_path, "r", encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f]

    entries = []
    i = 0
    n = len(lines)
    while i < n:
        m = _RE_NAME_ENTRY_HEADER.match(lines[i])
        if not m:
            i += 1
            continue
        code, file_rel, line_no = m.group("code"), m.group("file"), int(m.group("line"))
        old = new = who_font = None
        j = i + 1
        while j < n and lines[j].strip():
            mo = _RE_NAME_OLD.match(lines[j])
            mn = _RE_NAME_NEW.match(lines[j])
            mf = _RE_NAME_WHO_FONT_LINE.match(lines[j])
            if mo:
                old = mo.group(1)
            elif mn:
                new = mn.group(1)
            elif mf:
                who_font = mf.group(1)
            j += 1
        if old is not None:
            entries.append({
                "code": code, "file": file_rel, "line": line_no,
                "old": old, "new": new if new is not None else old,
                "who_font": who_font,
            })
        i = j
    return entries


def _patch_character_line(path, code, new_name, new_who_font, log):
    """Заново находит определение персонажа по коду (не по сохранённому номеру
    строки — файл мог измениться) и подменяет в нём имя (первый строковый
    аргумент, в том числе внутри _("...")) и, если new_who_font не None,
    значение who_font=. new_name — в виде строки в двойных кавычках (как в файле
    имён). Вызов может занимать несколько строк. Возвращает True, если файл
    действительно изменился."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    except Exception as e:
        log(tr("  ОШИБКА чтения {0}: {1}").format(path, e))
        return False

    found = None
    for d in _parse_character_defs(text):
        if d["code"] == code:
            found = d
            break
    if found is None:
        log(tr("  {0}: определение персонажа {1!r} не найдено — пропускаю.").format(path, code))
        return False

    edits = [(found["name_span"][0], found["name_span"][1],
              _convert_quote(new_name, '"', found["quote"]))]
    if new_who_font is not None and found["font_span"]:
        edits.append((found["font_span"][0], found["font_span"][1], new_who_font))

    new_text = text
    for start, end, value in sorted(edits, reverse=True):
        new_text = new_text[:start] + value + new_text[end:]
    if new_text == text:
        return False

    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
    except Exception as e:
        log(tr("  ОШИБКА записи {0}: {1}").format(path, e))
        return False
    # Рядом с .rpy почти наверняка лежит скомпилированный .rpyc, оставшийся от
    # предыдущего запуска игры (или сохранённый вместе с .rpy при раскомпиляции).
    # Ren'Py обычно сам замечает, что .rpy новее, и перекомпилирует — но на
    # некоторых системах/сборках этого не происходит надёжно, и тогда игра
    # продолжает показывать СТАРЫЙ вариант строки. Удаляем устаревший .rpyc
    # явно, чтобы Ren'Py гарантированно пересобрал его из свежего .rpy.
    rpyc_path = path[:-4] + ".rpyc" if path.lower().endswith(".rpy") else None
    if rpyc_path and os.path.isfile(rpyc_path):
        try:
            os.remove(rpyc_path)
        except Exception as e:
            log(tr("  ОШИБКА удаления устаревшего {0}: {1}").format(rpyc_path, e))
    return True


def apply_character_names(game_dir, entries, use_translated, light_font_rel=None, log=None):
    """Применяет (use_translated=True) или откатывает (False) подмену
    имён персонажей и, где был задан свой шрифт (who_font), — шрифта
    для этого конкретного имени. entries — результат parse_names_file().

    Имя подменяется, только если оно уже переведено (new != old) — не
    переведённые персонажи получают запись в журнал ("не переведено") и
    остаются с оригинальным именем. ШРИФТ при этом меняется на общий
    запасной (light_font_rel) ВСЕГДА, когда у персонажа задан свой
    who_font и use_translated=True — даже если само имя ещё не
    переведено: иначе часть имён в игре будет уже в новом шрифте, а
    часть — всё ещё в старом декоративном, что выглядит разнобоем.

    Журнал НЕ перечисляет каждого успешно применённого персонажа (при
    полусотне имён это превращалось в стену текста) — только тех, чьё
    имя ещё не переведено, и настоящие ошибки. Итоговую сводку по
    числам вызывающий код может собрать сам из возвращаемого словаря.

    Возвращает {'changed': число персонажей, у которых реально
    изменилась строка в игре, 'untranslated': число персонажей, чьё имя
    ещё не переведено (шрифт у них при этом мог быть заменён)}."""
    log = log or (lambda msg: None)
    stats = {"changed": 0, "untranslated": 0}
    warned_no_light_font = False

    for e in entries:
        code = e["code"]
        is_translated = bool(e["new"]) and e["new"] != e["old"]

        if use_translated:
            target_name = e["new"] if is_translated else e["old"]
        else:
            target_name = e["old"]

        target_who_font = None
        if e["who_font"]:
            if use_translated:
                if light_font_rel:
                    target_who_font = light_font_rel
                elif not warned_no_light_font:
                    log(
                        tr("Запасной шрифт (*-Light.ttf/.otf) не найден в "
                        "папке fonts — шрифты персонажей со своим who_font "
                        "не меняются (текст имени может отображаться "
                        "некорректно).")
                    )
                    warned_no_light_font = True
            else:
                target_who_font = e["who_font"]

        if use_translated and not is_translated:
            stats["untranslated"] += 1
            log(
                tr("Имя {0!r} не переведено — переведите его в {1} и "
                "включите галочку заново.").format(e["old"], NAMES_FILENAME)
            )

        path = os.path.join(game_dir, *e["file"].split("/"))
        if _patch_character_line(path, code, target_name, target_who_font, log):
            stats["changed"] += 1

    return stats


def speaker_label_for_gender(who, names, player_gender="male"):
    """Подпись говорящего для режима «Согласование по роду» (Gemma 4):
    настоящее имя персонажа ("Aelira"), "Player (male)" для игрока
    (динамическое имя из [..]) и None — когда имени нет (рассказчик,
    либо код персонажа не найден среди define ... = Character(...)):
    такая реплика уходит без имени, как строки old/new."""
    who = (who or "").strip()
    if not who:
        return None
    if who[0] in "\"'":
        name = who[1:-1] if len(who) >= 2 and who[-1] == who[0] else who
        return name or None
    code = who.split()[0]
    if code not in names:
        return None
    name = names[code]
    if name == PLAYER_PLACEHOLDER:
        return "{0} ({1})".format(PLAYER_PLACEHOLDER, player_gender)
    return name

