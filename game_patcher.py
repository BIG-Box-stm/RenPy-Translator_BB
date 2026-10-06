# -*- coding: utf-8 -*-
"""
Подключение шрифтов и выбора языка в самой игре Ren'Py — то, что
раньше приходилось делать руками (кнопка «Обновить шрифты, включить
язык» в окне программы):

1) папка game рядом с программой (в ней game/fonts/...) копируется в
   папку игры с сохранением структуры;
2) в конец game/gui.rpy добавляется блок «translate <язык> python:», в
   котором для этого языка переопределяются шрифты интерфейса;
3) в screen preferences() (game/screens.rpy) сразу после блока с
   «Rollback Side» добавляется блок выбора языка.

Правки текста вынесены в отдельные функции, которые работают только со
строками и не трогают диск, — так их можно проверять отдельно. Обе
правки безопасно повторять: если нужное уже есть в файле, шаг
пропускается, а не дублируется.

Переносы строк (CRLF/LF) и кодировка файлов игры сохраняются как были.
"""

import os
import re
import shutil

# Раньше шрифты были зашиты жёстко (4_LXGWWenKaiTC-Regular/-Bold) — теперь
# программа сама находит нужную пару файлов по универсальному суффиксу
# имени "-Regular.ttf/.otf" и "-Bold.ttf/.otf" где угодно внутри папки
# bundle_dir (обычно это game/fonts/...). Это позволяет подставить в игру
# любой другой шрифт — достаточно положить в game/fonts ровно два файла
# с такими суффиксами, остальное (сопутствующие файлы шрифтов для других
# языков и т.п.) не трогается и не мешает поиску.
_RE_REGULAR_FONT = re.compile(r'-Regular\.(ttf|otf)$', re.IGNORECASE)
_RE_BOLD_FONT = re.compile(r'-Bold\.(ttf|otf)$', re.IGNORECASE)
# "Light" — третий, НЕОБЯЗАТЕЛЬНЫЙ шрифт из того же комплекта. Нужен не
# для общего текста игры (для этого хватает Regular/Bold), а для замены
# ИНДИВИДУАЛЬНЫХ шрифтов конкретных персонажей (who_font=... в
# Character(...)) — такие шрифты часто декоративные/стилизованные и почти
# никогда не поддерживают кириллицу, значит, при переводе имени персонажа
# на русский текст в его собственном шрифте превратится в кракозябры,
# если не подменить и этот шрифт тоже. См. character_names.py.
_RE_LIGHT_FONT = re.compile(r'-Light\.(ttf|otf)$', re.IGNORECASE)


class FontsNotFoundError(Exception):
    """Не удалось однозначно найти пару файлов шрифта (Regular/Bold) в
    папке bundle_dir — либо ни одного подходящего файла, либо больше
    одного кандидата (тогда программа не может сама угадать, какой из
    них нужен)."""


def _find_one_font(bundle_dir, pattern, suffix_label):
    matches = []
    for root, _dirs, files in os.walk(bundle_dir):
        for name in files:
            if pattern.search(name):
                rel = os.path.relpath(os.path.join(root, name), bundle_dir)
                matches.append(rel.replace(os.sep, "/"))
    if not matches:
        raise FontsNotFoundError(
            "В папке {0} не найден файл шрифта с именем вида "
            "\"*-{1}.ttf\" или \"*-{1}.otf\". Положите туда файл шрифта "
            "с таким суффиксом в имени (например, \"MyFont-{1}.ttf\")."
            .format(bundle_dir, suffix_label)
        )
    if len(matches) > 1:
        raise FontsNotFoundError(
            "В папке {0} найдено сразу несколько файлов с суффиксом "
            "\"-{1}\": {2}. Оставьте там только один такой файл — "
            "программа не может сама угадать, какой из них нужен "
            "использовать.".format(bundle_dir, suffix_label, ", ".join(matches))
        )
    return matches[0]


def find_light_font(bundle_dir):
    """Ищет необязательный запасной шрифт с суффиксом "-Light" (та же
    логика, что у _find_one_font, но без исключения при полном
    отсутствии файла — не у каждой игры есть персонажи со своим
    who_font, так что этот шрифт может быть не нужен вовсе). Бросает
    FontsNotFoundError только при НЕОДНОЗНАЧНОСТИ (несколько
    кандидатов) — как и для Regular/Bold, программа не угадывает,
    какой из них нужен. Возвращает относительный путь (с "/") или None,
    если такого файла в bundle_dir просто нет."""
    matches = []
    for root, _dirs, files in os.walk(bundle_dir):
        for name in files:
            if _RE_LIGHT_FONT.search(name):
                rel = os.path.relpath(os.path.join(root, name), bundle_dir)
                matches.append(rel.replace(os.sep, "/"))
    if not matches:
        return None
    if len(matches) > 1:
        raise FontsNotFoundError(
            "В папке {0} найдено сразу несколько файлов с суффиксом "
            "\"-Light\": {1}. Оставьте там только один такой файл."
            .format(bundle_dir, ", ".join(matches))
        )
    return matches[0]


