# -*- coding: utf-8 -*-
"""Окно «Редактор переводов»: список строк, которые не удалось перевести
(или любых других), с правкой перевода прямо в таблице.

Столбцы: «Файл» (путь начиная с tl\\), «Оригинал строки», «Перевод»
(двойной щелчок — править). После правки нужно нажать «Применить»: перевод
записывается в файл игры, строка получает защиту от перезаписи при
следующем автопереводе и пропадает из списка «Не переведённые».
"""

import tkinter as tk
from tkinter import messagebox, ttk

import core
import editor_store

MAX_ROWS = 3000
FILTERS = ["Не переведённые", "Все строки", "Правленные вручную"]
ALL_FILES = "Все файлы"


class EditorWindow(tk.Toplevel):
    def __init__(self, master, lang_dir, lang_folder, log=None):
        super().__init__(master)
        self.title("Редактор переводов")
        self.geometry("1150x650")
        self.lang_dir = lang_dir
        self.lang_folder = lang_folder
        self.log = log or (lambda msg: None)
        self.edits = {}        # (rel, key) -> новый текст (внутри кавычек)
        self.rows = []         # результат editor_store.scan()
        self.iid_row = {}      # iid -> строка из self.rows

        self.filter_var = tk.StringVar(value=FILTERS[0])
        self.file_var = tk.StringVar(value=ALL_FILES)
        self.search = tk.StringVar()

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="Показать:").pack(side="left")
        ttk.Combobox(top, textvariable=self.filter_var, values=FILTERS,
                     state="readonly", width=20).pack(side="left", padx=6)
        ttk.Label(top, text="Файл:").pack(side="left", padx=(12, 0))
        self.file_combo = ttk.Combobox(top, textvariable=self.file_var, values=[ALL_FILES],
                                       state="readonly", width=40)
        self.file_combo.pack(side="left", padx=6)
        ttk.Label(top, text="Поиск:").pack(side="left", padx=(12, 0))
        ttk.Entry(top, textvariable=self.search, width=24).pack(side="left", padx=6)
        for v in (self.filter_var, self.file_var, self.search):
            v.trace_add("write", lambda *a: self.refresh())
        ttk.Button(top, text="Применить", command=self.apply).pack(side="right")
        ttk.Button(top, text="Обновить список", command=self.reload).pack(side="right", padx=(0, 8))

        cols = ("file", "src", "tr")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, title, w, stretch in (
            ("file", "Файл", 260, False),
            ("src", "Оригинал строки", 420, True),
            ("tr", "Перевод (двойной щелчок — править)", 420, True),
        ):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor="w", stretch=stretch)
        self.tree.tag_configure("edited", background="#e6f4ea")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        sb.pack(side="left", fill="y", pady=6, padx=(0, 8))
        self.tree.bind("<Double-1>", self.on_double)
        self.info = ttk.Label(self, text="")
        self.info.pack(side="bottom", anchor="w", padx=8, pady=(0, 6))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.reload()

    # ---- данные ---------------------------------------------------------
    def reload(self):
        self.rows = editor_store.scan(self.lang_dir)
        files = sorted({r["rel"] for r in self.rows})
        self.file_combo.configure(values=[ALL_FILES] + [
            editor_store.display_path(self.lang_folder, f) for f in files])
        self.refresh()

    def shown_translation(self, row):
        return self.edits.get((row["rel"], row["key"]), row["cur"])

    def match(self, row, needle, only_file):
        mode = self.filter_var.get()
        edited = (row["rel"], row["key"]) in self.edits
        if mode == "Не переведённые" and not (row["untranslated"] or edited):
            return False
        if mode == "Правленные вручную" and not (row["manual"] or edited):
            return False
        if only_file != ALL_FILES and editor_store.display_path(self.lang_folder, row["rel"]) != only_file:
            return False
        if needle:
            hay = (row["rel"] + " " + row["orig"] + " " + self.shown_translation(row)).lower()
            if needle not in hay:
                return False
        return True

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.iid_row = {}
        needle = self.search.get().strip().lower()
        only_file = self.file_var.get()
        shown = total = 0
        for row in self.rows:
            if not self.match(row, needle, only_file):
                continue
            total += 1
            if shown >= MAX_ROWS:
                continue
            iid = self.tree.insert("", "end", values=self.values_of(row), tags=self.tags_of(row))
            self.iid_row[iid] = row
            shown += 1
        self.info.configure(text="Строк: {0}{1}{2}".format(
            total,
            " (показаны первые {0} — уточните фильтр или поиск)".format(MAX_ROWS) if total > shown else "",
            "   Изменено, но не применено: {0}".format(len(self.edits)) if self.edits else "",
        ))

    def values_of(self, row):
        return (
            editor_store.display_path(self.lang_folder, row["rel"]),
            row["orig"],
            self.shown_translation(row),
        )

    def tags_of(self, row):
        return ("edited",) if (row["rel"], row["key"]) in self.edits else ()

    # ---- правка ---------------------------------------------------------
    def on_double(self, event):
        if self.tree.identify_column(event.x) != "#3":
            return
        iid = self.tree.identify_row(event.y)
        if iid not in self.iid_row:
            return
        row = self.iid_row[iid]
        x, y, w, h = self.tree.bbox(iid, "#3")
        var = tk.StringVar(value=self.shown_translation(row))
        ent = ttk.Entry(self.tree, textvariable=var)
        ent.place(x=x, y=y, width=w, height=h)
        ent.focus_set()
        ent.icursor("end")

        def commit(_e=None):
            value = var.get()
            ent.destroy()
            if value.strip() == self.shown_translation(row).strip():
                return
            inner, error = editor_store.normalize(value)
            if error:
                messagebox.showerror("Редактор переводов", error)
                return
            problems = core.tag_problems(row["orig"], inner)
            if problems and not messagebox.askyesno(
                "Редактор переводов",
                "Теги и подстановки в переводе не совпадают с оригиналом:\n\n- {0}\n\n"
                "Игра может отобразить строку неправильно или выдать ошибку. "
                "Всё равно сохранить?".format("\n- ".join(problems)),
            ):
                return
            self.edits[(row["rel"], row["key"])] = inner
            self.tree.item(iid, values=self.values_of(row), tags=self.tags_of(row))
            self.update_info()

        ent.bind("<Return>", commit)
        ent.bind("<Escape>", lambda _e: ent.destroy())
        ent.bind("<FocusOut>", lambda _e: ent.destroy())

    def update_info(self):
        text = self.info.cget("text").split("   Изменено")[0]
        if self.edits:
            text += "   Изменено, но не применено: {0}".format(len(self.edits))
        self.info.configure(text=text)

    # ---- применение -------------------------------------------------------
    def apply(self):
        if not self.edits:
            messagebox.showinfo("Редактор переводов", "Нет изменений для применения.")
            return True
        by_file = {}
        for (rel, key), inner in self.edits.items():
            by_file.setdefault(rel, {})[key] = inner
        try:
            n = editor_store.apply_edits(self.lang_dir, by_file, log=self.log)
        except Exception as e:
            messagebox.showerror("Редактор переводов", "Не удалось применить правки: {0}".format(e))
            return False
        self.log("Редактор переводов: применено строк — {0}. Они защищены от "
                 "перезаписи при следующем переводе.".format(n))
        self.edits = {}
        self.reload()
        messagebox.showinfo("Редактор переводов",
                            "Применено строк: {0}.\nОни записаны в файлы перевода и защищены "
                            "от перезаписи при следующем автопереводе.".format(n))
        return True

    def on_close(self):
        if self.edits:
            answer = messagebox.askyesnocancel(
                "Редактор переводов", "Есть изменения, которые не применены. Применить их перед закрытием?")
            if answer is None:
                return
            if answer and not self.apply():
                return
        self.destroy()
