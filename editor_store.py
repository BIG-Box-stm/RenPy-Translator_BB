# -*- coding: utf-8 -*-
"""
Данные «Редактора переводов»: поиск строк в файлах game/tl/<язык>, запись
правок обратно в файлы и два списка строк, защищённых от перезаписи при
следующем автопереводе (даже если включена галочка «Перезаписывать уже
переведённые строки»):

  * «переведено вручную» — строки, перевод которых вписан в редакторе;
  * «перевод не требуется» — строки, которые должны остаться как в
    оригинале (например "60" или "{color=#f00}[item.name]{/color}"):
    текст не меняется, но строка больше не числится непереведённой и
    автоперевод её не трогает.

Без Tkinter, чтобы логику можно было проверять отдельно от окна
(само окно — ui_editor.py).

Защита хранится в lang_dir/_manual_edits.json:
    {"files": {"scripts/city.rpy": ["d:start_7c722cef#1", "s:Yes", ...]},
     "skip":  {"scripts/city.rpy": ["s:60", ...]}}
Ключ строки (entry['key'] из core.find_translatable()): для диалога —
"d:<id блока translate>#<номер>", для old/new — "s:<оригинал>".
Старые файлы без раздела "skip" читаются как есть.
"""

import json
import os
import re

import character_names
import core
import gemma4_translate
import project

MANUAL_FILENAME = "_manual_edits.json"

# «Система»: файлы перевода со служебными строками интерфейса (меню, Save/
# Load/Quit и т.п.). Сравнение — по имени файла, без учёта регистра и папок.
SYSTEM_FILES = {"common.rpy", "screens.rpy", "gui.rpy", "options.rpy"}

# Языки перевода с не-латинским письмом: там латиница в переводе — признак
# непереведённого текста (для немецкого, французского и т.п. это норма).
NON_LATIN_DEST = {"ru", "uk", "zh", "zh-cn", "zh-tw", "ja", "ko", "ar", "hi", "th"}

# Пороги проверки «Подозрительные» (знаки препинания сознательно НЕ
# сравниваются: запятые, тире и кавычки в переводе законно отличаются от
# оригинала).
LONGER_RATIO = 2.2      # перевод длиннее оригинала более чем во столько раз
SHORTER_RATIO = 0.3     # ...или короче менее чем во столько раз
MIN_LEN_FOR_RATIO = 25  # на более коротких строках длину не сравниваем
LATIN_SHARE = 0.5       # доля латинских букв (вне тегов), выше которой перевод подозрителен


def list_files(lang_dir):
    """Все файлы перевода языковой папки: *.rpy плюс файл имён
    персонажей (он не .rpy, но переводится так же — см. character_names)."""
    files = project.find_rpy_files(lang_dir)
    names_path = os.path.join(lang_dir, character_names.NAMES_FILENAME)
    if os.path.isfile(names_path) and names_path not in files:
        files.append(names_path)
    return files


def rel_of(lang_dir, path):
    return os.path.relpath(path, lang_dir).replace(os.sep, "/")


def is_system_rel(rel):
    return os.path.basename(rel).lower() in SYSTEM_FILES


def is_names_rel(rel):
    return os.path.basename(rel) == character_names.NAMES_FILENAME


def display_path(lang_folder, rel):
    """Путь для столбца «Файл»: начиная с папки tl."""
    return "tl\\{0}\\{1}".format(lang_folder, rel.replace("/", "\\"))


# ---------------------------------------------------------------------------
# Защищённые строки (ручные правки и «перевод не требуется»)
# ---------------------------------------------------------------------------

def _read_raw(lang_dir):
    """(manual, skip): оба — {rel-путь файла: set(ключей)}."""
    path = os.path.join(lang_dir, MANUAL_FILENAME)
    if not os.path.isfile(path):
        return {}, {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}, {}

    def conv(section):
        return {rel: set(keys) for rel, keys in (section or {}).items()}

    return conv(data.get("files")), conv(data.get("skip"))