def find_font_files(bundle_dir):
    """Ищет в bundle_dir пару файлов шрифта по суффиксам "-Regular" и
    "-Bold" (расширение .ttf или .otf, регистр не важен). Возвращает
    (regular_rel, bold_rel) — пути относительно bundle_dir с "/" вместо
    os.sep (как их ожидает Ren'Py в gui.rpy). Бросает FontsNotFoundError
    с понятным текстом, если файл не найден или найдено больше одного
    кандидата на роль."""
    regular = _find_one_font(bundle_dir, _RE_REGULAR_FONT, "Regular")
    bold = _find_one_font(bundle_dir, _RE_BOLD_FONT, "Bold")
    return regular, bold

# Коды языков, с которыми работает программа, и их английские названия
# для подписи кнопки в меню игры (так же называет их сам Ren'Py).
LANGUAGE_NAMES = {
    "ru": "Russian",
    "fr": "French",
    "es": "Spanish",
    "zh": "Chinese",
    "en": "English",
}

# Результаты правки текста файла.
DONE = "done"                  # текст изменён, его нужно записать
DONE_APPENDED = "done_appended"  # текст изменён: наш язык добавлен в уже существующий список
ALREADY = "already"            # нужное уже есть — ничего не менять
NO_SCREEN = "no_screen"        # в файле нет screen preferences()
NO_ANCHOR = "no_anchor"        # в этом экране нет вообще ни одного vbox:


def detect_newline(text):
    """Какой перенос строки использует файл (по первому найденному)."""
    idx = text.find("\n")
    if idx > 0 and text[idx - 1] == "\r":
        return "\r\n"
    return "\n"


def _indent_len(line):
    return len(line) - len(line.lstrip(" \t"))


def _leading_ws(line):
    return line[:_indent_len(line)]


# ---------------------------------------------------------------------------
# gui.rpy — шрифты для выбранного языка
# ---------------------------------------------------------------------------

def font_block_lines(lang, regular_font, bold_font):
    return [
        "translate {0} python:".format(lang),
        "",
        '    gui.text_font = "{0}"'.format(regular_font),
        '    gui.button_text_font = "{0}"'.format(bold_font),
        '    gui.name_text_font = "{0}"'.format(bold_font),
        '    gui.interface_text_font = "{0}"'.format(regular_font),
        "",
        '    gui.default_font = "{0}"'.format(regular_font),
        '    gui.name_font = "{0}"'.format(bold_font),
        '    gui.credit_font = "{0}"'.format(regular_font),
        '    gui.main_menu_button_text_font = "{0}"'.format(regular_font),
        '    gui.interface_font = "{0}"'.format(regular_font),
        "",
        '    gui.choice_button_text_font = "{0}"'.format(bold_font),
    ]


def patch_gui_text(text, lang, regular_font, bold_font):
    """Добавляет в конец текста gui.rpy блок шрифтов для языка lang.
    Возвращает (статус, новый_текст). ALREADY — блок для этого языка
    уже есть (свой, добавленный ранее или вручную)."""
    if re.search(r'(?m)^translate\s+{0}\s+python\s*:'.format(re.escape(lang)), text):
        return ALREADY, text
    nl = detect_newline(text)
    if text and not text.endswith(("\n", "\r")):
        text += nl
    # Ровно одна пустая строка между старым содержимым и новым блоком.
    if text and not re.search(r'(?:\r?\n)[ \t]*\r?\n\Z', text):
        text += nl
    return DONE, text + nl.join(font_block_lines(lang, regular_font, bold_font)) + nl


# ---------------------------------------------------------------------------
# screens.rpy — выбор языка в настройках
# ---------------------------------------------------------------------------

def language_selector_lines(lang, name, base_indent, child_indent):
    return [
        base_indent + "vbox:",
        child_indent + 'style_prefix "radio"',
        child_indent + 'label _("Language")',
        child_indent + 'textbutton _("English") action Language(None)',
        child_indent + 'textbutton _("{0}") action Language("{1}")'.format(name, lang),
    ]


