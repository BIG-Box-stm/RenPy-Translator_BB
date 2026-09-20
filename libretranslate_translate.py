# -*- coding: utf-8 -*-
"""
Клиент локального LibreTranslate (self-hosted, работает офлайн на вашем
компьютере). В отличие от Google, LibreTranslate поддерживает
ОФИЦИАЛЬНУЮ пакетную отправку: можно передать список строк в поле "q",
и сервер вернёт список переводов той же длины и в ТОМ ЖЕ порядке — без
всякого трюка с разделителями.

Документация: https://docs.libretranslate.com/
"""

import json
import time
import urllib.error
import urllib.request

import core

DEFAULT_URL = "http://localhost:5000"


class LibreTranslateError(Exception):
    """Базовая ошибка LibreTranslate."""


class LibreTranslateConnectionError(LibreTranslateError):
    """Не удалось подключиться к серверу вообще (не запущен, неверный
    адрес и т.п.) — пытаться дальше бессмысленно."""


class LibreTranslateRequestError(LibreTranslateError):
    """Сервер ответил ошибкой на конкретный запрос (например, слишком
    длинный текст за один раз) — саму пачку стоит попробовать поделить
    на части поменьше."""

    def __init__(self, status, message):
        super().__init__("HTTP {0}: {1}".format(status, message))
        self.status = status


def raw_translate_batch(texts, base_url, src, dest, api_key=None, timeout=60):
    """Переводит список строк ОДНИМ запросом (родная пакетная отправка
    LibreTranslate). Возвращает список переводов той же длины и в том же
    порядке, что texts."""
    url = base_url.rstrip("/") + "/translate"
    payload = {
        "q": texts,
        "source": src,
        "target": dest,
        "format": "text",
    }
    if api_key:
        payload["api_key"] = api_key
    body = json.dumps(payload).encode("utf-8")
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
        msg = body_text
        try:
            msg = json.loads(body_text).get("error", body_text)
        except Exception:
            pass
        raise LibreTranslateRequestError(e.code, msg)
    except urllib.error.URLError as e:
        raise LibreTranslateConnectionError(
            "Не удалось подключиться к LibreTranslate по адресу {0} ({1}). "
            "Убедитесь, что программа LibreTranslate запущена на этом "
            "компьютере и адрес указан верно.".format(url, e.reason)
        )
    data = json.loads(raw)
    result = data.get("translatedText")
    if isinstance(result, str):
        result = [result]
    if not isinstance(result, list) or len(result) != len(texts):
        raise LibreTranslateRequestError(
            200,
            "Неожиданный ответ сервера ({0} строк вместо {1})."
            .format(len(result) if isinstance(result, list) else "?", len(texts)),
        )
    return result


class LibreTranslator:
    """Тот же интерфейс, что у gtranslate.CachedTranslator и
    deepl_translate.DeepLTranslator (вызываемый объект + .stats +
    .stop() + .warm_batch()), чтобы программа могла использовать любой
    из движков перевода одинаково."""

    BASE_DELAY = 0.0        # локальный сервер — задержка обычно не нужна
    RETRY_COOLDOWN = 3.0
    MAX_COOLDOWN = 20.0

    BATCH_ITEMS = 25
    BATCH_CHARS = 6000

    def __init__(self, base_url, src, dest, api_key=None, cache=None,
                 delay=None, retries=3, log=None, on_progress=None,
                 batch_items=None, batch_chars=None):
        self.base_url = (base_url or DEFAULT_URL).strip()
        self.src = src
        self.dest = dest
        self.api_key = api_key or None
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
        self._fatal_error = None

    def stop(self):
        self._stopped = True

    def __call__(self, text):
        if self._stopped or self._fatal_error:
            return text
        if text in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[text]
        self._translate_with_split([text])
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
            self._translate_with_split(batch)

    def _translate_with_split(self, batch):
        """Переводит пачку; если сервер отклоняет запрос как таковой
        (например, суммарный текст слишком длинный), делит пачку пополам
        и пробует снова — пока не получится или пока не останется по
        одной строке за раз."""
        if not batch or self._stopped or self._fatal_error:
            return
        if self._translate_once(batch) or len(batch) == 1:
            return
        mid = len(batch) // 2
        self._translate_with_split(batch[:mid])
        self._translate_with_split(batch[mid:])

    def _translate_once(self, batch):
        """True — пачка обработана (успешно или после исчерпания
        попыток, оригинал оставлен как есть). False — стоит попробовать
        разделить пачку на части поменьше."""
        cooldown = self.RETRY_COOLDOWN
        for attempt in range(1, self.retries + 1):
            if self._stopped or self._fatal_error:
                return True
            try:
                parts = raw_translate_batch(
                    batch, self.base_url, self.src, self.dest, self.api_key,
                )
                bad = [
                    src_text for src_text, translated in zip(batch, parts)
                    if not core.tokens_preserved(src_text, translated)
                ]
                if bad:
                    if len(batch) > 1:
                        # Не кэшируем ничего из этой пачки и пробуем
                        # разделить её на части поменьше (см.
                        # _translate_with_split) — так проблема сузится
                        # до конкретной строки, а не испортит кэш для
                        # всех остальных, у которых маркеры были в порядке.
                        self.log(
                            "LibreTranslate повредил защищённый маркер "
                            "(тег/подстановку) как минимум в одной строке из "
                            "{0} — делю пачку на части поменьше, чтобы не "
                            "закэшировать испорченный результат.".format(len(batch))
                        )
                        return False
                    self.log(
                        "LibreTranslate повредил защищённый маркер "
                        "(тег/подстановку) (попытка {0}/{1}) — не сохраняю "
                        "в кэш, пробую ещё раз.".format(attempt, self.retries)
                    )
                    time.sleep(cooldown)
                    cooldown = min(self.MAX_COOLDOWN, cooldown * 1.7)
                    continue
                for src_text, translated in zip(batch, parts):
                    self.cache[src_text] = translated
                self.stats["requests"] += 1
                if len(batch) > 1:
                    self.stats["batch_requests"] += 1
                    self.stats["batched_lines"] += len(batch)
                for _ in batch:
                    self.on_progress()
                if self.delay:
                    time.sleep(self.delay)
                return True

            except LibreTranslateConnectionError as e:
                self._fatal_error = e
                self.log(str(e))
                return True

            except LibreTranslateRequestError as e:
                if len(batch) > 1:
                    # Скорее всего пачка слишком большая для сервера —
                    # молча пробуем разделить её (см. _translate_with_split).
                    return False
                self.log(
                    "Ошибка LibreTranslate (попытка {0}/{1}): {2}."
                    .format(attempt, self.retries, e)
                )
                time.sleep(cooldown)
                cooldown = min(self.MAX_COOLDOWN, cooldown * 1.7)

            except Exception as e:
                wait = min(1.0 * attempt, 5.0)
                self.log(
                    "Ошибка сети LibreTranslate (попытка {0}/{1}): {2}. "
                    "Повтор через {3:.1f} с.".format(attempt, self.retries, e, wait)
                )
                time.sleep(wait)

        self.stats["errors"] += len(batch)
        self.log(
            "Не удалось перевести {0} строк через LibreTranslate, оставляю "
            "оригинал.".format(len(batch))
        )
        return True
