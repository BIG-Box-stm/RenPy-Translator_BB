# -*- coding: utf-8 -*-
"""
Промпты нейросетевых переводчиков (Translate Gemma, Gemma 4).

В программе НЕТ встроенных промптов: все тексты лежат в обычных текстовых
файлах в папке prompts/ рядом с программой. Их можно открыть в Блокноте и
править; перезапускать программу не нужно (список файлов обновляется при
каждом открытии выпадающего списка «Промпт», сам текст читается при каждом
«Начать перевод»).

    prompts/prompt_context.txt — перевод диалогов с контекстом (только
                                 Translate Gemma; в выпадающий список не
                                 входит);
    prompts/prompt_1.txt,
    prompts/prompt_2.txt       — «Промпт 1», «Промпт 2» (однострочный перевод
                                 без контекста);
    любой другой *.txt         — появляется в списке «Промпт» под своим
    в папке prompts/             именем (prompt_3.txt -> «Промпт 3», файл
                                 «мой промпт.txt» -> «мой промпт»).

В тексте промптов вместо названий языков стоят плейсхолдеры {SRC_LANG}
(язык оригинала) и {DEST_LANG} (язык перевода): программа подставляет туда
английские названия языков, выбранных в окне («English», «Russian»...).

Если нужного файла нет или он пуст, функции загрузки возвращают None, а
окно программы показывает уведомление с просьбой добавить промпт в папку.
"""

import os
import re

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

CONTEXT_FILE = "prompt_context.txt"
DEFAULT_LABEL = "Промпт 1"

# Английские названия языков для подстановки в промпт вместо {SRC_LANG} /
# {DEST_LANG}. Ключи — коды из списка языков окна (в нижнем регистре).
LANGUAGE_NAMES_EN = {
    "en": "English",
    "ru": "Russian",
    "uk": "Ukrainian",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pl": "Polish",
    "pt": "Portuguese",
    "zh-cn": "Simplified Chinese",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "tr": "Turkish",
    "ar": "Arabic",
    "hi": "Hindi",
    "id": "Indonesian",
    "vi": "Vietnamese",
}


def language_name(code):
    """Английское название языка по коду (для текста промпта). Для
    неизвестного кода возвращает сам код."""
    code = (code or "").strip()
    return LANGUAGE_NAMES_EN.get(code.lower(), code)


def apply_languages(text, src_lang=None, dest_lang=None, log=None):
    """Подставляет в текст промпта языки вместо {SRC_LANG} и {DEST_LANG}
    (коды из окна программы, например "en"/"ru"). Намеренно простая
    замена подстроки, а не str.format(): в промптах полно фигурных
    скобок ({...}, {*}), которые format() принял бы за поля.

    Если оба кода не переданы — текст возвращается как есть. Если в
    промпте нет ни одного плейсхолдера, а выбранная пара языков не
    "английский -> русский" (на который такие промпты обычно были
    жёстко написаны), в журнал пишется предупреждение: язык, выбранный в
    окне, в этот промпт не попадёт."""
    if text is None or (not src_lang and not dest_lang):
        return text
    log = log or (lambda msg: None)
    src_name = language_name(src_lang)
    dest_name = language_name(dest_lang)
    has_placeholders = "{SRC_LANG}" in text or "{DEST_LANG}" in text
    if not has_placeholders and (src_name != "English" or dest_name != "Russian"):
        log(
            "Внимание: в тексте промпта нет {{SRC_LANG}} / {{DEST_LANG}} — языки "
            "({0} -> {1}), выбранные в окне, в него не подставляются. Если "
            "в промпте жёстко прописаны другие языки, перевод может "
            "получиться не на тот язык.".format(src_name, dest_name)
        )
    return text.replace("{SRC_LANG}", src_name).replace("{DEST_LANG}", dest_name)


_RE_NUMBERED = re.compile(r"^prompt_(\d+)$", re.IGNORECASE)


def ensure_dir(log=None):
    """Создаёт папку prompts/, если её нет (пустую — промпты пользователь
    кладёт сам)."""
    log = log or (lambda msg: None)
    try:
        os.makedirs(PROMPTS_DIR, exist_ok=True)
    except Exception as e:
        log("Не удалось создать папку промптов {0}: {1}".format(PROMPTS_DIR, e))


def list_prompts(log=None):
    """[(подпись, имя файла)] для выпадающего списка: все *.txt из папки
    prompts/, кроме контекстного. prompt_N.txt -> «Промпт N» (по номеру),
    остальные — по имени файла без расширения (по алфавиту, после
    нумерованных)."""
    ensure_dir(log)
    items = []
    try:
        names = os.listdir(PROMPTS_DIR)
    except Exception:
        names = []
    for name in names:
        if not name.lower().endswith(".txt") or name.lower() == CONTEXT_FILE:
            continue
        if not os.path.isfile(os.path.join(PROMPTS_DIR, name)):
            continue
        stem = name[:-4]
        m = _RE_NUMBERED.match(stem)
        if m:
            items.append(((0, int(m.group(1)), ""), "Промпт {0}".format(int(m.group(1))), name))
        else:
            items.append(((1, 0, stem.lower()), stem, name))
    items.sort(key=lambda it: it[0])
    return [(label, name) for _key, label, name in items]


def file_for_label(label):
    """Имя файла по подписи из списка; None, если такого промпта в папке
    нет (папка пуста или файл удалили)."""
    for lab, name in list_prompts():
        if lab == label:
            return name
    return None


def load_file(filename, log=None, src_lang=None, dest_lang=None):
    """Текст промпта из prompts/<filename> с подставленными языками (см.
    apply_languages()). None — файла нет, он пуст или не читается (причина
    пишется в журнал); вызывающий код должен показать пользователю
    уведомление."""
    log = log or (lambda msg: None)
    if not filename:
        return None
    ensure_dir(log)
    path = os.path.join(PROMPTS_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read().strip()
    except Exception as e:
        log("Не удалось прочитать {0}: {1}.".format(path, e))
        return None
    if not text:
        log("Файл {0} пуст.".format(filename))
        return None
    return apply_languages(text, src_lang, dest_lang, log)


def load_context(log=None, src_lang=None, dest_lang=None):
    return load_file(CONTEXT_FILE, log, src_lang, dest_lang)