def find_existing_language_button(text, lang, name):
    """Возвращает строку-описание того, что найдено (для журнала), либо
    None, если в файле выбора этого языка ещё нет. Ищем и кнопку с
    подписью языка, и любой другой вызов Language("<код>") — например,
    если у игры уже есть свой выбор языка."""
    if re.search(r'''textbutton\s+_\(\s*(["']){0}\1\s*\)'''.format(re.escape(name)), text):
        return 'textbutton _("{0}")'.format(name)
    if re.search(r'''Language\(\s*(["']){0}\1\s*\)'''.format(re.escape(lang)), text):
        return 'Language("{0}")'.format(lang)
    return None


_RE_VBOX_HEADER = re.compile(r'^[ \t]*vbox\s*:\s*(#.*)?$')
# Название блока языка в разных играх пишут то с обёрткой _(...) для
# перевода, то без неё ("Language" — служебное слово интерфейса, не
# реплика, так что оба варианта встречаются).
_RE_LANGUAGE_LABEL = re.compile(r'''label\s+_?\(?\s*(["'])Language\1\)?''')
_RE_LANGUAGE_ACTION = re.compile(r'Language\s*\(')
_RE_EXISTING_TEXTBUTTON_WRAPPED = re.compile(r'''textbutton\s+_\(\s*["']''')


def _vbox_span(lines, header_idx, end):
    """Для vbox:, начинающегося в header_idx, возвращает
    (last_content_idx, child_indent) — индекс последней содержательной
    строки его тела (туда же вставляется новая строка, если понадобится)
    и отступ, которым пользуются его дочерние строки (сам отступ как
    литеральная строка пробелов/табов — какой встретился в файле, такой
    и повторяем; None, если тело пустое)."""
    header_len = _indent_len(lines[header_idx])
    last_content = header_idx
    child_indent = None
    for k in range(header_idx + 1, end):
        line = lines[k]
        if not line.strip():
            continue
        if _indent_len(line) <= header_len:
            break
        if child_indent is None:
            child_indent = _leading_ws(line)
        last_content = k
    return last_content, child_indent


def patch_screens_text(text, lang, name):
    """Вставляет выбор языка в screen preferences(). Порядок попыток:

    1. Ищет уже существующий vbox с выбором языка — по строке
       label "Language"/_("Language") или по любому действию
       Language(...) внутри него — и добавляет туда ОДНУ строку с нашим
       языком, подстраиваясь под уже используемый в этом файле стиль
       (тот же отступ — теми же символами, что у соседних строк; та же
       обёртка _(...) вокруг подписи кнопки, если ею пользуются другие
       кнопки в этом же блоке, и без нее, если нет).
    2. Если такого блока нет вообще — вставляет свой собственный полный
       блок (см. language_selector_lines) сразу после ПОСЛЕДНЕГО vbox:
       в этом экране, какой бы он ни был. Раньше вместо этого требовался
       конкретно блок с label "Rollback Side" — это было привязкой к
       структуре одной конкретной игры, которая есть далеко не у каждой.

    Возвращает (статус, новый_текст_или_описание):
      DONE_APPENDED — наш язык добавлен в уже существующий список языков;
      DONE          — вставлен новый блок выбора языка целиком;
      ALREADY       — выбор этого языка уже есть, второй элемент — что
                      именно найдено (строка для журнала);
      NO_SCREEN     — в файле нет screen preferences(), файл не менялся;
      NO_ANCHOR     — в этом экране вообще нет ни одного vbox:, не знаем,
                      куда вставлять, файл не менялся.
    """
    found = find_existing_language_button(text, lang, name)
    if found:
        return ALREADY, found

    nl = detect_newline(text)
    lines = text.splitlines(keepends=True)

    start = None
    for i, line in enumerate(lines):
        if re.match(r'screen\s+preferences\s*\(', line):
            start = i
            break
    if start is None:
        return NO_SCREEN, text

    # Экран заканчивается на первой непустой строке без отступа
    # (следующий верхнеуровневый оператор).
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line[0] in " \t" and not line.lstrip().startswith("#"):
            end = i
            break

    vbox_headers = [i for i in range(start, end) if _RE_VBOX_HEADER.match(lines[i])]
    if not vbox_headers:
        return NO_ANCHOR, text

    # --- Попытка №1: свой список языков у игры уже есть — дописываем
    # туда, а не заводим второй, дублирующий блок. -----------------------
    for header_idx in vbox_headers:
        last_content, child_indent = _vbox_span(lines, header_idx, end)
        body = "".join(lines[header_idx + 1:last_content + 1])
        if not (_RE_LANGUAGE_LABEL.search(body) or _RE_LANGUAGE_ACTION.search(body)):
            continue
        if child_indent is None:
            child_indent = _leading_ws(lines[header_idx]) + "    "
        wrapped = bool(_RE_EXISTING_TEXTBUTTON_WRAPPED.search(body))
        if wrapped:
            new_line = child_indent + 'textbutton _("{0}") action Language("{1}")'.format(name, lang)
        else:
            new_line = child_indent + 'textbutton "{0}" action Language("{1}")'.format(name, lang)
        if not lines[last_content].endswith(("\n", "\r")):
            lines[last_content] += nl
        lines[last_content + 1:last_content + 1] = [new_line + nl]
        return DONE_APPENDED, "".join(lines)

    # --- Попытка №2: своего блока с языком нет — вставляем целиком новый,
    # сразу после последнего vbox: в этом экране (было: обязательно после
    # блока с "Rollback Side", что есть не у каждой игры). ----------------
    header_idx = vbox_headers[-1]
    last_content, child_indent = _vbox_span(lines, header_idx, end)
    header_indent = _leading_ws(lines[header_idx])
    if child_indent is None:
        child_indent = header_indent + "    "

    if not lines[last_content].endswith(("\n", "\r")):
        lines[last_content] += nl

    new_block = [nl]  # пустая строка перед новым блоком
    new_block += [
        l + nl for l in language_selector_lines(lang, name, header_indent, child_indent)
    ]
    lines[last_content + 1:last_content + 1] = new_block
    return DONE, "".join(lines)


