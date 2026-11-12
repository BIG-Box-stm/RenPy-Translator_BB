# -*- coding: utf-8 -*-
"""
Логика «Мастера установки» (без Tkinter, чтобы можно было проверять отдельно
от окна — само окно в setup_wizard.py).

Что здесь есть:
1. Сведения о системе (Windows/архитектура, ОЗУ, видеокарты, видеопамять,
   драйвер/CUDA у NVIDIA) и рекомендация переводчика по видеопамяти.
2. Поиск сборок llama.cpp на GitHub и выбор нужного zip-архива (CPU / Vulkan /
   CUDA 12 / CUDA 13, x64 / arm64) + отдельный архив с CUDA-библиотеками.
3. Скачивание файлов с докачкой после обрыва и проверкой размера/SHA-256
   (для моделей Hugging Face эти значения берутся с «указателя» файла по
   ссылке /raw/ — это тот же текст, что на вкладке Xet Pointer Details).
4. Установка llama.cpp в выбранную папку с манифестом (_llama_install.json):
   по нему при переустановке удаляются ТОЛЬКО файлы самой llama.cpp, а папка
   models и CUDA-библиотеки не трогаются (последние — только при смене версии
   CUDA).
5. Установка LibreTranslate через pip.

Важно: релизы llama.cpp на GitHub выходят как «Pre-release», поэтому обычный
запрос releases/latest не подходит (он вернул бы информационный релиз без
файлов) — берётся список релизов и пропускаются те, где нет архивов.
"""

from i18n import tr
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

USER_AGENT = "UniversalRenPyTranslator/1.0"
LLAMA_REPO = "ggml-org/llama.cpp"
GITHUB_API = "https://api.github.com/repos/" + LLAMA_REPO
GITHUB_DL = "https://github.com/" + LLAMA_REPO + "/releases/download"
GITHUB_ATOM = "https://github.com/" + LLAMA_REPO + "/releases.atom"
HF_BASE = "https://huggingface.co"

MANIFEST_NAME = "_llama_install.json"
MODELS_DIRNAME = "models"
DOWNLOADS_DIRNAME = "_downloads"
SERVER_EXE = "llama-server.exe"

# Известные версии CUDA в сборках llama.cpp — используются, только если не
# удалось получить список файлов релиза через API (тогда имена собираются по
# шаблону, который виден в ссылках на скачивание).
FALLBACK_CUDA = {"cuda12": "12.4", "cuda13": "13.4"}


class SetupError(Exception):
    """Ошибка с понятным пользователю текстом."""


class Cancelled(Exception):
    """Пользователь нажал «Отмена»."""


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def _run(cmd, timeout=20):
    """Запуск внешней команды без окна консоли. (код возврата, stdout)."""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=flags)
        return p.returncode, p.stdout or ""
    except Exception:
        return -1, ""


def _http_get(url, timeout=30, headers=None):
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _sleep(seconds, cancel):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        _check_cancel(cancel)
        time.sleep(0.1)


def format_size(num_bytes):
    if num_bytes is None:
        return "?"
    value = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return ("{0:.0f} {1}" if unit in ("Б", "КБ") else "{0:.1f} {1}").format(value, tr(unit))
        value /= 1024.0


# ---------------------------------------------------------------------------
# Сведения о системе
# ---------------------------------------------------------------------------

def detect_arch():
    m = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE")
         or platform.machine() or "").lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("amd64", "x86_64", "x64"):
        return "x64"
    return m or "x64"


def total_ram_bytes():
    if os.name == "nt":
        try:
            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = _MemStatus()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullTotalPhys)
        except Exception:
            return None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return None


