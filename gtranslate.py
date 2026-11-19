# -*- coding: utf-8 -*-
"""
Простой клиент бесплатного (неофициального) Google Translate — того же
эндпоинта, которым пользуется веб-страница translate.google.com и многие
открытые библиотеки. Ключ API не нужен, но есть неявные лимиты на
количество запросов в единицу времени — поэтому есть пауза между
запросами, и эта пауза автоматически увеличивается, если Google начинает
отвечать "429 Too Many Requests".
"""

from i18n import tr
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import core
from speaker_mixin import SpeakerMixin

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


def raw_translate_batch(texts, src="en", dest="ru", timeout=15, inline=False):
    """Переводит несколько строк одним запросом, объединив их особым
    разделителем. Возвращает список переводов той же длины, что texts.
    Бросает BatchMismatch, если Google вернул не то количество кусков —
    тогда вызывающий код должен переводить эту пачку по одной строке.
    inline=True: строки склеиваются в ОДИН абзац (разделитель с пробелами, без
    переводов строки) — так перевод одной реплики учитывает соседние (нужно,
    чтобы имя говорящего в начале влияло на род во всех репликах пачки); по
    умолчанию каждая строка — отдельный абзац, и Google переводит их независимо."""
    sep = (" " + _BATCH_SEP + " ") if inline else ("\n" + _BATCH_SEP + "\n")
    combined = sep.join(texts)
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


