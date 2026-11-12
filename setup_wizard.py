# -*- coding: utf-8 -*-
"""Окно «Мастер установки»: сведения о системе, рекомендация переводчика и
установка в несколько кликов (LibreTranslate через pip; llama.cpp с GitHub и
модели Gemma с Hugging Face). Вся логика — в setup_core.py, здесь только
окно и фоновые потоки.

Как это работает:
1. При открытии в фоне определяется система (nvidia-smi, PowerShell).
2. По видеопамяти (её можно поправить вручную — Windows неточно показывает
   память у карт не от NVIDIA) отмечается рекомендуемый вариант.
3. «Установить»: сначала поток «план» собирает размеры/хэши и список файлов,
   окно спрашивает подтверждение (сколько скачать и сколько свободно), затем
   поток «установка» скачивает и распаковывает. Прогресс — в полосе и в
   журнале; «Отмена» прерывает загрузку (скачанное докачивается при
   следующем запуске).
4. По окончании в главное окно передаются пути (on_apply), они сохраняются в
   настройках программы.
"""

from i18n import mark, tr
import os
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import setup_core as sc

# Описания переводчиков: (ключ, название, требование к видеопамяти, время на
# 120 тестовых строк в секундах, описание). Замеры сделаны на RTX 3070.
OPTIONS = [
    ("libretranslate", mark("LibreTranslate (офлайн)"),
     mark("Видеокарта не нужна: работает на процессоре (подходит и при видеопамяти меньше 4 ГБ)."), 19,
     mark("Быстрый, но качество заметно ниже. Часто переводит грубо и дословно, допускает "
          "неестественные формулировки, смысловые ошибки и проблемы с форматированием — "
          "классический «машинный перевод». Почти все фразы переводит от мужского лица, даже "
          "для женских персонажей.")),
    ("translategemma", mark("TranslateGemma 4 Sub E4B Q4 (офлайн)"),
     mark("Нужно около 6 ГБ видеопамяти."), 59,
     mark("Приемлемое качество при высокой скорости. В основном сохраняет смысл и стиль оригинала, "
          "справляется с художественными описаниями и формулирует фразы лучше LibreTranslate. "
          "Иногда встречаются буквальные, не вполне естественные обороты; бывает, что путает "
          "пол говорящего.")),
    ("gemma4_q4", mark("Gemma 4 12B Q4 QAT (офлайн)"),
     mark("Нужно около 8 ГБ видеопамяти."), 113,
     mark("Уже хорошее качество: связные и в целом естественные переводы. Передаёт основную мысль, "
          "эмоциональные оттенки, интонацию и тон говорящего, справляется с игрой слов. Изредка "
          "встречаются кальки, неудачные словосочетания и грамматически несовершенные "
          "конструкции. Текст ближе к литературному, чем к машинному переводу.")),
    ("gemma4_q6", mark("Gemma 4 12B Q6 (офлайн)"),
     mark("Нужно около 12 ГБ видеопамяти."), 1850,
     mark("То же, что Gemma 4 12B Q4 QAT, но лучше передаёт эмоции, игру слов и шутки оригинала, "
          "лучше строит предложения, кальки практически отсутствуют.")),
]

GOOGLE_NOTE = mark(
    "Онлайн-вариант, устанавливать не нужно — Google Translate (12 секунд на 120 тестовых строк). "
    "Качество между LibreTranslate и TranslateGemma: встречаются кальки, но перевод ближе к "
    "естественному, чем к «грубой машине»; пытается использовать словосочетания и речевые "
    "обороты, хотя часто неудачно. Главная проблема — это бесплатный неофициальный сервис: через "
    "15–30 минут работы Google блокирует соединение на 12–24 часа.")

SPEED_NOTE = mark(
    "Важно: модели очень зависят от видеопамяти. Резкий скачок времени между Gemma 4 Q4 и Q6 — не "
    "опечатка: модели не хватило видеопамяти тестовой карты. Все замеры сделаны на RTX 3070, у "
    "вас значения будут другими, поэтому ориентируйтесь на рекомендации мастера и не ставьте "
    "модель, требовательнее вашей системы.")