def _find_nvidia_smi():
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for path in (r"C:\Windows\System32\nvidia-smi.exe",
                 r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if os.path.isfile(path):
            return path
    return None


def _to_float(text):
    try:
        return float(str(text).strip())
    except Exception:
        return None


def detect_nvidia():
    """Видеокарты NVIDIA по nvidia-smi: [{'name', 'vram_mb', 'driver', 'cc', 'cuda'}].
    cuda — максимальная версия CUDA, которую поддерживает драйвер, кортеж
    (major, minor) или None. Пустой список — nvidia-smi нет/не отвечает."""
    exe = _find_nvidia_smi()
    if not exe:
        return []
    rc, out = _run([exe, "--query-gpu=name,memory.total,driver_version,compute_cap",
                    "--format=csv,noheader,nounits"])
    with_cc = True
    if rc != 0 or not out.strip():
        rc, out = _run([exe, "--query-gpu=name,memory.total,driver_version",
                        "--format=csv,noheader,nounits"])
        with_cc = False
    if rc != 0:
        return []
    cuda = None
    rc2, out2 = _run([exe])
    m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", out2)
    if m:
        cuda = (int(m.group(1)), int(m.group(2)))
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        mem = _to_float(parts[1])
        gpus.append({
            "name": parts[0], "vendor": "nvidia",
            "vram_mb": mem if mem else None,
            "driver": parts[2],
            "cc": _to_float(parts[3]) if with_cc and len(parts) > 3 else None,
            "cuda": cuda,
        })
    return gpus


def _vendor_of(name):
    low = (name or "").lower()
    if any(w in low for w in ("microsoft basic", "virtual", "vmware", "remote display", "parsec")):
        return "virtual"
    if any(w in low for w in ("nvidia", "geforce", "quadro", "tesla")):
        return "nvidia"
    if any(w in low for w in ("amd", "radeon", "ati ")):
        return "amd"
    if "intel" in low:
        return "intel"
    return "other"


def _registry_vram():
    """{описание адаптера (в нижнем регистре): байт} из реестра Windows —
    там объём видеопамяти 64-битный и точнее, чем AdapterRAM (тот обрезается
    на 4 ГБ). Пустой словарь, если реестр недоступен."""
    result = {}
    try:
        import winreg
    except Exception:
        return result
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    for i in range(0, 32):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + "\\%04d" % i) as key:
                try:
                    desc = winreg.QueryValueEx(key, "DriverDesc")[0]
                except OSError:
                    continue
                size = None
                for value_name in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                    try:
                        raw = winreg.QueryValueEx(key, value_name)[0]
                    except OSError:
                        continue
                    if isinstance(raw, (bytes, bytearray)):
                        raw = int.from_bytes(bytes(raw), "little")
                    if isinstance(raw, int) and raw > 0:
                        size = raw
                        break
                if size:
                    result[str(desc).lower()] = size
        except OSError:
            continue
        except Exception:
            continue
    return result


def detect_windows_adapters():
    """Все видеоадаптеры Windows через PowerShell (Win32_VideoController)."""
    if os.name != "nt":
        return []
    ps = ("Get-CimInstance Win32_VideoController | "
          "Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json -Compress")
    rc, out = _run(["powershell", "-NoProfile", "-Command", ps], timeout=30)
    items = []
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
            items = data if isinstance(data, list) else [data]
        except Exception:
            items = []
    if not items:
        rc, out = _run(["wmic", "path", "win32_VideoController", "get", "Name,AdapterRAM", "/format:csv"])
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3 and parts[0] and parts[0] != "Node":
                items.append({"Name": parts[2], "AdapterRAM": _to_float(parts[1])})
    reg = _registry_vram()
    adapters = []
    for it in items:
        name = str(it.get("Name") or "").strip()
        if not name:
            continue
        vendor = _vendor_of(name)
        if vendor == "virtual":
            continue
        ram = it.get("AdapterRAM")
        vram_bytes = None
        low = name.lower()
        if low in reg:
            vram_bytes = reg[low]
        else:
            for desc, size in reg.items():
                if desc in low or low in desc:
                    vram_bytes = size
                    break
        if vram_bytes is None and isinstance(ram, (int, float)) and ram > 0:
            vram_bytes = int(ram)
        adapters.append({
            "name": name, "vendor": vendor,
            "vram_mb": (vram_bytes / 1048576.0) if vram_bytes else None,
            "driver": it.get("DriverVersion"), "cc": None, "cuda": None,
        })
    return adapters


