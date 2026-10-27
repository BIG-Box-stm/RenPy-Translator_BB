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
import re
import subprocess
import sys

_LAUNCHER_NAMES = ["renpy.exe", "renpy.sh", "renpy.py"]

# SDK лежат рядом с программой: <папка программы>\RenPy SDK\renpy-8.5.3-sdk
# (и renpy-7.8.7-sdk и т.д.). Какой из них взять, программа решает сама по
# версии игры (см. detect_game_version() / pick_sdk()).
SDK_BASE_FOLDER_NAME = "RenPy SDK"
_RE_SDK_DIR = re.compile(r'^renpy-(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?-sdk$', re.IGNORECASE)
_RE_VERSION_TUPLE = re.compile(r'\(\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(\d+))?')
_RE_VERSION_TUPLE_PY = re.compile(r'version_tuple\s*=\s*\(\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(\d+))?')


class RenpySdkError(Exception):
    def __init__(self, message, output_lines=None):
        super().__init__(message)
        # Сырой (без нашего префикса "  [renpy] ") построчный вывод SDK —
        # нужен вызывающему коду (GUI), чтобы разобрать в нём конкретные
        # места ошибок парсера (файл + номер строки) и попытаться их
        # точечно починить (см. rpy_autofix.py). Пусто, если процесс SDK
        # вообще не запускался (например, не найден сам исполняемый файл).
        self.output_lines = output_lines or []


def sdk_base_dir(program_dir):
    """Папка, где программа ищет установленные SDK."""
    return os.path.join(program_dir, SDK_BASE_FOLDER_NAME)


def _has_launcher(sdk_dir):
    return any(os.path.isfile(os.path.join(sdk_dir, n)) for n in _LAUNCHER_NAMES)


def list_installed_sdks(base_dir):
    """Список SDK в base_dir: [{'name', 'path', 'version'}], по возрастанию
    версии. Берутся только папки вида renpy-X.Y.Z-sdk (допустима и
    четвёртая цифра: renpy-6.99.14.3-sdk), в которых реально есть
    исполняемый файл Ren'Py."""
    result = []
    if not os.path.isdir(base_dir):
        return result
    for name in os.listdir(base_dir):
        m = _RE_SDK_DIR.match(name)
        full = os.path.join(base_dir, name)
        if not m or not os.path.isdir(full) or not _has_launcher(full):
            continue
        version = tuple(int(g) for g in m.groups() if g is not None)
        result.append({"name": name, "path": full, "version": version})
    result.sort(key=lambda item: item["version"])
    return result


def pick_sdk(installed, game_major):
    """Самый свежий SDK, подходящий игре с мажорной версией game_major:
    для Ren'Py 8 и новее — самый свежий SDK 8+, для 7 — самый свежий 7.x,
    для 6 и старше — самый свежий 6.x. Более новый SDK собирает и более
    старые игры, поэтому полного совпадения версий не требуется. None —
    подходящего SDK в списке нет."""
    if game_major >= 8:
        pool = [s for s in installed if s["version"][0] >= 8]
    elif game_major == 7:
        pool = [s for s in installed if s["version"][0] == 7]
    else:
        pool = [s for s in installed if s["version"][0] == 6]
    return max(pool, key=lambda item: item["version"]) if pool else None


def best_per_major(installed):
    """По одному (самому свежему) SDK на каждую мажорную версию, от новых
    к старым — для окна ручного выбора."""
    best = {}
    for s in installed:
        major = s["version"][0]
        if major not in best or s["version"] > best[major]["version"]:
            best[major] = s
    return [best[m] for m in sorted(best, reverse=True)]


def _game_dir_of(project_path):
    project_path = (project_path or "").strip()
    if not project_path:
        return None
    if os.path.basename(os.path.normpath(project_path)).lower() == "game":
        return project_path
    sub = os.path.join(project_path, "game")
    return sub if os.path.isdir(sub) else None


def _tuple_from_match(m):
    return tuple(int(g) if g is not None else 0 for g in m.groups())


def detect_game_version(project_path):
    """Определяет версию Ren'Py, на которой сделана игра. Возвращает
    ((major, minor, patch), источник) либо (None, None).

    1) game/script_version*.txt (там записано что-то вроде "(8, 5, 2)");
    2) запасной вариант — renpy/__init__.py рядом с папкой game (есть у
       игр, собранных с полной копией движка): строка version_tuple = (...)."""
    game_dir = _game_dir_of(project_path)
    if not game_dir:
        return None, None
    try:
        names = sorted(os.listdir(game_dir))
    except Exception:
        names = []
    for name in names:
        low = name.lower()
        if low.startswith("script_version") and low.endswith(".txt"):
            try:
                with open(os.path.join(game_dir, name), "r", encoding="utf-8-sig", errors="replace") as f:
                    text = f.read()
            except Exception:
                continue
            m = _RE_VERSION_TUPLE.search(text)
            if m:
                return _tuple_from_match(m), name

    init_py = os.path.join(os.path.dirname(os.path.normpath(game_dir)), "renpy", "__init__.py")
    if os.path.isfile(init_py):
        try:
            with open(init_py, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            text = ""
        m = _RE_VERSION_TUPLE_PY.search(text)
        if m:
            return _tuple_from_match(m), "renpy/__init__.py"
    return None, None


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
    output_lines = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            output_lines.append(line)
            log("  [renpy] " + line)
    proc.wait(timeout=timeout)
    if proc.returncode != 0:
        raise RenpySdkError(
            "Ren'Py SDK завершился с ошибкой (код {0}). Смотрите "
            "сообщения выше в журнале — обычно это неверно указанная "
            "папка проекта/SDK либо несовместимая версия SDK.".format(proc.returncode),
            output_lines=output_lines,
        )
    return proc.returncode
