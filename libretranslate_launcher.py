# -*- coding: utf-8 -*-
"""
Запуск локального сервера LibreTranslate (кнопка «Запустить
LibreTranslate» в окне программы) — того самого, к которому потом
обращается libretranslate_translate.py.

LibreTranslate у пользователя стоит не в виртуальном окружении, а прямо
в Scripts\\ той версии Python, что установлена из Microsoft
Store/python.org в %LocalAppData%\\Python\\pythoncore-<версия>-<разрядность>
(это стандартное расположение для "user install" Python на Windows).
Так как версия Python на компьютере пользователя может со временем
обновиться, путь не зашит в код — при каждом нажатии кнопки программа
сама ищет все папки pythoncore-*, в каждой проверяет
Scripts\\libretranslate.exe и берёт самую свежую версию Python среди
тех, где он нашёлся.

Сам процесс запускается в отдельном окне консоли (как попросил
пользователь — «запускает командную строку»), чтобы был виден вывод
LibreTranslate (адрес, ошибки загрузки моделей и т.п.) и чтобы его
можно было остановить Ctrl+C, не закрывая саму программу перевода.
"""

import os
import re
import subprocess

DEFAULT_LOAD_ONLY = ["en", "ru", "zh", "fr", "es"]

_RE_VERSION_DIR = re.compile(r'^pythoncore-(\d+)\.(\d+)-(32|64)$')


class LibreTranslateLauncherError(Exception):
    pass


def _local_app_data():
    path = os.environ.get("LocalAppData") or os.environ.get("LOCALAPPDATA")
    if not path:
        raise LibreTranslateLauncherError(
            "Не удалось определить папку %LocalAppData% — переменная "
            "окружения не задана (это не Windows?)."
        )
    return path


def find_libretranslate_exe(python_root=None):
    """Ищет libretranslate.exe в самой свежей версии Python, найденной
    под %LocalAppData%\\Python\\pythoncore-<версия>-<разрядность>\\Scripts.
    Среди нескольких версий выбирается та, где сам exe присутствует, с
    наибольшим (major, minor); 64-битная разрядность предпочитается
    32-битной при равной версии. Возвращает полный путь к exe.
    Бросает LibreTranslateLauncherError с понятным текстом, если ничего
    не нашлось."""
    root = python_root or os.path.join(_local_app_data(), "Python")
    if not os.path.isdir(root):
        raise LibreTranslateLauncherError(
            "Папка {0} не найдена. Похоже, Python здесь не установлен "
            "через официальный установщик/Microsoft Store как "
            "'для этого пользователя', либо LibreTranslate не был "
            "установлен командой 'pip install libretranslate'.".format(root)
        )

    candidates = []
    for name in os.listdir(root):
        m = _RE_VERSION_DIR.match(name)
        if not m:
            continue
        exe = os.path.join(root, name, "Scripts", "libretranslate.exe")
        if os.path.isfile(exe):
            major, minor, bits = int(m.group(1)), int(m.group(2)), int(m.group(3))
            candidates.append(((major, minor, bits), exe))

    if not candidates:
        raise LibreTranslateLauncherError(
            "В папке {0} не нашлось ни одной версии Python с "
            "установленным LibreTranslate (файл Scripts\\libretranslate.exe). "
            "Установите его командой: pip install libretranslate".format(root)
        )

    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def build_command(exe_path, languages=None):
    languages = languages or DEFAULT_LOAD_ONLY
    return [exe_path, "--load-only", ",".join(languages)]


def launch(python_root=None, languages=None, log=None):
    """Находит libretranslate.exe и запускает его в новом окне консоли.
    Не ждёт завершения (сервер работает, пока окно открыто) — функция
    возвращает сразу после запуска. Возвращает путь к найденному exe."""
    log = log or (lambda msg: None)
    exe_path = find_libretranslate_exe(python_root)
    cmd = build_command(exe_path, languages)
    log("Найден LibreTranslate: {0}".format(exe_path))
    log("Запускаю: {0}".format(" ".join(cmd)))
    # CREATE_NEW_CONSOLE — отдельное окно консоли, не заблокированное
    # ожиданием (start /wait не нужен); работает только на Windows, но
    # весь этот модуль имеет смысл только на Windows.
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(cmd, creationflags=creationflags)
    return exe_path