# ---------------------------------------------------------------------------
# Работа с файлами
# ---------------------------------------------------------------------------

def read_text(path):
    """Читает файл как UTF-8 БЕЗ преобразования переносов строк (и с
    сохранением BOM, если он был) — чтобы при записи обратно файл не
    менялся нигде, кроме нашей вставки."""
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def write_text(path, text):
    with open(path, "wb") as f:
        f.write(text.encode("utf-8"))


def copy_bundle(src_game_dir, dst_game_dir, log=None):
    """Копирует содержимое папки game рядом с программой в папку game
    игры, сохраняя структуру (game/fonts/... -> <игра>/game/fonts/...).
    Уже существующие файлы с теми же именами перезаписываются. Возвращает
    число скопированных файлов."""
    log = log or (lambda msg: None)
    count = 0
    for root, _dirs, files in os.walk(src_game_dir):
        rel = os.path.relpath(root, src_game_dir)
        target_dir = dst_game_dir if rel == "." else os.path.join(dst_game_dir, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(root, name), os.path.join(target_dir, name))
            count += 1
    return count


def missing_fonts(game_dir, regular_font, bold_font):
    """Список шрифтов (из пары regular_font/bold_font), на которые
    ссылается блок в gui.rpy, но которых нет в папке игры."""
    result = []
    for rel in (regular_font, bold_font):
        if not os.path.isfile(os.path.join(game_dir, *rel.split("/"))):
            result.append(rel)
    return result


class PatchTargetError(Exception):
    """Не удалось однозначно найти файл, который нужно поправить (gui.rpy
    или screens.rpy) — либо его нет нигде в game/, либо найдено больше
    одного кандидата с таким именем в разных подпапках."""


def find_game_file(game_dir, filename):
    """Ищет файл с именем filename (без учёта регистра) где угодно
    внутри game_dir, кроме game/tl (там переводы, не исходники игры).
    Многие игры кладут gui.rpy/screens.rpy не в корень game/, а в свою
    произвольную подпапку (например game/scripts/GUI/) — поэтому вместо
    жёсткого os.path.join(game_dir, filename) ищем по всему дереву.
    Возвращает полный путь, либо None, если файла нигде нет. Бросает
    PatchTargetError, если найдено больше одного совпадения — программа
    не гадает, какой из них настоящий."""
    matches = []
    target = filename.lower()
    for root, dirs, files in os.walk(game_dir):
        dirs[:] = [d for d in dirs if d.lower() != "tl"]
        for name in files:
            if name.lower() == target:
                matches.append(os.path.join(root, name))
    if not matches:
        return None
    if len(matches) > 1:
        raise PatchTargetError(
            "Найдено больше одного файла {0}: {1}. Программа не может "
            "сама угадать, какой из них настоящий — уберите лишний или "
            "разберитесь вручную.".format(filename, ", ".join(matches))
        )
    return matches[0]


