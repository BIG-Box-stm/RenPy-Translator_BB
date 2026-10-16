# -*- coding: utf-8 -*-
"""
Клиент локального llama-server для обычной (не специализированной) LLM
Gemma 4 (gemma-4-12B-it-qat-UD-Q4_K_XL.gguf от unsloth).

В отличие от Translate Gemma, эта модель работает ОДНОСТРОЧНО, без
контекста предыдущей реплики и без имени говорящего: в запросе только
системный промпт (из папки prompts/, см. prompts.py) и сама строка. Поэтому
по интерфейсу это "пакетный" движок, как Google/DeepL/LibreTranslate:
вызываемый объект + .stats + .stop() + .warm_batch() + общий кэш. Строки
переводятся параллельно (ThreadPoolExecutor, число потоков = -np
сервера) — так же, как в тестовом скрипте __gemma_np_test.py.

Сервер для неё нужно запускать с --jinja и --reasoning off (без них
модель работает некорректно) — см. EXTRA_SERVER_ARGS, их добавляет
gemma_launcher.launch().
"""

import json
import re
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import core

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_NGL = 99
DEFAULT_NP = 1
DEFAULT_CTX = 2048
DEFAULT_N_PREDICT = 256

EXTRA_SERVER_ARGS = ["--jinja", "--reasoning", "off"]


_COLON = "[:：]"


_RE_NAME_PREFIX = re.compile(
    r'^\s*(?P<name>[A-ZА-ЯЁ][^\s:：{\[\]]*(?:\s+[^\s:：{\[\]]+){0,3}\s*(?:\([^()]{0,20}\))?)\s*'
    + _COLON + r'\s+'
)


def split_speaker(result, speaker, source_text, known=None):
    """Если модель вернула перевод вместе с именем говорящего ("Aelira:
    Привет" / "Аэлира: Привет"), убирает имя — строку не бракуем, а
    чиним. Возвращает (текст, найденный_префикс_или_None).

    Порядок:
    1. отправленное имя ("Aelira", "Player (male)", без скобки) и уже
       ИЗВЕСТНЫЕ переводы имени `known` (из файла имён персонажей и те,
       что программа уже встречала в ответах) — срезаются всегда, даже
       если в самой реплике есть двоеточие ("Hello: ...");
    2. незнакомое имя: любое имя (до 4 слов, первое с заглавной буквы,
       возможно со скобкой) и двоеточие в начале — срезается, если в
       исходной реплике двоеточий нет вообще, либо в ответе их стало БОЛЬШЕ,
       чем в исходной реплике (появилось лишнее "Имя:"). Иначе легко
       срезать кусок настоящего текста ("Иди сюда: пожалуйста")."""
    if not speaker:
        return result, None
    base = speaker.split(" (")[0]
    names = list(dict.fromkeys([speaker, base] + sorted(known or ())))
    for name in names:
        m = re.match(r'^\s*' + re.escape(name) + r'\s*' + _COLON + r'\s*', result, re.IGNORECASE)
        if m and len(result) > m.end():
            return result[m.end():], name
    m = _RE_NAME_PREFIX.match(result)
    if m and len(result) > m.end():
        src_colons = len(re.findall(_COLON, source_text))
        if src_colons == 0 or len(re.findall(_COLON, result)) > src_colons:
            return result[m.end():], m.group("name").strip()
    return result, None


def strip_speaker(result, speaker, source_text, known=None):
    return split_speaker(result, speaker, source_text, known)[0]


class Gemma4Error(Exception):
    """Ошибка ответа сервера — стоит повторить запрос."""


class Gemma4ConnectionError(Gemma4Error):
    """Сервер недоступен вообще — повторять остальные строки бессмысленно."""


