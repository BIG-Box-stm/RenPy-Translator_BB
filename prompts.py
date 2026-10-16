# -*- coding: utf-8 -*-
"""
Промпты нейросетевых переводчиков (Translate Gemma, Gemma 4).

Тексты промптов лежат в обычных текстовых файлах в папке prompts/ рядом
с программой — их можно открыть в Блокноте и править; перезапускать
программу не нужно (список файлов обновляется при каждом открытии
выпадающего списка «Промпт», сам текст читается при каждом «Начать
перевод»).

    prompts/prompt_context.txt — перевод диалогов с контекстом (только
                                 Translate Gemma; в выпадающий список не
                                 входит);
    prompts/prompt_1.txt,
    prompts/prompt_2.txt       — встроенные «Промпт 1» и «Промпт 2»
                                 (однострочный перевод без контекста);
    любой другой *.txt         — появляется в списке «Промпт» под своим
    в папке prompts/             именем (prompt_3.txt -> «Промпт 3», файл
                                 «мой промпт.txt» -> «мой промпт»).

Если встроенного файла (context/1/2) нет или он пуст, берётся встроенный
вариант (BUILTIN ниже), а недостающий файл создаётся заново — так всегда
можно вернуть исходный текст, просто удалив файл. На код программы выбор
промпта не влияет: промпт — это только текст системного сообщения.
"""

import os
import re

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

CONTEXT_FILE = "prompt_context.txt"
DEFAULT_FILE = "prompt_1.txt"
DEFAULT_LABEL = "Промпт 1"

BUILTIN = {
    "prompt_context.txt": """TASK: Translate English game dialogue into natural Russian.

STYLE: natural conversational Russian suitable for a story-driven video game.

Translate only [CURRENT_SOURCE].
[PREVIOUS_SOURCE] and [PREVIOUS_TRANSLATION] are context only.

Preserve:
- meaning and intent;
- tone and emotional nuance;
- slang, profanity and informal speech;
- hesitation, repetition and incomplete sentences;
- names, terminology and important formatting.

Use the previous translation to maintain consistent terminology and phrasing.

If a literal translation sounds unnatural in Russian, rewrite it naturally while preserving the original meaning.

If the current line contains a joke, idiom, pun or wordplay, preserve its function and humor. If a literal translation would destroy the joke, recreate an equivalent natural Russian wordplay where reasonably possible.

Do not explain translation decisions.
Do not add information that is absent from the source.
Return only the final Russian translation.

IMPORTANT: If the [CURRENT_SOURCE] contains text markers {*} or [*] or @@*@@ or other similar markers, the translated text must also have the same markers in the same places. The markers themselves should not be changed, translated, deleted or moved in the sentence.""",

    "prompt_1.txt": """TASK: Translate English game text into natural Russian.

STYLE: natural conversational Russian suitable for a story-driven video game.

Preserve:
- meaning and intent;
- tone and emotional nuance;
- slang, profanity and informal speech;
- hesitation, repetition and incomplete sentences;
- names, terminology and important formatting.

If a literal translation sounds unnatural in Russian, rewrite it naturally while preserving the original meaning.

If the current line contains a joke, idiom, pun or wordplay, preserve its function and humor. If a literal translation would destroy the joke, recreate an equivalent natural Russian wordplay where reasonably possible.

Do not explain translation decisions.
Do not add information that is absent from the source.
Return only the final Russian translation.

IMPORTANT: If the text line for translating contains text markers {*} or [*] or @@*@@ or other similar markers, the translated text must also have the same markers in the same places. The markers themselves should not be changed, translated, deleted or moved in the sentence.""",

    "prompt_2.txt": """You are a professional English to Russian translator specializing in story-driven video games.

HARD RULES — PROTECTED TOKENS:

Treat all machine-readable markers and formatting tokens in the source as opaque, immutable tokens. Examples: {...}, [...], @@...@@, other similar variables, placeholders, control codes or machine-readable tokens.

For every marker:

-preserve its exact spelling, content and capitalization;
-preserve the exact number, order and nesting;
-never translate, modify, add, delete, duplicate, merge or split it;
-keep it attached to the same text span it marks in the source.
-Translate only the ordinary text around markers.

Every marker in the source must appear in the translation exactly the same number of times and with exactly the same spelling and content.
The words inside a tagged span may be translated and naturally reordered as required by Russian grammar, but the markers themselves must continue to surround that same piece of translated text.

TRANSLATION TASK:

Translate the English game text into natural, fluent Russian suitable for a story-driven video game.

STYLE:

* Preserve the original meaning and intent.
* Preserve tone, mood and emotional nuance.
* Preserve slang, profanity and informal speech.
* Preserve hesitation, repetition, pauses and incomplete sentences.
* Preserve the character's individual manner of speaking.
* Prefer natural Russian phrasing over literal translation when literal wording sounds unnatural.
* Do not make the translation unnecessarily formal or literary if the original is conversational.
* When the original is poetic, emotional or dramatic, preserve that quality naturally in Russian.
* Preserve jokes, idioms, puns and wordplay. When a literal translation would destroy a joke or wordplay, recreate its function and humor with a natural Russian equivalent, but do not invent new information.

OUTPUT:

Return only the final Russian translation.
Do not explain translation decisions.
Do not add notes or comments.
Return exactly one translated line for one source line.
Do not split, merge or reorder source lines.""",

}

_RE_NUMBERED = re.compile(r"^prompt_(\d+)$", re.IGNORECASE)


def ensure_files(log=None):
    """Создаёт недостающие встроенные файлы промптов."""
    log = log or (lambda msg: None)
    try:
        os.makedirs(PROMPTS_DIR, exist_ok=True)
    except Exception as e:
        log("Не удалось создать папку промптов {0}: {1}".format(PROMPTS_DIR, e))
        return
    for filename, text in BUILTIN.items():
        path = os.path.join(PROMPTS_DIR, filename)
        if os.path.isfile(path):
            continue
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text + "\n")
            log("Создан файл промпта: {0}".format(path))
        except Exception as e:
            log("Не удалось создать {0}: {1}".format(path, e))


def list_prompts(log=None):
    """[(подпись, имя файла)] для выпадающего списка: все *.txt из папки
    prompts/, кроме контекстного. prompt_N.txt -> «Промпт N» (по номеру),
    остальные — по имени файла без расширения (по алфавиту, после
    нумерованных)."""
    ensure_files(log)
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
    """Имя файла по подписи из списка (если такой подписи уже нет —
    встроенный «Промпт 1»)."""
    for lab, name in list_prompts():
        if lab == label:
            return name
    return DEFAULT_FILE


def load_file(filename, log=None):
    """Текст промпта из prompts/<filename>. Для встроенных файлов пустой
    или нечитаемый файл -> встроенный текст; для остальных -> None (вызывающий
    код должен сообщить об этом пользователю)."""
    log = log or (lambda msg: None)
    ensure_files(log)
    path = os.path.join(PROMPTS_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read().strip()
    except Exception as e:
        log("Не удалось прочитать {0}: {1}.".format(path, e))
        return BUILTIN.get(filename)
    if not text:
        log("Файл {0} пуст.".format(filename))
        return BUILTIN.get(filename)
    return text


def load_context(log=None):
    return load_file(CONTEXT_FILE, log)
