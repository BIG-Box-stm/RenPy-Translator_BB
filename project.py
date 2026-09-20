# -*- coding: utf-8 -*-
"""
Работа с папкой проекта Ren'Py: поиск tl/<язык>/, поиск .rpy файлов,
чтение/запись с сохранением исходных переносов строк и кодировки.
"""

import json
import os

CACHE_FILENAME = "_translator_cache.json"
CACHE_FILENAME_DEEPL = "_translator_cache_deepl.json"
CACHE_FILENAME_LIBRE = "_translator_cache_libretranslate.json"


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
