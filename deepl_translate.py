# -*- coding: utf-8 -*-
"""
Клиент бесплатного DeepL API (DeepL API Free). В отличие от Google, это
официальный, документированный сервис с ключом: нужно один раз бесплатно
зарегистрироваться на https://www.deepl.com/pro-api и получить ключ вида
"xxxxxxxx-xxxx-...-xx:fx" (суффикс ":fx" означает бесплатный ключ).

Бесплатный лимит — 500 000 символов в месяц. DeepL поддерживает
официальную пакетную отправку до 50 строк за один запрос — в отличие от
самодельного трюка с разделителем для Google, здесь соответствие строк
гарантировано самим API, без риска перепутать реплики.
"""

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

FREE_ENDPOINT = "https://api-free.deepl.com/v2/translate"
PRO_ENDPOINT = "https://api.deepl.com/v2/translate"

# DeepL требует конкретный вариант для некоторых языков-целей.
_TARGET_LANG_MAP = {
    "en": "EN-US",
    "pt": "PT-BR",
    "zh-cn": "ZH",
    "zh": "ZH",
}


def _deepl_lang(code):
    code = code.strip()
    mapped = _TARGET_LANG_MAP.get(code.lower())
    return mapped if mapped else code.upper()


class RateLimited(Exception):
    def __init__(self, retry_after=None):
        super().__init__("DeepL: too many requests")
        self.retry_after = retry_after


class DeepLError(Exception):
    """Ошибка, после которой повторять запрос бессмысленно (неверный
    ключ, исчерпана квота и т.п.) — нужно показать её пользователю."""


def _endpoint_for_key(api_key):
    return FREE_ENDPOINT if api_key.strip().endswith(":fx") else PRO_ENDPOINT


def raw_translate_batch(texts, api_key, src, dest, timeout=15):
    """Переводит до 50 строк ОДНИМ запросом через официальный batch-режим
    DeepL. Возвращает список переводов той же длины и в том же порядке,
    что texts — DeepL гарантирует это сам, в отличие от нашего трюка с
    разделителем для Google."""
    url = _endpoint_for_key(api_key)
    data = [
        ("auth_key", api_key),
        ("source_lang", _deepl_lang(src).split("-")[0]),  # источник без варианта
        ("target_lang", _deepl_lang(dest)),
    ]
    for t in texts:
        data.append(("text", t))
    body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
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
        if e.code == 429:
            retry_after = None
            try:
                ra = e.headers.get("Retry-After")
                if ra is not None:
                    retry_after = float(ra)
            except Exception:
                pass
            raise RateLimited(retry_after)
        if e.code == 456:
            raise DeepLError(
                "Месячная бесплатная квота DeepL (500 000 символов) исчерпана. "
                "Подождите начала следующего месяца или используйте Google Translate."
            )
        if e.code == 403:
            raise DeepLError(
                "DeepL отклонил ключ API (403) — проверьте, что ключ скопирован "
                "полностью и без лишних пробелов."
            )
        raise DeepLError("Ошибка DeepL (HTTP {0}): {1}".format(e.code, body_text[:200]))
    parsed = json.loads(raw)
    return [item["text"] for item in parsed["translations"]]


class DeepLTranslator:
    """Тот же интерфейс, что и gtranslate.CachedTranslator (вызываемый
    объект + .stats + .stop() + .warm_batch()), чтобы программа могла
    использовать DeepL и Google Translate как взаимозаменяемые движки."""

    BASE_DELAY = 0.3
    MAX_DELAY = 4.0
    RATE_LIMIT_COOLDOWN = 15.0
    MAX_COOLDOWN = 60.0
    RECOVERY_STEP = 0.05
    RECOVERY_EVERY = 8

    BATCH_ITEMS = 50       # официальный лимит DeepL за один запрос
    BATCH_CHARS = 20000

    def __init__(self, api_key, src, dest, cache=None, delay=None, retries=4,
                 log=None, on_progress=None):
        self.api_key = api_key
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
        self._fatal_error = None  # DeepLError, после которой дальше не пытаемся

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
        if self._stopped or self._fatal_error:
            return text
        if text in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[text]
        self._translate_batch_once([text])
        return self.cache.get(text, text)

    def warm_batch(self, texts):
        pending = [t for t in dict.fromkeys(texts) if t not in self.cache]
        idx = 0
        while idx < len(pending):
            if self._stopped or self._fatal_error:
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
            self._translate_batch_once(batch)

    def _translate_batch_once(self, batch):
        cooldown = self.RATE_LIMIT_COOLDOWN
        for attempt in range(1, self.retries + 1):
            if self._stopped or self._fatal_error:
                return
            try:
                parts = raw_translate_batch(batch, self.api_key, self.src, self.dest)
                if len(parts) != len(batch):
                    self.log(
                        "DeepL вернул неожиданное число строк ({0} вместо {1}) "
                        "— пропускаю эту пачку.".format(len(parts), len(batch))
                    )
                    return
                for src_text, translated in zip(batch, parts):
                    self.cache[src_text] = translated
                self.stats["requests"] += 1
                if len(batch) > 1:
                    self.stats["batch_requests"] += 1
                    self.stats["batched_lines"] += len(batch)
                self._note_success()
                for _ in batch:
                    self.on_progress()
                sleep_for = self.delay + random.uniform(0, 0.15)
                if sleep_for:
                    time.sleep(sleep_for)
                return

            except RateLimited as e:
                self.stats["rate_limit_hits"] += 1
                self._note_rate_limit()
                wait = e.retry_after if e.retry_after else cooldown
                wait = min(wait, self.MAX_COOLDOWN)
                self.log(
                    "DeepL ответил 'слишком много запросов'. Пауза {0:.0f} с "
                    "(попытка {1}/{2})...".format(wait, attempt, self.retries)
                )
                time.sleep(wait)
                cooldown = min(self.MAX_COOLDOWN, cooldown * 1.7)

            except DeepLError as e:
                self._fatal_error = e
                self.log("DeepL: {0}".format(e))
                return

            except Exception as e:
                wait = min(1.5 * attempt, 8.0)
                self.log(
                    "Ошибка сети при обращении к DeepL (попытка {0}/{1}): {2}. "
                    "Повтор через {3:.1f} с.".format(attempt, self.retries, e, wait)
                )
                time.sleep(wait)

        self.stats["errors"] += len(batch)
        self.log(
            "Не удалось перевести пачку из {0} строк через DeepL, оставляю "
            "оригинал.".format(len(batch))
        )
