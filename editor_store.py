# -*- coding: utf-8 -*-
"""
Данные «Редактора переводов»: поиск строк в файлах game/tl/<язык>, запись
правок обратно в файлы и список строк, переведённых вручную (они защищены
от перезаписи при следующем автопереводе, даже если включена галочка
«Перезаписывать уже переведённые строки»).

Без Tkinter, чтобы логику можно было проверять отдельно от окна
(само окно — ui_editor.py).

Защита хранится в lang_dir/_manual_edits.json:
    {"files": {"scripts/city.rpy": ["d:start_7c722cef#1", "s:Yes", ...]}}
Ключ строки (entry['key'] из core.find_translatable()): для диалога —
"d:<id блока translate>#<номер>", для old/new — "s:<оригинал>".
"""

import json
import os
import re

import character_names
import core
import project

MANUAL_FILENAME = "_manual_edits.json"


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


def display_path(lang_folder, rel):
    """Путь для столбца «Файл»: начиная с папки tl."""
    return "tl\\{0}\\{1}".format(lang_folder, rel.replace("/", "\\"))


# ---------------------------------------------------------------------------
# Ручные правки
# ---------------------------------------------------------------------------

def load_manual(lang_dir):
    """{rel-путь файла: set(ключей)}. Пустой словарь, если файла нет."""
    path = os.path.join(lang_dir, MANUAL_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {rel: set(keys) for rel, keys in data.get("files", {}).items()}
    except Exception:
        return {}


def save_manual(lang_dir, manual):
    path = os.path.join(lang_dir, MANUAL_FILENAME)
    data = {"files": {rel: sorted(keys) for rel, keys in manual.items() if keys}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Строки
# ---------------------------------------------------------------------------

def scan(lang_dir, manual=None):
    """Все переводимые строки языковой папки. Элемент списка — dict:
    rel, key, kind ('old_new'/'dialogue'), orig (текст внутри кавычек, как
    в файле), cur (текущий перевод, как в файле), untranslated (перевода
    нет: пусто либо совпадает с оригиналом), manual (правлена вручную)."""
    manual = manual if manual is not None else load_manual(lang_dir)
    rows = []
    for path in list_files(lang_dir):
        rel = rel_of(lang_dir, path)
        try:
            lines = project.read_lines(path)
        except Exception:
            continue
        file_manual = manual.get(rel, set())
        for entry in core.find_translatable(lines):
            if not core.needs_translation(entry['orig_quoted']):
                continue
            rows.append({
                "rel": rel,
                "key": entry['key'],
                "kind": entry['type'],
                "orig": entry['orig_quoted'][1:-1],
                "cur": entry['current_inner'],
                "untranslated": core._is_untranslated(entry, False),
                "manual": entry['key'] in file_manual,
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


def apply_edits(lang_dir, edits, log=None):
    """edits: {rel: {key: новый_текст_внутри_кавычек}}. Записывает правки в
    файлы и отмечает строки как переведённые вручную. Возвращает число
    реально применённых строк."""
    log = log or (lambda msg: None)
    manual = load_manual(lang_dir)
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
            inner = file_edits[key]
            if entry['type'] == 'old_new':
                eol = core.get_line_ending(out[entry['new_line_idx']])
                out[entry['new_line_idx']] = '{0}new "{1}"{2}'.format(entry['new_indent'], inner, eol)
            else:
                who_prefix = "{0} ".format(entry['who']) if entry['who'] else ""
                eol = core.get_line_ending(out[entry['code_line_idx']])
                out[entry['code_line_idx']] = '{0}{1}"{2}"{3}'.format(
                    entry['code_indent'], who_prefix, inner, eol)
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
        applied += len(done)
    save_manual(lang_dir, manual)
    return applied