def detect_gpus():
    """Список видеокарт. NVIDIA берётся из nvidia-smi (точные данные, поле
    exact=True), остальные — из Windows (exact=False: Windows показывает для
    них неточный объём, его нужно проверить вручную)."""
    gpus = []
    nvidia = detect_nvidia()
    for g in nvidia:
        g["exact"] = True
        gpus.append(g)
    for a in detect_windows_adapters():
        if a["vendor"] == "nvidia" and nvidia:
            continue
        a["exact"] = False
        gpus.append(a)
    return gpus


def primary_gpu(gpus):
    """Основная видеокарта: приоритет — NVIDIA, затем по объёму видеопамяти."""
    if not gpus:
        return None
    return sorted(
        gpus, key=lambda g: (g["vendor"] == "nvidia", g.get("vram_mb") or 0), reverse=True)[0]


def collect_system_info():
    gpus = detect_gpus()
    return {
        "is_windows": os.name == "nt",
        "os": "{0} {1} ({2})".format(platform.system(), platform.release(), platform.version()),
        "arch": detect_arch(),
        "ram_bytes": total_ram_bytes(),
        "gpus": gpus,
        "gpu": primary_gpu(gpus),
        "python": platform.python_version(),
    }


# ---------------------------------------------------------------------------
# Рекомендации
# ---------------------------------------------------------------------------

# Необходимая видеопамять (ГБ) для каждого варианта. Допуск 0.5 ГБ: карты
# «на 12 ГБ» нередко сообщают 11,99 ГБ.
VRAM_SLACK_GB = 0.5
REQUIRED_VRAM_GB = {"libretranslate": 0, "translategemma": 6, "gemma4_q4": 8, "gemma4_q6": 12}
ORDER = ["libretranslate", "translategemma", "gemma4_q4", "gemma4_q6"]


def recommend(vram_gb):
    """Ключ рекомендуемого варианта по видеопамяти (ГБ; None/0 — нет карты)."""
    v = vram_gb or 0
    for key in ("gemma4_q6", "gemma4_q4", "translategemma"):
        if v + VRAM_SLACK_GB >= REQUIRED_VRAM_GB[key]:
            return key
    return "libretranslate"


def fits(key, vram_gb):
    return (vram_gb or 0) + VRAM_SLACK_GB >= REQUIRED_VRAM_GB[key]


def auto_variant(gpu, arch):
    """Тип сборки llama.cpp по видеокарте: cuda13 / cuda12 / vulkan / cpu.
    CUDA 13 — если драйвер поддерживает CUDA 13 и карта Turing (RTX 20) или
    новее (CUDA 13 не поддерживает более старые карты); иначе CUDA 12 при
    драйвере от CUDA 12.4; в остальных случаях Vulkan (работает на любой
    карте, в том числе AMD и Intel)."""
    if not gpu:
        return "cpu"
    if gpu["vendor"] == "nvidia":
        cuda, cc = gpu.get("cuda"), gpu.get("cc")
        if cuda is None:
            return "vulkan"
        if cuda >= (13, 0) and (cc is None or cc >= 7.5):
            return "cuda13"
        if arch == "x64" and cuda >= (12, 4):
            return "cuda12"
        return "vulkan"
    return "vulkan"


def driver_warnings(gpu):
    """Список ключей предупреждений о драйвере NVIDIA."""
    warnings = []
    if gpu and gpu["vendor"] == "nvidia":
        if gpu.get("cuda") is None:
            warnings.append("no_cuda_info")
        elif gpu["cuda"] < (12, 4):
            warnings.append("old_driver")
    return warnings


# ---------------------------------------------------------------------------
# llama.cpp: список релизов и выбор архива
# ---------------------------------------------------------------------------

def list_llama_releases(limit=50):
    """Свежие сборки llama.cpp: [{'tag', 'date', 'assets'}], новые первыми.
    assets — {имя: {'url', 'size', 'sha256'}} или None, если список файлов
    получить не удалось (тогда имена собираются по шаблону). Релизы без
    файлов (информационные) пропускаются."""
    try:
        data = json.loads(_http_get(
            GITHUB_API + "/releases?per_page={0}".format(limit),
            headers={"Accept": "application/vnd.github+json"}).decode("utf-8"))
        result = []
        for rel in data:
            if rel.get("draft") or not rel.get("assets"):
                continue
            result.append({
                "tag": rel["tag_name"],
                "date": (rel.get("published_at") or "")[:10],
                "assets": _assets_of(rel),
            })
        if result:
            return result
    except Exception:
        pass
    return _releases_from_atom(limit)