def raw_translate(base_url, system_prompt, text, max_tokens=DEFAULT_N_PREDICT, timeout=600):
    """Один запрос к /v1/chat/completions: system = промпт, user = голый
    текст строки (как в тестовом скрипте)."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
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
        raise Gemma4Error("HTTP {0}: {1}".format(e.code, body_text[:300]))
    except urllib.error.URLError as e:
        raise Gemma4ConnectionError(
            "Не удалось подключиться к Gemma 4 по адресу {0} ({1}). "
            "Убедитесь, что сервер запущен (кнопка «Запустить Gemma 4») и "
            "адрес указан верно.".format(url, e.reason)
        )
    try:
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        raise Gemma4Error("Неожиданный ответ сервера: {0}".format(e))


class Gemma4Translator:
    def __init__(self, base_url, system_prompt, np=DEFAULT_NP,
                 max_tokens=DEFAULT_N_PREDICT, cache=None, retries=2,
                 log=None, on_progress=None):
        self.base_url = (base_url or DEFAULT_URL).strip()
        self.system_prompt = system_prompt
        self.np = max(1, int(np))
        self.max_tokens = max_tokens
        self.cache = cache if cache is not None else {}
        self.retries = retries
        self.log = log or (lambda msg: None)
        self.on_progress = on_progress or (lambda: None)
        self.stats = {
            "requests": 0, "cache_hits": 0, "errors": 0, "rate_limit_hits": 0,
            "batch_requests": 0, "batched_lines": 0,
        }
        self._stopped = False
        self._fatal_error = None
        self._failed = set()   # строки, которые уже безуспешно пробовали в warm_batch()
        self._lock = threading.Lock()
        # Режим "с именем говорящего": ключ запроса "Имя: текст" ->
        # (имя, текст). Заполняется через key_for() до начала перевода.
        self._parts = {}
        # Известные переводы имён говорящих: speaker -> {"Венди", ...}.
        # Пополняется из файла имён персонажей (add_known_prefix) и по ходу
        # работы — когда имя в ответе удалось опознать и срезать.
        self._known = {}

    def add_known_prefix(self, speaker, translated_name):
        if speaker and translated_name:
            self._known.setdefault(speaker, set()).add(translated_name)

    def key_for(self, speaker, text):
        """Ключ запроса (он же ключ кэша): без говорящего — просто текст,
        иначе "Имя: текст" (имя, затем двоеточие, без кавычек/звёздочек)."""
        if not speaker:
            return text
        key = "{0}: {1}".format(speaker, text)
        self._parts[key] = (speaker, text)
        return key

    def stop(self):
        self._stopped = True

    def _inactive(self):
        return self._stopped or self._fatal_error is not None

    def _bad_reason(self, source, result):
        if not result:
            return "пустой ответ"
        if "\n" in result or "\r" in result:
            return "перенос строки в ответе"
        if not core.tokens_preserved(source, result):
            return "потеряны или повреждены теги/подстановки"
        if core.markers_collapsed(source, result):
            return "теги слиплись, текст между ними пропал"
        if core.looks_truncated(source, result):
            return "потерян текст за пределами тегов"
        return None

    def _attempt(self, request):
        """Перевод одной строки с повторами. Возвращает текст или None.
        request — либо сам текст, либо "Имя: текст" (см. key_for())."""
        speaker, text = self._parts.get(request, (None, request))
        for attempt in range(1, self.retries + 1):
            if self._inactive():
                return None
            try:
                result = raw_translate(self.base_url, self.system_prompt, request, self.max_tokens)
            except Gemma4ConnectionError as e:
                self._fatal_error = e
                self.log(str(e))
                return None
            except Gemma4Error as e:
                self.log("Ошибка Gemma 4 (попытка {0}/{1}): {2}.".format(attempt, self.retries, e))
                continue
            result, learned = split_speaker(result, speaker, text, self._known.get(speaker))
            if learned:
                self._known.setdefault(speaker, set()).add(learned)
            reason = self._bad_reason(text, result)
            if reason:
                self.log(
                    "Gemma 4 вернула непригодный ответ ({0}) — повторяю "
                    "(попытка {1}/{2})...".format(reason, attempt, self.retries)
                )
                continue
            return result
        return None

    def _work(self, text):
        if self._inactive() or text in self.cache:
            return
        result = self._attempt(text)
        with self._lock:
            if result is not None:
                self.cache[text] = result
                self.stats["requests"] += 1
            else:
                if not self._inactive():
                    self.stats["errors"] += 1
                self._failed.add(text)
            # Считаем и неудачные строки: для полосы прогресса и ETA важно,
            # сколько строк уже ОБРАБОТАНО. Вызов внутри блокировки — чтобы
            # сохранение кэша на диск не шло одновременно с записью в него.
            if not self._stopped:
                self.on_progress()

    def __call__(self, text):
        # Кэш читаем и после остановки: уже переведённое должно попасть в файл.
        if text in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[text]
        if self._stopped or self._fatal_error:
            return None
        if text in self._failed:
            return None
        self._work(text)
        return self.cache.get(text)

    def warm_batch(self, texts):
        pending = [t for t in dict.fromkeys(texts) if t not in self.cache]
        if not pending:
            return
        with ThreadPoolExecutor(max_workers=self.np) as executor:
            list(executor.map(self._work, pending))
