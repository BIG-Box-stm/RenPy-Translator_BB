# -*- coding: utf-8 -*-
"""
Клиент локального сервера llama.cpp (llama-server.exe), раздающего
модель Translate Gemma по OpenAI-совместимому эндпоинту
/v1/chat/completions.

В отличие от Google/DeepL/LibreTranslate, эта модель НЕ поддерживает
пакетный перевод — и не должна: весь смысл в том, что каждая реплика
переводится с учётом предыдущей (диалог связный). Поэтому у этого
движка нет warm_batch()/общего интерфейса с тремя остальными —
вызывающий код (см. core.process_lines_contextual()) сам ведёт per-
файловое состояние "предыдущая реплика/перевод" и вызывает
translate_line() на каждую строку по очереди.

Формат запроса: системное сообщение — фиксированная инструкция
(SYSTEM_PROMPT) плюс необязательная строка "RULES: Speakers name: <имя>"
для реплик с известным говорящим; пользовательское сообщение —
[PREVIOUS_SOURCE]/[PREVIOUS_TRANSLATION] (опускаются, если контекста ещё
нет — начало файла, либо "внеконтекстный" режим для строк меню/
интерфейса) и [CURRENT_SOURCE].
"""

import json
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_NGL = 99
DEFAULT_NP = 2
DEFAULT_CTX = 2048
DEFAULT_N_PREDICT = 256

SYSTEM_PROMPT = """TASK: Translate English game dialogue into natural Russian.

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

IMPORTANT: If the [CURRENT_SOURCE] contains text markers {*} or [*] or @@@ or other similar markers, the translated text must also have the same markers in the same places. The markers themselves should not be changed, translated, deleted or moved in the sentence."""


class GemmaError(Exception):
    """Сетевая или протокольная ошибка при обращении к серверу."""


def build_messages(previous_source, previous_translation, current_source, rules_name=None):
    system = SYSTEM_PROMPT
    if rules_name:
        system += "\n\nRULES: Speakers name: {0}".format(rules_name)

    parts = []
    if previous_source and previous_translation:
        parts.append("[PREVIOUS_SOURCE]\n{0}".format(previous_source))
        parts.append("[PREVIOUS_TRANSLATION]\n{0}".format(previous_translation))
    parts.append("[CURRENT_SOURCE]\n{0}".format(current_source))
    user = "\n\n".join(parts)

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def raw_translate(base_url, previous_source, previous_translation, current_source,
                   rules_name=None, max_tokens=DEFAULT_N_PREDICT, timeout=60):
    """Один запрос к /v1/chat/completions. Возвращает переведённый текст
    (обрезанный по краям пробелов). Бросает GemmaError при сетевой или
    протокольной ошибке."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "messages": build_messages(previous_source, previous_translation, current_source, rules_name),
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        raise GemmaError("HTTP {0}: {1}".format(e.code, body_text[:300]))
    except urllib.error.URLError as e:
        raise GemmaError(
            "Не удалось подключиться к Translate Gemma по адресу {0} ({1}). "
            "Убедитесь, что сервер запущен (кнопка «Запустить Translate "
            "Gemma») и адрес указан верно.".format(url, e.reason)
        )
    try:
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        raise GemmaError("Неожиданный ответ сервера: {0}".format(e))


class GemmaTranslator:
    """Контекстный движок перевода. НЕ совместим по интерфейсу с
    остальными тремя (нет __call__(text)/warm_batch()) — вызывающий код
    сам ведёт previous_source/previous_translation и обращается к
    translate_line() на каждую строку по порядку. Не потокобезопасен по
    .stats — при параллельном переводе нескольких файлов создавайте свой
    экземпляр на каждый рабочий поток."""

    RETRY_COOLDOWN = 2.0
    MAX_COOLDOWN = 15.0

    def __init__(self, base_url, max_tokens=DEFAULT_N_PREDICT, retries=3,
                 log=None, on_progress=None, stop_event=None):
        self.base_url = (base_url or DEFAULT_URL).strip()
        self.max_tokens = max_tokens
        self.retries = retries
        self.log = log or (lambda msg: None)
        self.on_progress = on_progress or (lambda: None)
        self.stats = {"requests": 0, "errors": 0}
        self._stopped = False
        # Необязательный общий (threading.Event) стоп-флаг — нужен, когда
        # несколько файлов переводятся параллельно в разных потоках
        # (см. renpy_translator.pyw:_worker_gemma), каждый со своим
        # экземпляром GemmaTranslator, но с одной кнопкой "Остановить" на
        # всех сразу.
        self.stop_event = stop_event

    def stop(self):
        self._stopped = True

    def _is_stopped(self):
        return self._stopped or (self.stop_event is not None and self.stop_event.is_set())

    def translate_line(self, previous_source, previous_translation, current_source, rules_name=None):
        """Возвращает перевод current_source (уже защищённого маркерами
        текста) или None при неудаче (после исчерпания попыток)."""
        if self._is_stopped():
            return None
        cooldown = self.RETRY_COOLDOWN
        for attempt in range(1, self.retries + 1):
            if self._is_stopped():
                return None
            try:
                result = raw_translate(
                    self.base_url, previous_source, previous_translation,
                    current_source, rules_name=rules_name, max_tokens=self.max_tokens,
                )
                self.stats["requests"] += 1
                self.on_progress()
                return result
            except GemmaError as e:
                self.log(
                    "Ошибка Translate Gemma (попытка {0}/{1}): {2}."
                    .format(attempt, self.retries, e)
                )
                time.sleep(cooldown)
                cooldown = min(self.MAX_COOLDOWN, cooldown * 1.6)
        self.stats["errors"] += 1
        self.log("Не удалось перевести строку через Translate Gemma, оставляю оригинал.")
        return None
