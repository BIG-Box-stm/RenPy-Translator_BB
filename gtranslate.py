# -*- coding: utf-8 -*-
"""
Простой клиент бесплатного (неофициального) Google Translate — того же
эндпоинта, которым пользуется веб-страница translate.google.com и многие
открытые библиотеки. Ключ API не нужен, но есть неявные лимиты на
количество запросов в единицу времени — поэтому есть пауза между
запросами, и эта пауза автоматически увеличивается, если Google начинает
отвечать "429 Too Many Requests".
"""

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_ENDPOINT = "https://translate.googleapis.com/translate_a/single"

# Редкий символ из частного диапазона Юникода, которым разделяем строки
# при пакетном переводе. Google не пытается его "переводить" и оставляет
# как есть — так же, как теги {b}/[name], которые мы защищаем в core.py.
_BATCH_SEP = "\uE0F0"
_BATCH_SEP_RE = re.compile(r'[ \t]*\r?\n?[ \t]*' + _BATCH_SEP + r'[ \t]*\r?\n?[ \t]*')


class RateLimited(Exception):
    """Google ответил 429 Too Many Requests."""

    def __init__(self, retry_after=None):
        super().__init__("HTTP 429 Too Many Requests")
        self.retry_after = retry_after


def raw_translate(text, src="en", dest="ru", timeout=10):
    """Один запрос к Google Translate. Бросает RateLimited при 429,
    иначе обычное исключение при ошибке сети/неожиданном ответе."""
    params = {
        "client": "gtx",
        "sl": src,
        "tl": dest,
        "dt": "t",
        "q": text,
    }
    url = _ENDPOINT + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = None
            try:
                ra = e.headers.get("Retry-After")
                if ra is not None:
                    retry_after = float(ra)
            except Exception:
                retry_after = None
            raise RateLimited(retry_after)
        raise
    data = json.loads(raw)
    segments = data[0] if data and data[0] else []
    return "".join(seg[0] for seg in segments if seg and seg[0])


def raw_translate_batch(texts, src="en", dest="ru", timeout=15):
    """Переводит несколько строк одним запросом, объединив их особым
    разделителем. Возвращает список переводов той же длины, что texts.
    Бросает BatchMismatch, если Google вернул не то количество кусков —
    тогда вызывающий код должен переводить эту пачку по одной строке."""
    combined = ("\n" + _BATCH_SEP + "\n").join(texts)
    translated_combined = raw_translate(combined, src, dest, timeout=timeout)
    parts = _BATCH_SEP_RE.split(translated_combined)
    parts = [p.strip() for p in parts]
    if len(parts) != len(texts):
        raise BatchMismatch(len(texts), len(parts))
    return parts


class BatchMismatch(Exception):
    """Число кусков в ответе не совпало с числом отправленных строк —
    пакет нужно перевести по одной строке, чтобы не перепутать реплики."""

    def __init__(self, expected, got):
        super().__init__(f"expected {expected} parts, got {got}")
        self.expected = expected
        self.got = got


