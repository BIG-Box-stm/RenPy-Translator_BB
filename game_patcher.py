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
ALREADY = "already"            # нужное уже есть — ничего не менять
NO_SCREEN = "no_screen"        # в файле нет screen preferences()
NO_ANCHOR = "no_anchor"        # в этом экране нет блока "Rollback Side"
BAD_STRUCTURE = "bad_structure"  # блок "Rollback Side" стоит не в vbox


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


def patch_screens_text(text, lang, name):
    """Вставляет блок выбора языка в screen preferences() сразу после
    vbox, в котором стоит label _("Rollback Side"). Возвращает
    (статус, новый_текст_или_описание):
      DONE          — вставлено, второй элемент — новый текст;
      ALREADY       — выбор этого языка уже есть, второй элемент — что
                      именно найдено (строка для журнала);
      NO_SCREEN / NO_ANCHOR / BAD_STRUCTURE — вставить не удалось,
                      файл менять не нужно."""
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

    label_re = re.compile(r'''label\s+_\(\s*(["'])Rollback Side\1\s*\)''')
    label_idx = None
    for i in range(start, end):
        if label_re.search(lines[i]):
            label_idx = i
            break
    if label_idx is None:
        return NO_ANCHOR, text

    label_indent = _leading_ws(lines[label_idx])

    # Ближайшая выше строка с меньшим отступом — заголовок блока, в
    # котором стоит label. Обычно это "vbox:".
    header_idx = None
    for j in range(label_idx - 1, start, -1):
        line = lines[j]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _indent_len(line) < _indent_len(lines[label_idx]):
            header_idx = j
            break
    if header_idx is None or not re.match(r'\s*vbox\b', lines[header_idx]):
        return BAD_STRUCTURE, text

    header_indent = _leading_ws(lines[header_idx])
    header_len = len(header_indent)

    # Конец vbox: последняя непустая строка перед первой строкой с
    # отступом не глубже заголовка.
    last_content = label_idx
    for k in range(label_idx + 1, end):
        line = lines[k]
        if not line.strip():
            continue
        if _indent_len(line) <= header_len:
            break
        last_content = k

    if not lines[last_content].endswith(("\n", "\r")):
        lines[last_content] += nl

    new_block = [nl]  # пустая строка перед новым блоком
    new_block += [
        l + nl for l in language_selector_lines(lang, name, header_indent, label_indent)
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
    gui_path = os.path.join(game_dir, "gui.rpy")
    if not os.path.isfile(gui_path):
        log(
            "  gui.rpy не найден в папке игры — шаг пропущен. Если у игры "
            "остался только gui.rpyc, сначала нажмите «Подготовить игру»."
        )
    else:
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
    screens_path = os.path.join(game_dir, "screens.rpy")
    if not os.path.isfile(screens_path):
        log(
            "  screens.rpy не найден в папке игры — шаг пропущен. Если у "
            "игры остался только screens.rpyc, сначала нажмите "
            "«Подготовить игру»."
        )
    else:
        try:
            status, payload = patch_screens_text(read_text(screens_path), lang, name)
            if status == DONE:
                write_text(screens_path, payload)
                log(
                    "  В screen preferences() после блока «Rollback Side» "
                    "добавлен выбор языка (кнопка «{0}»).".format(name)
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
            elif status == NO_ANCHOR:
                log(
                    "  В screen preferences() нет блока label "
                    "_(\"Rollback Side\") — не знаю, куда вставлять, шаг "
                    "пропущен, файл не менялся. Добавьте блок выбора "
                    "языка вручную."
                )
            else:
                log(
                    "  Блок «Rollback Side» стоит не внутри vbox — "
                    "структура экрана нестандартная, шаг пропущен, файл "
                    "не менялся. Добавьте блок выбора языка вручную."
                )
        except Exception as e:
            log("  ОШИБКА при правке screens.rpy: {0}".format(e))
            result["screens"] = "error"

    return result
