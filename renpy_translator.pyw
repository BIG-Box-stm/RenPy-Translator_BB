# -*- coding: utf-8 -*-
"""
Universal RenPy Translator (свободная утилита)
------------------------------------------------
Программа с окном для автоматического перевода уже сгенерированных
файлов перевода Ren'Py (game/tl/<язык>/*.rpy) через Google Translate,
DeepL или локальный LibreTranslate.

Как это работает:
1. В самой Ren'Py-игре (в меню разработчика, обычно Shift+O -> Console,
   либо через Ren'Py SDK -> "Generate Translations", либо кнопкой
   «Сгенерировать файлы перевода» в этом окне) нужно один раз создать
   файлы перевода для нужного языка. После этого в папке
   game/tl/<язык>/ появятся .rpy файлы со строками вида:
       old "Yes"
       new ""
   и диалогами, где переведённая копия пока совпадает с оригиналом.
2. Эта программа находит все такие непереведённые места и подставляет
   туда перевод с английского на русский (или на другой выбранный язык),
   не трогая теги {b}...{/b} и подстановки [name].
3. Уже переведённые вручную строки не перезаписываются (если не включена
   соответствующая галочка).

Программа написана "с нуля" и не использует код каких-либо платных
инструментов — только официальные/открытые механизмы Ren'Py и
публичные/локальные эндпоинты переводчиков.
"""

import concurrent.futures
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import character_names
import core
import deepl_translate
import game_patcher
import gemma_launcher
import gtranslate
import libretranslate_launcher
import libretranslate_translate
import project
import renpy_sdk
import rpa_tools
import rpy_autofix
import translate_gemma
import unrpyc_manager

APP_TITLE = "Universal RenPy Translator"

# Небольшой набор популярных языков для "Язык оригинала"/"Язык
# перевода". Код языка — стандартный код Google Translate. Список можно
# расширять по необходимости.
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

# "Имя языка" (для Ren'Py SDK / кнопки шрифтов) — программа работает
# только с этими языками, поэтому это закрытый список, а не свободный
# ввод.
RENPY_LANG_CODES = ["ru", "fr", "es", "zh", "en"]

# Готовые варианты разделителя (маркера защиты тегов/подстановок).
# core.DEFAULT_MARKER_TEMPLATE — текущий формат по умолчанию (третья
# итерация, см. историю в core.py). Поле также принимает свой вариант,
# набранный вручную — единственное требование к нему описано в
# core.parse_marker_template().
MARKER_PRESETS = [
    "@@{N}@@",
    "Zqtx{N}xtqZ",
    "[ZXQPROTECTEDTOKEN{N}QXZ]",
]

# Значения по умолчанию для размера пачки/задержки — берутся прямо из
# самих классов движков (единственный источник правды), чтобы подсказка
# в интерфейсе никогда не разошлась с реальным поведением программы.
ENGINE_DEFAULTS = {
    "google": {
        "batch_items": gtranslate.CachedTranslator.BATCH_ITEMS,
        "batch_chars": gtranslate.CachedTranslator.BATCH_CHARS,
        "delay": gtranslate.CachedTranslator.BASE_DELAY,
    },
    "deepl": {
        "batch_items": deepl_translate.DeepLTranslator.BATCH_ITEMS,
        "batch_chars": deepl_translate.DeepLTranslator.BATCH_CHARS,
        "delay": deepl_translate.DeepLTranslator.BASE_DELAY,
    },
    "libretranslate": {
        "batch_items": libretranslate_translate.LibreTranslator.BATCH_ITEMS,
        "batch_chars": libretranslate_translate.LibreTranslator.BATCH_CHARS,
        "delay": libretranslate_translate.LibreTranslator.BASE_DELAY,
    },
}