def _assets_of(rel):
    assets = {}
    for a in rel.get("assets", []):
        digest = a.get("digest") or ""
        assets[a["name"]] = {
            "url": a["browser_download_url"], "size": a.get("size"),
            "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
        }
    return assets


def _releases_from_atom(limit):
    """Запасной вариант без API: лента релизов (не упирается в лимит запросов)."""
    try:
        text = _http_get(GITHUB_ATOM).decode("utf-8", errors="replace")
    except Exception as e:
        raise SetupError(tr("Не удалось получить список версий llama.cpp с GitHub: {0}").format(e))
    result = []
    for entry in re.findall(r"<entry>(.*?)</entry>", text, re.S):
        m_tag = re.search(r'releases/tag/([^"\']+)', entry)
        if not m_tag or not re.match(r"^b\d+$", m_tag.group(1)):
            continue
        m_date = re.search(r"<updated>(\d{4}-\d{2}-\d{2})", entry)
        result.append({"tag": m_tag.group(1), "date": m_date.group(1) if m_date else "", "assets": None})
        if len(result) >= limit:
            break
    if not result:
        raise SetupError(tr("Не удалось получить список версий llama.cpp с GitHub."))
    return result


def get_release(tag):
    """Релиз по тегу (когда номер сборки введён вручную)."""
    if not re.match(r"^b\d+$", tag or ""):
        raise SetupError(tr("Неверный номер сборки «{0}» — ожидается вид b11723.").format(tag))
    try:
        rel = json.loads(_http_get(
            GITHUB_API + "/releases/tags/" + tag,
            headers={"Accept": "application/vnd.github+json"}).decode("utf-8"))
        if rel.get("assets"):
            return {"tag": tag, "date": (rel.get("published_at") or "")[:10], "assets": _assets_of(rel)}
        raise SetupError(tr("В релизе {0} нет файлов для скачивания.").format(tag))
    except SetupError:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SetupError(tr("Сборка {0} не найдена на GitHub.").format(tag))
    except Exception:
        pass
    # API недоступен — собираем ссылки по шаблону.
    return {"tag": tag, "date": "", "assets": None}


def build_asset_plan(tag, assets, arch, variant):
    """Какие архивы скачать для варианта variant ('cpu'/'vulkan'/'cuda12'/'cuda13').
    Возвращает {'llama': {name,url,size,sha256}, 'cudart': {...}|None,
    'cuda_version': '12.4'|None, 'variant_label': 'cuda-12.4-x64'}."""
    def entry(name):
        if assets is not None:
            a = assets.get(name)
            if not a:
                return None
            return {"name": name, "url": a["url"], "size": a.get("size"), "sha256": a.get("sha256")}
        return {"name": name, "url": GITHUB_DL + "/" + tag + "/" + name, "size": None, "sha256": None}

    def find(pattern):
        rx = re.compile(pattern)
        names = sorted(n for n in assets if rx.match(n))
        return names

    if variant in ("cpu", "vulkan"):
        name = "llama-{0}-bin-win-{1}-{2}.zip".format(tag, variant, arch)
        e = entry(name)
        if e is None:
            raise SetupError(tr("В сборке {0} нет архива «{1}» для {2}.").format(tag, variant, arch))
        return {"llama": e, "cudart": None, "cuda_version": None,
                "variant_label": "{0}-{1}".format(variant, arch)}

    major = "12" if variant == "cuda12" else "13"
    if assets is not None:
        rx = r"^llama-.+-bin-win-cuda-({0}\.\d+)-{1}\.zip$".format(major, arch)
        versions = []
        for n in assets:
            m = re.match(rx, n)
            if m:
                versions.append(m.group(1))
        if not versions:
            raise SetupError(tr("В сборке {0} нет версии CUDA {1} для {2}.").format(tag, major, arch))
        ver = sorted(versions, key=lambda v: [int(x) for x in v.split(".")])[-1]
    else:
        ver = FALLBACK_CUDA[variant]
    llama = entry("llama-{0}-bin-win-cuda-{1}-{2}.zip".format(tag, ver, arch))
    cudart = entry("cudart-llama-bin-win-cuda-{0}-{1}.zip".format(ver, arch))
    if llama is None or cudart is None:
        raise SetupError(tr("В сборке {0} нет полного набора файлов для CUDA {1} ({2}).").format(tag, ver, arch))
    return {"llama": llama, "cudart": cudart, "cuda_version": ver,
            "variant_label": "cuda-{0}-{1}".format(ver, arch)}


