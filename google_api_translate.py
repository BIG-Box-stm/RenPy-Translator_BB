# -*- coding: utf-8 -*-
"""
Клиент ОФИЦИАЛЬНОГО Google Cloud Translation API (v2, "Basic") — для
случая, когда в поле «Ключ Google API» введён ключ. Без ключа программа
по-прежнему пользуется бесплатным неофициальным эндпоинтом из
gtranslate.py.

Как получить ключ: проект в Google Cloud Console -> включить «Cloud
Translation API» -> «Credentials» -> «API key» (для API нужен подключённый
платёжный аккаунт; есть бесплатная месячная квота символов).

Как и DeepL, этот API поддерживает официальную пакетную отправку: в поле
"q" можно передать список строк, ответ приходит списком той же длины и
в том же порядке — без самодельного трюка с разделителем, который нужен
бесплатному эндпоинту. Формат "text" означает, что текст не обрабатывается
как HTML (ничего не экранируется в &amp; и т.п.).

Интерфейс класса тот же, что у DeepLTranslator/CachedTranslator
(вызываемый объект + .stats + .stop() + .warm_batch()).
"""

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

import core

ENDPOINT = "https://translation.googleapis.com/language/translate/v2"


class RateLimited(Exception):
    def __init__(self, retry_after=None):
        super().__init__("Google API: too many requests")
        self.retry_after = retry_after


class GoogleApiError(Exception):
    """Ошибка, после которой повторять запрос бессмысленно (неверный
    ключ, API не включён, квота исчерпана) — показывается пользователю."""


def raw_translate_batch(texts, api_key, src, dest, timeout=30):
    """Переводит список строк ОДНИМ запросом. Возвращает список переводов
    той же длины и в том же порядке."""
    url = ENDPOINT + "?" + urllib.parse.urlencode({"key": api_key})
    payload = {"q": texts, "source": src, "target": dest, "format": "text"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
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
        message = body_text[:300]
        reason = ""
        try:
            err = json.loads(body_text).get("error", {})
            message = err.get("message", message)
            reason = ",".join(d.get("reason", "") for d in err.get("errors", []) if d.get("reason"))
        except Exception:
            pass
        if e.code == 429 or reason in ("rateLimitExceeded", "userRateLimitExceeded"):
            retry_after = None
            try:
                ra = e.headers.get("Retry-After")
                if ra is not None:
                    retry_after = float(ra)
            except Exception:
                pass
            raise RateLimited(retry_after)
        if e.code in (400, 401, 403):
            raise GoogleApiError(
                "Google Cloud Translation отклонил запрос (HTTP {0}): {1}. "
                "Проверьте, что ключ скопирован полностью, что в проекте "
                "включён «Cloud Translation API» и подключён платёжный "
                "аккаунт, а ключ не ограничен другим API."
                .format(e.code, message)
            )
        raise GoogleApiError("Ошибка Google Cloud Translation (HTTP {0}): {1}".format(e.code, message))
    data = json.loads(raw)
    items = data["data"]["translations"]
    return [item["translatedText"] for item in items]


class GoogleApiTranslator:
    BASE_DELAY = 0.1
    MAX_DELAY = 4.0
    RATE_LIMIT_COOLDOWN = 10.0
    MAX_COOLDOWN = 60.0
    RECOVERY_STEP = 0.05
    RECOVERY_EVERY = 8

    BATCH_ITEMS = 50       # лимит API — 128 строк за запрос, берём с запасом
    BATCH_CHARS = 20000    # и меньше рекомендованных 30 000 символов

    def __init__(self, api_key, src, dest, cache=None, delay=None, retries=4,
                 log=None, on_progress=None, batch_items=None, batch_chars=None):
        self.api_key = api_key
        self.src = src
        self.dest = dest
        self.cache = cache if cache is not None else {}
        self.delay = delay if delay is not None else self.BASE_DELAY
        self.retries = retries
        self.log = log or (lambda msg: None)
        self.on_progress = on_progress or (lambda: None)
        if batch_items is not None:
            self.BATCH_ITEMS = batch_items
        if batch_chars is not None:
            self.BATCH_CHARS = batch_chars
        self.stats = {
            "requests": 0, "cache_hits": 0, "errors": 0, "rate_limit_hits": 0,
            "batch_requests": 0, "batched_lines": 0,
        }
        self._stopped = False
        self._success_streak = 0
        self._fatal_error = None

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
                        "Google API вернул неожиданное число строк ({0} вместо "
                        "{1}) — пропускаю эту пачку.".format(len(parts), len(batch))
                    )
                    return
                bad = [
                    s for s, t in zip(batch, parts)
                    if not core.tokens_preserved(s, t)
                ]
                if bad:
                    # Как и у DeepL: испорченную пачку не кэшируем (порча
                    # "залипала" бы навсегда). Строки получат новый шанс по
                    # одной в process_lines().
                    self.log(
                        "Google API повредил защищённый маркер (тег/подстановку) "
                        "как минимум в одной строке из {0} — эта пачка не "
                        "кэшируется.".format(len(batch))
                    )
                    if attempt < self.retries:
                        time.sleep(min(1.0 * attempt, 5.0))
                        continue
                    return
                for s, t in zip(batch, parts):
                    self.cache[s] = t
                self.stats["requests"] += 1
                if len(batch) > 1:
                    self.stats["batch_requests"] += 1
                    self.stats["batched_lines"] += len(batch)
                self._note_success()
                for _ in batch:
                    self.on_progress()
                sleep_for = self.delay + random.uniform(0, 0.1)
                if sleep_for:
                    time.sleep(sleep_for)
                return

            except RateLimited as e:
                self.stats["rate_limit_hits"] += 1
                self._note_rate_limit()
                wait = min(e.retry_after if e.retry_after else cooldown, self.MAX_COOLDOWN)
                self.log(
                    "Google API ответил 'слишком много запросов'. Пауза {0:.0f} с "
                    "(попытка {1}/{2})...".format(wait, attempt, self.retries)
                )
                time.sleep(wait)
                cooldown = min(self.MAX_COOLDOWN, cooldown * 1.7)

            except GoogleApiError as e:
                self._fatal_error = e
                self.log("Google API: {0}".format(e))
                return

            except Exception as e:
                wait = min(1.5 * attempt, 8.0)
                self.log(
                    "Ошибка сети при обращении к Google API (попытка {0}/{1}): "
                    "{2}. Повтор через {3:.1f} с.".format(attempt, self.retries, e, wait)
                )
                time.sleep(wait)

        self.stats["errors"] += len(batch)
        self.log(
            "Не удалось перевести пачку из {0} строк через Google API, "
            "оставляю оригинал.".format(len(batch))
        )