def apply_all(bundle_dir, game_dir, lang, log=None):
    """Выполняет все три шага. Пишет ход работы в log(msg). Возвращает
    словарь со статусом каждого шага: 'fonts', 'gui', 'screens' — одно
    из 'done' / 'already' / 'skipped' / 'error'."""
    log = log or (lambda msg: None)
    name = LANGUAGE_NAMES[lang]
    result = {"fonts": "error", "gui": "skipped", "screens": "skipped"}

    # --- 0. определяем, какая пара файлов шрифта лежит в bundle_dir ----
    # (ищем по суффиксам "-Regular"/"-Bold" в имени файла — так это
    # работает для любого шрифта, а не только для одного зашитого).
    try:
        regular_font, bold_font = find_font_files(bundle_dir)
    except FontsNotFoundError as e:
        log(str(e))
        return result
    log("Найдена пара шрифтов: {0} / {1}.".format(regular_font, bold_font))

    # --- 1. шрифты -----------------------------------------------------
    log("1/3. Копирую шрифты в папку игры...")
    try:
        count = copy_bundle(bundle_dir, game_dir, log=log)
    except Exception as e:
        log("  ОШИБКА копирования: {0}".format(e))
        return result
    log("  Скопировано файлов: {0} (в {1}).".format(count, game_dir))
    missing = missing_fonts(game_dir, regular_font, bold_font)
    if missing:
        log(
            "  ОШИБКА: после копирования в игре нет шрифтов, на которые "
            "должен ссылаться gui.rpy: {0}. Дальше не иду, чтобы не "
            "сломать игру ссылкой на несуществующий шрифт.".format(", ".join(missing))
        )
        return result
    result["fonts"] = "done"

    # --- 2. gui.rpy ------------------------------------------------------
    log("2/3. Подключаю шрифты в gui.rpy (язык: {0})...".format(lang))
    try:
        gui_path = find_game_file(game_dir, "gui.rpy")
    except PatchTargetError as e:
        log("  ОШИБКА: {0}".format(e))
        gui_path = None
        result["gui"] = "error"
    if gui_path is None and result.get("gui") != "error":
        log(
            "  gui.rpy не найден нигде в папке game — шаг пропущен. Если у "
            "игры остался только gui.rpyc, сначала нажмите «Подготовить "
            "игру»."
        )
    elif gui_path is not None:
        try:
            status, new_text = patch_gui_text(read_text(gui_path), lang, regular_font, bold_font)
            if status == ALREADY:
                log(
                    "  В gui.rpy уже есть блок «translate {0} python:» — "
                    "шаг пропущен, файл не менялся.".format(lang)
                )
                result["gui"] = "already"
            else:
                write_text(gui_path, new_text)
                log("  В конец gui.rpy добавлен блок «translate {0} python:».".format(lang))
                result["gui"] = "done"
        except Exception as e:
            log("  ОШИБКА при правке gui.rpy: {0}".format(e))
            result["gui"] = "error"

    # --- 3. screens.rpy ----------------------------------------------------
    log("3/3. Добавляю выбор языка в настройки (screens.rpy)...")
    try:
        screens_path = find_game_file(game_dir, "screens.rpy")
    except PatchTargetError as e:
        log("  ОШИБКА: {0}".format(e))
        screens_path = None
        result["screens"] = "error"
    if screens_path is None and result.get("screens") != "error":
        log(
            "  screens.rpy не найден нигде в папке game — шаг пропущен. "
            "Если у игры остался только screens.rpyc, сначала нажмите "
            "«Подготовить игру»."
        )
    elif screens_path is not None:
        try:
            status, payload = patch_screens_text(read_text(screens_path), lang, name)
            if status == DONE_APPENDED:
                write_text(screens_path, payload)
                log(
                    "  В screen preferences() уже был свой список языков — "
                    "в него добавлена кнопка «{0}».".format(name)
                )
                result["screens"] = "done"
            elif status == DONE:
                write_text(screens_path, payload)
                log(
                    "  В screen preferences() добавлен новый блок выбора "
                    "языка (кнопка «{0}»).".format(name)
                )
                result["screens"] = "done"
            elif status == ALREADY:
                log(
                    "  В screens.rpy уже есть выбор этого языка ({0}) — "
                    "шаг пропущен, файл не менялся.".format(payload)
                )
                result["screens"] = "already"
            elif status == NO_SCREEN:
                log(
                    "  В screens.rpy не найден screen preferences() — шаг "
                    "пропущен, файл не менялся. Добавьте блок выбора языка "
                    "в меню настроек вручную."
                )
            else:  # NO_ANCHOR
                log(
                    "  В screen preferences() нет вообще ни одного vbox: — "
                    "не знаю, куда вставлять, шаг пропущен, файл не "
                    "менялся. Добавьте блок выбора языка вручную."
                )
        except Exception as e:
            log("  ОШИБКА при правке screens.rpy: {0}".format(e))
            result["screens"] = "error"

    return result