# ---------------------------------------------------------------------------
# Hugging Face: размер и SHA-256 файла
# ---------------------------------------------------------------------------

def hf_resolve_url(repo, path):
    return "{0}/{1}/resolve/main/{2}".format(HF_BASE, repo, urllib.parse.quote(path))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def hf_pointer(repo, path):
    """(размер в байтах, sha256) файла из репозитория Hugging Face.
    Основной способ — «указатель» файла по ссылке /raw/ (там точный размер и
    хэш), запасной — заголовки X-Linked-Size / X-Linked-ETag ответа на
    /resolve/. Если ничего не получилось — (None, None): файл скачается без
    проверки хэша."""
    try:
        head = _http_get("{0}/{1}/raw/main/{2}".format(HF_BASE, repo, urllib.parse.quote(path)),
                         timeout=30)[:4096].decode("utf-8", errors="replace")
        m_oid = re.search(r"oid sha256:([0-9a-f]{64})", head)
        m_size = re.search(r"^size (\d+)\s*$", head, re.M)
        if m_oid and m_size:
            return int(m_size.group(1)), m_oid.group(1)
    except Exception:
        pass
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(hf_resolve_url(repo, path), method="HEAD",
                                     headers={"User-Agent": USER_AGENT})
        try:
            resp = opener.open(req, timeout=30)
            headers = resp.headers
        except urllib.error.HTTPError as e:
            headers = e.headers
        size = headers.get("X-Linked-Size") or headers.get("Content-Length")
        etag = (headers.get("X-Linked-ETag") or "").strip('"')
        return (int(size) if size else None,
                etag if re.match(r"^[0-9a-f]{64}$", etag) else None)
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Скачивание с докачкой и проверкой
# ---------------------------------------------------------------------------

def sha256_file(path, progress=None, cancel=None):
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    last = 0.0
    with open(path, "rb") as f:
        while True:
            _check_cancel(cancel)
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            now = time.monotonic()
            if progress and now - last > 0.2:
                last = now
                progress(done, total, "verify")
    if progress:
        progress(total, total, "verify")
    return h.hexdigest()


def _ok_marker(path):
    return path + ".ok"


def _is_verified(dest, expected_size, expected_sha, progress, cancel):
    """Готовый файл на месте и совпадает с ожидаемым? Результат проверки
    хэша запоминается в «.ok», чтобы не хэшировать многогигабайтный файл при
    каждом запуске."""
    if not os.path.isfile(dest):
        return False
    size = os.path.getsize(dest)
    if expected_size is not None and size != expected_size:
        return False
    if not expected_sha:
        return expected_size is not None
    marker = _ok_marker(dest)
    stamp = {"size": size, "mtime": int(os.path.getmtime(dest)), "sha256": expected_sha}
    try:
        with open(marker, "r", encoding="utf-8") as f:
            if json.load(f) == stamp:
                return True
    except Exception:
        pass
    if sha256_file(dest, progress, cancel) == expected_sha:
        _write_ok(dest, expected_sha)
        return True
    return False


def _write_ok(dest, sha):
    try:
        stamp = {"size": os.path.getsize(dest), "mtime": int(os.path.getmtime(dest)), "sha256": sha}
        with open(_ok_marker(dest), "w", encoding="utf-8") as f:
            json.dump(stamp, f)
    except Exception:
        pass


