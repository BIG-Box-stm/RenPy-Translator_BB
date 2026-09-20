# -*- coding: utf-8 -*-
"""
Обёртка над официальным открытым проектом unrpyc
(https://github.com/CensoredUsername/unrpyc, автор CensoredUsername,
лицензия MIT). Раскомпиляция байт-кода Ren'Py (.rpyc -> .rpy) — это
отдельная многолетняя работа по обратной разработке AST движка, и эта
программа не пытается повторить её самостоятельно. Вместо этого при
необходимости скачивается настоящий unrpyc (один раз, с подтверждения
пользователя) и запускается как есть, с указанием авторства.

Ветка "master" — для Ren'Py 8 / Python 3 (актуальные игры).
Ветка "legacy_python2_ast" (или похожая) — для очень старых игр на
Ren'Py 6/7, но сам инструмент в этой ветке рассчитан на Python 2 — если
он не запустится, актуальные игры это обычно не затрагивает.
"""

import io
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

REPO_ZIP_URL = "https://github.com/CensoredUsername/unrpyc/archive/refs/heads/{branch}.zip"
DEFAULT_BRANCH = "master"


class UnrpycError(Exception):
    pass


def is_installed(tools_dir):
    return os.path.isfile(os.path.join(tools_dir, "unrpyc.py"))


def download(tools_dir, branch=DEFAULT_BRANCH, log=None, timeout=60):
    """Скачивает unrpyc с GitHub (официальный репозиторий,
    CensoredUsername/unrpyc, лицензия MIT) и распаковывает в tools_dir.
    Нужен интернет только на этот разовый шаг."""
    log = log or (lambda msg: None)
    url = REPO_ZIP_URL.format(branch=branch)
    log("Скачиваю unrpyc (CensoredUsername/unrpyc, MIT) с {0} ...".format(url))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.URLError as e:
        raise UnrpycError(
            "Не удалось скачать unrpyc ({0}). Проверьте подключение к "
            "интернету, либо скачайте вручную с "
            "https://github.com/CensoredUsername/unrpyc и распакуйте "
            "содержимое в папку: {1}".format(e, tools_dir)
        )

    os.makedirs(tools_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        if not names:
            raise UnrpycError("Скачанный архив unrpyc оказался пустым.")
        # В архиве всё лежит внутри одной папки вида "unrpyc-master/" —
        # убираем этот общий префикс, чтобы unrpyc.py оказался прямо в
        # tools_dir, рядом с папкой decompiler/.
        root_prefix = names[0].split("/")[0] + "/"
        for name in names:
            if name.endswith("/"):
                continue
            if not name.startswith(root_prefix):
                continue
            rel = name[len(root_prefix):]
            if not rel:
                continue
            out_path = os.path.join(tools_dir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with zf.open(name) as src, open(out_path, "wb") as dst:
                dst.write(src.read())

    if not is_installed(tools_dir):
        raise UnrpycError(
            "unrpyc скачался, но unrpyc.py не найден внутри архива — "
            "возможно, структура репозитория изменилась."
        )
    log("unrpyc готов к использованию ({0}).".format(tools_dir))


_RE_PROCESSED = re.compile(r'Processed (\d+) files\.')
_RE_DECOMPILED = re.compile(r'>\s*(\d+) files were successfully decompiled')
_RE_SKIPPED = re.compile(r'>\s*(\d+) files were skipped')


def _find_rpy_targets(game_dir):
    """Для каждого .rpyc в игре возвращает ожидаемый путь итогового
    .rpy (тот же путь, но с .rpy вместо .rpyc)."""
    targets = []
    for root, _dirs, files in os.walk(game_dir):
        for f in files:
            if f.lower().endswith(".rpyc"):
                targets.append(os.path.join(root, f)[:-1])  # .rpyc -> .rpy
    return targets


def decompile_all(game_dir, tools_dir, log=None, timeout=None, clobber=False):
    """Запускает unrpyc.py на всей папке игры — он сам находит и
    раскомпилирует все .rpyc файлы, создавая рядом читаемые .rpy (если
    такого .rpy ещё нет, если только не указан clobber=True — тогда
    существующие .rpy будут перезаписаны).

    Собственная итоговая сводка unrpyc ("Processed N files" и т.п.)
    разбирается и кладётся в результат под ключами 'reported_*' — но
    как чисто справочная информация. При параллельной раскомпиляции
    (несколько worker-процессов) у неё случается гонка: к моменту
    вывода финальной сводки .rpy уже создан ДРУГИМ воркером буквально
    долю секунды назад, и unrpyc по ошибке рапортует "already
    exists / skipped" для файлов, которые он же сам только что создал
    в этом самом запуске (итог — "0 успешно, N пропущено" даже когда
    все N реально раскомпилированы). Поэтому реальные, достоверные
    числа считаем сами: сравниваем список .rpy-файлов ДО запуска
    unrpyc и ПОСЛЕ — ключи 'real_created' (реально появившиеся в этом
    запуске), 'real_pre_existing' (были и до запуска) и
    'real_still_missing' (так и не появились — вероятно, ошибка при
    раскомпиляции конкретного файла)."""
    log = log or (lambda msg: None)
    unrpyc_py = os.path.join(tools_dir, "unrpyc.py")
    if not os.path.isfile(unrpyc_py):
        raise UnrpycError("unrpyc.py не найден в {0}".format(tools_dir))

    targets = _find_rpy_targets(game_dir)
    existed_before = {t for t in targets if os.path.isfile(t)}

    cmd = [sys.executable, unrpyc_py, game_dir]
    if clobber:
        cmd.append("--clobber")
    log("Запускаю: {0}".format(" ".join(cmd)))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    result = {"reported_processed": None, "reported_decompiled": None, "reported_skipped": None}
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        log("  [unrpyc] " + line)
        m = _RE_PROCESSED.search(line)
        if m:
            result["reported_processed"] = int(m.group(1))
        m = _RE_DECOMPILED.search(line)
        if m:
            result["reported_decompiled"] = int(m.group(1))
        m = _RE_SKIPPED.search(line)
        if m:
            result["reported_skipped"] = int(m.group(1))
    proc.wait(timeout=timeout)
    if proc.returncode != 0:
        raise UnrpycError(
            "unrpyc завершился с ошибкой (код {0}). Смотрите сообщения "
            "выше в журнале.".format(proc.returncode)
        )

    existed_after = {t for t in targets if os.path.isfile(t)}
    result["real_created"] = len(existed_after - existed_before)
    result["real_pre_existing"] = len(existed_before)
    result["real_still_missing"] = len(set(targets) - existed_after)
    return result
