# -*- coding: utf-8 -*-
"""
Языковые пакеты LibreTranslate (Argos Translate): какие есть, какие уже
установлены на компьютере и автоматическая установка недостающих.

LibreTranslate при запуске берёт модели из папки пакетов Argos
(Windows: C:\\Users\\<имя>\\.local\\share\\argos-translate\\packages; на других
системах — $XDG_DATA_HOME/argos-translate/packages или
~/.local/share/argos-translate/packages; папку можно переопределить
переменной ARGOS_PACKAGES_DIR). Каждая модель — подпапка вида
translate-en_ru-1_9. Все модели переводят либо НА английский, либо С
английского (исключение — прямая пара pt<->es); между двумя неанглийскими
языками Argos переводит «через английский», и для этого нужны обе модели
(например, fr->en и en->ru).

Модуль:
- хранит снимок индекса пакетов (файл argos_index.json рядом с программой);
  при необходимости пытается скачать свежий индекс из интернета;
- сравнивает нужную пару языков с тем, что уже установлено;
- скачивает недостающие пакеты (.argosmodel — это zip-архив) и распаковывает
  их в папку пакетов — так же, как это делает сам Argos.
"""

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from i18n import tr

INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "argos_index.json")
DEFAULT_LINK_TEMPLATE = "https://argos-net.com/v1/translate-{from}_{to}-{version_}.argosmodel"

_RE_DIR = re.compile(r'^translate-([a-z]{2,3})_([a-z]{2,3})(?:-(.*))?$')


class ArgosError(Exception):
    pass


def packages_dir():
    """Папка, где Argos/LibreTranslate хранит установленные модели."""
    env = os.environ.get("ARGOS_PACKAGES_DIR")
    if env:
        return env
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(data_home, "argos-translate", "packages")


def _link_for(from_code, to_code, version, template=DEFAULT_LINK_TEMPLATE):
    return template.format(**{"from": from_code, "to": to_code,
                              "version_": str(version).replace(".", "_")})


