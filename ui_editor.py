# -*- coding: utf-8 -*-
"""Окно «Редактор переводов»: список строк, которые не удалось перевести
(или любых других), с правкой перевода прямо в таблице.

Столбцы: «Файл» (путь начиная с tl\\), «Оригинал строки», «Перевод»
(двойной щелчок — править). После правки нужно нажать «Применить»: перевод
записывается в файл игры, строка получает защиту от перезаписи при
следующем автопереводе и пропадает из списка «Не переведённые».

«Переведено корректно» (кнопка или правый щелчок) подтверждает, что готовый
перевод верен (например, ложное срабатывание в «Подозрительных»): текст не
меняется, строка защищается как правленная вручную и попадает в «Правленные
вручную». «Снять отметку» снимает любую из защит.

Фильтр «Показать»: «Не переведённые», «Все строки», «Правленные вручную»,
«Перевод не требуется», «Подозрительные» (готовые переводы, в которых
программа заметила возможную проблему — причина в последнем столбце),
«Имена» (файл имён персонажей) и «Система» (файлы common.rpy, screens.rpy,
gui.rpy, options.rpy: меню, Save/Load/Quit и т.п.). Столбец «Говорящий» —
кто произносит реплику. «Отмена последней правки» откатывает ещё не
применённые изменения по одному шагу.

Строки, которым перевод не нужен (например, просто "60" или
"{color=#f00}[item.name]{/color}"), можно выделить (несколько сразу —
Ctrl/Shift) и нажать «Перевод не требуется» (или правый щелчок): текст
остаётся как в оригинале, но строка перестаёт числиться непереведённой и
защищается от автоперевода. Такие строки видны в фильтре «Перевод не
требуется»; отметку можно снять кнопкой «Снять отметку». Как и правки,
отметки вступают в силу после «Применить».
"""

from i18n import mark, tr
import tkinter as tk
from tkinter import messagebox, ttk

import core
import editor_store

MAX_ROWS = 3000
# Названия фильтров и «Все файлы» — ключи перевода; на экране они показываются
# через tr() и сравниваются через self.F / self.ALL (см. EditorWindow.__init__).
FILTER_KEYS = [mark("Не переведённые"), mark("Все строки"), mark("Правленные вручную"),
               mark("Перевод не требуется"), mark("Подозрительные"), mark("Имена"),
               mark("Система")]
ALL_FILES = mark("Все файлы")


def _kof(row):
    return (row["rel"], row["key"])


def _group(pairs):
    grouped = {}
    for rel, key in pairs:
        grouped.setdefault(rel, set()).add(key)
    return grouped


