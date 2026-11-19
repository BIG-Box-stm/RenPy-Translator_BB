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

from i18n import tr
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

import core
import gtranslate

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
                tr("Google Cloud Translation отклонил запрос (HTTP {0}): {1}. "
                "Проверьте, что ключ скопирован полностью, что в проекте "
                "включён «Cloud Translation API» и подключён платёжный "
                "аккаунт, а ключ не ограничен другим API.")
                .format(e.code, message)
            )
        raise GoogleApiError(tr("Ошибка Google Cloud Translation (HTTP {0}): {1}").format(e.code, message))
    data = json.loads(raw)
    items = data["data"]["translations"]
    return [item["translatedText"] for item in items]


class GoogleApiTranslator(gtranslate.CachedTranslator):
    """Официальный Cloud Translation API. Вся логика (пачки, деление
    пополам при «браке», режим «Учитывать пол говорящего», учёт обрыва связи)
    общая с бесплатным Google (gtranslate.CachedTranslator); здесь только
    сами запросы к API. Пачка — это ОДИН элемент "q" с несколькими
    строками, склеенными разделителем (как у бесплатного Google); в режиме
    «Учитывать пол говорящего» имя стоит перед каждой строкой пачки."""

    BASE_DELAY = 0.1
    MAX_DELAY = 4.0
    RATE_LIMIT_COOLDOWN = 10.0
    MAX_COOLDOWN = 60.0
    RECOVERY_STEP = 0.05
    RECOVERY_EVERY = 8
    JITTER = 0.1

    BATCH_ITEMS = 2        # по умолчанию 2 строки: API платный, и битая пачка тратит символы
    BATCH_CHARS = 20000
    # Имя говорящего — перед КАЖДОЙ строкой пачки («Имя: текст»), как у бесплатного
    # Google: режим "inline" (имя один раз на пачку) на практике не даёт нужного
    # рода. Включается обратно ключом "google_speaker_join": "inline" в настройках.
    SPEAKER_JOIN = "prefix"

    def __init__(self, api_key, src, dest, cache=None, delay=None, retries=4,
                 log=None, on_progress=None, batch_items=None, batch_chars=None,
                 speaker_join=None):
        super().__init__(src, dest, cache=cache, delay=delay, retries=retries, log=log,
                         on_progress=on_progress, batch_items=batch_items, batch_chars=batch_chars,
                         speaker_join=speaker_join)
        self.api_key = api_key

    def _api(self, q):
        try:
            return raw_translate_batch(q, self.api_key, self.src, self.dest)
        except RateLimited as e:
            raise gtranslate.RateLimited(e.retry_after)
        except GoogleApiError as e:
            # Ошибка ключа/квоты — повторять бессмысленно.
            self._fatal_error = e
            self.log("Google API: {0}".format(e))
            raise gtranslate._GaveUp()

    def _raw_one(self, text):
        return self._api([text])[0]

    def _raw_many(self, texts, inline=False):
        sep = (" " + gtranslate._BATCH_SEP + " ") if inline else ("\n" + gtranslate._BATCH_SEP + "\n")
        combined = sep.join(texts)
        translated = self._api([combined])[0]
        parts = [p.strip() for p in gtranslate._BATCH_SEP_RE.split(translated)]
        if len(parts) != len(texts):
            raise gtranslate.BatchMismatch(len(texts), len(parts))
        return parts