def _write_raw(lang_dir, manual, skip):
    path = os.path.join(lang_dir, MANUAL_FILENAME)
    data = {
        "files": {rel: sorted(keys) for rel, keys in manual.items() if keys},
        "skip": {rel: sorted(keys) for rel, keys in skip.items() if keys},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def load_manual(lang_dir):
    """{rel-путь файла: set(ключей)} — переведено вручную."""
    return _read_raw(lang_dir)[0]


def load_skip(lang_dir):
    """{rel-путь файла: set(ключей)} — отмечено «перевод не требуется»."""
    return _read_raw(lang_dir)[1]


def save_manual(lang_dir, manual):
    _manual, skip = _read_raw(lang_dir)
    _write_raw(lang_dir, manual, skip)


def load_protected(lang_dir):
    """{rel-путь файла: set(ключей)} — ВСЕ строки, которые автоперевод не
    должен трогать (ручные правки + «перевод не требуется»). Именно это
    передаётся в core как protected."""
    manual, skip = _read_raw(lang_dir)
    result = {rel: set(keys) for rel, keys in manual.items()}
    for rel, keys in skip.items():
        result.setdefault(rel, set()).update(keys)
    return result


# ---------------------------------------------------------------------------
# Строки
# ---------------------------------------------------------------------------

def suspicious_reasons(orig, cur, speaker=None, known=None, dest_code=None):
    """Список причин, по которым готовый перевод выглядит подозрительно
    (пустой список — всё в порядке). Проверяются: теги/подстановки/\\n
    (core.tag_problems), оставшийся обрывок промпта, «Имя:» говорящего в
    начале перевода, перевод почти целиком латиницей (только для языков с
    не-латинским письмом), перевод сильно длиннее или короче оригинала."""
    cur_s = cur.strip()
    if not cur_s or cur == orig:
        return []     # непереведённые строки живут в фильтре «Не переведённые»
    reasons = []
    problems = core.tag_problems(orig, cur)
    if problems:
        reasons.append("теги/подстановки: " + "; ".join(problems))
    if any(m in cur for m in ("[CURRENT_SOURCE]", "[PREVIOUS_SOURCE]", "[PREVIOUS_TRANSLATION]")):
        reasons.append("в переводе остался обрывок промпта")
    stripped, learned = gemma4_translate.split_speaker(cur, speaker, orig, known) if speaker else (cur, None)
    if learned:
        reasons.append("в начале осталось имя говорящего («{0}:»)".format(learned))
    elif not speaker and ":" not in orig and "：" not in orig:
        if gemma4_translate._RE_NAME_PREFIX.match(cur):
            reasons.append("возможно, в начале осталось имя говорящего")
    if (dest_code or "").lower() in NON_LATIN_DEST:
        text = core._RE_PROTECT.sub(" ", cur)
        latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
        letters = sum(1 for ch in text if ch.isalpha())
        if letters >= 4 and latin / letters > LATIN_SHARE:
            reasons.append("почти весь перевод латиницей — возможно, не переведено")
    if len(orig) >= MIN_LEN_FOR_RATIO:
        if len(cur) > LONGER_RATIO * len(orig):
            reasons.append("перевод намного длиннее оригинала")
        elif len(cur) < SHORTER_RATIO * len(orig):
            reasons.append("перевод намного короче оригинала (обрыв?)")
    return reasons


def scan(lang_dir, manual=None, skip=None, dest_code=None):
    """Все переводимые строки языковой папки. Элемент списка — dict:
    rel, key, kind ('old_new'/'dialogue'), who (имя говорящего для
    диалогов, иначе ""), orig (текст внутри кавычек, как в файле), cur
    (текущий перевод, как в файле), untranslated (перевода нет: пусто либо
    совпадает с оригиналом, и строка не защищена), manual (правлена
    вручную), skip (отмечена «перевод не требуется»), suspicious (список
    причин, см. suspicious_reasons(); для ручных и отмеченных строк пуст),
    note (те же причины одной строкой)."""
    if manual is None or skip is None:
        file_manual_all, file_skip_all = _read_raw(lang_dir)
        manual = file_manual_all if manual is None else manual
        skip = file_skip_all if skip is None else skip

    # Имена персонажей: lang_dir = <игра>/game/tl/<язык>.
    game_dir = os.path.dirname(os.path.dirname(os.path.normpath(lang_dir)))
    names = {}
    try:
        if os.path.isdir(game_dir):
            names = character_names.scan_character_names(game_dir)
    except Exception:
        names = {}
    known_map = {}
    try:
        for e in character_names.parse_names_file(
                os.path.join(lang_dir, character_names.NAMES_FILENAME)):
            if e["new"] and e["new"] != e["old"]:
                known_map.setdefault(e["old"], set()).add(e["new"])
    except Exception:
        pass

    rows = []
    for path in list_files(lang_dir):
        rel = rel_of(lang_dir, path)
        try:
            lines = project.read_lines(path)
        except Exception:
            continue
        file_manual = manual.get(rel, set())
        file_skip = skip.get(rel, set())
        protected = file_manual | file_skip
        for entry in core.find_translatable(lines):
            if not core.needs_translation(entry['orig_quoted']):
                continue
            is_dialogue = entry['type'] == 'dialogue'
            who_name = character_names.resolve_speaker_name(entry['who'], names) if is_dialogue else ""
            speaker = character_names.speaker_label_for_gender(entry['who'], names) if is_dialogue else None
            orig = entry['orig_quoted'][1:-1]
            cur = entry['current_inner']
            is_protected = entry['key'] in protected
            reasons = [] if is_protected else suspicious_reasons(
                orig, cur, speaker=speaker,
                known=(known_map.get(speaker.split(" (")[0]) if speaker else None),
                dest_code=dest_code)
            rows.append({
                "rel": rel,
                "key": entry['key'],
                "kind": entry['type'],
                "who": who_name,
                "orig": orig,
                "cur": cur,
                "untranslated": core._is_untranslated(entry, False, protected),
                "manual": entry['key'] in file_manual,
                "skip": entry['key'] in file_skip,
                "suspicious": reasons,
                "note": "; ".join(reasons),
            })
    return rows


def normalize(value):
    """Приводит введённый вручную текст к виду, допустимому внутри
    кавычек .rpy. Возвращает (текст, None) либо (None, сообщение об
    ошибке)."""
    value = value.strip()
    if not value:
        return None, "Пустой перевод не применяется."
    if "\n" in value or "\r" in value:
        return None, "Перевод должен быть в одну строку (перенос строки в игре — \\n)."
    value = re.sub(r'(?<!\\)"', r'\\"', value)
    if core._ends_with_odd_backslash(value):
        return None, "Строка не может заканчиваться одиночным \\ — он сломает кавычку."
    return value, None


def _set_inner(out, entry, inner):
    """Записывает inner (текст внутри кавычек) в строку перевода,
    найденную core.find_translatable(), — в списке строк out."""
    if entry['type'] == 'old_new':
        eol = core.get_line_ending(out[entry['new_line_idx']])
        out[entry['new_line_idx']] = '{0}new "{1}"{2}'.format(entry['new_indent'], inner, eol)
    else:
        who_prefix = "{0} ".format(entry['who']) if entry['who'] else ""
        eol = core.get_line_ending(out[entry['code_line_idx']])
        out[entry['code_line_idx']] = '{0}{1}"{2}"{3}'.format(
            entry['code_indent'], who_prefix, inner, eol)


def apply_edits(lang_dir, edits, log=None):
    """edits: {rel: {key: новый_текст_внутри_кавычек}}. Записывает правки в
    файлы и отмечает строки как переведённые вручную (если строка была
    отмечена «перевод не требуется» — эта отметка снимается). Возвращает
    число реально применённых строк."""
    log = log or (lambda msg: None)
    manual, skip = _read_raw(lang_dir)
    applied = 0
    for rel, file_edits in edits.items():
        path = os.path.join(lang_dir, *rel.split("/"))
        try:
            lines = project.read_lines(path)
        except Exception as e:
            log("  {0}: ОШИБКА чтения ({1})".format(rel, e))
            continue
        out = list(lines)
        done = set()
        for entry in core.find_translatable(out):
            key = entry['key']
            if key not in file_edits or key in done:
                continue
            _set_inner(out, entry, file_edits[key])
            done.add(key)
        if not done:
            log("  {0}: строки не найдены (файл изменился?) — пропущено.".format(rel))
            continue
        try:
            project.write_lines(path, out)
        except Exception as e:
            log("  {0}: ОШИБКА записи ({1})".format(rel, e))
            continue
        manual.setdefault(rel, set()).update(done)
        if rel in skip:
            skip[rel] -= done
        applied += len(done)
    _write_raw(lang_dir, manual, skip)
    return applied


def mark_skip(lang_dir, items, log=None):
    """items: {rel: set(ключей)}. Отмечает строки «перевод не требуется»:
    в файле строка остаётся (или становится) равной оригиналу — если там
    было пусто или другой текст, туда записывается оригинал; строка
    защищается от автоперевода и перестаёт считаться непереведённой.
    Возвращает число отмеченных строк."""
    log = log or (lambda msg: None)
    manual, skip = _read_raw(lang_dir)
    marked = 0
    for rel, keys in items.items():
        path = os.path.join(lang_dir, *rel.split("/"))
        try:
            lines = project.read_lines(path)
        except Exception as e:
            log("  {0}: ОШИБКА чтения ({1})".format(rel, e))
            continue
        out = list(lines)
        found = set()
        changed = False
        for entry in core.find_translatable(out):
            key = entry['key']
            if key not in keys or key in found:
                continue
            orig_inner = entry['orig_quoted'][1:-1]
            if entry['current_inner'] != orig_inner:
                _set_inner(out, entry, orig_inner)
                changed = True
            found.add(key)
        if not found:
            log("  {0}: строки не найдены (файл изменился?) — пропущено.".format(rel))
            continue
        if changed:
            try:
                project.write_lines(path, out)
            except Exception as e:
                log("  {0}: ОШИБКА записи ({1})".format(rel, e))
                continue
        skip.setdefault(rel, set()).update(found)
        if rel in manual:
            manual[rel] -= found
        marked += len(found)
    _write_raw(lang_dir, manual, skip)
    return marked


def unmark_skip(lang_dir, items):
    """items: {rel: set(ключей)}. Снимает отметку «перевод не требуется»
    (текст в файле не меняется: строка снова станет непереведённой и
    будет предложена автопереводу). Возвращает число снятых отметок."""
    manual, skip = _read_raw(lang_dir)
    removed = 0
    for rel, keys in items.items():
        have = skip.get(rel, set())
        hit = have & set(keys)
        if hit:
            skip[rel] = have - hit
            removed += len(hit)
    _write_raw(lang_dir, manual, skip)
    return removed
