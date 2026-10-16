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
import urllib.error
import urllib.request

import core  # переиспользуем текущий формат маркеров защиты (core.set_marker())

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_NGL = 99
DEFAULT_NP = 2
DEFAULT_CTX = 2048
DEFAULT_N_PREDICT = 256

# Тексты промптов вынесены в папку prompts/ (см. prompts.py). Эти две
# константы — встроенные запасные варианты; рабочий текст GemmaTranslator
# получает снаружи (context_prompt / no_context_prompt).
import prompts

SYSTEM_PROMPT = prompts.BUILTIN["prompt_context.txt"]
SYSTEM_PROMPT_NO_CONTEXT = prompts.BUILTIN["prompt_1.txt"]


class GemmaError(Exception):
    """Сетевая или протокольная ошибка при обращении к серверу."""


# Иногда (небольшая локальная модель, не идеальная) Translate Gemma вместо
# чистого перевода повторяет кусок собственного промпта — например,
# буквально вставляет "[CURRENT_SOURCE]" в ответ вместо того, чтобы
# перевести только то, что после этой метки. core.translate_quoted() и так
# отбросит такой ответ (там есть отдельная проверка на перенос строки), но
# здесь проверяем раньше и явно, чтобы можно было ПОВТОРИТЬ попытку — та же
# логика, что уже используется для сетевых ошибок и повреждённых маркеров
# в остальных движках (gtranslate.py и т.д.).
_PROMPT_LEAK_MARKERS = ("[CURRENT_SOURCE]", "[PREVIOUS_SOURCE]", "[PREVIOUS_TRANSLATION]")


def _looks_like_prompt_leak(text):
    if "\n" in text or "\r" in text:
        return True
    return any(marker in text for marker in _PROMPT_LEAK_MARKERS)


# Проверка "пропал текст до первого/после последнего тега" общая для
# Translate Gemma и Gemma 4 и живёт в core.py.
_looks_truncated = core.looks_truncated


def build_messages(previous_source, previous_translation, current_source, rules_name=None,
                    system_prompt=None):
    system = system_prompt if system_prompt is not None else SYSTEM_PROMPT
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
                   rules_name=None, system_prompt=None, max_tokens=DEFAULT_N_PREDICT, timeout=60):
    """Один запрос к /v1/chat/completions. Возвращает переведённый текст
    (обрезанный по краям пробелов). Бросает GemmaError при сетевой или
    протокольной ошибке."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "messages": build_messages(
            previous_source, previous_translation, current_source, rules_name, system_prompt,
        ),
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

    def __init__(self, base_url, max_tokens=DEFAULT_N_PREDICT, retries=2,
                 log=None, on_progress=None, stop_event=None,
                 context_prompt=None, no_context_prompt=None):
        # Тексты системных промптов приходят снаружи (из папки prompts/);
        # None -> встроенные запасные варианты.
        self.context_prompt = context_prompt or SYSTEM_PROMPT
        self.no_context_prompt = no_context_prompt or SYSTEM_PROMPT_NO_CONTEXT
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
        текста) или None при неудаче (после исчерпания попыток).

        rules_name=None — сигнал из core.process_lines_contextual(), что
        это строка БЕЗ контекста (блок old/new: пункты меню, текст
        интерфейса и т.п. — для диалоговых реплик rules_name всегда
        задан, минимум "Narrator", см. character_names.resolve_speaker_name()).
        Для таких строк используется отдельный промпт
        (SYSTEM_PROMPT_NO_CONTEXT), не упоминающий [PREVIOUS_SOURCE]/
        [PREVIOUS_TRANSLATION] — в запросе их всё равно не будет, а
        промпт, ссылающийся на отсутствующие поля, на практике чаще
        путает модель, чем помогает.

        Без задержек между попытками: в отличие от облачных API (Google/
        DeepL/LibreTranslate), откуда этот класс унаследовал саму
        структуру retry-цикла, здесь сервер локальный — ждать несколько
        секунд "чтобы не перегрузить сервис" бессмысленно, это просто
        простой впустую. По умолчанию всего 2 попытки (одна исходная и
        один повтор) — по опыту, если битый ответ не исправился со
        второго раза, с теми же настройками он не исправится и дальше."""
        system_prompt = self.context_prompt if rules_name is not None else self.no_context_prompt
        if self._is_stopped():
            return None
        for attempt in range(1, self.retries + 1):
            if self._is_stopped():
                return None
            try:
                result = raw_translate(
                    self.base_url, previous_source, previous_translation,
                    current_source, rules_name=rules_name, system_prompt=system_prompt,
                    max_tokens=self.max_tokens,
                )
                if _looks_like_prompt_leak(result):
                    self.log(
                        "Translate Gemma вернула повреждённый ответ (перенос "
                        "строки или обрывок промпта вроде \"[CURRENT_SOURCE]\" "
                        "в тексте) — повторяю (попытка {0}/{1})..."
                        .format(attempt, self.retries)
                    )
                    continue
                if _looks_truncated(current_source, result):
                    self.log(
                        "Translate Gemma потеряла часть текста за пределами "
                        "тегов (в ответе осталось только то, что внутри "
                        "{{...}}) — повторяю (попытка {0}/{1})..."
                        .format(attempt, self.retries)
                    )
                    continue
                self.stats["requests"] += 1
                self.on_progress()
                return result
            except GemmaError as e:
                self.log(
                    "Ошибка Translate Gemma (попытка {0}/{1}): {2}."
                    .format(attempt, self.retries, e)
                )
        self.stats["errors"] += 1
        self.log("Не удалось перевести строку через Translate Gemma, оставляю оригинал.")
        return None
