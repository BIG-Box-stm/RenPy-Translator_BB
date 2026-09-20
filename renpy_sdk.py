# -*- coding: utf-8 -*-
"""
Автоматический запуск официального Ren'Py SDK для генерации файлов
перевода — то же самое действие, что и пункт меню разработчика в самой
игре "Generate Translations" (или команда SDK "translate"), только без
ручного открытия лаунчера SDK.

Важно: генерацию идентификаторов блоков (translate <язык> start_7c722cef:)
здесь никто не переизобретает — Ren'Py считает их по хэшу от внутренней
структуры сценария, и если сделать это "на глаз", метки могут не
совпасть с тем, что ожидает движок при загрузке перевода в игре.
Поэтому, как и с unrpyc, мы просто запускаем настоящий, официальный
исполняемый файл SDK с нужными аргументами — со всеми гарантиями
корректности, которые он даёт сам.

Имя языка (последний аргумент команды translate) — это просто название
папки game/tl/<имя>/, и одновременно то, что должно совпадать с
идентификатором языка, который использует сама игра при переключении
языка (Language() / renpy.change_language(...)). У разных игр это по
своему усмотрению разработчика: где-то это полное английское слово
("russian" — так называет языки сам Ren'Py по умолчанию), а где-то
короткий код ("ru"). Программа не может знать заранее, что ждёт
конкретная игра, поэтому имя языка ей передаёт вызывающий код (GUI),
а не этот модуль."""

import os
import subprocess
import sys

_LAUNCHER_NAMES = ["renpy.exe", "renpy.sh", "renpy.py"]


class RenpySdkError(Exception):
    pass


def find_launcher(sdk_dir):
    """Ищет исполняемый файл Ren'Py SDK внутри указанной пользователем
    папки: renpy.exe на Windows, renpy.sh на Linux/Mac, либо renpy.py
    как более старый общий вариант (запускается тем же интерпретатором,
    что и наша программа)."""
    for name in _LAUNCHER_NAMES:
        full = os.path.join(sdk_dir, name)
        if os.path.isfile(full):
            return full
    raise RenpySdkError(
        "В указанной папке не найден исполняемый файл Ren'Py SDK "
        "(renpy.exe / renpy.sh). Проверьте, что это папка самого SDK "
        "(например, 'renpy-8.х.х-sdk'), а не папка игры или проекта."
    )


def generate_translations(project_dir, sdk_dir, renpy_lang_name, log=None, timeout=None):
    """Запускает 'renpy <project_dir> translate <renpy_lang_name>' —
    официальную команду Ren'Py SDK, которая создаёт (или дополняет
    новыми строками) game/tl/<язык>/*.rpy, точно так же, как пункт меню
    разработчика в самой игре 'Generate Translations'. project_dir —
    папка проекта, СОДЕРЖАЩАЯ папку game (не сама game)."""
    log = log or (lambda msg: None)
    launcher = find_launcher(sdk_dir)

    if launcher.lower().endswith(".py"):
        cmd = [sys.executable, launcher, project_dir, "translate", renpy_lang_name]
    else:
        cmd = [launcher, project_dir, "translate", renpy_lang_name]

    log("Запускаю Ren'Py SDK: {0}".format(" ".join(cmd)))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log("  [renpy] " + line)
    proc.wait(timeout=timeout)
    if proc.returncode != 0:
        raise RenpySdkError(
            "Ren'Py SDK завершился с ошибкой (код {0}). Смотрите "
            "сообщения выше в журнале — обычно это неверно указанная "
            "папка проекта/SDK либо несовместимая версия SDK.".format(proc.returncode)
        )
    return proc.returncode