class _GenerateResult:
    """Результат одного запуска renpy_sdk.generate_translations() —
    удобная обёртка, чтобы _generate_tl_worker мог организовать несколько
    попыток подряд, не полагаясь на исключения для управления потоком."""

    def __init__(self, ok, output_lines):
        self.ok = ok
        self.output_lines = output_lines


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("900x680")
        self.minsize(820, 560)

        self.project_path = tk.StringVar()
        self.tl_dir = None
        self.game_dir = None
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.tools_dir = os.path.join(self.script_dir, "tools", "unrpyc")
        # Папка с шрифтами/выбором языка, которую копирует кнопка
        # «Обновить шрифты, включить язык» — см. game_patcher.py и
        # прилагающийся README. Лежит рядом с программой.
        self.fonts_bundle_dir = os.path.join(self.script_dir, "game")
        self.settings_path = os.path.join(self.script_dir, "_translator_settings.json")
        self.settings = project.load_settings(self.settings_path)

        self.clobber_var = tk.BooleanVar(value=False)
        self.sdk_dir_var = tk.StringVar(value=self.settings.get("sdk_dir", ""))
        self.lang_var = tk.StringVar()
        self.src_lang_var = tk.StringVar(value="Английский")
        self.dest_lang_var = tk.StringVar(value="Русский")
        self.overwrite_var = tk.BooleanVar(value=False)
        self.use_cache_var = tk.BooleanVar(value=True)
        self.engine_var = tk.StringVar(value="Google Translate (без ключа)")
        self.deepl_key_var = tk.StringVar()
        self.libre_url_var = tk.StringVar(value=libretranslate_translate.DEFAULT_URL)
        self.libre_key_var = tk.StringVar()
        self.marker_var = tk.StringVar(value=core.DEFAULT_MARKER_TEMPLATE)
        self.renpy_lang_var = tk.StringVar(value="ru")

        # --- Translate Gemma (см. group_gemma в _build_ui) -------------
        self.gemma_llama_folder_var = tk.StringVar()
        self.gemma_model_path_var = tk.StringVar()
        self.gemma_ngl_var = tk.StringVar()
        self.gemma_np_var = tk.StringVar()
        self.gemma_ctx_var = tk.StringVar()
        self.gemma_ntokens_var = tk.StringVar()
        self.gemma_url_var = tk.StringVar(value=translate_gemma.DEFAULT_URL)
        self._gemma_stop_event = None

        self.log_queue = queue.Queue()
        self.worker_thread = None
        self.translator = None
        self.stop_requested = False

        self._build_ui()
        self._on_engine_changed()
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        # --- Папка игры -----------------------------------------------
        frame_top = ttk.Frame(self)
        frame_top.pack(fill="x", **pad)

        ttk.Label(frame_top, text="Папка игры (или папка проекта с папкой game):").pack(anchor="w")
        row = ttk.Frame(frame_top)
        row.pack(fill="x", pady=(2, 0))
        entry = ttk.Entry(row, textvariable=self.project_path)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор...", command=self._choose_folder).pack(side="left", padx=(6, 0))

        row_prepare_opts = ttk.Frame(frame_top)
        row_prepare_opts.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(
            row_prepare_opts,
            text="Перезаписать уже раскомпилированные .rpy (--clobber)",
            variable=self.clobber_var,
        ).pack(side="left")
        self.prepare_btn = ttk.Button(
            row_prepare_opts, text="Подготовить игру (распаковать .rpa / .rpyc)",
            command=self._start_prepare, state="disabled",
        )
        self.prepare_btn.pack(side="right")

        # --- Папка SDK ---------------------------------------------------
        frame_sdk = ttk.Frame(self)
        frame_sdk.pack(fill="x", **pad)
        ttk.Label(frame_sdk, text="Папка Ren'Py SDK (для автогенерации файлов перевода):").pack(anchor="w")
        row_sdk = ttk.Frame(frame_sdk)
        row_sdk.pack(fill="x", pady=(2, 0))
        sdk_entry = ttk.Entry(row_sdk, textvariable=self.sdk_dir_var)
        sdk_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row_sdk, text="Обзор...", command=self._choose_sdk_dir).pack(side="left", padx=(6, 0))

        # --- Переводчик + ключ DeepL + LibreTranslate --------------------
        frame_engine = ttk.Frame(self)
        frame_engine.pack(fill="x", **pad)
        ttk.Label(frame_engine, text="Переводчик:").grid(row=0, column=0, sticky="w")
        self.engine_combo = ttk.Combobox(
            frame_engine, textvariable=self.engine_var, state="readonly", width=30,
            values=[
                "Google Translate (без ключа)",
                "DeepL (нужен ключ API)",
                "LibreTranslate (офлайн, локально)",
                "Translate Gemma (офлайн)",
            ],
        )
        self.engine_combo.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_changed)

        ttk.Label(frame_engine, text="Ключ DeepL API:").grid(row=0, column=1, sticky="w", padx=(20, 0))
        self.deepl_key_entry = ttk.Entry(
            frame_engine, textvariable=self.deepl_key_var, width=30, show="•",
            state="disabled",
        )
        self.deepl_key_entry.grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(2, 0))

        # Поля адреса/запуска для LibreTranslate и Translate Gemma живут
        # в ОДНИХ И ТЕХ ЖЕ ячейках грида (row0-1, column2-3) и просто
        # показываются/скрываются через grid()/grid_remove() в
        # _on_engine_changed() в зависимости от выбранного движка — так
        # уже введённые значения не теряются при переключении туда и
        # обратно (grid_remove(), в отличие от destroy(), не забывает
        # свои прежние grid()-параметры).
        self.libre_url_label = ttk.Label(frame_engine, text="Адрес LibreTranslate:")
        self.libre_url_label.grid(row=0, column=2, sticky="w", padx=(20, 0))
        self.libre_url_entry = ttk.Entry(frame_engine, textvariable=self.libre_url_var, width=22)
        self.libre_url_entry.grid(row=1, column=2, sticky="w", padx=(20, 0), pady=(2, 0))

        self.launch_libre_label = ttk.Label(frame_engine, text="Только для LibreTranslate:")
        self.launch_libre_label.grid(row=0, column=3, sticky="w", padx=(16, 0))
        self.launch_libre_btn = ttk.Button(
            frame_engine, text="Запустить LibreTranslate",
            command=self._start_launch_libretranslate,
        )
        self.launch_libre_btn.grid(row=1, column=3, sticky="w", padx=(16, 0), pady=(2, 0))

        self.gemma_url_label = ttk.Label(frame_engine, text="Адрес Translate Gemma:")
        self.gemma_url_label.grid(row=0, column=2, sticky="w", padx=(20, 0))
        self.gemma_url_entry = ttk.Entry(frame_engine, textvariable=self.gemma_url_var, width=22)
        self.gemma_url_entry.grid(row=1, column=2, sticky="w", padx=(20, 0), pady=(2, 0))

        self.gemma_launch_label = ttk.Label(frame_engine, text="Только для Translate Gemma:")
        self.gemma_launch_label.grid(row=0, column=3, sticky="w", padx=(16, 0))
        self.launch_gemma_btn = ttk.Button(
            frame_engine, text="Запустить Translate Gemma",
            command=self._start_launch_gemma,
        )
        self.launch_gemma_btn.grid(row=1, column=3, sticky="w", padx=(16, 0), pady=(2, 0))

        # --- Настройка Translate Gemma (отдельный блок, показывается
        # только для этого движка — см. _on_engine_changed) -------------
        self.group_gemma = ttk.LabelFrame(self, text="Настройка Translate Gemma")
        self.group_gemma.columnconfigure(0, weight=1)

        ttk.Label(self.group_gemma, text="Папка llama.cpp:").grid(
            row=0, column=0, sticky="w", padx=8, pady=(6, 0)
        )
        ttk.Label(self.group_gemma, text="Загрузка GPU").grid(
            row=0, column=1, sticky="w", padx=(16, 0), pady=(6, 0)
        )
        ttk.Label(self.group_gemma, text="Число потоков").grid(
            row=0, column=2, sticky="w", padx=(16, 0), pady=(6, 0)
        )

        row_gemma_folder = ttk.Frame(self.group_gemma)
        row_gemma_folder.grid(row=1, column=0, sticky="we", padx=8, pady=(2, 0))
        ttk.Entry(row_gemma_folder, textvariable=self.gemma_llama_folder_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            row_gemma_folder, text="Обзор...", command=self._choose_gemma_llama_folder,
        ).pack(side="left", padx=(6, 0))

        ttk.Entry(self.group_gemma, textvariable=self.gemma_ngl_var, width=8).grid(
            row=1, column=1, sticky="w", padx=(16, 0), pady=(2, 0)
        )
        ttk.Entry(self.group_gemma, textvariable=self.gemma_np_var, width=8).grid(
            row=1, column=2, sticky="w", padx=(16, 0), pady=(2, 0)
        )

        ttk.Label(self.group_gemma, text="Модель Translate Gemma .gguf:").grid(
            row=2, column=0, sticky="w", padx=8, pady=(8, 0)
        )
        ttk.Label(self.group_gemma, text="Контекстное окно").grid(
            row=2, column=1, sticky="w", padx=(16, 0), pady=(8, 0)
        )
        ttk.Label(self.group_gemma, text="Токенов в ответе (max)").grid(
            row=2, column=2, sticky="w", padx=(16, 0), pady=(8, 0)
        )

        row_gemma_model = ttk.Frame(self.group_gemma)
        row_gemma_model.grid(row=3, column=0, sticky="we", padx=8, pady=(2, 0))
        ttk.Entry(row_gemma_model, textvariable=self.gemma_model_path_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            row_gemma_model, text="Обзор...", command=self._choose_gemma_model_file,
        ).pack(side="left", padx=(6, 0))

        ttk.Entry(self.group_gemma, textvariable=self.gemma_ctx_var, width=8).grid(
            row=3, column=1, sticky="w", padx=(16, 0), pady=(2, 0)
        )
        ttk.Entry(self.group_gemma, textvariable=self.gemma_ntokens_var, width=8).grid(
            row=3, column=2, sticky="w", padx=(16, 0), pady=(2, 0)
        )

        ttk.Label(
            self.group_gemma,
            text="По умолчанию:  -ngl {0}  /  -np {1}  /  -c {2}  /  -n {3}  "
                 "(оставьте поля пустыми, чтобы использовать эти значения)"
            .format(translate_gemma.DEFAULT_NGL, translate_gemma.DEFAULT_NP,
                    translate_gemma.DEFAULT_CTX, translate_gemma.DEFAULT_N_PREDICT),
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 8))

        # --- Две группы: Настройка перевода / Настройки языка -----------
        self.frame_groups = ttk.Frame(self)
        frame_groups = self.frame_groups
        frame_groups.pack(fill="x", **pad)
        frame_groups.columnconfigure(0, weight=1)
        frame_groups.columnconfigure(1, weight=1)

        group_translate = ttk.LabelFrame(frame_groups, text="Настройка перевода")
        group_translate.grid(row=0, column=0, sticky="nwe", padx=(0, 8))
        group_lang = ttk.LabelFrame(frame_groups, text="Настройки языка")
        group_lang.grid(row=0, column=1, sticky="nwe", padx=(8, 0))

        gt_pad = {"padx": 8, "pady": (4, 0)}

        ttk.Label(group_translate, text="Разделитель:").grid(row=0, column=0, sticky="w", **gt_pad)
        self.batch_items_label = ttk.Label(group_translate, text="Строк в пачке:")
        self.batch_items_label.grid(row=0, column=1, sticky="w", **gt_pad)
        self.batch_delay_label = ttk.Label(group_translate, text="Задержка запросов, сек:")
        self.batch_delay_label.grid(row=0, column=2, sticky="w", **gt_pad)

        self.marker_combo = ttk.Combobox(
            group_translate, textvariable=self.marker_var, width=18,
            values=MARKER_PRESETS,
        )
        self.marker_combo.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 0))
        self.batch_items_var = tk.StringVar(value="")
        self.batch_items_entry = ttk.Entry(group_translate, textvariable=self.batch_items_var, width=8)
        self.batch_items_entry.grid(row=1, column=1, sticky="w", padx=8, pady=(0, 0))
        self.batch_delay_var = tk.StringVar(value="")
        self.batch_delay_entry = ttk.Entry(group_translate, textvariable=self.batch_delay_var, width=8)
        self.batch_delay_entry.grid(row=1, column=2, sticky="w", padx=8, pady=(0, 0))
        self.batch_defaults_label = ttk.Label(group_translate, text="", justify="left")
        self.batch_defaults_label.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 0))
        ttk.Label(
            group_translate,
            text="(оставьте поля пустыми, чтобы использовать эти значения)",
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 8))

        ttk.Label(group_lang, text="Имя языка:").grid(row=0, column=0, sticky="w", **gt_pad)
        ttk.Label(group_lang, text="Папка перевода:").grid(row=0, column=1, sticky="w", **gt_pad)
        ttk.Label(group_lang, text="Язык оригинала:").grid(row=0, column=2, sticky="w", padx=(8, 8), pady=(4, 0))
        self.renpy_lang_combo = ttk.Combobox(
            group_lang, textvariable=self.renpy_lang_var, state="readonly", width=6,
            values=RENPY_LANG_CODES,
        )
        self.renpy_lang_combo.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 4))
        self.lang_combo = ttk.Combobox(group_lang, textvariable=self.lang_var, state="readonly", width=16)
        self.lang_combo.grid(row=1, column=1, sticky="w", padx=8, pady=(0, 4))
        self.src_combo = ttk.Combobox(
            group_lang, textvariable=self.src_lang_var, state="readonly", width=16,
            values=[n for n, _ in LANGUAGES],
        )
        self.src_combo.grid(row=1, column=2, sticky="w", padx=(8, 8), pady=(0, 4))

        ttk.Label(group_lang, text="Язык перевода:").grid(row=2, column=2, sticky="w", padx=(8, 8), pady=(4, 0))
        self.dest_combo = ttk.Combobox(
            group_lang, textvariable=self.dest_lang_var, state="readonly", width=16,
            values=[n for n, _ in LANGUAGES],
        )
        self.dest_combo.grid(row=3, column=2, sticky="w", padx=(8, 8), pady=(0, 4))
        self.dest_combo.bind("<<ComboboxSelected>>", self._on_dest_lang_changed)

        self.generate_tl_btn = ttk.Button(
            group_lang, text="Сгенерировать файлы перевода",
            command=self._generate_tl_files, state="disabled",
        )
        self.generate_tl_btn.grid(row=2, column=0, columnspan=2, rowspan=2, sticky="wes", padx=8, pady=(6, 8))

        # --- Опции + кнопки перевода/шрифтов -----------------------------
        frame_opts = ttk.Frame(self)
        frame_opts.pack(fill="x", **pad)
        ttk.Checkbutton(
            frame_opts, text="Перезаписывать уже переведённые строки",
            variable=self.overwrite_var,
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            frame_opts, text="Использовать кэш переводов (ускоряет повторный запуск)",
            variable=self.use_cache_var,
        ).grid(row=0, column=1, sticky="w", padx=(20, 0))

        frame_btns_left = ttk.Frame(frame_opts)
        frame_btns_left.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.start_btn = ttk.Button(frame_btns_left, text="Начать перевод", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(frame_btns_left, text="Остановить", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        self.update_fonts_btn = ttk.Button(
            frame_opts, text="Обновить шрифты, включить язык",
            command=self._start_update_fonts, state="disabled",
        )
        self.update_fonts_btn.grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(8, 0))

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

    def _choose_sdk_dir(self):
        path = filedialog.askdirectory(title="Выберите папку Ren'Py SDK")
        if not path:
            return
        self.sdk_dir_var.set(path)
        self.settings["sdk_dir"] = path
        project.save_settings(self.settings_path, self.settings)

    def _choose_gemma_llama_folder(self):
        path = filedialog.askdirectory(
            title="Выберите папку llama.cpp (там, где лежит llama-server.exe)"
        )
        if path:
            self.gemma_llama_folder_var.set(path)

    def _choose_gemma_model_file(self):
        path = filedialog.askopenfilename(
            title="Выберите файл модели Translate Gemma",
            filetypes=[("GGUF модели", "*.gguf"), ("Все файлы", "*.*")],
        )
        if path:
            self.gemma_model_path_var.set(path)

    def _int_or_default(self, raw, default, field_label):
        """Разбирает содержимое числового поля GUI: пустая строка →
        значение по умолчанию, иначе — целое число. Бросает ValueError с
        понятным текстом (имя поля) при нечисловом вводе — вызывающий
        код сам решает, как показать ошибку (messagebox или журнал)."""
        raw = (raw or "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ValueError("«{0}» должно быть целым числом.".format(field_label))

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
        self.update_fonts_btn.configure(state="normal" if self.game_dir else "disabled")

        tl_dir = project.find_tl_dir(path)
        self.tl_dir = tl_dir
        if not tl_dir:
            # Папка game/tl создаётся самим Ren'Py SDK (кнопкой
            # «Сгенерировать файлы перевода» или вручную через меню
            # разработчика в игре) — на этапе первого выбора папки игры
            # её вполне ожидаемо ещё нет, поэтому предупреждение здесь не
            # нужно (раньше messagebox.showwarning всплывал при каждом
            # выборе папки, хотя это нормальная, а не ошибочная ситуация).
            self.lang_combo["values"] = []
            self.lang_var.set("")
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



    def _save_log(self):
        content = self.log_text.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showinfo("Журнал пуст", "Пока нечего сохранять.")
            return
        logs_dir = os.path.join(self.script_dir, "logs")
        try:
            os.makedirs(logs_dir, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Ошибка", "Не удалось создать папку logs: {0}".format(e))
            return
        raw_name = os.path.basename(os.path.normpath(self.project_path.get())) \
            if self.project_path.get() else ""
        game_name = raw_name or "game"
        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        filename = "{0}_{1}.txt".format(timestamp, game_name)
        path = os.path.join(logs_dir, filename)
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

    def _current_engine_key(self):
        engine = self.engine_var.get()
        if engine.startswith("DeepL"):
            return "deepl"
        if engine.startswith("LibreTranslate"):
            return "libretranslate"
        if engine.startswith("Translate Gemma"):
            return "gemma"
        return "google"

    def _on_engine_changed(self, _event=None):
        key = self._current_engine_key()
        self.deepl_key_entry.configure(state="normal" if key == "deepl" else "disabled")

        show_libre = key == "libretranslate"
        for w in (self.libre_url_label, self.libre_url_entry,
                  self.launch_libre_label, self.launch_libre_btn):
            w.grid() if show_libre else w.grid_remove()

        show_gemma = key == "gemma"
        for w in (self.gemma_url_label, self.gemma_url_entry,
                  self.gemma_launch_label, self.launch_gemma_btn):
            w.grid() if show_gemma else w.grid_remove()

        if show_gemma:
            self.group_gemma.pack(fill="x", padx=8, pady=6, before=self.frame_groups)
        else:
            self.group_gemma.pack_forget()

        # У Translate Gemma нет пакетного перевода (каждая строка зависит
        # от предыдущей) — поля "Строк в пачке"/"Задержка запросов" ей не
        # нужны и только загромождают интерфейс.
        show_batch_fields = not show_gemma
        for w in (self.batch_items_label, self.batch_items_entry,
                  self.batch_delay_label, self.batch_delay_entry):
            w.grid() if show_batch_fields else w.grid_remove()

        if show_gemma:
            self.batch_defaults_label.configure(text="")
        else:
            d = ENGINE_DEFAULTS[key]
            self.batch_defaults_label.configure(
                text="по умолчанию:   {0} строк / {1} симв.   {2} с"
                .format(d["batch_items"], d["batch_chars"], d["delay"])
            )

    def _on_dest_lang_changed(self, _event=None):
        # Если код языка перевода входит в список, с которым работает
        # программа (RENPY_LANG_CODES), сразу подставляем его и в поле
        # «Имя языка» — это то же самое, что использует Ren'Py SDK и
        # кнопка «Обновить шрифты, включить язык». Для остальных языков
        # поле нужно выбрать вручную.
        code = LANG_NAME_TO_CODE.get(self.dest_lang_var.get(), "")
        if code in RENPY_LANG_CODES:
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
                    self.update_fonts_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.start_btn.configure(state="normal")
                    self.status_var.set("")
                    continue
                if msg == "__GENERATE_TL_DONE__":
                    self.prepare_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.generate_tl_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.update_fonts_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.start_btn.configure(state="normal")
                    self.status_var.set("")
                    self._refresh_project_dirs(show_warnings=False)
                    continue
                if msg == "__UPDATE_FONTS_DONE__":
                    self.prepare_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.generate_tl_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.update_fonts_btn.configure(state="normal" if self.game_dir else "disabled")
                    self.start_btn.configure(state="normal")
                    self.status_var.set("")
                    continue
                if isinstance(msg, tuple) and msg[0] == "__ENABLE_WIDGET__":
                    widget = getattr(self, msg[1], None)
                    if widget is not None:
                        widget.configure(state="normal")
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

        engine = self._current_engine_key()

        if engine == "gemma":
            marker_raw = self.marker_var.get().strip() or core.DEFAULT_MARKER_TEMPLATE
            try:
                marker_open, marker_close = core.parse_marker_template(marker_raw)
            except ValueError as e:
                messagebox.showerror("Неверный разделитель", str(e))
                return
            try:
                gemma_np = self._int_or_default(
                    self.gemma_np_var.get(), translate_gemma.DEFAULT_NP, "Число потоков"
                )
                gemma_n_predict = self._int_or_default(
                    self.gemma_ntokens_var.get(), translate_gemma.DEFAULT_N_PREDICT,
                    "Токенов в ответе (max)",
                )
            except ValueError as e:
                messagebox.showerror("Неверное значение", str(e))
                return
            gemma_url = self.gemma_url_var.get().strip() or translate_gemma.DEFAULT_URL

            # Формат маркеров общий для всего модуля core — как и для
            # остальных движков, меняем его здесь, до запуска фонового
            # потока.
            core.set_marker(marker_open, marker_close)

            self.stop_requested = False
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.progress.configure(maximum=len(files), value=0)
            self.log_text.delete("1.0", "end")

            overwrite = self.overwrite_var.get()

            self.worker_thread = threading.Thread(
                target=self._worker_gemma,
                args=(lang_dir, files, overwrite, gemma_url, gemma_np, gemma_n_predict),
                daemon=True,
            )
            self.worker_thread.start()
            return

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

        marker_raw = self.marker_var.get().strip() or core.DEFAULT_MARKER_TEMPLATE
        try:
            marker_open, marker_close = core.parse_marker_template(marker_raw)
        except ValueError as e:
            messagebox.showerror("Неверный разделитель", str(e))
            return

        batch_items_raw = self.batch_items_var.get().strip()
        batch_delay_raw = self.batch_delay_var.get().strip()
        batch_items = None
        batch_delay = None
        try:
            if batch_items_raw:
                batch_items = int(batch_items_raw)
                if batch_items < 1:
                    raise ValueError
        except ValueError:
            messagebox.showerror(
                "Неверное значение",
                "«Строк в пачке» должно быть целым числом больше нуля "
                "(или пустым — тогда используется значение по умолчанию).",
            )
            return
        try:
            if batch_delay_raw:
                batch_delay = float(batch_delay_raw.replace(",", "."))
                if batch_delay < 0:
                    raise ValueError
        except ValueError:
            messagebox.showerror(
                "Неверное значение",
                "«Задержка запросов» должна быть числом не меньше нуля "
                "(или пустым — тогда используется значение по "
                "умолчанию).",
            )
            return

        # Формат маркеров общий для всего модуля core — меняем его здесь,
        # до запуска фонового потока, чтобы вся сессия перевода
        # использовала один и тот же формат (см. core.set_marker()).
        core.set_marker(marker_open, marker_close)

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
                  engine, deepl_key, libre_url, libre_key, batch_items, batch_delay),
            daemon=True,
        )
        self.worker_thread.start()

    def _stop(self):
        self.stop_requested = True
        if self.translator:
            self.translator.stop()
        if self._gemma_stop_event is not None:
            self._gemma_stop_event.set()
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
        self.update_fonts_btn.configure(state="disabled")
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
            self._log("")
            self._log(
                "Готово. Теперь можно сгенерировать файлы перевода: "
                "кнопкой «Сгенерировать файлы перевода» выше (если указана "
                "папка SDK), либо вручную через меню разработчика в игре."
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

        renpy_lang = self.renpy_lang_var.get().strip()
        if not renpy_lang:
            messagebox.showerror(
                "Нужно имя языка",
                "Выберите имя языка в поле «Имя языка» — это название "
                "папки, которую создаст Ren'Py SDK внутри game/tl/.",
            )
            return

        self.settings["sdk_dir"] = sdk_dir
        project.save_settings(self.settings_path, self.settings)

        self.prepare_btn.configure(state="disabled")
        self.generate_tl_btn.configure(state="disabled")
        self.update_fonts_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.log_text.delete("1.0", "end")
        self.status_var.set("Генерирую файлы перевода через Ren'Py SDK...")
        threading.Thread(
            target=self._generate_tl_worker, args=(sdk_dir, renpy_lang), daemon=True,
        ).start()

    def _generate_tl_worker(self, sdk_dir, renpy_lang):
        try:
            project_dir = self.project_path.get()
            self._log(
                "Запускаю Ren'Py SDK для генерации файлов перевода "
                "(язык: {0})...".format(renpy_lang)
            )
            result = self._run_generate_translations_once(project_dir, sdk_dir, renpy_lang)
            if result.ok:
                self._log("")
                self._log("Готово. Языковая папка создана.")
                return

            # --- Попытка №1 провалилась — ищем в выводе SDK конкретные
            # места ошибок (файл + номер строки) и точечно чиним только
            # их, не трогая остальной файл (см. rpy_autofix.py про то,
            # почему НЕ сканируем файлы целиком). ------------------------
            locations_1 = rpy_autofix.parse_renpy_error_locations(result.output_lines)
            if not locations_1:
                self._log("")
                self._log(
                    "Не удалось разобрать сообщение SDK на конкретные места "
                    "(файл/строка) — почините вручную по журналу выше."
                )
                return

            self._log("")
            self._log(
                "Обнаружено проблемных мест: {0}. Пробую точечно "
                "исправить известные ошибки unrpyc и повторить "
                "генерацию...".format(len(locations_1))
            )
            self._apply_targeted_fixes(project_dir, locations_1)

            self._log("")
            self._log("Повторяю генерацию файлов перевода (попытка 2)...")
            result2 = self._run_generate_translations_once(project_dir, sdk_dir, renpy_lang)
            if result2.ok:
                self._log("")
                self._log("Готово. Языковая папка создана (после автоисправления).")
                return

            # --- Попытка №2 тоже провалилась. Если ошибка осталась
            # ровно на тех же местах (файл+строка), что и в попытке 1 —
            # значит, точечная правка её не решила (или строка не
            # подошла ни под одно правило) — такие строки удаляем
            # целиком, чтобы SDK мог продолжить работу дальше. Новые
            # места, которых не было в попытке 1, не трогаем — их могло
            # заслонять более раннее место ошибки в том же файле. -------
            locations_2 = rpy_autofix.parse_renpy_error_locations(result2.output_lines)
            keys_1 = {(loc["file"], loc["line"]) for loc in locations_1}
            keys_2 = {(loc["file"], loc["line"]) for loc in locations_2}
            still_broken = keys_1 & keys_2

            self._log("")
            if still_broken:
                self._log(
                    "На {0} месте(ах) ошибка сохранилась на той же строке "
                    "после попытки исправления — удаляю эти строки целиком "
                    "(проверьте эти места в игре вручную после перевода):"
                    .format(len(still_broken))
                )
                self._delete_lines(project_dir, still_broken)
            else:
                self._log(
                    "Прежние места исправлены, но обнаружились новые "
                    "ошибки в других местах — удалять нечего, перехожу к "
                    "финальной попытке как есть."
                )

            self._log("")
            self._log("Финальная попытка сгенерировать файлы перевода (попытка 3)...")
            result3 = self._run_generate_translations_once(project_dir, sdk_dir, renpy_lang)
            self._log("")
            if result3.ok:
                self._log("Готово. Языковая папка создана (после удаления неисправимых строк).")
            else:
                self._log(
                    "Не удалось сгенерировать файлы перевода даже после "
                    "автоисправлений — смотрите сообщения об ошибках выше, "
                    "оставшиеся места придётся поправить вручную."
                )
        except Exception as e:
            self._log("Неожиданная ошибка при запуске Ren'Py SDK: {0}".format(e))
        finally:
            self.log_queue.put("__GENERATE_TL_DONE__")

    def _run_generate_translations_once(self, project_dir, sdk_dir, renpy_lang):
        """Один запуск renpy_sdk.generate_translations(). Не бросает
        исключение наружу — возвращает объект с .ok/.output_lines, чтобы
        вызывающий код (_generate_tl_worker) мог организовать повторные
        попытки. Само сообщение об ошибке (если есть) всегда попадает в
        журнал здесь же, независимо от того, будет ли повтор."""
        try:
            renpy_sdk.generate_translations(project_dir, sdk_dir, renpy_lang, log=self._log)
            return _GenerateResult(True, [])
        except renpy_sdk.RenpySdkError as e:
            self._log("ОШИБКА: {0}".format(e))
            return _GenerateResult(False, e.output_lines)

    def _apply_targeted_fixes(self, project_dir, locations):
        """Пытается починить каждое место из locations (список dict с
        'file'/'line'/'message') по отдельности, читая и перезаписывая
        только тот .rpy файл, где оно находится. Каждый результат (успех
        или "не подошло ни одно правило") пишет в журнал."""
        for loc in locations:
            path = os.path.join(project_dir, *loc["file"].split("/"))
            try:
                with open(path, "r", encoding="utf-8", newline="") as f:
                    lines = f.readlines()
            except Exception as e:
                self._log("  {0}: ОШИБКА чтения ({1})".format(loc["file"], e))
                continue
            new_lines, desc = rpy_autofix.apply_fix_at_line(lines, loc["line"])
            if desc is None:
                self._log(
                    "  {0}, строка {1}: не подошло ни одно известное "
                    "правило автоисправления ({2})"
                    .format(loc["file"], loc["line"], loc["message"])
                )
                continue
            try:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.writelines(new_lines)
            except Exception as e:
                self._log("  {0}: ОШИБКА записи ({1})".format(loc["file"], e))
                continue
            self._log("  {0}, строка {1}: {2}".format(loc["file"], loc["line"], desc))

    def _delete_lines(self, project_dir, keys):
        """Удаляет строки, перечисленные в keys (множество пар
        (file_rel, line_no)), целиком из соответствующих файлов. Строки
        внутри одного файла удаляются от конца к началу, чтобы номера
        остальных удаляемых строк не съезжали по ходу удаления."""
        by_file = {}
        for file_rel, line_no in keys:
            by_file.setdefault(file_rel, []).append(line_no)

        for file_rel, line_nos in by_file.items():
            path = os.path.join(project_dir, *file_rel.split("/"))
            try:
                with open(path, "r", encoding="utf-8", newline="") as f:
                    lines = f.readlines()
            except Exception as e:
                self._log("  {0}: ОШИБКА чтения ({1})".format(file_rel, e))
                continue
            removed = []
            for line_no in sorted(set(line_nos), reverse=True):
                lines, text = rpy_autofix.delete_line(lines, line_no)
                if text is not None:
                    removed.append((line_no, text))
            try:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.writelines(lines)
            except Exception as e:
                self._log("  {0}: ОШИБКА записи ({1})".format(file_rel, e))
                continue
            for line_no, text in sorted(removed):
                self._log(
                    "  {0}, строка {1}: строка удалена целиком — было: {2!r}"
                    .format(file_rel, line_no, text)
                )

    # ------------------------------------------------------------------
    # Запуск локального сервера LibreTranslate
    # ------------------------------------------------------------------
    def _start_launch_libretranslate(self):
        self.launch_libre_btn.configure(state="disabled")
        threading.Thread(target=self._launch_libretranslate_worker, daemon=True).start()

    def _launch_libretranslate_worker(self):
        try:
            libretranslate_launcher.launch(log=self._log)
            self._log(
                "LibreTranslate запущен в отдельном окне консоли. Дождитесь "
                "в нём строки о готовности сервера (обычно "
                "'Running on http://127.0.0.1:5000'), прежде чем начинать "
                "перевод — при первом запуске сервер ещё и скачивает "
                "языковые модели, это может занять время."
            )
        except libretranslate_launcher.LibreTranslateLauncherError as e:
            self._log("ОШИБКА: {0}".format(e))
        except Exception as e:
            self._log("Неожиданная ошибка при запуске LibreTranslate: {0}".format(e))
        finally:
            self.log_queue.put(("__ENABLE_WIDGET__", "launch_libre_btn"))

    # ------------------------------------------------------------------
    # Запуск локального сервера Translate Gemma
    # ------------------------------------------------------------------
    def _start_launch_gemma(self):
        self.launch_gemma_btn.configure(state="disabled")
        threading.Thread(target=self._launch_gemma_worker, daemon=True).start()

    def _launch_gemma_worker(self):
        try:
            folder = self.gemma_llama_folder_var.get().strip()
            if not folder:
                raise gemma_launcher.GemmaLauncherError(
                    "Укажите папку llama.cpp (там, где лежит llama-server.exe)."
                )
            model_path = self.gemma_model_path_var.get().strip()
            ngl = self._int_or_default(
                self.gemma_ngl_var.get(), translate_gemma.DEFAULT_NGL, "Загрузка GPU"
            )
            gemma_np = self._int_or_default(
                self.gemma_np_var.get(), translate_gemma.DEFAULT_NP, "Число потоков"
            )
            ctx = self._int_or_default(
                self.gemma_ctx_var.get(), translate_gemma.DEFAULT_CTX, "Контекстное окно"
            )
            n_predict = self._int_or_default(
                self.gemma_ntokens_var.get(), translate_gemma.DEFAULT_N_PREDICT,
                "Токенов в ответе (max)",
            )
            base_url = self.gemma_url_var.get().strip() or translate_gemma.DEFAULT_URL
            gemma_launcher.launch(
                folder, model_path, ngl, gemma_np, ctx, n_predict, base_url, log=self._log,
            )
            self._log(
                "Translate Gemma запущена в отдельном окне консоли. Дождитесь "
                "в нём сообщения о готовности сервера, прежде чем начинать "
                "перевод — загрузка модели в память может занять время."
            )
        except (gemma_launcher.GemmaLauncherError, ValueError) as e:
            self._log("ОШИБКА: {0}".format(e))
        except Exception as e:
            self._log("Неожиданная ошибка при запуске Translate Gemma: {0}".format(e))
        finally:
            self.log_queue.put(("__ENABLE_WIDGET__", "launch_gemma_btn"))

    # ------------------------------------------------------------------
    # Обновить шрифты, включить язык
    # ------------------------------------------------------------------
    def _start_update_fonts(self):
        if not self.game_dir:
            messagebox.showerror("Ошибка", "Сначала выберите папку игры.")
            return
        if not os.path.isdir(self.fonts_bundle_dir):
            messagebox.showerror(
                "Папка game не найдена",
                "Рядом с программой должна лежать папка game (со "
                "шрифтами внутри game\\fonts) — та, что идёт в комплекте. "
                "Ожидаемый путь:\n{0}".format(self.fonts_bundle_dir),
            )
            return
        lang = self.renpy_lang_var.get().strip()
        if lang not in game_patcher.LANGUAGE_NAMES:
            messagebox.showerror(
                "Неизвестный язык",
                "Выберите язык в поле «Имя языка» — программа умеет "
                "подключать шрифты только для: {0}."
                .format(", ".join(game_patcher.LANGUAGE_NAMES)),
            )
            return

        if not messagebox.askyesno(
            "Изменить файлы игры?",
            "Сейчас будут скопированы файлы шрифтов в папку игры и "
            "изменены game/gui.rpy и game/screens.rpy (добавится выбор "
            "языка «{0}» в меню настроек). Если что-то уже было "
            "добавлено раньше — программа это не продублирует. "
            "Продолжить?".format(lang),
        ):
            return

        self.prepare_btn.configure(state="disabled")
        self.generate_tl_btn.configure(state="disabled")
        self.update_fonts_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.log_text.delete("1.0", "end")
        self.status_var.set("Обновляю шрифты и меню языка...")
        threading.Thread(
            target=self._update_fonts_worker, args=(lang,), daemon=True,
        ).start()

    def _update_fonts_worker(self, lang):
        try:
            result = game_patcher.apply_all(
                self.fonts_bundle_dir, self.game_dir, lang, log=self._log,
            )
            self._log("")
            if result.get("fonts") == "done" and "error" not in result.values():
                self._log(
                    "Готово. Не забудьте протестировать игру — если шрифт "
                    "не отображается, проверьте, что имя файла в "
                    "game/gui.rpy совпадает с реальным файлом в "
                    "game/fonts."
                )
            else:
                self._log(
                    "Завершено с замечаниями — см. сообщения выше."
                )
        except Exception as e:
            self._log("Неожиданная ошибка при обновлении шрифтов: {0}".format(e))
        finally:
            self.log_queue.put("__UPDATE_FONTS_DONE__")

    # ------------------------------------------------------------------
    # Фоновый поток перевода
    # ------------------------------------------------------------------
    def _worker(self, lang_dir, files, src_code, dest_code, overwrite, use_cache,
                engine, deepl_key, libre_url, libre_key, batch_items=None,
                batch_delay=None):
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
                delay=batch_delay, batch_items=batch_items,
            )
        elif engine == "libretranslate":
            translator = libretranslate_translate.LibreTranslator(
                base_url=libre_url, src=src_code, dest=dest_code,
                api_key=(libre_key or None), cache=cache,
                log=self._log, on_progress=on_progress,
                delay=batch_delay, batch_items=batch_items,
            )
        else:
            translator = gtranslate.CachedTranslator(
                src=src_code, dest=dest_code, cache=cache,
                log=self._log, on_progress=on_progress,
                delay=batch_delay, batch_items=batch_items,
            )
        self.translator = translator

        total_stats = {"translated": 0, "skipped": 0, "failed": 0, "nothing_to_translate": 0}

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
                file_stats = {"translated": 0, "skipped": 0, "failed": 0, "nothing_to_translate": 0}
                new_lines = core.process_lines(
                    lines, translator, overwrite=overwrite, stats=file_stats,
                    log=self._log,
                )
                if file_stats["translated"] > 0:
                    project.write_lines(path, new_lines)
                total_stats["translated"] += file_stats["translated"]
                total_stats["skipped"] += file_stats["skipped"]
                total_stats["failed"] += file_stats["failed"]
                total_stats["nothing_to_translate"] += file_stats["nothing_to_translate"]
                self._log(
                    "  переведено: {0}, пропущено (уже переведено): {1}{2}".format(
                        file_stats["translated"], file_stats["skipped"],
                        ", НЕ УДАЛОСЬ: {0}".format(file_stats["failed"])
                        if file_stats["failed"] else "",
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
            "не удалось перевести: {2}, без переводимого текста (только "
            "теги — пропущены): {3}, запросов к переводчику ({4}): {5} "
            "(из них пакетных: {6}, переведено ими строк: {7}), из кэша: "
            "{8}, срабатываний ограничения скорости: {9}, ошибок "
            "перевода: {10}.".format(
                total_stats["translated"], total_stats["skipped"],
                total_stats["failed"], total_stats["nothing_to_translate"],
                engine_titles[engine],
                translator.stats["requests"], translator.stats["batch_requests"],
                translator.stats["batched_lines"], translator.stats["cache_hits"],
                translator.stats["rate_limit_hits"], translator.stats["errors"],
            )
        )
        if total_stats["failed"] > 0:
            self._log(
                "{0} строк(и) остались непереведёнными (см. пометки "
                "«Не удалось перевести» выше) — обычно это значит, что "
                "переводчик вернул ответ с повреждённым тегом/подстановкой "
                "внутри, и программа сознательно не стала его использовать, "
                "чтобы не сломать игру. Такие строки не запоминаются как "
                "«готовые» и будут снова предложены к переводу при "
                "следующем запуске — просто запустите перевод ещё раз "
                "(при необходимости — с другим движком)."
                .format(total_stats["failed"])
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

    # ------------------------------------------------------------------
    # Фоновый поток перевода — Translate Gemma (контекстный, по файлам
    # параллельно, без общего кэша — см. обсуждение в handoff-документе)
    # ------------------------------------------------------------------
    def _worker_gemma(self, lang_dir, files, overwrite, gemma_url, gemma_np, n_predict):
        self._gemma_stop_event = threading.Event()

        # --- Словарь "код персонажа -> настоящее имя" собирается ОДИН
        # раз перед началом перевода — дальше только читается, поэтому
        # безопасно использовать из нескольких потоков одновременно. ----
        names = {}
        if self.game_dir:
            self._log(
                "Сканирую game/ на определения персонажей "
                "(define ... = Character(...))..."
            )
            try:
                names = character_names.scan_character_names(self.game_dir)
            except Exception as e:
                self._log("  ОШИБКА при сканировании персонажей: {0}".format(e))
            self._log("Найдено определений персонажей: {0}.".format(len(names)))
        else:
            self._log(
                "Папка game не определена — имена персонажей резолвиться "
                "не будут (в RULES попадёт код персонажа как есть)."
            )

        def resolve_speaker(who):
            return character_names.resolve_speaker_name(who, names)

        stats_lock = threading.Lock()
        total_stats = {"translated": 0, "skipped": 0, "failed": 0, "nothing_to_translate": 0}
        files_done = {"n": 0}
        total_files = len(files)

        def translate_one_file(path):
            rel = os.path.relpath(path, lang_dir)
            if self._gemma_stop_event.is_set():
                return
            self._log("Начинаю: {0}".format(rel))

            # Свой экземпляр движка на каждый файл/поток — translate_line()
            # не потокобезопасен по .stats, а общий стоп-флаг всё равно
            # передаётся всем экземплярам, так что "Остановить" действует
            # сразу на все потоки.
            translator = translate_gemma.GemmaTranslator(
                base_url=gemma_url, max_tokens=n_predict, log=self._log,
                stop_event=self._gemma_stop_event,
            )

            try:
                lines = project.read_lines(path)
            except Exception as e:
                self._log("  ОШИБКА чтения файла {0}: {1}".format(rel, e))
                return

            file_stats = {"translated": 0, "skipped": 0, "failed": 0, "nothing_to_translate": 0}
            try:
                new_lines = core.process_lines_contextual(
                    lines, translator.translate_line, resolve_speaker,
                    overwrite=overwrite, stats=file_stats, log=self._log,
                )
                if file_stats["translated"] > 0:
                    project.write_lines(path, new_lines)
            except Exception as e:
                self._log("  ОШИБКА при обработке файла {0}: {1}".format(rel, e))

            with stats_lock:
                for k in total_stats:
                    total_stats[k] += file_stats.get(k, 0)
                files_done["n"] += 1
                n = files_done["n"]
            self.log_queue.put(("__PHASE2_PROGRESS__", n, total_files, rel))
            self._log(
                "Готово: {0} — переведено: {1}, пропущено: {2}{3}".format(
                    rel, file_stats["translated"], file_stats["skipped"],
                    ", НЕ УДАЛОСЬ: {0}".format(file_stats["failed"])
                    if file_stats["failed"] else "",
                )
            )

        self._log(
            "Запускаю перевод {0} файлов, потоков: {1}...".format(total_files, gemma_np)
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, gemma_np)) as executor:
            list(executor.map(translate_one_file, files))

        self._log("")
        if self.stop_requested:
            self._log("Остановлено пользователем.")
        self._log(
            "Готово. Всего переведено строк: {0}, пропущено: {1}, не "
            "удалось перевести: {2}, без переводимого текста (только "
            "теги — пропущены): {3}.".format(
                total_stats["translated"], total_stats["skipped"],
                total_stats["failed"], total_stats["nothing_to_translate"],
            )
        )
        if total_stats["failed"] > 0:
            self._log(
                "{0} строк(и) остались непереведёнными — Translate Gemma "
                "не ответила или повредила защищённый маркер (тег/"
                "подстановку). Такие строки не запоминаются как «готовые» "
                "и будут снова найдены при следующем запуске.".format(
                    total_stats["failed"]
                )
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
