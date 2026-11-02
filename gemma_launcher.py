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

from i18n import tr
import os
import re
import subprocess
import urllib.parse

EXE_NAME = "llama-server.exe"

# MTP — модуль ускорения генерации токенов для Gemma. Файл лежит в папке с
# основной моделью; имена у разных авторов и версий разные, поэтому программа
# сама ищет подходящий файл (см. find_mtp_candidates / choose_mtp). Допустимое
# число предлагаемых токенов: 0 (выключено) или 1..MTP_MAX_TOKENS.
MTP_MAX_TOKENS = 4

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
            tr("Файл {0} не найден в папке {1}. Укажите папку, где лежит "
            "сам llama-server.exe (обычно корень распакованного "
            "llama.cpp).").format(EXE_NAME, llama_folder)
        )
    return exe_path


def _host_port_from_url(base_url):
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8080
    return host, port


# Размер модели в имени файла: "12B", "31B", "E4B", "A4B" (для MoE) и т.п.
_RE_SIZE = re.compile(r'(?<![a-z0-9])([ea]?\d+(?:\.\d+)?b)(?![a-z0-9])', re.IGNORECASE)


def size_tokens(filename):
    """Множество «размерных» меток из имени файла: {"12b"}, {"e4b"}, {"26b", "a4b"}."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    return {m.group(1).lower() for m in _RE_SIZE.finditer(stem)}


def find_mtp_candidates(model_path):
    """Файлы MTP в папке с моделью: *.gguf с «mtp» в имени (кроме самой модели)."""
    folder = os.path.dirname(model_path or "")
    if not folder or not os.path.isdir(folder):
        return []
    model_name = os.path.basename(model_path).lower()
    result = []
    for name in sorted(os.listdir(folder)):
        low = name.lower()
        if low.endswith(".gguf") and "mtp" in low and low != model_name:
            result.append(name)
    return result


def choose_mtp(model_path, saved_name=None):
    """Какой файл MTP относится к выбранной модели. Возвращает
    (путь_или_None, варианты_для_выбора, причина):
      - ранее запомненный выбор пользователя (если файл на месте) -> ("saved");
      - единственный файл с той же «размерной» меткой (12B, 31B, E4B...) -> ("auto");
      - иначе путь None и список файлов, из которых пользователю нужно
        выбрать (все совпавшие по размеру, если их несколько, либо все
        найденные) -> ("ask");
      - в папке нет ни одного файла MTP -> (None, [], "none")."""
    folder = os.path.dirname(model_path or "")
    candidates = find_mtp_candidates(model_path)
    if not candidates:
        return None, [], "none"
    if saved_name and saved_name in candidates:
        return os.path.join(folder, saved_name), [], "saved"
    model_sizes = size_tokens(model_path)
    matched = [c for c in candidates if model_sizes and size_tokens(c) == model_sizes]
    if len(matched) == 1:
        return os.path.join(folder, matched[0]), [], "auto"
    return None, (matched if len(matched) > 1 else candidates), "ask"


def build_command(exe_path, model_path, ngl, np, ctx, n_predict, base_url, extra_args=None,
                  mtp_tokens=0, mtp_path=None):
    """Команда запуска llama-server. "-fa on" (flash attention) добавляется
    всегда — и для Translate Gemma, и для Gemma 4. Если mtp_tokens > 0,
    добавляются параметры модуля MTP: "--tensor-split 1" (только в этом
    режиме), затем загрузка MTP после основной модели: "--model-draft <файл>
    --spec-type draft-mtp --spec-draft-n-max <число> -ngld 99"."""
    host, port = _host_port_from_url(base_url)
    cmd = [
        exe_path,
        "-m", model_path,
        "-ngl", str(ngl),
        "-np", str(np),
        "-c", str(ctx),
        "-n", str(n_predict),
        "-fa", "on",
        *(extra_args or []),
    ]
    if mtp_tokens and mtp_tokens > 0:
        cmd += [
            "--tensor-split", "1",
            "--model-draft", mtp_path,
            "--spec-type", "draft-mtp",
            "--spec-draft-n-max", str(mtp_tokens),
            "-ngld", "99",
        ]
    cmd += ["--host", host, "--port", str(port)]
    return cmd


def launch(llama_folder, model_path, ngl, np, ctx, n_predict, base_url, log=None,
           extra_args=None, mtp_tokens=0, mtp_path=None):
    """Находит llama-server.exe в llama_folder и запускает его в новом
    окне консоли с указанными параметрами. Не ждёт завершения — сервер
    работает, пока окно открыто. Возвращает использованную команду
    (список аргументов)."""
    log = log or (lambda msg: None)
    if not model_path or not os.path.isfile(model_path):
        raise GemmaLauncherError(
            tr("Файл модели .gguf не найден: {0!r}. Укажите его в поле "
            "с путём к модели .gguf.").format(model_path)
        )
    if mtp_tokens and mtp_tokens > 0 and not (mtp_path and os.path.isfile(mtp_path)):
        raise GemmaLauncherError(
            tr("Файл MTP не найден: {0}. Проверьте, что он лежит в папке с моделью, "
               "либо поставьте «Предлагать MTP токенов» в 0.").format(mtp_path))
    exe_path = find_exe(llama_folder)
    cmd = build_command(exe_path, model_path, ngl, np, ctx, n_predict, base_url, extra_args,
                        mtp_tokens, mtp_path)
    log(tr("Найден llama-server: {0}").format(exe_path))
    log(tr("Запускаю: {0}").format(
        " ".join('"{0}"'.format(c) if " " in c else c for c in cmd)
    ))
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(cmd, cwd=llama_folder, creationflags=creationflags)
    return cmd