def _download_once(url, part, expected_size, progress, cancel):
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = "bytes={0}-".format(have)
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            if expected_size is not None and have == expected_size:
                return
            os.remove(part)
            raise RuntimeError("range not satisfiable, restart")
        raise
    with resp:
        status = getattr(resp, "status", 200)
        length = int(resp.headers.get("Content-Length") or 0)
        if status == 206 and have:
            total, mode = have + length, "ab"
        else:
            have, total, mode = 0, length, "wb"
        if expected_size is not None:
            total = expected_size
        done = have
        last = 0.0
        with open(part, mode) as f:
            while True:
                _check_cancel(cancel)
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if progress and now - last > 0.2:
                    last = now
                    progress(done, total, "download")
    if progress:
        progress(done, total or done, "download")
    if total and done != total:
        raise RuntimeError("incomplete: {0} of {1}".format(done, total))


def download_file(url, dest, expected_size=None, expected_sha=None, progress=None,
                  cancel=None, label="", retries=5):
    """Скачивает url в dest (через dest.part, с докачкой). После скачивания
    сверяет размер и, если известен, SHA-256. progress(done, total, phase),
    phase = 'download' | 'verify'. Возвращает 'exists' или 'downloaded'."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if _is_verified(dest, expected_size, expected_sha, progress, cancel):
        return "exists"
    if os.path.isfile(dest):
        os.remove(dest)
    part = dest + ".part"
    attempt = 0
    while True:
        attempt += 1
        _check_cancel(cancel)
        try:
            _download_once(url, part, expected_size, progress, cancel)
            break
        except Cancelled:
            raise
        except Exception as e:
            if attempt >= retries:
                raise SetupError(tr("Не удалось скачать {0}: {1}").format(label or url, e))
            _sleep(min(2 * attempt, 10), cancel)
    size = os.path.getsize(part)
    if expected_size is not None and size != expected_size:
        os.remove(part)
        raise SetupError(tr("Размер скачанного файла {0} не совпал с ожидаемым ({1} вместо {2}).")
                         .format(label or dest, size, expected_size))
    if expected_sha:
        if sha256_file(part, progress, cancel) != expected_sha:
            os.remove(part)
            raise SetupError(tr("Контрольная сумма файла {0} не совпала — файл повреждён при "
                                "скачивании, попробуйте ещё раз.").format(label or dest))
    os.replace(part, dest)
    if expected_sha:
        _write_ok(dest, expected_sha)
    return "downloaded"


# ---------------------------------------------------------------------------
# Манифест и установка llama.cpp
# ---------------------------------------------------------------------------

def manifest_path(folder):
    return os.path.join(folder, MANIFEST_NAME)


def read_manifest(folder):
    try:
        with open(manifest_path(folder), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_manifest(folder, data):
    with open(manifest_path(folder), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def installed_summary(folder):
    """Строка вида 'b11721 · cuda-12.4-x64 (CUDA 12.4)' либо None."""
    if not folder or not os.path.isfile(os.path.join(folder, SERVER_EXE)):
        return None
    m = read_manifest(folder)
    llama = m.get("llama") or {}
    if not llama.get("tag"):
        return tr("версия неизвестна (установлена не мастером)")
    text = "{0} · {1}".format(llama["tag"], llama.get("variant", "?"))
    cuda = m.get("cuda") or {}
    if cuda.get("version"):
        text += " (CUDA {0})".format(cuda["version"])
    return text


def _unlink_manifest_files(folder, rel_files, log):
    removed = 0
    base = os.path.normcase(os.path.abspath(folder))
    for rel in rel_files:
        path = os.path.abspath(os.path.join(folder, rel))
        if not os.path.normcase(path).startswith(base + os.sep):
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except Exception as e:
            log(tr("  Не удалось удалить {0}: {1}").format(rel, e))
    return removed


def _zip_prefix(names):
    """Префикс внутри архива, который нужно отбросить при распаковке: папка,
    в которой лежит llama-server.exe, либо единственная общая корневая папка."""
    for n in names:
        if n.lower().endswith("/" + SERVER_EXE.lower()) or n.lower() == SERVER_EXE.lower():
            return n[: len(n) - len(SERVER_EXE)]
    files = [n for n in names if not n.endswith("/")]
    tops = {n.split("/", 1)[0] for n in files if "/" in n}
    if files and len(tops) == 1 and all("/" in n for n in files):
        return tops.pop() + "/"
    return ""


def extract_zip(zip_path, folder, log=None, cancel=None, skip_top=("models",)):
    """Распаковывает архив прямо в folder (без вложенной папки). Возвращает
    список относительных путей распакованных файлов (с «/»)."""
    log = log or (lambda m: None)
    base = os.path.abspath(folder)
    extracted = []
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        prefix = _zip_prefix(names)
        for info in zf.infolist():
            _check_cancel(cancel)
            name = info.filename
            if name.endswith("/") or not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not rel or rel.split("/", 1)[0].lower() in skip_top:
                continue
            target = os.path.abspath(os.path.join(base, *rel.split("/")))
            if not os.path.normcase(target).startswith(os.path.normcase(base) + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            extracted.append(rel)
    log(tr("  Распаковано файлов: {0}").format(len(extracted)))
    return extracted


def _server_locked(folder):
    exe = os.path.join(folder, SERVER_EXE)
    if not os.path.isfile(exe):
        return False
    try:
        with open(exe, "ab"):
            return False
    except PermissionError:
        return True
    except Exception:
        return False


def install_llama(folder, tag, arch, variant, assets, log=None, progress=None, cancel=None):
    """Устанавливает/переустанавливает llama.cpp в folder.
    Порядок такой, чтобы неудачная загрузка не ломала рабочую установку:
    сначала скачиваются все архивы, и только потом удаляются старые файлы
    llama.cpp (по манифесту) и распаковываются новые. Папка models и
    CUDA-библиотеки той же версии не затрагиваются. Возвращает манифест."""
    log = log or (lambda m: None)
    os.makedirs(os.path.join(folder, MODELS_DIRNAME), exist_ok=True)
    plan = build_asset_plan(tag, assets, arch, variant)
    manifest = read_manifest(folder)
    old_cuda = manifest.get("cuda") or {}

    need_cudart = bool(plan["cudart"])
    if need_cudart and old_cuda.get("version") == plan["cuda_version"] \
            and old_cuda.get("arch") == arch and old_cuda.get("files") \
            and all(os.path.isfile(os.path.join(folder, f)) for f in old_cuda["files"]):
        need_cudart = False
        log(tr("CUDA-библиотеки {0} уже установлены — пропускаю их скачивание.").format(plan["cuda_version"]))

    dl_dir = os.path.join(folder, DOWNLOADS_DIRNAME)
    jobs = [("llama", plan["llama"])] + ([("cudart", plan["cudart"])] if need_cudart else [])
    zips = {}
    for kind, item in jobs:
        dest = os.path.join(dl_dir, item["name"])
        size, sha = item.get("size"), item.get("sha256")
        if size is None:
            size = _content_length(item["url"])
        log(tr("Скачиваю {0}...").format(item["name"]))
        download_file(item["url"], dest, size, sha, progress, cancel, label=item["name"])
        zips[kind] = dest

    if _server_locked(folder):
        raise SetupError(tr("llama-server сейчас запущен и держит файлы. Закройте его окно "
                            "(консоль «Запустить …») и повторите установку."))

    old_llama = manifest.get("llama") or {}
    if old_llama.get("files"):
        n = _unlink_manifest_files(folder, old_llama["files"], log)
        log(tr("Удалены файлы прежней сборки llama.cpp: {0}.").format(n))
    new_manifest = {"llama": {}, "cuda": dict(old_cuda) if old_cuda else {}}
    if need_cudart:
        if old_cuda.get("files"):
            n = _unlink_manifest_files(folder, old_cuda["files"], log)
            log(tr("Удалены CUDA-библиотеки прежней версии: {0}.").format(n))
        log(tr("Распаковываю CUDA-библиотеки..."))
        files = extract_zip(zips["cudart"], folder, log, cancel)
        new_manifest["cuda"] = {"version": plan["cuda_version"], "arch": arch, "files": files}
    log(tr("Распаковываю llama.cpp {0}...").format(tag))
    files = extract_zip(zips["llama"], folder, log, cancel)
    if not os.path.isfile(os.path.join(folder, SERVER_EXE)):
        raise SetupError(tr("После распаковки в папке нет {0} — архив оказался неожиданного вида.")
                         .format(SERVER_EXE))
    new_manifest["llama"] = {
        "tag": tag, "variant": plan["variant_label"], "arch": arch,
        "files": files, "installed": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_manifest(folder, new_manifest)
    shutil.rmtree(dl_dir, ignore_errors=True)
    return new_manifest


def _content_length(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            v = resp.headers.get("Content-Length")
            return int(v) if v else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Модели (Hugging Face)
# ---------------------------------------------------------------------------

# Папка модели внутри <llama>\models — у каждой своя, чтобы файлы MTP разных
# моделей не путались (программа подбирает MTP по размеру модели в имени).
MODELS = {
    "translategemma": {
        "role": "tg", "folder": "translate-gemma-e4b",
        "repo": "17slever17/translate-gemma-4-sub-e4b-GGUF",
        "model": "translate_gemma4_sub-E4B-Q4_K_XL.gguf",
        "mtp": "mtp-gemma-4-E4B-it.gguf",
    },
    "gemma4_q4": {
        "role": "g4", "folder": "gemma4-12b-q4-qat",
        "repo": "unsloth/gemma-4-12B-it-qat-GGUF",
        "model": "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",
        "mtp": "MTP/mtp-gemma-4-12B-it-Q4_0.gguf",
    },
    "gemma4_q6": {
        "role": "g4", "folder": "gemma4-12b-q6",
        "repo": "unsloth/gemma-4-12b-it-GGUF",
        "model": "gemma-4-12b-it-UD-Q6_K_XL.gguf",
        "mtp": "MTP/mtp-gemma-4-12b-it-Q8_0.gguf",
    },
}

# Разумные значения запуска по умолчанию (подставляются, только если поля
# в окне пусты): ngl, np, ctx, MTP.
DEFAULT_LAUNCH = {
    "translategemma": (99, 2, 4096, 1),
    "gemma4_q4": (99, 1, 2048, 2),
    "gemma4_q6": (99, 1, 2048, 2),
}


def plan_model_files(key, models_dir):
    """[{key, repo, path, dest, size, sha256, url, label}] — файлы модели и
    её MTP с размером и хэшем, взятыми со страницы файла на Hugging Face."""
    spec = MODELS[key]
    items = []
    for kind in ("model", "mtp"):
        path = spec[kind]
        size, sha = hf_pointer(spec["repo"], path)
        items.append({
            "key": key, "kind": kind, "repo": spec["repo"], "path": path,
            "dest": os.path.join(models_dir, spec["folder"], os.path.basename(path)),
            "size": size, "sha256": sha, "url": hf_resolve_url(spec["repo"], path),
            "label": os.path.basename(path),
        })
    return items


def free_space_bytes(path):
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return shutil.disk_usage(p).free
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LibreTranslate
# ---------------------------------------------------------------------------

def python_exe():
    """python.exe текущего интерпретатора (у pythonw нет консольного вывода)."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        cand = exe[: -len("pythonw.exe")] + "python.exe"
        if os.path.isfile(cand):
            return cand
    return exe


def install_libretranslate(log=None, cancel=None):
    """pip install libretranslate в текущий Python. Возвращает путь к
    libretranslate.exe."""
    import libretranslate_launcher
    log = log or (lambda m: None)
    cmd = [python_exe(), "-m", "pip", "install", "libretranslate"]
    log(tr("Запускаю: {0}").format(" ".join(cmd)))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", creationflags=flags)
    except Exception as e:
        raise SetupError(tr("Не удалось запустить pip: {0}").format(e))
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log("  [pip] " + line)
            if cancel is not None and cancel.is_set():
                proc.terminate()
                raise Cancelled()
        rc = proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
    if rc != 0:
        raise SetupError(tr("pip завершился с ошибкой (код {0}) — подробности в журнале выше. "
                            "Если пакеты для вашей версии Python не нашлись, попробуйте "
                            "поставить Python 3.12–3.14.").format(rc))
    try:
        return libretranslate_launcher.find_libretranslate_exe()
    except libretranslate_launcher.LibreTranslateLauncherError as e:
        raise SetupError(str(e))