class EditorWindow(tk.Toplevel):
    def __init__(self, master, lang_dir, lang_folder, log=None, dest_code=None):
        super().__init__(master)
        self.title(tr("Редактор переводов"))
        self.geometry("1300x650")
        self.lang_dir = lang_dir
        self.lang_folder = lang_folder
        self.dest_code = dest_code     # язык перевода (для проверки «Подозрительные»)
        self.history = []              # снимки (edits, skips, unskips) для отмены
        self.log = log or (lambda msg: None)
        self.edits = {}        # (rel, key) -> новый текст (внутри кавычек)
        self.skips = set()     # (rel, key) — отметить «перевод не требуется» (ещё не применено)
        self.unskips = set()   # (rel, key) — снять отметку (ещё не применено)
        self.confirms = set()    # (rel, key) — «Переведено корректно» (ещё не применено)
        self.unconfirms = set()  # (rel, key) — снять защиту «вручную/корректно» (ещё не применено)
        self.rows = []         # результат editor_store.scan()
        self.iid_row = {}      # iid -> строка из self.rows
        self.info_base = ""

        self.F = {k: tr(k) for k in FILTER_KEYS}   # ключ -> название на языке интерфейса
        self.ALL = tr(ALL_FILES)
        self.filter_var = tk.StringVar(value=self.F[FILTER_KEYS[0]])
        self.file_var = tk.StringVar(value=self.ALL)
        self.search = tk.StringVar()

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text=tr("Показать:")).pack(side="left")
        ttk.Combobox(top, textvariable=self.filter_var, values=[self.F[k] for k in FILTER_KEYS],
                     state="readonly", width=22).pack(side="left", padx=6)
        ttk.Label(top, text=tr("Файл:")).pack(side="left", padx=(12, 0))
        self.file_combo = ttk.Combobox(top, textvariable=self.file_var, values=[self.ALL],
                                       state="readonly", width=40)
        self.file_combo.pack(side="left", padx=6)
        ttk.Label(top, text=tr("Поиск:")).pack(side="left", padx=(12, 0))
        ttk.Entry(top, textvariable=self.search, width=24).pack(side="left", padx=6)
        for v in (self.filter_var, self.file_var, self.search):
            v.trace_add("write", lambda *a: self.refresh())
        ttk.Button(top, text=tr("Применить"), command=self.apply).pack(side="right")
        ttk.Button(top, text=tr("Обновить список"), command=self.reload).pack(side="right", padx=(0, 8))

        bar2 = ttk.Frame(self)
        bar2.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Button(bar2, text=tr("Перевод не требуется"), command=self.mark_skip_selected).pack(side="left")
        ttk.Button(bar2, text=tr("Переведено корректно"), command=self.mark_confirm_selected).pack(side="left", padx=(8, 0))
        ttk.Button(bar2, text=tr("Снять отметку"), command=self.unmark_selected).pack(side="left", padx=(8, 0))
        ttk.Button(bar2, text=tr("Отмена последней правки"), command=self.undo_last).pack(side="left", padx=(8, 0))
        ttk.Label(
            bar2,
            text=tr("Выделите строки (Ctrl/Shift — несколько). Правый щелчок — то же меню."),
        ).pack(side="left", padx=(12, 0))

        cols = ("file", "who", "src", "tr", "note")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="extended")
        for c, title, w, stretch in (
            ("file", tr("Файл"), 200, False),
            ("who", tr("Говорящий"), 100, False),
            ("src", tr("Оригинал строки"), 360, True),
            ("tr", tr("Перевод (двойной щелчок — править)"), 360, True),
            ("note", tr("Замечание"), 200, False),
        ):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor="w", stretch=stretch)
        self.tree.tag_configure("edited", background="#e6f4ea")
        self.tree.tag_configure("skipped", background="#e8eefc")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        sb.pack(side="left", fill="y", pady=6, padx=(0, 8))
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label=tr("Переведено корректно"), command=self.mark_confirm_selected)
        self.menu.add_command(label=tr("Перевод не требуется"), command=self.mark_skip_selected)
        self.menu.add_command(label=tr("Снять отметку"), command=self.unmark_selected)
        self.info = ttk.Label(self, text="")
        self.info.pack(side="bottom", anchor="w", padx=8, pady=(0, 6))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.reload()

    # ---- данные ---------------------------------------------------------
    def reload(self):
        self.rows = editor_store.scan(self.lang_dir, dest_code=self.dest_code)
        files = sorted({r["rel"] for r in self.rows})
        self.file_combo.configure(values=[self.ALL] + [
            editor_store.display_path(self.lang_folder, f) for f in files])
        self.refresh()

    def pending_count(self):
        return (len(self.edits) + len(self.skips) + len(self.unskips)
                + len(self.confirms) + len(self.unconfirms))

    def eff_manual(self, row):
        """Строка сейчас защищена как «переведена вручную / корректно» (с
        учётом ещё не применённых изменений)."""
        k = _kof(row)
        if k in self.edits or k in self.confirms:
            return True
        return row["manual"] and k not in self.unconfirms and k not in self.skips

    def eff_skip(self, row):
        """Строка сейчас числится «перевод не требуется» (с учётом ещё не
        применённых отметок)."""
        k = _kof(row)
        return k in self.skips or (row["skip"] and k not in self.unskips)

    def shown_translation(self, row):
        k = _kof(row)
        if k in self.edits:
            return self.edits[k]
        if k in self.skips:
            return row["orig"]
        return row["cur"]

    def match(self, row, needle, only_file):
        mode = self.filter_var.get()
        k = _kof(row)
        edited = k in self.edits
        skip = self.eff_skip(row)
        manual = self.eff_manual(row)
        if mode == self.F["Не переведённые"] and not ((row["raw_untranslated"] and not skip and not manual)
                                               or edited):
            return False
        if mode == self.F["Правленные вручную"] and not manual:
            return False
        if mode == self.F["Перевод не требуется"] and not skip:
            return False
        if mode == self.F["Подозрительные"] and not (row["suspicious"] and not edited and not skip
                                              and not manual):
            return False
        if mode == self.F["Имена"] and not editor_store.is_names_rel(row["rel"]):
            return False
        if mode == self.F["Система"] and not editor_store.is_system_rel(row["rel"]):
            return False
        if only_file != self.ALL and editor_store.display_path(self.lang_folder, row["rel"]) != only_file:
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
        self.info_base = tr("Строк: {0}{1}").format(
            total,
            tr(" (показаны первые {0} — уточните фильтр или поиск)").format(MAX_ROWS) if total > shown else "",
        )
        self.update_info()

    def refresh_keep_scroll(self):
        pos = self.tree.yview()[0]
        self.refresh()
        self.tree.yview_moveto(pos)

    def values_of(self, row):
        return (
            editor_store.display_path(self.lang_folder, row["rel"]),
            row["who"],
            row["orig"],
            self.shown_translation(row),
            "" if (_kof(row) in self.edits or _kof(row) in self.skips
                   or _kof(row) in self.confirms) else row["note"],
        )

    def tags_of(self, row):
        if _kof(row) in self.edits or _kof(row) in self.confirms:
            return ("edited",)
        if self.eff_skip(row):
            return ("skipped",)
        return ()

    # ---- правка ---------------------------------------------------------
    def on_double(self, event):
        if self.tree.identify_column(event.x) != "#4":
            return
        iid = self.tree.identify_row(event.y)
        if iid not in self.iid_row:
            return
        row = self.iid_row[iid]
        x, y, w, h = self.tree.bbox(iid, "#4")
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
                messagebox.showerror(tr("Редактор переводов"), error)
                return
            problems = core.tag_problems(row["orig"], inner)
            if problems and not messagebox.askyesno(
                tr("Редактор переводов"),
                tr("Теги и подстановки в переводе не совпадают с оригиналом:\n\n- {0}\n\n"
                "Игра может отобразить строку неправильно или выдать ошибку. "
                "Всё равно сохранить?").format("\n- ".join(problems)),
            ):
                return
            k = _kof(row)
            self.push_history()
            self.edits[k] = inner
            self.confirms.discard(k)
            # Правка отменяет отметку «перевод не требуется» (и ещё не
            # применённую, и уже сохранённую).
            self.skips.discard(k)
            if row["skip"]:
                self.unskips.add(k)
            self.tree.item(iid, values=self.values_of(row), tags=self.tags_of(row))
            self.update_info()

        ent.bind("<Return>", commit)
        ent.bind("<Escape>", lambda _e: ent.destroy())
        ent.bind("<FocusOut>", lambda _e: ent.destroy())

    # ---- «Перевод не требуется» ---------------------------------------------
    def on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if self.tree.selection():
            self.menu.tk_popup(event.x_root, event.y_root)

    def selected_rows(self):
        return [self.iid_row[i] for i in self.tree.selection() if i in self.iid_row]

    def mark_skip_selected(self):
        rows = self.selected_rows()
        if not rows:
            messagebox.showinfo(tr("Редактор переводов"), tr("Сначала выделите одну или несколько строк."))
            return
        replaced = [
            r for r in rows
            if _kof(r) in self.edits
            or r["cur"].strip() not in ("", r["orig"].strip())
        ]
        if replaced and not messagebox.askyesno(
            tr("Редактор переводов"),
            tr("У {0} из выбранных строк уже есть перевод (или несохранённая правка). "
            "Если отметить их как «Перевод не требуется», вместо перевода "
            "в файл будет записан оригинал. Продолжить?").format(len(replaced)),
        ):
            return
        self.push_history()
        for row in rows:
            k = _kof(row)
            self.edits.pop(k, None)
            self.confirms.discard(k)
            self.unskips.discard(k)
            if not row["skip"]:
                self.skips.add(k)
        self.refresh_keep_scroll()

    def mark_confirm_selected(self):
        """«Переведено корректно»: текст не меняется, строка защищается как
        переведённая вручную (и уходит из «Подозрительных»/«Не переведённых»)."""
        rows = self.selected_rows()
        if not rows:
            messagebox.showinfo(tr("Редактор переводов"), tr("Сначала выделите одну или несколько строк."))
            return
        self.push_history()
        for row in rows:
            k = _kof(row)
            self.skips.discard(k)
            if row["skip"]:
                self.unskips.add(k)
            self.unconfirms.discard(k)
            if not row["manual"]:
                self.confirms.add(k)
        self.refresh_keep_scroll()

    def unmark_selected(self):
        rows = self.selected_rows()
        if not rows:
            messagebox.showinfo(tr("Редактор переводов"), tr("Сначала выделите одну или несколько строк."))
            return
        self.push_history()
        for row in rows:
            k = _kof(row)
            self.skips.discard(k)
            if row["skip"]:
                self.unskips.add(k)
            self.confirms.discard(k)
            if row["manual"]:
                self.unconfirms.add(k)
        self.refresh_keep_scroll()

    # ---- отмена последней правки ------------------------------------------
    def push_history(self):
        """Запоминает состояние ещё не применённых изменений — перед
        каждым новым действием (правка, отметка, снятие отметки)."""
        self.history.append((dict(self.edits), set(self.skips), set(self.unskips),
                             set(self.confirms), set(self.unconfirms)))

    def undo_last(self):
        if not self.history:
            messagebox.showinfo(
                tr("Редактор переводов"),
                tr("Нечего отменять: отменяются только ещё не применённые правки."))
            return
        (self.edits, self.skips, self.unskips,
         self.confirms, self.unconfirms) = self.history.pop()
        self.refresh_keep_scroll()

    def update_info(self):
        text = self.info_base
        if self.pending_count():
            text += tr("   Изменено, но не применено: {0}").format(self.pending_count())
        self.info.configure(text=text)

    # ---- применение -------------------------------------------------------
    def apply(self):
        if not self.pending_count():
            messagebox.showinfo(tr("Редактор переводов"), tr("Нет изменений для применения."))
            return True
        by_file = {}
        for (rel, key), inner in self.edits.items():
            by_file.setdefault(rel, {})[key] = inner
        try:
            n_edit = editor_store.apply_edits(self.lang_dir, by_file, log=self.log) if self.edits else 0
            n_skip = editor_store.mark_skip(self.lang_dir, _group(self.skips), log=self.log) if self.skips else 0
            n_unskip = editor_store.unmark_skip(self.lang_dir, _group(self.unskips)) if self.unskips else 0
            n_conf = editor_store.mark_confirmed(self.lang_dir, _group(self.confirms)) if self.confirms else 0
            n_unconf = editor_store.unmark_manual(self.lang_dir, _group(self.unconfirms)) if self.unconfirms else 0
        except Exception as e:
            messagebox.showerror(tr("Редактор переводов"), tr("Не удалось применить изменения: {0}").format(e))
            return False
        self.log(
            tr("Редактор переводов: переведено вручную — {0}, отмечено «Перевод не "
            "требуется» — {1}, снято отметок — {2}, подтверждено «Переведено "
            "корректно» — {3}, снято защит — {4}. Защищённые строки не "
            "перезаписываются при следующем автопереводе.").format(
                n_edit, n_skip, n_unskip, n_conf, n_unconf))
        self.edits = {}
        self.skips = set()
        self.unskips = set()
        self.confirms = set()
        self.unconfirms = set()
        self.history = []     # применённое откатить кнопкой нельзя
        self.reload()
        parts = []
        if n_edit:
            parts.append(tr("записано переводов: {0}").format(n_edit))
        if n_skip:
            parts.append(tr("отмечено «Перевод не требуется»: {0}").format(n_skip))
        if n_unskip:
            parts.append(tr("снято отметок: {0}").format(n_unskip))
        if n_conf:
            parts.append(tr("подтверждено как корректное: {0}").format(n_conf))
        if n_unconf:
            parts.append(tr("снято защит: {0}").format(n_unconf))
        messagebox.showinfo(
            tr("Редактор переводов"),
            tr("Готово: {0}.\nСтроки с ручным переводом и с отметкой «Перевод не "
            "требуется» защищены от перезаписи при следующем автопереводе.").format(
                "; ".join(parts) if parts else tr("изменений не потребовалось")))
        return True

    def on_close(self):
        if self.pending_count():
            answer = messagebox.askyesnocancel(
                tr("Редактор переводов"), tr("Есть изменения, которые не применены. Применить их перед закрытием?"))
            if answer is None:
                return
            if answer and not self.apply():
                return
        self.destroy()
