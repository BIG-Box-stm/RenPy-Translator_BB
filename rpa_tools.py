# -*- coding: utf-8 -*-
"""
Распаковка архивов Ren'Py (.rpa). Формат полностью открытый и простой
(это не байт-код, а обычный индекс со смещениями в файле), поэтому
написано с нуля, без сторонних библиотек.

Поддерживаются:
- RPA-1.0 — на деле это просто ZIP-файл с расширением .rpa;
- RPA-2.0 — заголовок "RPA-2.0 <hex-смещение>\\n";
- RPA-3.0 / RPA-3.2 — заголовок "RPA-3.0 <hex-смещение> <hex-ключ...>\\n",
  где смещения и длины в индексе зашифрованы через XOR этим ключом
  (в 3.2 может быть несколько частей ключа — они просто XOR'ятся вместе).

Индекс — это pickle (обычно сжатый zlib), со словарём
{имя_файла: [(смещение, длина[, префикс]), ...]}.
"""

import os
import pickle
import zlib


class RpaError(Exception):
    pass


def _read_index(f, offset, key=0):
    f.seek(offset)
    comp = f.read()
    try:
        raw = zlib.decompress(comp)
    except zlib.error:
        raw = comp  # на всякий случай, если индекс не сжат
    index = pickle.loads(raw, encoding="bytes")
    normalized = {}
    for name, entries in index.items():
        if isinstance(name, bytes):
            name = name.decode("utf-8")
        entry = entries[0]
        if len(entry) == 2:
            off, length = entry
            prefix = b""
        else:
            off, length, prefix = entry
            if isinstance(prefix, str):
                prefix = prefix.encode("latin1")
        off ^= key
        length ^= key
        normalized[name] = (off, length, prefix)
    return normalized


def _extract_zip(path, out_dir):
    import zipfile
    count = 0
    with zipfile.ZipFile(path) as zf:
        zf.extractall(out_dir)
        count = len(zf.namelist())
    return count


def extract_rpa(path, out_dir, log=None):
    """Распаковывает один .rpa файл в out_dir (с сохранением внутренней
    структуры папок). Возвращает количество распакованных файлов."""
    log = log or (lambda msg: None)
    with open(path, "rb") as f:
        header = f.readline()
        if not header.startswith(b"RPA-"):
            # RPA-1.0 — просто ZIP с другим расширением.
            return _extract_zip(path, out_dir)

        parts = header.split()
        version = parts[0].decode("ascii", errors="replace")
        offset = int(parts[1], 16)
        key = 0
        for part in parts[2:]:
            key ^= int(part, 16)

        index = _read_index(f, offset, key)
        count = 0
        for name, (off, length, prefix) in index.items():
            out_path = os.path.join(out_dir, name.replace("/", os.sep))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            f.seek(off)
            data = prefix + f.read(length - len(prefix))
            with open(out_path, "wb") as out:
                out.write(data)
            count += 1
        log("  {0}: {1}, файлов внутри — {2}".format(os.path.basename(path), version, count))
        return count


def find_rpa_files(game_dir):
    result = []
    for root, _dirs, files in os.walk(game_dir):
        for f in files:
            if f.lower().endswith(".rpa"):
                result.append(os.path.join(root, f))
    return sorted(result)


def extract_all(game_dir, log=None):
    """Распаковывает все .rpa файлы, найденные в папке игры, прямо в неё
    же (рядом с самими .rpa, с сохранением структуры папок внутри
    архива — то есть файлы лягут в свои обычные game/... пути).
    Возвращает (число архивов, общее число распакованных файлов)."""
    log = log or (lambda msg: None)
    archives = find_rpa_files(game_dir)
    if not archives:
        log("Архивов .rpa не найдено — возможно, игра уже распакована.")
        return 0, 0
    total_files = 0
    for path in archives:
        try:
            total_files += extract_rpa(path, game_dir, log=log)
        except Exception as e:
            log("  ОШИБКА распаковки {0}: {1}".format(os.path.basename(path), e))
    return len(archives), total_files
