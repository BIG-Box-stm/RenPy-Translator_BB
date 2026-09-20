# -*- coding: utf-8 -*-
"""
Ren'Py Translator (свободная утилита)
-------------------------------------
Простая программа с окном для автоматического перевода уже
сгенерированных файлов перевода Ren'Py (game/tl/<язык>/*.rpy) с помощью
бесплатного Google Translate (без API-ключа).

Как это работает:
1. В самой Ren'Py-игре (в меню разработчика, обычно Shift+O -> Console,
   либо через Ren'Py SDK -> "Generate Translations") нужно один раз
   создать файлы перевода для нужного языка. После этого в папке
   game/tl/<язык>/ появятся .rpy файлы со строками вида:
       old "Yes"
       new ""
   и диалогами, где переведённая копия пока совпадает с оригиналом.
2. Эта программа находит все такие непереведённые места и подставляет
   туда перевод с английского на русский (или на другой выбранный язык),
   не трогая теги {b}...{/b} и подстановки [name].
3. Уже переведённые вручную строки не перезаписываются (если не включена
   соответствующая галочka).

Программа написана "с нуля" и не использует код каких-либо платных
инструментов — только официальные/открытые механизмы Ren'Py и публичный
бесплатный эндпоинт Google Translate.
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import core
import deepl_translate
import gtranslate
import libretranslate_translate
import project
import renpy_sdk
import rpa_tools
import unrpyc_manager

APP_TITLE = "Ren'Py Translator — Google / DeepL / LibreTranslate"

# Небольшой набор популярных языков. Код языка — стандартный код Google
# Translate. Список можно расширять по необходимости.
LANGUAGES = [
    ("Английский", "en"),
    ("Русский", "ru"),
    ("Украинский", "uk"),
    ("Немецкий", "de"),
    ("Французский", "fr"),
    ("Испанский", "es"),
    ("Итальянский", "it"),
    ("Польский", "pl"),
    ("Португальский", "pt"),
    ("Китайский (упрощ.)", "zh-CN"),
    ("Японский", "ja"),
    ("Корейский", "ko"),
    ("Турецкий", "tr"),
]
LANG_NAME_TO_CODE = dict(LANGUAGES)
LANG_CODE_TO_NAME = {v: k for k, v in LANGUAGES}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x560")
        self.minsize(680, 480)

        self.project_path = tk.StringVar()
        self.tl_dir = None
        self.game_dir = None
        self.tools_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tools", "unrpyc"
        )
        self.settings_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "_translator_settings.json"
        )
        self.settings = project.load_settings(self.settings_path)
        self.clobber_var = tk.BooleanVar(value=False)
        self.sdk_dir_var = tk.StringVar(value=self.settings.get("sdk_dir", ""))
        self.lang_var = tk.StringVar()
        self.src_lang_var = tk.StringVar(value="Английский")
        self.dest_lang_var = tk.StringVar(value="Русский")
        self.overwrite_var = tk.BooleanVar(value=False)
        self.use_cache_var = tk.BooleanVar(value=True)
        self.engine_var = tk.StringVar(value="Google Translate (бесплатно, без ключа)")
        self.deepl_key_var = tk.StringVar()
        self.libre_url_var = tk.StringVar(value=libretranslate_translate.DEFAULT_URL)
        self.libre_key_var = tk.StringVar()

        self.log_queue = queue.Queue()
        self.worker_thread = None
        self.translator = None
        self.stop_requested = False

        self._build_ui()
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        frame_top = ttk.Frame(self)
        frame_top.pack(fill="x", **pad)

        ttk.Label(frame_top, text="Папка игры (или папка проекта с папкой game):").pack(anchor="w")
        row = ttk.Frame(frame_top)
        row.pack(fill="x", pady=(2, 0))
        entry = ttk.Entry(row, textvariable=self.project_path)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор...", command=self._choose_folder).pack(side="left", padx=(6, 0))
        self.prepare_btn = ttk.Button(
            row, text="Подготовить игру (распаковать .rpa / .rpyc)",
            command=self._start_prepare, state="disabled",
        )
        self.prepare_btn.pack(side="left", padx=(6, 0))

        row_prepare_opts = ttk.Frame(frame_top)
        row_prepare_opts.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(
            row_prepare_opts,
            text="Перезаписать уже раскомпилированные .rpy (--clobber)",
            variable=self.clobber_var,
        ).pack(side="left")

        frame_sdk = ttk.Frame(self)
        frame_sdk.pack(fill="x", **pad)
        ttk.Label(frame_sdk, text="Папка Ren'Py SDK (для автогенерации файлов перевода):").pack(anchor="w")
        row_sdk = ttk.Frame(frame_sdk)
        row_sdk.pack(fill="x", pady=(2, 0))
        sdk_entry = ttk.Entry(row_sdk, textvariable=self.sdk_dir_var)
        sdk_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row_sdk, text="Обзор...", command=self._choose_sdk_dir).pack(side="left", padx=(6, 0))
        ttk.Label(row_sdk, text="Имя языка для Ren'Py:").pack(side="left", padx=(12, 0))
        self.renpy_lang_var = tk.StringVar(value="ru")
        ttk.Entry(row_sdk, textvariable=self.renpy_lang_var, width=8).pack(side="left", padx=(4, 0))
        self.generate_tl_btn = ttk.Button(
            row_sdk, text="Сгенерировать файлы перевода (Ren'Py SDK)",
            command=self._generate_tl_files, state="disabled",
        )
        self.generate_tl_btn.pack(side="left", padx=(6, 0))

        frame_lang = ttk.Frame(self)
        frame_lang.pack(fill="x", **pad)

        ttk.Label(frame_lang, text="Папка перевода (game/tl/...):").grid(row=0, column=0, sticky="w")
        self.lang_combo = ttk.Combobox(frame_lang, textvariable=self.lang_var, state="readonly", width=20)
        self.lang_combo.grid(row=1, column=0, sticky="w", pady=(2, 0))

        ttk.Label(frame_lang, text="Язык оригинала:").grid(row=0, column=1, sticky="w", padx=(20, 0))
        self.src_combo = ttk.Combobox(
            frame_lang, textvariable=self.src_lang_var, state="readonly", width=20,
            values=[n for n, _ in LANGUAGES],
        )
        self.src_combo.grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(2, 0))

        ttk.Label(frame_lang, text="Язык перевода:").grid(row=0, column=2, sticky="w", padx=(20, 0))
        self.dest_combo = ttk.Combobox(
            frame_lang, textvariable=self.dest_lang_var, state="readonly", width=20,
            values=[n for n, _ in LANGUAGES],
        )
        self.dest_combo.grid(row=1, column=2, sticky="w", padx=(20, 0), pady=(2, 0))
        self.dest_combo.bind("<<ComboboxSelected>>", self._on_dest_lang_changed)

        frame_engine = ttk.Frame(self)
        frame_engine.pack(fill="x", **pad)
        ttk.Label(frame_engine, text="Переводчик:").grid(row=0, column=0, sticky="w")
        self.engine_combo = ttk.Combobox(
            frame_engine, textvariable=self.engine_var, state="readonly", width=38,
            values=[
                "Google Translate (бесплатно, без ключа)",
                "DeepL (бесплатно, нужен свой ключ API)",
                "LibreTranslate (офлайн, свой локальный сервер)",
            ],
        )
        self.engine_combo.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_changed)

        ttk.Label(frame_engine, text="Ключ DeepL API (только для DeepL):").grid(
            row=0, column=1, sticky="w", padx=(20, 0)
        )
        self.deepl_key_entry = ttk.Entry(
            frame_engine, textvariable=self.deepl_key_var, width=30, show="•",
            state="disabled",
        )
        self.deepl_key_entry.grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(2, 0))

        ttk.Label(frame_engine, text="Адрес LibreTranslate (только для LibreTranslate):").grid(
            row=0, column=2, sticky="w", padx=(20, 0)
        )
        self.libre_url_entry = ttk.Entry(
            frame_engine, textvariable=self.libre_url_var, width=24,
            state="disabled",
        )
        self.libre_url_entry.grid(row=1, column=2, sticky="w", padx=(20, 0), pady=(2, 0))

        frame_opts = ttk.Frame(self)
        frame_opts.pack(fill="x", **pad)
        ttk.Checkbutton(
            frame_opts, text="Перезаписывать уже переведённые строки",
            variable=self.overwrite_var,
        ).pack(side="left")
        ttk.Checkbutton(
            frame_opts, text="Использовать кэш переводов (ускоряет повторный запуск)",
            variable=self.use_cache_var,
        ).pack(side="left", padx=(20, 0))

        frame_btns = ttk.Frame(self)
        frame_btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(frame_btns, text="Начать перевод", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(frame_btns, text="Остановить", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=8)
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", **pad)

        row_log_header = ttk.Frame(self)
        row_log_header.pack(fill="x", padx=8)
        ttk.Label(row_log_header, text="Журнал:").pack(side="left")
        ttk.Button(
            row_log_header, text="Сохранить журнал в файл...",
            command=self._save_log,
        ).pack(side="right")
        log_frame = ttk.Frame(self)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, wrap="word")
        self.log_text.bind("<Key>", self._log_text_keypress)
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

    # ------------------------------------------------------------------
    # Действия пользователя
    # ------------------------------------------------------------------
    def _choose_folder(self):
        path = filedialog.askdirectory(title="Выберите папку игры")
        if not path:
            return
        self.project_path.set(path)
        self._refresh_project_dirs(show_warnings=True)

    def _refresh_project_dirs(self, show_warnings):
        """Обновляет self.game_dir/self.tl_dir и связанные элементы UI
        по текущему self.project_path. Общая логика для выбора папки
        игры и для обновления после автогенерации файлов перевода через
        Ren'Py SDK (тогда предупреждения не нужны — мы только что сами
        создали эти файлы)."""
        path = self.project_path.get()
        self.game_dir = project.find_game_dir(path)
        self.prepare_btn.configure(state="normal" if self.game_dir else "disabled")
        self.generate_tl_btn.configure(state="normal" if self.game_dir else "disabled")

        tl_dir = project.find_tl_dir(path)
        self.tl_dir = tl_dir
        if not tl_dir:
            self.lang_combo["values"] = []
            self.lang_var.set("")
            if show_warnings:
                messagebox.showwarning(
                    "Папка tl не найдена",
                    "В этой папке не нашлась папка game/tl.\n\n"
                    "Сгенерируйте файлы перевода: либо кнопкой "
                    "«Сгенерировать файлы перевода (Ren'Py SDK)» выше "
                    "(если указана папка SDK), либо вручную — запустите "
                    "игру, откройте меню разработчика (обычно Shift+O -> "
                    "'Developer Menu' -> 'Generate Translations') и "
                    "создайте перевод для нужного языка. После этого в "
                    "папке game/tl/<язык>/ появятся файлы, и эта "
                    "программа сможет их обработать.",
                )
            return
        langs = project.list_language_folders(tl_dir)
        self.lang_combo["values"] = langs
        if langs:
            self.lang_var.set(langs[0])
        else:
            self.lang_var.set("")
            if show_warnings:
                messagebox.showinfo(
                    "Нет языков",
                    "Папка game/tl найдена, но внутри нет ни одной языковой папки.",
                )

    def _choose_sdk_dir(self):
        path = filedialog.askdirectory(title="Выберите папку Ren'Py SDK")
        if not path:
            return
        self.sdk_dir_var.set(path)
        self.settings["sdk_dir"] = path
        project.save_settings(self.settings_path, self.settings)

    def _save_log(self):
        content = self.log_text.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showinfo("Журнал пуст", "Пока нечего сохранять.")
            return
        default_dir = os.path.dirname(os.path.abspath(__file__))
        path = filedialog.asksaveasfilename(
            title="Сохранить журнал",
            initialdir=default_dir,
            initialfile="renpy_translator_log.txt",
            defaultextension=".txt",
            filetypes=[("Текстовые файлы", "*.txt"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            messagebox.showerror("Ошибка", "Не удалось сохранить журнал: {0}".format(e))
            return
        messagebox.showinfo("Готово", "Журнал сохранён:\n{0}".format(path))

    def _log_text_keypress(self, event):
        """Журнал должен оставаться доступным только для чтения, но при
        этом свободно выделяться и копироваться (Ctrl+C, Ctrl+A). Text
        в состоянии 'disabled' в Tkinter иногда блокирует и копирование
        тоже — поэтому держим виджет 'normal' и просто игнорируем любой
        ввод, кроме навигации и копирования/выделения.

        На русской (и любой не-английской) раскладке клавиатуры Windows
        event.keysym для Ctrl+C возвращает не "c", а что-то вроде
        "Cyrillic_es" — раскладка подменяет опознанную клавишу, поэтому
        старая проверка по keysym попросту не срабатывала. Зато
        event.char для Ctrl+<буква> Windows отдаёт управляющий символ
        (Ctrl+C -> chr(3), Ctrl+A -> chr(1)) одинаково при любой
        раскладке — на него и опираемся в первую очередь."""
        ctrl = bool(event.state & 0x4)
        if ctrl and (event.char in ("\x03", "\x01") or event.keysym.lower() in ("c", "a", "insert")):
            return None
        if event.keysym in (
            "Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End",
            "Shift_L", "Shift_R", "Control_L", "Control_R",
        ):
            return None
        return "break"

    def _on_engine_changed(self, _event=None):
        engine = self.engine_var.get()
        self.deepl_key_entry.configure(
            state="normal" if engine.startswith("DeepL") else "disabled"
        )
        self.libre_url_entry.configure(
            state="normal" if engine.startswith("LibreTranslate") else "disabled"
        )

    def _on_dest_lang_changed(self, _event=None):
        # По умолчанию имя языка для Ren'Py совпадает с коротким кодом,
        # как и весь остальной интерфейс программы (ru/en/de/...). Если
        # для конкретной игры нужно полное имя (например "russian" —
        # так называет языки сам Ren'Py по умолчанию), можно вписать
        # его в поле вручную перед нажатием кнопки генерации.
        code = LANG_NAME_TO_CODE.get(self.dest_lang_var.get(), "")
        if code:
            self.renpy_lang_var.set(code)

    def _log(self, msg):
        self.log_queue.put(msg)

    def _poll_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg == "__DONE__":
                    self._on_finished()
                    continue
                if msg == "__PREPARE_DONE__":
                    self.prepare_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.generate_tl_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.start_btn.configure(state="normal")
                    self.status_var.set("")
                    continue
                if msg == "__GENERATE_TL_DONE__":
                    self.prepare_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.generate_tl_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.start_btn.configure(state="normal")
                    self.status_var.set("")
                    self._refresh_project_dirs(show_warnings=False)
                    continue
                if isinstance(msg, tuple) and msg[0] == "__PHASE1_PROGRESS__":
                    _, done, total = msg
                    self.progress.configure(maximum=total, value=done)
                    self.status_var.set(
                        "Перевод строк: {0} / {1}".format(done, total)
                    )
                    continue
                if isinstance(msg, tuple) and msg[0] == "__PHASE2_PROGRESS__":
                    _, idx, total, rel = msg
                    self.progress.configure(maximum=total, value=idx)
                    self.status_var.set(
                        "Файл {0} / {1}: {2}".format(idx, total, rel)
                    )
                    continue
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _start(self):
        if not self.tl_dir:
            messagebox.showerror("Ошибка", "Сначала выберите папку игры с папкой game/tl.")
            return
        lang_folder = self.lang_var.get()
        if not lang_folder:
            messagebox.showerror("Ошибка", "Выберите языковую папку для перевода.")
            return

        src_code = LANG_NAME_TO_CODE.get(self.src_lang_var.get(), "en")
        dest_code = LANG_NAME_TO_CODE.get(self.dest_lang_var.get(), "ru")
        if src_code == dest_code:
            if not messagebox.askyesno(
                "Одинаковый язык",
                "Язык оригинала и язык перевода совпадают. Продолжить всё равно?",
            ):
                return

        lang_dir = os.path.join(self.tl_dir, lang_folder)
        files = project.find_rpy_files(lang_dir)
        if not files:
            messagebox.showinfo("Нет файлов", "В выбранной языковой папке нет .rpy файлов.")
            return

        engine_label = self.engine_var.get()
        if engine_label.startswith("DeepL"):
            engine = "deepl"
        elif engine_label.startswith("LibreTranslate"):
            engine = "libretranslate"
        else:
            engine = "google"

        deepl_key = self.deepl_key_var.get().strip()
        if engine == "deepl" and not deepl_key:
            messagebox.showerror(
                "Нужен ключ DeepL",
                "Для DeepL нужно вставить ключ API. Бесплатно получить его можно "
                "на странице https://www.deepl.com/pro-api (без привязки карты, "
                "тариф 'DeepL API Free').",
            )
            return

        libre_url = self.libre_url_var.get().strip() or libretranslate_translate.DEFAULT_URL
        libre_key = self.libre_key_var.get().strip()

        self.stop_requested = False
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(maximum=len(files), value=0)
        self.log_text.delete("1.0", "end")

        overwrite = self.overwrite_var.get()
        use_cache = self.use_cache_var.get()

        self.worker_thread = threading.Thread(
            target=self._worker,
            args=(lang_dir, files, src_code, dest_code, overwrite, use_cache,
                  engine, deepl_key, libre_url, libre_key),
            daemon=True,
        )
        self.worker_thread.start()

    def _stop(self):
        self.stop_requested = True
        if self.translator:
            self.translator.stop()
        self._log("Останавливаю после текущего файла...")

    def _on_finished(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _start_prepare(self):
        if not self.game_dir:
            messagebox.showerror("Ошибка", "Сначала выберите папку игры.")
            return

        need_unrpyc = not unrpyc_manager.is_installed(self.tools_dir)
        if need_unrpyc:
            ok = messagebox.askyesno(
                "Скачать unrpyc?",
                "Для раскомпиляции .rpyc в читаемый .rpy нужен открытый "
                "инструмент unrpyc (github.com/CensoredUsername/unrpyc, "
                "лицензия MIT). Он не входит в эту программу и будет "
                "скачан один раз ({0}). Продолжить?".format(
                    unrpyc_manager.REPO_ZIP_URL.format(branch=unrpyc_manager.DEFAULT_BRANCH)
                ),
            )
            if not ok:
                return

        self.prepare_btn.configure(state="disabled")
        self.generate_tl_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.log_text.delete("1.0", "end")
        self.status_var.set("Подготовка игры...")
        clobber = self.clobber_var.get()
        threading.Thread(
            target=self._prepare_worker, args=(need_unrpyc, clobber), daemon=True,
        ).start()

    def _prepare_worker(self, need_unrpyc, clobber):
        try:
            self._log("Ищу и распаковываю архивы .rpa...")
            n_archives, n_files = rpa_tools.extract_all(self.game_dir, log=self._log)
            if n_archives:
                self._log(
                    "Распаковано архивов: {0}, файлов внутри — {1}."
                    .format(n_archives, n_files)
                )

            if need_unrpyc:
                unrpyc_manager.download(self.tools_dir, log=self._log)

            self._log("Раскомпилирую .rpyc в читаемый .rpy (unrpyc)...")
            result = unrpyc_manager.decompile_all(
                self.game_dir, self.tools_dir, log=self._log, clobber=clobber,
            )

            self._log("")
            real_created = result.get("real_created", 0)
            real_pre = result.get("real_pre_existing", 0)
            real_missing = result.get("real_still_missing", 0)
            if clobber:
                self._log(
                    "Перезаписано (пересоздано из .rpyc) файлов: {0}."
                    .format(real_created if real_created else result.get("reported_processed", "?"))
                )
            elif real_created > 0:
                self._log(
                    "Реально раскомпилировано новых файлов: {0}{1}. (Собственная "
                    "сводка unrpyc выше может показывать другие числа — она "
                    "иногда путается при параллельной раскомпиляции, эта "
                    "строка посчитана программой отдельно, простым сравнением "
                    "файлов до и после запуска.)".format(
                        real_created,
                        " (уже было готово раньше: {0})".format(real_pre) if real_pre else "",
                    )
                )
            elif real_pre > 0:
                self._log(
                    "Раскомпилировать было нечего: все {0} файлов .rpyc уже "
                    "имели рядом читаемый .rpy — так бывает, некоторые игры "
                    "распространяются без обфускации исходников. Программа "
                    "их не трогала, это нормально, если только вы не хотите "
                    "принудительно перегенерировать их заново — тогда "
                    "включите галочку «Перезаписать уже раскомпилированные "
                    ".rpy (--clobber)» выше и запустите подготовку ещё раз."
                    .format(real_pre)
                )
            if real_missing:
                self._log(
                    "Внимание: {0} файлов .rpyc так и не получили .rpy — "
                    "посмотрите сообщения об ошибках выше в журнале unrpyc "
                    "(такое бывает на отдельных повреждённых или "
                    "нестандартных .rpyc)."
                    .format(real_missing)
                )
            self._log(
                "Готово. Теперь можно сгенерировать файлы перевода: "
                "кнопкой «Сгенерировать файлы перевода (Ren'Py SDK)» выше "
                "(если указана папка SDK), либо вручную через меню "
                "разработчика в игре."
            )
        except (rpa_tools.RpaError, unrpyc_manager.UnrpycError) as e:
            self._log("ОШИБКА: {0}".format(e))
        except Exception as e:
            self._log("Неожиданная ошибка при подготовке игры: {0}".format(e))
        finally:
            self.log_queue.put("__PREPARE_DONE__")

    def _generate_tl_files(self):
        if not self.game_dir:
            messagebox.showerror("Ошибка", "Сначала выберите папку игры.")
            return
        sdk_dir = self.sdk_dir_var.get().strip()
        if not sdk_dir:
            messagebox.showerror(
                "Нужна папка SDK",
                "Укажите папку Ren'Py SDK (там же, где renpy.exe / renpy.sh) "
                "в поле выше.",
            )
            return

        dest_code = LANG_NAME_TO_CODE.get(self.dest_lang_var.get(), "ru")
        renpy_lang = self.renpy_lang_var.get().strip()
        if not renpy_lang:
            messagebox.showerror(
                "Нужно имя языка",
                "Укажите имя языка для Ren'Py в поле рядом с кнопкой — "
                "обычно это тот же короткий код, что и в «Язык перевода» "
                "выше (например «{0}»), но для некоторых игр может "
                "требоваться полное английское название (например "
                "«russian») — если после генерации язык не появляется в "
                "самой игре, попробуйте именно так.".format(dest_code),
            )
            return

        self.settings["sdk_dir"] = sdk_dir
        project.save_settings(self.settings_path, self.settings)

        self.prepare_btn.configure(state="disabled")
        self.generate_tl_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.log_text.delete("1.0", "end")
        self.status_var.set("Генерирую файлы перевода через Ren'Py SDK...")
        threading.Thread(
            target=self._generate_tl_worker, args=(sdk_dir, renpy_lang), daemon=True,
        ).start()

    def _generate_tl_worker(self, sdk_dir, renpy_lang):
        try:
            self._log(
                "Запускаю Ren'Py SDK для генерации файлов перевода "
                "(язык: {0})...".format(renpy_lang)
            )
            renpy_sdk.generate_translations(
                self.project_path.get(), sdk_dir, renpy_lang, log=self._log,
            )
            self._log("")
            self._log("Готово. Обновляю список языковых папок...")
        except renpy_sdk.RenpySdkError as e:
            self._log("ОШИБКА: {0}".format(e))
        except Exception as e:
            self._log("Неожиданная ошибка при запуске Ren'Py SDK: {0}".format(e))
        finally:
            self.log_queue.put("__GENERATE_TL_DONE__")

    # ------------------------------------------------------------------
    # Фоновый поток
    # ------------------------------------------------------------------
    def _worker(self, lang_dir, files, src_code, dest_code, overwrite, use_cache,
                engine, deepl_key, libre_url, libre_key):
        cache_names = {
            "google": project.CACHE_FILENAME,
            "deepl": project.CACHE_FILENAME_DEEPL,
            "libretranslate": project.CACHE_FILENAME_LIBRE,
        }
        engine_titles = {
            "google": "Google Translate",
            "deepl": "DeepL",
            "libretranslate": "LibreTranslate",
        }
        cache_name = cache_names[engine]
        cache = project.load_cache(lang_dir, cache_name) if use_cache else {}

        # Периодически сохраняем кэш во время работы, чтобы при остановке
        # посреди большого файла переведённые строки не терялись.
        progress_counter = {"n": 0}
        phase1_done = {"n": 0}
        phase1_total = {"n": 0}

        def on_progress():
            progress_counter["n"] += 1
            if use_cache and progress_counter["n"] % 15 == 0:
                project.save_cache(lang_dir, cache, cache_name)
            phase1_done["n"] += 1
            n, total = phase1_done["n"], phase1_total["n"]
            self.log_queue.put(("__PHASE1_PROGRESS__", n, total))
            # Не спамим журнал на каждую строку — только периодически,
            # чтобы было видно, что программа не зависла на долгом пакетном
            # переводе, но лог не разрастался на тысячи строк.
            if total and (n == total or n % 50 == 0):
                self._log("Переведено {0} из {1} уникальных строк...".format(n, total))

        if engine == "deepl":
            translator = deepl_translate.DeepLTranslator(
                api_key=deepl_key, src=src_code, dest=dest_code, cache=cache,
                log=self._log, on_progress=on_progress,
            )
        elif engine == "libretranslate":
            translator = libretranslate_translate.LibreTranslator(
                base_url=libre_url, src=src_code, dest=dest_code,
                api_key=(libre_key or None), cache=cache,
                log=self._log, on_progress=on_progress,
            )
        else:
            translator = gtranslate.CachedTranslator(
                src=src_code, dest=dest_code, cache=cache,
                log=self._log, on_progress=on_progress,
            )
        self.translator = translator

        total_stats = {"translated": 0, "skipped": 0}

        # --- Фаза 1: читаем все файлы и собираем уникальные строки,
        # которые нужно перевести, чтобы перевести их пачками (несколько
        # строк за один запрос) — это в разы быстрее, чем по одной. -----
        self._log("Читаю файлы и ищу непереведённые строки...")
        files_lines = {}
        all_needed = set()
        for path in files:
            try:
                lines = project.read_lines(path)
            except Exception as e:
                self._log("  ОШИБКА чтения файла {0}: {1}".format(path, e))
                continue
            files_lines[path] = lines
            all_needed |= core.collect_needed_texts(lines, overwrite=overwrite)

        already_cached = sum(1 for t in all_needed if t in cache)
        self._log(
            "Найдено уникальных строк для перевода: {0} (из них уже в кэше: {1})."
            .format(len(all_needed), already_cached)
        )
        if all_needed:
            self._log("Перевожу пачками (несколько строк за один запрос)...")
            phase1_total["n"] = len(all_needed)
            self.log_queue.put(("__PHASE1_PROGRESS__", 0, len(all_needed)))
            translator.warm_batch(all_needed)
            if use_cache:
                project.save_cache(lang_dir, cache, cache_name)
            self._log(
                "Пакетный перевод завершён: запросов к {0} — {1} "
                "(из них пакетных — {2}, ими переведено {3} строк), "
                "прямо из кэша — {4}.".format(
                    engine_titles[engine],
                    translator.stats["requests"], translator.stats["batch_requests"],
                    translator.stats["batched_lines"], translator.stats["cache_hits"],
                )
            )

        # --- Фаза 2: расставляем переводы по местам в каждом файле.
        # Благодаря фазе 1 это почти не делает новых сетевых запросов. ---
        total_files = len(files)
        for idx, path in enumerate(files, start=1):
            if self.stop_requested:
                self._log("Остановлено пользователем.")
                break
            rel = os.path.relpath(path, lang_dir)
            if path not in files_lines:
                self.log_queue.put(("__PHASE2_PROGRESS__", idx, total_files, rel))
                continue
            self._log("Обрабатываю: {0} [{1}/{2}]".format(rel, idx, total_files))
            try:
                lines = files_lines[path]
                file_stats = {"translated": 0, "skipped": 0}
                new_lines = core.process_lines(
                    lines, translator, overwrite=overwrite, stats=file_stats,
                )
                if file_stats["translated"] > 0:
                    project.write_lines(path, new_lines)
                total_stats["translated"] += file_stats["translated"]
                total_stats["skipped"] += file_stats["skipped"]
                self._log(
                    "  переведено: {0}, пропущено (уже переведено): {1}".format(
                        file_stats["translated"], file_stats["skipped"]
                    )
                )
            except Exception as e:
                self._log("  ОШИБКА при обработке файла {0}: {1}".format(rel, e))

            if use_cache:
                project.save_cache(lang_dir, cache, cache_name)

            self.log_queue.put(("__PHASE2_PROGRESS__", idx, total_files, rel))

        self._log("")
        self._log(
            "Готово. Всего переведено строк: {0}, пропущено: {1}, "
            "запросов к переводчику ({2}): {3} (из них пакетных: {4}, "
            "переведено ими строк: {5}), из кэша: {6}, срабатываний "
            "ограничения скорости: {7}, ошибок перевода: {8}.".format(
                total_stats["translated"], total_stats["skipped"],
                engine_titles[engine],
                translator.stats["requests"], translator.stats["batch_requests"],
                translator.stats["batched_lines"], translator.stats["cache_hits"],
                translator.stats["rate_limit_hits"], translator.stats["errors"],
            )
        )
        if translator.stats["rate_limit_hits"] > 0:
            self._log(
                "Сервис перевода несколько раз ограничивал скорость — это "
                "нормально при большом количестве строк для бесплатного "
                "сервиса. Программа сама притормаживала запросы. Уже "
                "переведённые строки сохранены в кэш — если что-то "
                "осталось непереведённым, просто запустите перевод ещё "
                "раз (лучше через какое-то время), и он продолжит с того "
                "места, на котором лимит был исчерпан."
            )
        self._log(
            "Не забудьте протестировать игру и вручную поправить неудачные "
            "автопереводы — машинный перевод не идеален, особенно для имён, "
            "шуток и игровых терминов."
        )
        self.log_queue.put("__DONE__")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
