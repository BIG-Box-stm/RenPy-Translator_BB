# -*- coding: utf-8 -*-
"""
Определение отображаемых имён персонажей по их коду (тому же, что стоит
перед репликой в .rpy, например "sm") — используется в двух местах:

1. Движком Translate Gemma — чтобы передавать модели настоящее имя
   персонажа (для контекста/пола говорящего) вместо короткого
   внутреннего кода, который разработчик игры мог придумать как угодно
   (см. scan_character_names()/resolve_speaker_name()).

2. Чек-боксом «Переводить имена» в GUI — имена персонажей (и, где
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
            "  Внимание: найдены оба файла имён — старый {0} и новый "
            "{1}. Оставляю как есть, разберитесь вручную, какой "
            "актуален (скорее всего можно удалить старый {0})."
            .format(LEGACY_NAMES_FILENAME, NAMES_FILENAME)
        )
        return False
    try:
        os.rename(legacy_path, new_path)
    except Exception as e:
        log("  ОШИБКА переноса {0} -> {1}: {2}".format(LEGACY_NAMES_FILENAME, NAMES_FILENAME, e))
        return False
    log(
        "  Старый {0} (Ren'Py пытался парсить его как часть сценария и "
        "падал с ошибкой \"Parsing the script failed.\") перенесён в "
        "{1} — уже переведённые имена сохранены, файла с расширением "
        ".rpy в tl/ больше нет.".format(LEGACY_NAMES_FILENAME, NAMES_FILENAME)
    )
    return True

# В отличие от scan_character_names() (только код+имя), здесь нужен ещё
# сам аргумент "who_font=..." целиком (если есть) — чтобы знать, какой
# шрифт вернуть обратно при отключении галочки. Поэтому вместо поиска
# только начала вызова разбираем весь список аргументов Character(...)
# одной строкой (то же допущение, что и у _RE_CHARACTER_DEF: вызов
# укладывается в одну строку — на практике так почти всегда и есть).
_RE_CHARACTER_DEF_FULL = re.compile(
    r'^[ \t]*(?:define|default)?[ \t]*(?P<code>\w+)[ \t]*=[ \t]*Character[ \t]*\('
    r'(?P<args>.*)\)[ \t]*$',
    re.MULTILINE,
)
_RE_ARG_NAME = re.compile(r'^\s*(?P<q>["\'])(?P<name>(?:\\.|(?!(?P=q)).)*)(?P=q)')
_RE_WHO_FONT = re.compile(r'who_font\s*=\s*(?P<q>["\'])(?P<font>(?:\\.|(?!(?P=q)).)*)(?P=q)')

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
    полную запись: код, оригинальное имя, свой шрифт имени (who_font,
    если задан — иначе None), файл и номер строки. Пропускает
    Character(None) и имена со вставкой переменной ("[...]") — там
    нечего подставлять в игру вместо фиксированного текста."""
    result = []
    for path in _iter_source_rpy_files(game_dir):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        rel = os.path.relpath(path, game_dir).replace(os.sep, "/")
        for m in _RE_CHARACTER_DEF_FULL.finditer(text):
            args = m.group("args")
            name_m = _RE_ARG_NAME.match(args.lstrip())
            if not name_m:
                continue  # Character(None) или без строки в начале — пропускаем
            name = name_m.group("name")
            if "[" in name:
                continue  # динамическое имя — фиксированного текста для подмены нет
            font_m = _RE_WHO_FONT.search(args)
            line_no = text.count("\n", 0, m.start()) + 1
            result.append({
                "code": m.group("code"),
                "name": name,
                "who_font": font_m.group("font") if font_m else None,
                "file": rel,
                "line": line_no,
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
    new_entries = [d for d in found if d["code"] not in known_codes]
    if not new_entries:
        log("Новых персонажей не найдено — файл имён не изменился.")
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

    log("Добавлено новых персонажей в файл имён: {0}.".format(len(new_entries)))
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
    """Заново находит строку 'define код = Character(...)' по коду (не
    по сохранённому номеру строки — файл мог измениться) и подменяет в
    ней имя (первый строковый аргумент) и, если new_who_font не None,
    значение who_font=. Возвращает True, если строка найдена и
    изменена."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            lines = f.readlines()
    except Exception as e:
        log("  ОШИБКА чтения {0}: {1}".format(path, e))
        return False

    pattern = re.compile(
        r'^([ \t]*(?:define|default)?[ \t]*' + re.escape(code) +
        r'[ \t]*=[ \t]*Character[ \t]*\()(.*)(\)[ \t]*)$'
    )
    for i, raw in enumerate(lines):
        stripped = raw.rstrip("\r\n")
        eol = raw[len(stripped):]
        m = pattern.match(stripped)
        if not m:
            continue

        prefix, args, suffix = m.group(1), m.group(2), m.group(3)
        name_m = _RE_ARG_NAME.match(args.lstrip())
        if not name_m:
            log("  {0}: не удалось найти имя в определении {1!r} — пропускаю.".format(path, code))
            return False
        lead_ws = args[:len(args) - len(args.lstrip())]
        quote = name_m.group("q")
        escaped = new_name.replace("\\", "\\\\").replace(quote, "\\" + quote)
        new_args = args[:len(lead_ws)] + quote + escaped + quote + args[len(lead_ws) + name_m.end():]

        if new_who_font is not None:
            font_m = _RE_WHO_FONT.search(new_args)
            if font_m:
                fq = font_m.group("q")
                new_args = (
                    new_args[:font_m.start("font")] + new_who_font
                    + new_args[font_m.end("font"):]
                )

        lines[i] = prefix + new_args + suffix + eol
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.writelines(lines)
        except Exception as e:
            log("  ОШИБКА записи {0}: {1}".format(path, e))
            return False
        # Рядом с .rpy почти наверняка лежит скомпилированный .rpyc,
        # оставшийся от предыдущего запуска игры (или сохранённый вместе
        # с .rpy при раскомпиляции). Ren'Py обычно сам замечает, что .rpy
        # новее, и перекомпилирует — но на некоторых системах/сборках
        # этого не происходит надёжно, и тогда игра продолжает
        # показывать СТАРЫЙ (уже неактуальный) вариант строки, как будто
        # наша правка не применилась. Удаляем устаревший .rpyc явно,
        # чтобы Ren'Py гарантированно пересобрал его из свежего .rpy при
        # следующем запуске.
        rpyc_path = path[:-4] + ".rpyc" if path.lower().endswith(".rpy") else None
        if rpyc_path and os.path.isfile(rpyc_path):
            try:
                os.remove(rpyc_path)
            except Exception as e:
                log("  ОШИБКА удаления устаревшего {0}: {1}".format(rpyc_path, e))
        return True

    log("  {0}: определение персонажа {1!r} не найдено — пропускаю.".format(path, code))
    return False


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
                        "Запасной шрифт (*-Light.ttf/.otf) не найден в "
                        "папке fonts — шрифты персонажей со своим who_font "
                        "не меняются (текст имени может отображаться "
                        "некорректно)."
                    )
                    warned_no_light_font = True
            else:
                target_who_font = e["who_font"]

        if use_translated and not is_translated:
            stats["untranslated"] += 1
            log(
                "Имя {0!r} не переведено — переведите его в {1} и "
                "включите галочку заново.".format(e["old"], NAMES_FILENAME)
            )

        path = os.path.join(game_dir, *e["file"].split("/"))
        if _patch_character_line(path, code, target_name, target_who_font, log):
            stats["changed"] += 1

    return stats
