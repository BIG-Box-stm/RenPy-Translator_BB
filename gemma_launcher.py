# -*- coding: utf-8 -*-
"""
Запуск локального сервера Translate Gemma (llama-server.exe из состава
llama.cpp) — кнопка «Запустить Translate Gemma» в окне программы.

В отличие от libretranslate_launcher.py, тут не нужно ничего искать по
диску подбором версий: пользователь один раз указывает в интерфейсе
папку, где лежит сам llama-server.exe, и отдельно — файл модели .gguf.
Параметры запуска (загрузка GPU, число потоков, контекстное окно, макс.
токенов на ответ) задаются в интерфейсе и могут отличаться от машины к
машине, поэтому передаются через launch(), а не зашиты внутри.
"""

import os
import subprocess
import urllib.parse

EXE_NAME = "llama-server.exe"


class GemmaLauncherError(Exception):
    pass


def find_exe(llama_folder):
    """llama-server.exe ожидается прямо в корне указанной папки (как в
    рабочей команде пользователя: cd /d X:\\llama && llama-server.exe ...).
    Никакого перебора версий/подпапок, в отличие от LibreTranslate —
    здесь нет стандартного места установки, которое можно было бы
    угадывать."""
    exe_path = os.path.join(llama_folder, EXE_NAME)
    if not os.path.isfile(exe_path):
        raise GemmaLauncherError(
            "Файл {0} не найден в папке {1}. Укажите папку, где лежит "
            "сам llama-server.exe (обычно корень распакованного "
            "llama.cpp).".format(EXE_NAME, llama_folder)
        )
    return exe_path


def _host_port_from_url(base_url):
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8080
    return host, port


def build_command(exe_path, model_path, ngl, np, ctx, n_predict, base_url):
    host, port = _host_port_from_url(base_url)
    return [
        exe_path,
        "-m", model_path,
        "-ngl", str(ngl),
        "-np", str(np),
        "-c", str(ctx),
        "-n", str(n_predict),
        "--host", host,
        "--port", str(port),
    ]


def launch(llama_folder, model_path, ngl, np, ctx, n_predict, base_url, log=None):
    """Находит llama-server.exe в llama_folder и запускает его в новом
    окне консоли с указанными параметрами. Не ждёт завершения — сервер
    работает, пока окно открыто. Возвращает использованную команду
    (список аргументов)."""
    log = log or (lambda msg: None)
    if not model_path or not os.path.isfile(model_path):
        raise GemmaLauncherError(
            "Файл модели .gguf не найден: {0!r}. Укажите его в поле "
            "«Модель Translate Gemma .gguf».".format(model_path)
        )
    exe_path = find_exe(llama_folder)
    cmd = build_command(exe_path, model_path, ngl, np, ctx, n_predict, base_url)
    log("Найден llama-server: {0}".format(exe_path))
    log("Запускаю: {0}".format(
        " ".join('"{0}"'.format(c) if " " in c else c for c in cmd)
    ))
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(cmd, cwd=llama_folder, creationflags=creationflags)
    return cmd