class CachedTranslator(SpeakerMixin):
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
    JITTER = 0.25          # случайная добавка к паузе между запросами, сек
    # Как передавать имя говорящего в режиме «Учитывать пол говорящего»:
    #   "prefix" — «Имя: текст» перед КАЖДОЙ строкой пачки (надёжно, как одиночный
    #              запрос; для бесплатного Google расход символов ничего не стоит);
    #   "inline" — реплики одного говорящего склеиваются в один абзац, имя стоит один
    #              раз в начале (экономит символы платного API, но влияет ли имя на
    #              все реплики абзаца, зависит от переводчика).
    SPEAKER_JOIN = "prefix"
    FATAL_AFTER = 3        # столько запросов подряд без ответа сети — считаем, что связи нет

    def __init__(self, src, dest, cache=None, delay=None, retries=5,
                 log=None, on_progress=None, batch_items=None, batch_chars=None,
                 unfit=None, speaker_join=None):
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
            "batch_requests": 0, "batched_lines": 0, "unfit": 0,
        }
        # Строки, которые бесплатный Google возвращает с повреждённым тегом/
        # подстановкой («непереводимые для этого переводчика»): их не отправляем
        # повторно. Словарь {текст: 1} хранится GUI в отдельном файле рядом с
        # кэшем (см. project.CACHE_FILENAME_GOOGLE_UNFIT).
        self.unfit = unfit if unfit is not None else {}
        self._unfit_new = 0
        self._stopped = False
        self._success_streak = 0
        self._fatal_error = None      # нет связи — дальше не пытаемся (см. _call_with_retries)
        self._consec_fail = 0
        self._init_speakers()
        if speaker_join in ("prefix", "inline"):
            self.SPEAKER_JOIN = speaker_join

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

    # ------------------------------------------------------------------
    # Запросы с повторами (сеть, 429) — общая часть для одиночных и пакетных
    # ------------------------------------------------------------------
    def _raw_one(self, text):
        """Один запрос с одной строкой (переопределяется в Google API)."""
        return raw_translate(text, self.src, self.dest)

    def _raw_many(self, texts, inline=False):
        """Один запрос с несколькими строками, склеенными разделителем."""
        return raw_translate_batch(texts, self.src, self.dest, inline=inline)

    def _gave_up(self):
        """Исчерпаны попытки запроса. Несколько таких случаев подряд — связи
        нет: ставим _fatal_error, и дальше программа не пытается переводить и
        не засоряет журнал сообщениями по каждой строке."""
        self._consec_fail += 1
        if self._consec_fail >= self.FATAL_AFTER and self._fatal_error is None:
            self._fatal_error = RuntimeError(
                tr("нет связи с сервисом перевода ({0} запроса подряд остались без ответа)")
                .format(self._consec_fail))
            self.log(tr("Перевод прерван: {0}.").format(self._fatal_error))
        raise _GaveUp()

    def _call_with_retries(self, func, lines):
        """func() с повторами при сетевых ошибках и «429». lines — сколько
        строк в запросе (для текста сообщений). BatchMismatch пробрасывается
        наружу (повторять тот же запрос бессмысленно). Если попытки
        исчерпаны — бросает _GaveUp."""
        cooldown = self.RATE_LIMIT_COOLDOWN
        for attempt in range(1, self.retries + 1):
            if self._stopped or self._fatal_error is not None:
                raise _GaveUp()
            try:
                return func()
            except BatchMismatch:
                raise
            except _GaveUp:
                raise
            except RateLimited as e:
                self.stats["rate_limit_hits"] += 1
                self._note_rate_limit()
                wait = e.retry_after if e.retry_after else cooldown
                wait = min(wait, self.MAX_COOLDOWN)
                if lines > 1:
                    self.log(
                        tr("Google Translate ответил '429 Too Many Requests' при "
                        "пакетном переводе ({0} строк). Пауза {1:.0f} с "
                        "(попытка {2}/{3})...").format(lines, wait, attempt, self.retries)
                    )
                else:
                    self.log(
                        tr("Google Translate ответил '429 Too Many Requests' "
                        "(слишком много запросов). Делаю паузу {0:.0f} с и "
                        "замедляю дальнейшие запросы (попытка {1}/{2})...")
                        .format(wait, attempt, self.retries)
                    )
                time.sleep(wait)
                cooldown = min(self.MAX_COOLDOWN, cooldown * 1.7)
            except Exception as e:  # сеть нестабильна и т.п.
                wait = min(1.5 * attempt, 8.0)
                if lines > 1:
                    self.log(
                        tr("Ошибка пакетного перевода (попытка {0}/{1}): {2}. "
                        "Повтор через {3:.1f} с.").format(attempt, self.retries, e, wait)
                    )
                else:
                    self.log(
                        tr("Ошибка перевода (попытка {0}/{1}): {2}. Повтор через {3:.1f} с.")
                        .format(attempt, self.retries, e, wait)
                    )
                time.sleep(wait)
        self._gave_up()

    def _after_success(self, requests_lines):
        self._consec_fail = 0
        self._note_success()
        sleep_for = self.delay + random.uniform(0, self.JITTER)
        if sleep_for:
            time.sleep(sleep_for)

    def _mark_unfit(self, key):
        """Переводчик вернул эту строку с повреждённым защищённым маркером —
        повторять запрос бессмысленно (результат тот же): строка помечается
        непереводимой для этого переводчика и больше не отправляется."""
        if key not in self.unfit:
            self.unfit[key] = 1
            self.stats["unfit"] += 1
            self._unfit_new += 1
        self.on_progress()      # строка обработана (пусть и неудачно) — для полосы прогресса

    # ------------------------------------------------------------------
    # Одиночный перевод. key — либо просто текст, либо «Имя: текст» (режим
    # «Учитывать пол говорящего»): тогда уходит весь ключ, а имя срезается.
    # ------------------------------------------------------------------
    def _translate_single(self, key):
        """Переводит одну строку. Возвращает перевод или None. Повреждённый
        маркер НЕ повторяется — строка сразу помечается непереводимой."""
        if key in self.cache:
            return self.cache[key]
        if key in self.unfit or self._fatal_error is not None:
            return None
        speaker, text = self._split_key(key)
        try:
            result = self._call_with_retries(lambda: self._raw_one(key), 1)
        except _GaveUp:
            if self._stopped or self._fatal_error is not None:
                return None
            self.stats["errors"] += 1
            self.log(tr("Не удалось перевести строку, оставляю оригинал: {0!r}").format(text[:60]))
            return None
        if speaker:
            result = self._strip_name(speaker, text, result)
        if not core.tokens_preserved(text, result):
            self._mark_unfit(key)
            return None
        self.cache[key] = result
        self.stats["requests"] += 1
        self.on_progress()
        self._after_success(1)
        return result

    def __call__(self, text):
        # Готовое из кэша отдаём и после остановки. Нет перевода — None (а не
        # исходный текст/ключ «Имя: текст», см. комментарий в DeepLTranslator).
        if text in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[text]
        if self._stopped or text in self.unfit or self._fatal_error is not None:
            return None
        return self._translate_single(text)

    # ------------------------------------------------------------------
    # Пакетный перевод (несколько строк одним запросом) — сильно ускоряет
    # работу на больших файлах по сравнению с переводом по одной строке.
    # ------------------------------------------------------------------
    def warm_batch(self, texts):
        """Заранее переводит список уникальных строк пачками и складывает
        результат в self.cache. В режиме «Учитывать пол говорящего» пачки
        собираются из реплик ОДНОГО говорящего, и имя ставится один раз в
        начале объединённого текста. Если в пачке есть «брак» (потерян/испорчен
        маркер или не совпало число кусков), пачка делится пополам, и так,
        пока не найдутся одиночные битые строки — они помечаются
        непереводимыми, а остальные переводятся и кэшируются сразу."""
        pending = [t for t in dict.fromkeys(texts) if t not in self.cache and t not in self.unfit]
        if self.SPEAKER_JOIN == "inline":
            groups = self._group_by_speaker(pending)      # пачки из реплик одного говорящего
        else:
            groups = [(None, pending)]                    # имя стоит у каждой строки — группировать не нужно
        for speaker, keys in groups:
            idx = 0
            while idx < len(keys):
                if self._stopped or self._fatal_error is not None:
                    return
                batch = []
                total_chars = 0
                while idx < len(keys) and len(batch) < self.BATCH_ITEMS:
                    k = keys[idx]
                    n = len(self._split_key(k)[1]) if self.SPEAKER_JOIN == "inline" else len(k)
                    if batch and total_chars + n > self.BATCH_CHARS:
                        break
                    batch.append(k)
                    total_chars += n
                    idx += 1
                self._translate_group(batch, top=True)
        if self._unfit_new:
            self.log(
                tr("Бесплатный Google не смог корректно перевести строк: {0} (теряется тег или "
                "подстановка). Они помечены непереводимыми для этого переводчика и не будут "
                "отправляться повторно — их можно перевести другим переводчиком.")
                .format(self._unfit_new)
            )
            self._unfit_new = 0

    def _translate_group(self, batch, top=False):
        if self._stopped or not batch or self._fatal_error is not None:
            return
        if len(batch) == 1:
            self._translate_single(batch[0])
            return
        speakers = [self._split_key(k)[0] for k in batch]
        texts = [self._split_key(k)[1] for k in batch]
        merged = self.SPEAKER_JOIN == "inline" and speakers[0] is not None
        if merged:
            # Реплики одного говорящего — один абзац, имя один раз в начале.
            send = list(texts)
            send[0] = "{0}: {1}".format(speakers[0], texts[0])
        else:
            send = list(batch)          # ключ «Имя: текст» (или просто текст) у каждой строки
        reason = None
        try:
            parts = self._call_with_retries(lambda: self._raw_many(send, inline=merged), len(batch))
        except BatchMismatch as e:
            reason = ("mismatch", e)
        except _GaveUp:
            if self._stopped or self._fatal_error is not None:
                return
            self.log(
                tr("Не удалось перевести пачку из {0} строк одним запросом — "
                "перевожу по одной.").format(len(batch))
            )
            for k in batch:
                if self._stopped or self._fatal_error is not None:
                    return
                self._translate_single(k)
            return
        else:
            if merged:
                parts = [self._strip_name(speakers[0], texts[0], parts[0])] + list(parts[1:])
            else:
                parts = [self._strip_name(sp, tx, pt) if sp else pt
                         for sp, tx, pt in zip(speakers, texts, parts)]
            bad = [t_ for t_, r_ in zip(texts, parts) if not core.tokens_preserved(t_, r_)]
            if not bad:
                for key, translated in zip(batch, parts):
                    self.cache[key] = translated
                self.stats["requests"] += 1
                self.stats["batch_requests"] += 1
                self.stats["batched_lines"] += len(batch)
                for _ in batch:
                    self.on_progress()
                self._after_success(len(batch))
                return
            reason = ("bad", None)

        if top:
            # Одно сообщение на исходную пачку — без отчёта о каждом шаге деления.
            if reason[0] == "bad":
                self.log(
                    tr("Пакетный перевод повредил защищённый маркер (тег/подстановку) "
                    "как минимум в одной строке из {0} — делю пачку пополам, пока не "
                    "переведётся.").format(len(batch))
                )
            else:
                e = reason[1]
                self.log(
                    tr("Пакетный перевод не совпал по количеству строк ({0} вместо {1}) — "
                    "делю пачку пополам, пока не переведётся.").format(e.got, e.expected)
                )
        mid = len(batch) // 2
        self._translate_group(batch[:mid])
        self._translate_group(batch[mid:])


class _GaveUp(Exception):
    """Попытки запроса исчерпаны (сеть/лимиты) — внутренний сигнал."""
