# -*- coding: utf-8 -*-
"""
Общие помощники для режима «Учитывать пол говорящего» у онлайн-переводчиков
(Google, Google API, DeepL, LibreTranslate).

Идея та же, что у Gemma 4: перед текстом реплики стоит имя говорящего
(«Aelira: I was so tired.»), переводчик по имени выбирает правильный род, а
имя потом срезается из ответа (gemma4_translate.split_speaker). Ключ
строки в кэше — «Имя: текст» (так же, как у Gemma 4), поэтому такие записи
не смешиваются с обычными.

Миксин даёт переводчику:
  key_for(speaker, text)  — строит ключ и запоминает пару (имя, текст);
  _split_key(key)         — (имя | None, текст) по ключу;
  _strip_name(...)        — срезает из перевода имя говорящего;
  add_known_prefix(...)   — уже известные переводы имён (из файла имён).
"""

from gemma4_translate import split_speaker


class SpeakerMixin:
    def _init_speakers(self):
        self._parts = {}
        self._known = {}

    def add_known_prefix(self, speaker, translated_name):
        if speaker and translated_name:
            self._known.setdefault(speaker, set()).add(translated_name)

    def key_for(self, speaker, text):
        """Ключ запроса (он же ключ кэша): без говорящего — просто текст,
        иначе «Имя: текст»."""
        if not speaker:
            return text
        key = "{0}: {1}".format(speaker, text)
        self._parts[key] = (speaker, text)
        return key

    def _split_key(self, key):
        return self._parts.get(key, (None, key))

    def _strip_name(self, speaker, source_text, result):
        out, learned = split_speaker(result, speaker, source_text, self._known.get(speaker))
        if learned:
            self._known.setdefault(speaker, set()).add(learned)
        return out

    def _group_by_speaker(self, keys):
        """[(имя | None, [ключи])] — ключи одного говорящего вместе, в порядке
        первого появления."""
        groups = {}
        for k in keys:
            groups.setdefault(self._split_key(k)[0], []).append(k)
        return list(groups.items())