VARIANTS = [("auto", mark("Авто")), ("cuda13", "CUDA 13"), ("cuda12", "CUDA 12"),
            ("vulkan", "Vulkan"), ("cpu", "CPU")]


class SetupWizard(tk.Toplevel):
    def __init__(self, master, script_dir, settings, on_apply, log=None, auto=False):
        super().__init__(master)
        self.title(tr("Мастер установки"))
        self.geometry("980x820")
        self.minsize(820, 560)
        self.script_dir = script_dir
        self.settings = settings
        self.on_apply = on_apply
        self.log_main = log or (lambda msg: None)
        self.info = None
        self.busy = False
        self.cancel = threading.Event()
        self.q = queue.Queue()
        self.releases = None
        self.plan = None
        self._checked_once = False
        self._last_progress_label = ""

        self.vram_var = tk.StringVar()
        self.rec_var = tk.StringVar(value=tr("Определяю систему..."))
        self.opt_vars = {key: tk.BooleanVar(value=False) for key, *_rest in OPTIONS}
        self.opt_status = {}
        default_folder = settings.get("gemma_llama_folder") or os.path.join(script_dir, "llama")
        self.folder_var = tk.StringVar(value=default_folder)
        self.reinstall_var = tk.BooleanVar(value=False)
        self.pick_version_var = tk.BooleanVar(value=False)
        self.version_var = tk.StringVar()
        self.variant_var = tk.StringVar(value=tr(VARIANTS[0][1]))
        self.installed_var = tk.StringVar()
        self.status_var = tk.StringVar(value="")

        self._build()
        self.vram_var.trace_add("write", lambda *a: self._update_statuses())
        self.folder_var.trace_add("write", lambda *a: self._update_installed())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self._update_installed()
        self._refresh_info()

    # ------------------------------------------------------------------
    # Построение окна (прокручиваемая область + нижняя панель с журналом)
    # ------------------------------------------------------------------
    def _build(self):
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        btns = ttk.Frame(bottom)
        btns.pack(fill="x", pady=(4, 4))
        self.install_btn = ttk.Button(btns, text=tr("Установить выбранное"), command=self._start_install)
        self.install_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text=tr("Отмена"), command=self._cancel_install, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(btns, text=tr("Закрыть"), command=self._on_close).pack(side="right")
        ttk.Label(bottom, textvariable=self.status_var).pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(2, 4))
        log_frame = ttk.Frame(bottom)
        log_frame.pack(fill="x")
        self.log_text = tk.Text(log_frame, height=8, wrap="word")
        sb = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="x", expand=True)
        sb.pack(side="left", fill="y")

        outer = ttk.Frame(self)
        outer.pack(side="top", fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(self.canvas)
        self._body_id = self.canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._body_id, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self.body = body
        self.wrap = 800

        pad = {"padx": 10, "pady": 4}

        # --- Система ----------------------------------------------------
        g_sys = ttk.LabelFrame(body, text=tr("Ваша система"))
        g_sys.pack(fill="x", **pad)
        self.sys_label = ttk.Label(g_sys, text=tr("Определяю систему..."), justify="left", wraplength=self.wrap)
        self.sys_label.pack(fill="x", padx=8, pady=(6, 2))
        row = ttk.Frame(g_sys)
        row.pack(fill="x", padx=8, pady=(2, 2))
        ttk.Label(row, text=tr("Видеопамять для рекомендаций, ГБ:")).pack(side="left")
        ttk.Entry(row, textvariable=self.vram_var, width=7).pack(side="left", padx=(6, 12))
        ttk.Button(row, text=tr("Определить заново"), command=self._refresh_info).pack(side="left")
        self.vram_warn = ttk.Label(g_sys, text="", foreground="#b25b00", justify="left", wraplength=self.wrap)
        self.vram_warn.pack(fill="x", padx=8, pady=(0, 2))
        self.sys_warn = ttk.Label(g_sys, text="", foreground="#b00020", justify="left", wraplength=self.wrap)
        self.sys_warn.pack(fill="x", padx=8, pady=(0, 6))

        # --- Что установить ---------------------------------------------
        g_opt = ttk.LabelFrame(body, text=tr("Что установить"))
        g_opt.pack(fill="x", **pad)
        ttk.Label(g_opt, textvariable=self.rec_var, font=("TkDefaultFont", 10, "bold"),
                  justify="left", wraplength=self.wrap).pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(g_opt, text=tr(SPEED_NOTE), justify="left", wraplength=self.wrap,
                  foreground="#555555").pack(fill="x", padx=8, pady=(0, 6))
        for key, title, need, seconds, desc in OPTIONS:
            frame = ttk.Frame(g_opt)
            frame.pack(fill="x", padx=8, pady=(2, 4))
            head = ttk.Frame(frame)
            head.pack(fill="x")
            ttk.Checkbutton(head, text=tr(title), variable=self.opt_vars[key]).pack(side="left")
            st = ttk.Label(head, text="")
            st.pack(side="left", padx=(10, 0))
            self.opt_status[key] = st
            ttk.Label(frame, text=tr(need) + "  " + tr("Тест: ≈{0} с на 120 строк (RTX 3070).").format(seconds),
                      justify="left", wraplength=self.wrap).pack(fill="x", padx=(24, 0))
            ttk.Label(frame, text=tr(desc), justify="left", wraplength=self.wrap,
                      foreground="#444444").pack(fill="x", padx=(24, 0))
        ttk.Separator(g_opt).pack(fill="x", padx=8, pady=4)
        ttk.Label(g_opt, text=tr(GOOGLE_NOTE), justify="left", wraplength=self.wrap,
                  foreground="#444444").pack(fill="x", padx=8, pady=(0, 8))

        # --- llama.cpp ----------------------------------------------------
        g_llama = ttk.LabelFrame(body, text=tr("llama.cpp (нужна для моделей Gemma)"))
        g_llama.pack(fill="x", **pad)
        ttk.Label(g_llama, text=tr("Папка llama.cpp (внутри будет папка models):")).pack(anchor="w", padx=8, pady=(6, 0))
        row = ttk.Frame(g_llama)
        row.pack(fill="x", padx=8, pady=(2, 2))
        ttk.Entry(row, textvariable=self.folder_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text=tr("Обзор..."), command=self._choose_folder).pack(side="left", padx=(6, 0))
        ttk.Label(g_llama, textvariable=self.installed_var, justify="left", wraplength=self.wrap).pack(
            anchor="w", padx=8, pady=(0, 2))
        ttk.Checkbutton(
            g_llama, text=tr("Переустановить llama.cpp (модели и CUDA-библиотеки той же версии сохраняются)"),
            variable=self.reinstall_var).pack(anchor="w", padx=8)
        row = ttk.Frame(g_llama)
        row.pack(fill="x", padx=8, pady=(2, 2))
        ttk.Checkbutton(row, text=tr("Выбрать версию"), variable=self.pick_version_var,
                        command=self._on_pick_version).pack(side="left")
        self.version_combo = ttk.Combobox(row, textvariable=self.version_var, width=30, state="disabled")
        self.version_combo.pack(side="left", padx=(8, 0))
        ttk.Label(row, text=tr("(список с GitHub или впишите номер сборки, например b11723)")).pack(
            side="left", padx=(8, 0))
        row = ttk.Frame(g_llama)
        row.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Label(row, text=tr("Тип сборки:")).pack(side="left")
        self.variant_combo = ttk.Combobox(row, textvariable=self.variant_var, width=24, state="readonly",
                                          values=[tr(label) for _k, label in VARIANTS])
        self.variant_combo.pack(side="left", padx=(8, 0))
        self.variant_hint = ttk.Label(row, text="")
        self.variant_hint.pack(side="left", padx=(8, 0))

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    def _log(self, msg):
        self.q.put(("log", msg))

    def _set_status(self, text):
        self.q.put(("status", text))

    def _progress(self, done, total, phase, label=""):
        self.q.put(("progress", done, total, phase, label))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.log_text.insert("end", item[1] + "\n")
                    self.log_text.see("end")
                    self.log_main(item[1])
                elif kind == "status":
                    self.status_var.set(item[1])
                elif kind == "progress":
                    _k, done, total, phase, label = item
                    if total:
                        self.progress.configure(mode="determinate", value=100.0 * done / total)
                    text = (tr("Проверка файла") if phase == "verify" else tr("Скачивание")) + ": "
                    text += "{0} / {1}".format(sc.format_size(done), sc.format_size(total))
                    self.status_var.set((self._last_progress_label + " — " if self._last_progress_label else "") + text)
                elif kind == "label":
                    self._last_progress_label = item[1]
                elif kind == "info":
                    self._apply_info(item[1])
                elif kind == "releases":
                    self._apply_releases(item[1], item[2])
                elif kind == "plan":
                    self._confirm_plan(item[1], item[2])
                elif kind == "done":
                    self._finish(item[1], item[2])
        except queue.Empty:
            pass
        try:
            self.after(100, self._poll)
        except tk.TclError:
            pass

    def _set_busy(self, busy):
        self.busy = busy
        unsupported = bool(self.info) and not self.info.get("is_windows", True)
        self.install_btn.configure(state="disabled" if (busy or unsupported) else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    def _on_close(self):
        if self.busy:
            if not messagebox.askyesno(tr("Мастер установки"),
                                       tr("Установка ещё идёт. Прервать её и закрыть окно?"), parent=self):
                return
            self.cancel.set()
        try:
            self.settings["wizard_done"] = True
            if hasattr(self.master, "_save_settings"):
                self.master._save_settings()
        except Exception:
            pass
        self.destroy()

    # ------------------------------------------------------------------
    # Определение системы и рекомендации
    # ------------------------------------------------------------------
    def _refresh_info(self):
        self.sys_label.configure(text=tr("Определяю систему..."))
        threading.Thread(target=self._info_worker, daemon=True).start()

    def _info_worker(self):
        try:
            info = sc.collect_system_info()
        except Exception:
            info = {"is_windows": os.name == "nt", "os": "?", "arch": sc.detect_arch(),
                    "ram_bytes": None, "gpus": [], "gpu": None, "python": "?"}
        self.q.put(("info", info))

    def _apply_info(self, info):
        self.info = info
        lines = [tr("Система: {0}, {1}").format(info["os"], info["arch"])]
        ram = info["ram_bytes"]
        lines.append(tr("Оперативная память: {0}").format(sc.format_size(ram) if ram else "?"))
        if info["gpus"]:
            for g in info["gpus"]:
                vram = "{0:.1f} {1}".format(g["vram_mb"] / 1024.0, tr("ГБ")) if g.get("vram_mb") else "?"
                extra = ""
                if g["vendor"] == "nvidia":
                    if g.get("driver"):
                        extra += tr(", драйвер {0}").format(g["driver"])
                    if g.get("cuda"):
                        extra += ", CUDA {0}.{1}".format(*g["cuda"])
                    if g.get("cc"):
                        extra += ", compute {0}".format(g["cc"])
                lines.append(tr("Видеокарта: {0} — видеопамять {1}{2}").format(g["name"], vram, extra))
        else:
            lines.append(tr("Видеокарта: не найдена (будет использоваться процессор)"))
        lines.append(tr("Python: {0}").format(info["python"]))
        self.sys_label.configure(text="\n".join(lines))

        gpu = info["gpu"]
        warn = ""
        if gpu and not gpu.get("exact"):
            warn = tr("Windows показывает неточный объём видеопамяти для этой видеокарты ({0}). "
                      "Проверьте значение в её свойствах или в спецификации и при необходимости "
                      "исправьте число в поле выше.").format(gpu["name"])
        self.vram_warn.configure(text=warn)
        sys_warn = []
        if not info["is_windows"]:
            sys_warn.append(tr("Мастер установки пока поддерживает только Windows — установка отключена."))
        for w in sc.driver_warnings(gpu):
            if w == "old_driver":
                sys_warn.append(tr("Драйвер NVIDIA поддерживает CUDA меньше 12.4 — обновите драйвер "
                                   "или выберите тип сборки Vulkan."))
            elif w == "no_cuda_info":
                sys_warn.append(tr("Не удалось определить версию CUDA драйвера NVIDIA — будет выбран Vulkan."))
        self.sys_warn.configure(text="\n".join(sys_warn))
        self.install_btn.configure(state="normal" if info["is_windows"] and not self.busy else "disabled")

        vram = (gpu["vram_mb"] / 1024.0) if gpu and gpu.get("vram_mb") else 0
        self.vram_var.set("{0:.1f}".format(vram) if vram else "0")
        if not self._checked_once:
            self._checked_once = True
            for key in self.opt_vars:
                self.opt_vars[key].set(False)
            self.opt_vars[sc.recommend(vram)].set(True)
        self._update_statuses()
        auto = sc.auto_variant(gpu, info["arch"])
        self.variant_hint.configure(text=tr("Авто → {0}").format(self._variant_name(auto)))

    @staticmethod
    def _variant_name(key):
        for k, label in VARIANTS:
            if k == key:
                return tr(label)
        return key

    def _vram_value(self):
        try:
            return float(self.vram_var.get().replace(",", "."))
        except ValueError:
            return 0.0

    def _update_statuses(self):
        vram = self._vram_value()
        rec = sc.recommend(vram)
        titles = {key: title for key, title, *_r in OPTIONS}
        self.rec_var.set(tr("Рекомендуем для вашей системы: {0}").format(tr(titles[rec])))
        for key, label in self.opt_status.items():
            if key == rec:
                label.configure(text=tr("— рекомендуется"), foreground="#1b7a1b")
            elif sc.fits(key, vram):
                label.configure(text=tr("— подойдёт"), foreground="#555555")
            else:
                label.configure(text=tr("— может не хватить видеопамяти"), foreground="#b00020")

    def _update_installed(self):
        summary = sc.installed_summary(self.folder_var.get().strip())
        if summary:
            self.installed_var.set(tr("Установлено сейчас: llama.cpp {0}").format(summary))
        else:
            self.installed_var.set(tr("llama.cpp в этой папке ещё не установлена."))

    def _choose_folder(self):
        path = filedialog.askdirectory(title=tr("Выберите папку для llama.cpp"), parent=self)
        if path:
            self.folder_var.set(path)

    # ------------------------------------------------------------------
    # Выбор версии llama.cpp
    # ------------------------------------------------------------------
    def _on_pick_version(self):
        if not self.pick_version_var.get():
            self.version_combo.configure(state="disabled")
            return
        self.version_combo.configure(state="normal")
        if self.releases is None:
            self.version_combo.set(tr("Загружаю список версий..."))
            threading.Thread(target=self._releases_worker, daemon=True).start()

    def _releases_worker(self):
        try:
            rels = sc.list_llama_releases()
            self.q.put(("releases", rels, None))
        except Exception as e:
            self.q.put(("releases", None, str(e)))

    def _apply_releases(self, rels, error):
        if rels is None:
            self.version_combo.set("")
            self._log(tr("Не удалось получить список версий: {0}").format(error))
            self._log(tr("Впишите номер сборки вручную (например b11723)."))
            return
        self.releases = rels
        values = ["{0} ({1})".format(r["tag"], r["date"]) if r["date"] else r["tag"] for r in rels]
        self.version_combo.configure(values=values)
        self.version_combo.set(values[0] if values else "")

    def _chosen_tag(self):
        if not self.pick_version_var.get():
            return None
        text = self.version_var.get().strip().split(" ")[0]
        return text or None

    def _chosen_variant(self):
        label = self.variant_var.get()
        for key, name in VARIANTS:
            if label in (name, tr(name)):
                return key
        return "auto"

    # ------------------------------------------------------------------
    # Установка: 1) план  2) подтверждение  3) выполнение
    # ------------------------------------------------------------------
    def _start_install(self):
        if self.busy:
            return
        keys = [k for k, v in self.opt_vars.items() if v.get()]
        need_llama = any(sc.MODELS.get(k) for k in keys)
        want_reinstall = self.reinstall_var.get()
        folder = self.folder_var.get().strip()
        if not keys and not want_reinstall:
            messagebox.showinfo(tr("Мастер установки"), tr("Ничего не выбрано."), parent=self)
            return
        if (need_llama or want_reinstall) and not folder:
            messagebox.showerror(tr("Мастер установки"), tr("Укажите папку для llama.cpp."), parent=self)
            return
        vram = self._vram_value()
        weak = [k for k in keys if not sc.fits(k, vram)]
        if weak and not messagebox.askyesno(
                tr("Мастер установки"),
                tr("Для выбранной модели может не хватить видеопамяти ({0} ГБ). Модель будет работать "
                   "очень медленно или не запустится. Продолжить всё равно?").format(vram), parent=self):
            return
        exe_exists = os.path.isfile(os.path.join(folder, sc.SERVER_EXE)) if folder else False
        install_llama = (need_llama and not exe_exists) or want_reinstall
        sel = {
            "keys": keys, "folder": folder, "install_llama": install_llama,
            "tag": self._chosen_tag(), "variant": self._chosen_variant(),
        }
        self.cancel.clear()
        self._set_busy(True)
        self.progress.configure(value=0)
        self.status_var.set(tr("Готовлю план установки..."))
        threading.Thread(target=self._plan_worker, args=(sel,), daemon=True).start()

    def _plan_worker(self, sel):
        try:
            info = self.info or {"arch": sc.detect_arch(), "gpu": None}
            plan = {"sel": sel, "models": [], "llama": None, "libre": "libretranslate" in sel["keys"]}
            if sel["install_llama"]:
                tag = sel["tag"]
                if tag:
                    rel = sc.get_release(tag)
                else:
                    self._log(tr("Ищу последнюю сборку llama.cpp на GitHub..."))
                    rel = sc.list_llama_releases(limit=10)[0]
                    tag = rel["tag"]
                variant = sel["variant"]
                if variant == "auto":
                    variant = sc.auto_variant(info.get("gpu"), info["arch"])
                bp = sc.build_asset_plan(tag, rel["assets"], info["arch"], variant)
                plan["llama"] = {"tag": tag, "variant": variant, "arch": info["arch"],
                                 "assets": rel["assets"], "build": bp}
                self._log(tr("llama.cpp: сборка {0}, тип {1} ({2}).").format(tag, variant, bp["variant_label"]))
            models_dir = os.path.join(sel["folder"], sc.MODELS_DIRNAME)
            for key in sel["keys"]:
                if key in sc.MODELS:
                    self._log(tr("Узнаю размер и контрольную сумму файлов модели {0}...").format(key))
                    plan["models"].extend(sc.plan_model_files(key, models_dir))
            total = sum(m["size"] or 0 for m in plan["models"])
            zip_total = 0
            if plan["llama"]:
                bp = plan["llama"]["build"]
                for part in (bp["llama"], bp["cudart"]):
                    if part:
                        zip_total += part["size"] or 0
            plan["download_bytes"] = total + zip_total
            plan["need_bytes"] = total + 3 * zip_total + 300 * 1024 * 1024
            plan["free_bytes"] = sc.free_space_bytes(sel["folder"]) if sel["folder"] else None
            self.q.put(("plan", plan, None))
        except sc.Cancelled:
            self.q.put(("done", "cancelled", None))
        except Exception as e:
            self.q.put(("done", "error", str(e) if isinstance(e, sc.SetupError) else traceback.format_exc()))

    def _confirm_plan(self, plan, _unused):
        lines = []
        if plan["libre"]:
            lines.append(tr("• LibreTranslate (pip install)"))
        if plan["llama"]:
            lines.append(tr("• llama.cpp {0} ({1})").format(plan["llama"]["tag"], plan["llama"]["build"]["variant_label"]))
        for key in plan["sel"]["keys"]:
            if key in sc.MODELS:
                lines.append("• " + tr(next(t for k, t, *_r in OPTIONS if k == key)))
        text = tr("Будет установлено:") + "\n" + "\n".join(lines) + "\n\n"
        text += tr("К скачиванию: {0}.").format(sc.format_size(plan["download_bytes"]))
        free = plan["free_bytes"]
        if free is not None:
            text += " " + tr("Свободно на диске: {0}.").format(sc.format_size(free))
            if free < plan["need_bytes"]:
                messagebox.showerror(
                    tr("Мастер установки"),
                    text + "\n\n" + tr("Не хватает места на диске (нужно около {0}). Освободите место или "
                                       "выберите другую папку.").format(sc.format_size(plan["need_bytes"])),
                    parent=self)
                self._set_busy(False)
                self.status_var.set("")
                return
        if not messagebox.askyesno(tr("Мастер установки"), text + "\n\n" + tr("Начать установку?"), parent=self):
            self._set_busy(False)
            self.status_var.set("")
            return
        self.plan = plan
        threading.Thread(target=self._exec_worker, args=(plan,), daemon=True).start()

    def _exec_worker(self, plan):
        sel = plan["sel"]
        updates = {"mtp_choices": {}, "defaults": {}}
        try:
            if plan["libre"]:
                self.q.put(("label", tr("LibreTranslate")))
                self._set_status(tr("Устанавливаю LibreTranslate (pip)..."))
                sc.install_libretranslate(log=self._log, cancel=self.cancel)
                self._log(tr("LibreTranslate установлен."))
            if plan["llama"]:
                self.q.put(("label", "llama.cpp"))
                self._set_status(tr("Устанавливаю llama.cpp..."))
                p = plan["llama"]
                sc.install_llama(sel["folder"], p["tag"], p["arch"], p["variant"], p["assets"],
                                 log=self._log, progress=self._progress, cancel=self.cancel)
                self._log(tr("llama.cpp {0} установлена в {1}.").format(p["tag"], sel["folder"]))
            if plan["models"]:
                updates["llama_folder"] = sel["folder"]
            done_models = []
            for item in plan["models"]:
                self.q.put(("label", item["label"]))
                self._log(tr("Скачиваю {0}...").format(item["label"]))
                sc.download_file(item["url"], item["dest"], item["size"], item["sha256"],
                                 self._progress, self.cancel, label=item["label"])
                self._log(tr("Готово: {0}").format(item["dest"]))
                done_models.append(item)
            for key in sel["keys"]:
                spec = sc.MODELS.get(key)
                if not spec:
                    continue
                files = {i["kind"]: i["dest"] for i in done_models if i["key"] == key}
                if "model" not in files:
                    continue
                updates["tg_model" if spec["role"] == "tg" else "g4_model"] = files["model"]
                if "mtp" in files:
                    updates["mtp_choices"][files["model"]] = os.path.basename(files["mtp"])
                group = "tg" if spec["role"] == "tg" else "g4"
                updates["defaults"][group] = sc.DEFAULT_LAUNCH[key]
            installed = [k for k in sel["keys"]]
            for key in ("gemma4_q6", "gemma4_q4", "translategemma", "libretranslate"):
                if key in installed:
                    updates["engine"] = {"libretranslate": "libretranslate", "translategemma": "gemma"}.get(key, "gemma4")
                    updates["recommended_key"] = key
                    break
            self.q.put(("done", "ok", updates))
        except sc.Cancelled:
            self.q.put(("done", "cancelled", updates))
        except Exception as e:
            self.q.put(("done", "error", str(e) if isinstance(e, sc.SetupError) else traceback.format_exc()))

    def _finish(self, status, payload):
        self._set_busy(False)
        self.progress.configure(value=0)
        self._last_progress_label = ""
        self._update_installed()
        if status == "ok":
            self.status_var.set(tr("Установка завершена."))
            try:
                self.on_apply(payload)
            except Exception as e:
                self._log(tr("Не удалось передать пути в главное окно: {0}").format(e))
            self.releases = None
            messagebox.showinfo(
                tr("Мастер установки"),
                tr("Готово! Пути и значения запуска сохранены в настройках программы. Для моделей Gemma "
                   "нажмите в главном окне «Запустить …», дождитесь готовности сервера и проверьте "
                   "переводчик кнопкой «Проверить переводчик»."), parent=self)
        elif status == "cancelled":
            self.status_var.set(tr("Установка отменена."))
            self._log(tr("Установка отменена. Уже скачанные части сохранены — при повторном запуске "
                         "загрузка продолжится."))
        else:
            self.status_var.set(tr("Ошибка установки."))
            self._log(tr("ОШИБКА: {0}").format(payload))
            messagebox.showerror(tr("Мастер установки"), str(payload)[:1500], parent=self)

    def _cancel_install(self):
        self.cancel.set()
        self.status_var.set(tr("Отменяю..."))
