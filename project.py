# -*- coding: utf-8 -*-
"""
Работа с папкой проекта Ren'Py: поиск tl/<язык>/, поиск .rpy файлов,
чтение/запись с сохранением исходных переносов строк и кодировки.
"""

from i18n import tr
import json
import os

CACHE_FILENAME = "_translator_cache.json"
CACHE_FILENAME_DEEPL = "_translator_cache_deepl.json"
CACHE_FILENAME_LIBRE = "_translator_cache_libretranslate.json"
# Строки, которые бесплатный Google возвращает с испорченным тегом/подстановкой
# («непереводимые для этого переводчика») — чтобы не отправлять их повторно.
CACHE_FILENAME_GOOGLE_UNFIT = "_translator_unfit_google.json"
# Gemma 4: один общий кэш на движок, без привязки к выбранному промпту. (Раньше
# у каждого промпта был свой файл «_translator_cache_gemma4_<тег>.json» — при
# первом запуске они сливаются в общий, см. load_gemma4_cache().) Строки режима
# «по полу» имеют ключ «Имя: текст», поэтому с обычными не смешиваются.
CACHE_FILENAME_GEMMA4 = "_translator_cache_gemma4.json"
CACHE_GEMMA4_LEGACY_PREFIX = "_translator_cache_gemma4_"
# Translate Gemma в режиме «Жёсткое согласование по полу» (построчно, «Имя: текст»).
CACHE_FILENAME_TRANSLATEGEMMA = "_translator_cache_translategemma.json"


def find_game_dir(project_path):
    """Возвращает путь к папке game (или None, если не нашлась) — нужна
    для распаковки .rpa/.rpyc, что происходит ДО генерации файлов
    перевода, так что папки tl может ещё не существовать."""
    game_sub = os.path.join(project_path, "game")
    if os.path.isdir(game_sub):
        return game_sub
    if os.path.basename(os.path.normpath(project_path)).lower() == "game":
        return project_path
    return None


def find_tl_dir(project_path):
    """Возвращает путь к папке game/tl (или None, если не найдена)."""
    candidates = [
        project_path,
        os.path.join(project_path, "game"),
    ]
    for base in candidates:
        tl = os.path.join(base, "tl")
        if os.path.isdir(tl):
            return tl
    return None


def list_language_folders(tl_dir):
    if not tl_dir or not os.path.isdir(tl_dir):
        return []
    result = []
    for name in sorted(os.listdir(tl_dir)):
        full = os.path.join(tl_dir, name)
        if os.path.isdir(full) and name.lower() != "none":
            result.append(name)
    return result


def find_rpy_files(lang_dir):
    result = []
    for root, _dirs, files in os.walk(lang_dir):
        for f in files:
            if f.lower().endswith(".rpy"):
                result.append(os.path.join(root, f))
    return sorted(result)


def read_lines(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.readlines()


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)


def load_cache(lang_dir, filename=CACHE_FILENAME):
    path = os.path.join(lang_dir, filename)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(lang_dir, cache, filename=CACHE_FILENAME):
    path = os.path.join(lang_dir, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=0)
    except Exception:
        pass


def load_settings(path):
    """Настройки самой программы (не проекта игры) — например, папка
    Ren'Py SDK, чтобы не указывать её заново при каждом запуске."""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_settings(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_gemma4_cache(lang_dir, log=None):
    """Общий кэш Gemma 4. Если его ещё нет, один раз сливает в него старые
    кэши по промптам («_translator_cache_gemma4_*.json»): при конфликте ключей
    побеждает самый свежий файл. Сами старые файлы не трогаются."""
    log = log or (lambda msg: None)
    path = os.path.join(lang_dir, CACHE_FILENAME_GEMMA4)
    if os.path.isfile(path):
        return load_cache(lang_dir, CACHE_FILENAME_GEMMA4)
    legacy = []
    try:
        for name in os.listdir(lang_dir):
            if name.startswith(CACHE_GEMMA4_LEGACY_PREFIX) and name.lower().endswith(".json"):
                legacy.append(os.path.join(lang_dir, name))
    except Exception:
        return {}
    if not legacy:
        return {}
    legacy.sort(key=lambda p: os.path.getmtime(p))
    merged = {}
    for p in legacy:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                merged.update(data)
        except Exception:
            continue
    if merged:
        save_cache(lang_dir, merged, CACHE_FILENAME_GEMMA4)
        log(tr("Gemma 4: старые кэши по промптам ({0} файл(ов)) слиты в общий {1}: записей — {2}.").format(
            len(legacy), CACHE_FILENAME_GEMMA4, len(merged)))
    return merged


def cache_files_of(lang_dir, engine_key):
    """Файлы кэша выбранного переводчика в папке перевода (существующие)."""
    names = {
        "google": [CACHE_FILENAME, CACHE_FILENAME_GOOGLE_UNFIT],
        "deepl": [CACHE_FILENAME_DEEPL],
        "libretranslate": [CACHE_FILENAME_LIBRE],
        "gemma4": [CACHE_FILENAME_GEMMA4],
        "gemma": [CACHE_FILENAME_TRANSLATEGEMMA],
    }.get(engine_key, [])
    paths = [os.path.join(lang_dir, n) for n in names]
    if engine_key == "gemma4":
        try:
            for name in sorted(os.listdir(lang_dir)):
                if name.startswith(CACHE_GEMMA4_LEGACY_PREFIX) and name.lower().endswith(".json"):
                    paths.append(os.path.join(lang_dir, name))
        except Exception:
            pass
    return [p for p in paths if os.path.isfile(p)]
