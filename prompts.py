# -*- coding: utf-8 -*-
"""
Промпты нейросетевых переводчиков (Translate Gemma, Gemma 4).

Сами тексты промптов лежат в обычных текстовых файлах в папке prompts/
рядом с программой — их можно открыть в Блокноте и править; перезапуск
программы не нужен, файлы перечитываются при каждом нажатии «Начать
перевод»:

    prompts/prompt_context.txt — перевод диалогов с контекстом
                                 (только Translate Gemma; в выпадающий
                                 список не входит);
    prompts/prompt_1.txt       — «Промпт 1»: однострочный перевод без
                                 контекста;
    prompts/prompt_2.txt       — «Промпт 2»: однострочный перевод без
                                 контекста (вариант из теста Gemma 4).

Если файла нет или он пуст, берётся встроенный вариант (DEFAULTS ниже),
а недостающий файл создаётся заново — так всегда можно вернуть исходный
текст, просто удалив файл. На код программы выбор промпта не влияет:
промпт — это только текст системного сообщения.
"""

import os

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

FILES = {
    "context": "prompt_context.txt",
    "1": "prompt_1.txt",
    "2": "prompt_2.txt",
}

# Подписи в выпадающем списке -> ключ промпта.
LABELS = {"Промпт 1": "1", "Промпт 2": "2"}
DEFAULT_LABEL = "Промпт 1"

DEFAULTS = {
    "context": """TASK: Translate English game dialogue into natural Russian.

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

    "1": """TASK: Translate English game text into natural Russian.

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

    "2": """You are a professional English to Russian translator specializing in story-driven video games.

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


def _path(key):
    return os.path.join(PROMPTS_DIR, FILES[key])


def ensure_files(log=None):
    """Создаёт недостающие файлы промптов со встроенным текстом."""
    log = log or (lambda msg: None)
    try:
        os.makedirs(PROMPTS_DIR, exist_ok=True)
    except Exception as e:
        log("Не удалось создать папку промптов {0}: {1}".format(PROMPTS_DIR, e))
        return
    for key in FILES:
        path = _path(key)
        if os.path.isfile(path):
            continue
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(DEFAULTS[key] + "\n")
            log("Создан файл промпта: {0}".format(path))
        except Exception as e:
            log("Не удалось создать {0}: {1}".format(path, e))


def load(key, log=None):
    """Текст промпта по ключу ("context", "1", "2"). Пустой или
    нечитаемый файл -> встроенный вариант."""
    log = log or (lambda msg: None)
    ensure_files(log)
    try:
        with open(_path(key), "r", encoding="utf-8-sig") as f:
            text = f.read().strip()
    except Exception as e:
        log("Не удалось прочитать {0}: {1} — беру встроенный промпт.".format(_path(key), e))
        return DEFAULTS[key]
    if not text:
        log("Файл {0} пуст — беру встроенный промпт.".format(FILES[key]))
        return DEFAULTS[key]
    return text


def key_for_label(label):
    return LABELS.get(label, LABELS[DEFAULT_LABEL])