class CachedTranslator:
    """Вызываемый объект: translator(text) -> переведённый текст.
    Кэширует результаты, повторяет запрос при временных ошибках и сам
    подстраивает скорость запросов, чтобы не словить долгую блокировку
    от Google.
    """

    BASE_DELAY = 0.8       # стартовая пауза между запросами, сек
    MAX_DELAY = 6.0        # выше этого паузу между обычными запросами не поднимаем
    RATE_LIMIT_COOLDOWN = 25.0   # базовая "заморозка" при первом 429
    MAX_COOLDOWN = 90.0
    RECOVERY_STEP = 0.05   # на сколько снижаем паузу после удачных запросов
    RECOVERY_EVERY = 8     # после скольки подряд успешных запросов снижаем паузу

    BATCH_ITEMS = 20       # сколько строк объединяем в один запрос максимум
    BATCH_CHARS = 1400     # и суммарно не длиннее стольки символов

    def __init__(self, src, dest, cache=None, delay=None, retries=5,
                 log=None, on_progress=None):
        self.src = src
        self.dest = dest
        self.cache = cache if cache is not None else {}
        self.delay = delay if delay is not None else self.BASE_DELAY
        self.retries = retries
        self.log = log or (lambda msg: None)
        self.on_progress = on_progress or (lambda: None)
        self.stats = {
            "requests": 0, "cache_hits": 0, "errors": 0, "rate_limit_hits": 0,
            "batch_requests": 0, "batched_lines": 0,
        }
        self._stopped = False
        self._success_streak = 0

    def stop(self):
        self._stopped = True

    def _note_success(self):
        self._success_streak += 1
        if (self._success_streak % self.RECOVERY_EVERY == 0
                and self.delay > self.BASE_DELAY):
            self.delay = max(self.BASE_DELAY, self.delay - self.RECOVERY_STEP)

    def _note_rate_limit(self):
        self._success_streak = 0
        self.delay = min(self.MAX_DELAY, self.delay * 1.6 + 0.2)

    def __call__(self, text):
        if self._stopped:
            return text
        if text in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[text]

        cooldown = self.RATE_LIMIT_COOLDOWN
        for attempt in range(1, self.retries + 1):
            if self._stopped:
                return text
            try:
                result = raw_translate(text, self.src, self.dest)
                self.cache[text] = result
                self.stats["requests"] += 1
                self._note_success()
                self.on_progress()
                sleep_for = self.delay + random.uniform(0, 0.25)
                if sleep_for:
                    time.sleep(sleep_for)
                return result

            except RateLimited as e:
                self.stats["rate_limit_hits"] += 1
                self._note_rate_limit()
                wait = e.retry_after if e.retry_after else cooldown
                wait = min(wait, self.MAX_COOLDOWN)
                self.log(
                    "Google Translate ответил '429 Too Many Requests' "
                    "(слишком много запросов). Делаю паузу {0:.0f} с и "
                    "замедляю дальнейшие запросы (попытка {1}/{2})..."
                    .format(wait, attempt, self.retries)
                )
                time.sleep(wait)
                cooldown = min(self.MAX_COOLDOWN, cooldown * 1.7)

            except Exception as e:  # сеть нестабильна и т.п.
                wait = min(1.5 * attempt, 8.0)
                self.log(
                    "Ошибка перевода (попытка {0}/{1}): {2}. Повтор через {3:.1f} с."
                    .format(attempt, self.retries, e, wait)
                )
                time.sleep(wait)

        self.stats["errors"] += 1
        self.log("Не удалось перевести строку, оставляю оригинал: {0!r}".format(text[:60]))
        return text

    # ------------------------------------------------------------------
    # Пакетный перевод (несколько строк одним запросом) — сильно ускоряет
    # работу на больших файлах по сравнению с переводом по одной строке.
    # ------------------------------------------------------------------
    def warm_batch(self, texts):
        """Заранее переводит список уникальных строк, объединяя их в
        пачки (несколько строк — один запрос к Google), и складывает
        результат в self.cache. После этого process_lines() в core.py
        почти не делает сетевых запросов — только читает из кэша.

        Если пакет не удаётся разобрать надёжно (несовпадение числа
        кусков в ответе) или происходит сетевая ошибка, эта пачка
        переводится по одной строке — медленнее, но без риска перепутать
        реплики местами.
        """
        pending = [t for t in dict.fromkeys(texts) if t not in self.cache]
        idx = 0
        while idx < len(pending):
            if self._stopped:
                return
            batch = []
            total_chars = 0
            while idx < len(pending) and len(batch) < self.BATCH_ITEMS:
                t = pending[idx]
                if batch and total_chars + len(t) > self.BATCH_CHARS:
                    break
                batch.append(t)
                total_chars += len(t)
                idx += 1

            if len(batch) <= 1:
                if batch:
                    self(batch[0])
                continue

            if not self._translate_batch_once(batch):
                for t in batch:
                    if self._stopped:
                        return
                    self(t)

    def _translate_batch_once(self, batch):
        """Пытается перевести пачку одним запросом. Возвращает True при
        успехе (self.cache уже заполнен для всех строк пачки) или False,
        если нужно откатиться на перевод по одной строке."""
        cooldown = self.RATE_LIMIT_COOLDOWN
        for attempt in range(1, self.retries + 1):
            if self._stopped:
                return True  # не считаем это неудачей, просто остановка
            try:
                parts = raw_translate_batch(batch, self.src, self.dest)
                for src_text, translated in zip(batch, parts):
                    self.cache[src_text] = translated
                self.stats["requests"] += 1
                self.stats["batch_requests"] += 1
                self.stats["batched_lines"] += len(batch)
                self._note_success()
                for _ in batch:
                    self.on_progress()
                sleep_for = self.delay + random.uniform(0, 0.25)
                if sleep_for:
                    time.sleep(sleep_for)
                return True

            except BatchMismatch as e:
                self.log(
                    "Пакетный перевод не совпал по количеству строк "
                    "({0} вместо {1}) — перевожу эту пачку по одной "
                    "строке, чтобы не перепутать реплики.".format(e.got, e.expected)
                )
                return False

            except RateLimited as e:
                self.stats["rate_limit_hits"] += 1
                self._note_rate_limit()
                wait = e.retry_after if e.retry_after else cooldown
                wait = min(wait, self.MAX_COOLDOWN)
                self.log(
                    "Google Translate ответил '429 Too Many Requests' при "
                    "пакетном переводе ({0} строк). Пауза {1:.0f} с "
                    "(попытка {2}/{3})...".format(len(batch), wait, attempt, self.retries)
                )
                time.sleep(wait)
                cooldown = min(self.MAX_COOLDOWN, cooldown * 1.7)

            except Exception as e:
                wait = min(1.5 * attempt, 8.0)
                self.log(
                    "Ошибка пакетного перевода (попытка {0}/{1}): {2}. "
                    "Повтор через {3:.1f} с.".format(attempt, self.retries, e, wait)
                )
                time.sleep(wait)

        self.log(
            "Не удалось перевести пачку из {0} строк одним запросом — "
            "перевожу по одной.".format(len(batch))
        )
        return False