def load_snapshot(path=SNAPSHOT_FILE):
    """{(из, в): {"version": ..., "links": [...]}} из встроенного снимка."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    template = data.get("link_template") or DEFAULT_LINK_TEMPLATE
    index = {}
    for p in data["packages"]:
        index[(p["from"], p["to"])] = {
            "version": p["version"],
            "links": [_link_for(p["from"], p["to"], p["version"], template)],
        }
    return index


def fetch_live_index(timeout=15):
    """Свежий индекс из интернета в том же виде, что и снимок; None — не
    удалось (нет сети и т.п.)."""
    try:
        req = urllib.request.Request(INDEX_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            items = json.loads(resp.read().decode("utf-8"))
        index = {}
        for p in items:
            links = [l for l in (p.get("links") or []) if l.lower().startswith("http")]
            if not links:
                links = [_link_for(p["from_code"], p["to_code"], p["package_version"])]
            index[(p["from_code"], p["to_code"])] = {"version": p["package_version"], "links": links}
        return index or None
    except Exception:
        return None


def get_index(log=None, use_live=True):
    """Индекс пакетов: свежий из интернета, а если не вышло — встроенный снимок."""
    log = log or (lambda msg: None)
    if use_live:
        live = fetch_live_index()
        if live:
            log(tr("Индекс пакетов Argos обновлён из интернета."))
            return live
        log(tr("Не удалось обновить индекс пакетов Argos (нет интернета?) — использую встроенную копию."))
    return load_snapshot()


def installed_pairs(pdir=None):
    """{(из, в): версия} — модели, уже лежащие в папке пакетов."""
    pdir = pdir or packages_dir()
    result = {}
    if not os.path.isdir(pdir):
        return result
    for name in os.listdir(pdir):
        full = os.path.join(pdir, name)
        if not os.path.isdir(full):
            continue
        from_code = to_code = version = None
        meta = os.path.join(full, "metadata.json")
        if os.path.isfile(meta):
            try:
                with open(meta, "r", encoding="utf-8") as f:
                    m = json.load(f)
                from_code, to_code = m.get("from_code"), m.get("to_code")
                version = m.get("package_version")
            except Exception:
                pass
        if not (from_code and to_code):
            m = _RE_DIR.match(name)
            if not m:
                continue
            from_code, to_code = m.group(1), m.group(2)
            if m.group(3):
                version = m.group(3).replace("_", ".")
        result[(from_code, to_code)] = version or "?"
    return result


def has_direct(src, dest):
    """Есть ли прямая модель src -> dest, не через английский (кроме пар с
    английским, где модель всегда прямая) — по встроенному снимку."""
    if src == dest:
        return False
    try:
        index = load_snapshot()
    except Exception:
        return False
    return (src, dest) in index and "en" not in (src, dest)


def pairs_needed(src, dest, index):
    """Какие модели нужны для перевода src -> dest: прямая (если она есть в
    индексе и ни один язык не английский), иначе src->en и en->dest."""
    if src == dest:
        return []
    if "en" in (src, dest) or (src, dest) in index:
        return [(src, dest)]
    return [(src, "en"), ("en", dest)]


def missing_pairs(needed, installed):
    return [p for p in needed if p not in installed]


def install_package(pair, info, pdir=None, log=None):
    """Скачивает один пакет и распаковывает в папку пакетов."""
    log = log or (lambda msg: None)
    pdir = pdir or packages_dir()
    os.makedirs(pdir, exist_ok=True)
    name = "translate-{0}_{1}".format(*pair)
    last_error = None
    for link in info["links"]:
        if not link.lower().startswith("http"):
            continue
        tmp_path = None
        try:
            req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                fd, tmp_path = tempfile.mkstemp(suffix=".argosmodel.part", dir=pdir)
                done = 0
                last_log = time.monotonic()
                with os.fdopen(fd, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= 3:
                            last_log = now
                            if total:
                                log(tr("Скачиваю {0}: {1:.0f}% ({2:.0f} из {3:.0f} МБ)").format(
                                    name, 100.0 * done / total, done / 1048576.0, total / 1048576.0))
                            else:
                                log(tr("Скачиваю {0}: {1:.0f} МБ").format(name, done / 1048576.0))
            if not zipfile.is_zipfile(tmp_path):
                raise ArgosError(tr("скачанный файл не является архивом .argosmodel"))
            with zipfile.ZipFile(tmp_path) as zf:
                zf.extractall(pdir)
            log(tr("Пакет {0} установлен.").format(name))
            return True
        except Exception as e:
            last_error = e
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
    raise ArgosError(tr("Не удалось скачать пакет {0}: {1}").format(name, last_error))


def ensure_pair(src, dest, log=None, pdir=None):
    """Проверяет, что для перевода src -> dest (коды LibreTranslate) на
    компьютере установлены все нужные модели, и докачивает недостающие.
    Возвращает список установленных пакетов-пар, которых не хватало (пустой —
    всё уже было). Бросает ArgosError, если нужной пары нет в индексе или
    скачать не удалось."""
    log = log or (lambda msg: None)
    pdir = pdir or packages_dir()
    log(tr("Проверяю языковые пакеты LibreTranslate (Argos) для пары {0} → {1}...").format(src, dest))
    log(tr("Папка языковых пакетов: {0}").format(pdir))
    installed = installed_pairs(pdir)
    snapshot_index = load_snapshot()
    needed = pairs_needed(src, dest, snapshot_index)
    missing = missing_pairs(needed, installed)
    if not missing:
        log(tr("Нужные языковые пакеты уже установлены: {0}.").format(
            ", ".join("{0}→{1}".format(*p) for p in needed) or "—"))
        return []
    index = get_index(log)
    needed = pairs_needed(src, dest, index)
    missing = missing_pairs(needed, installed)
    for p in missing:
        if p not in index:
            raise ArgosError(
                tr("Пары {0} → {1} нет в индексе пакетов Argos — LibreTranslate не сможет её перевести.")
                .format(*p))
    log(tr("Не хватает языковых пакетов: {0}. Скачиваю их (каждый около 100 МБ, нужен интернет)...").format(
        ", ".join("{0}→{1}".format(*p) for p in missing)))
    done = []
    for p in missing:
        install_package(p, index[p], pdir, log)
        done.append(p)
    return done
