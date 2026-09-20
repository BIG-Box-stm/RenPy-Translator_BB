# -*- coding: utf-8 -*-
"""
Ядро логики Ren'Py Translator.
Разбор файлов перевода Ren'Py (.rpy в папке game/tl/<язык>/) и заполнение
непереведённых строк машинным переводом.

Формат файлов, которые генерирует сам Ren'Py (пункт меню разработчика
"Generate Translations"):

1) Блоки диалогов:
    # game/script.rpy:12
    translate russian start_7c722cef:

        # e "Hello, how are you?"
        e "Hello, how are you?"

   Вторая (не закомментированная) строка изначально совпадает с
   закомментированной — это и есть место, куда нужно вписать перевод.

2) Блоки строк (меню, текст экранов и т.п.):
    translate russian strings:

        old "Yes"
        new ""

   Перевод нужно вписать в new "".

Этот модуль не переводит текст сам — он только находит, что нужно
перевести, вызывает переданную функцию translate_fn(text) -> str для
самого текста (без кавычек) и аккуратно вставляет результат обратно,
не трогая теги Ren'Py вида {b}...{/b} и подстановки вида [name].
"""

import re

# ---------------------------------------------------------------------------
# Регулярные выражения для разбора строк .rpy
# ---------------------------------------------------------------------------

_QUOTED = r'"(?:\\.|[^"\\])*"'

# Токен для имени персонажа/атрибута спрайта: обычный идентификатор,
# опционально с префиксом @ (маркер) или +/- (добавить/убрать атрибут).
_TOKEN = r'(?:[@+\-]?[A-Za-z_][A-Za-z0-9_]*)'
# "who" — ноль или больше таких токенов через пробел, например:
#   e "..."                      -> who = "e"
#   asheera neutral "..."        -> who = "asheera neutral"
#   k happy -glasses "..."       -> who = "k happy -glasses"
#   "..."  (рассказчик)          -> who = ""
_WHO = r'(?:' + _TOKEN + r'(?:[ \t]+' + _TOKEN + r')*)?'
# Иногда имя говорящего — само по себе строка в кавычках (динамическое
# имя персонажа), например:
#   "Captain Michael Kateway" "Mom, you have over 600 people on board!"
# В этом случае "who" — это сама эта кавычечная строка целиком, а не
# набор токенов-идентификаторов.
_WHO_PART = r'(?:' + _QUOTED + r'|' + _WHO + r')'

RE_OLD = re.compile(r'^(?P<indent>[ \t]*)old\s+(?P<str>' + _QUOTED + r')\s*$')
RE_NEW = re.compile(r'^(?P<indent>[ \t]*)new\s+(?P<str>' + _QUOTED + r')\s*$')

RE_COMMENT_DLG = re.compile(
    r'^(?P<indent>[ \t]*)#[ \t]*(?P<who>' + _WHO_PART + r')[ \t]*'
    r'(?P<str>' + _QUOTED + r')\s*$'
)
RE_DLG = re.compile(
    r'^(?P<indent>[ \t]*)(?P<who>' + _WHO_PART + r')[ \t]*'
    r'(?P<str>' + _QUOTED + r')\s*$'
)

# Ren'Py текстовые теги {tag}, {tag=value}, {/tag} и подстановки [var]/[var!x],
# а также Python-стиль форматирования %(name)s, %s, %d, %.2f, %% — это
# используется, например, в "%(you)s was busy..." (имя игрока
# подставляется в реплику). Если эти куски потеряются при переводе,
# получившаяся строка может не просто выглядеть некрасиво, а сломать игру
# (Ren'Py пытается интерполировать "%" как формат-спецификатор и падает
# с ошибкой, если синтаксис повреждён).
_RE_FORMAT_SPEC = (
    r'%\(\w+\)[#0\-+ ]*\d*(?:\.\d+)?[a-zA-Z]'   # %(name)s, %(count)d, ...
    r'|%[#0\-+ ]*\d*(?:\.\d+)?[a-zA-Z]'          # %s, %d, %.2f, ...
    r'|%%'                                        # экранированный %% (буквальный процент)
)
_RE_PROTECT = re.compile(
    _RE_FORMAT_SPEC + r'|\{[^{}]*\}|\[[^\[\]]*\]|\\n|\\"|\\\\'
)

# Маркер-обёртка для защищённых кусков текста. Раньше использовались
# символы из приватной зоны Юникода (U+E000/U+E001) — но выяснилось, что
# некоторые переводчики (особенно нейросетевые, например LibreTranslate)
# просто выбрасывают незнакомые управляющие символы при токенизации,
# оставляя голые цифры индекса прямо в переводе ("0 1 2" вместо тегов).
# Обычный ASCII-маркер вида "@@0@@" выглядит как единое "слово" без
# пробелов, и переводчики почти всегда копируют такие "слова" как есть,
# не разбирая их на части и не выбрасывая символы.
_TOKEN_OPEN = "@@"
_TOKEN_CLOSE = "@@"
_RE_TOKEN = re.compile(re.escape(_TOKEN_OPEN) + r'(\d+)' + re.escape(_TOKEN_CLOSE))


def get_line_ending(line):
    if line.endswith('\r\n'):
        return '\r\n'
    if line.endswith('\n'):
        return '\n'
    return ''


def _strip_eol(line):
    return line[:-2] if line.endswith('\r\n') else (line[:-1] if line.endswith('\n') else line)


def protect(text):
    """Заменяет теги/подстановки/экранированные символы на служебные
    маркеры, чтобы переводчик их не трогал.

    Если маркер оказывается вплотную (без пробела) к любому другому
    символу — букве, точке, другому маркеру и т.п. — например
    {i}El Beano{/i}, \\"interesting\\", research.{p}So или {p}{i} —
    часть переводчиков (в первую очередь бесплатный Google Translate)
    воспринимает такое слипание как один нераздельный "токен" и либо не
    переводит соседний текст, либо вовсе обрывает перевод всего остатка
    фразы после такого места. Поэтому здесь маркер, прилегающий вплотную
    к чему угодно (кроме пробела), окружается одним пробелом — только с
    той стороны, где реально нет разделителя. Эти добавленные пробелы
    запоминаются (per-token) и аккуратно снимаются обратно в restore(),
    так что на итоговый текст это никак не влияет (за исключением
    очень редкого случая, когда маркер стоит вплотную перед знаком
    препинания — тогда после восстановления может остаться один лишний
    пробел перед ним; это несравнимо безопаснее, чем полностью
    непереведённая фраза)."""
    tokens = []

    def repl(m):
        start, end = m.start(), m.end()
        left_pad = start > 0 and not text[start - 1].isspace()
        right_pad = end < len(text) and not text[end].isspace()
        tokens.append({
            'text': m.group(0),
            'left_pad': left_pad,
            'right_pad': right_pad,
        })
        marker = f"{_TOKEN_OPEN}{len(tokens) - 1}{_TOKEN_CLOSE}"
        if left_pad:
            marker = " " + marker
        if right_pad:
            marker = marker + " "
        return marker

    return _RE_PROTECT.sub(repl, text), tokens


def restore(text, tokens):
    """Обратная операция к protect(): подставляет исходные теги/
    подстановки на место маркеров и снимает пробелы, добавленные
    protect() вокруг маркеров, прилегавших вплотную к буквам/цифрам
    (см. комментарий в protect())."""
    parts = []
    last_end = 0
    pending_right_strip = False

    for m in _RE_TOKEN.finditer(text):
        idx = int(m.group(1))
        info = tokens[idx] if idx < len(tokens) else None
        piece = text[last_end:m.start()]

        if pending_right_strip and piece.startswith(" "):
            piece = piece[1:]
        pending_right_strip = False

        if info is None:
            parts.append(piece)
            parts.append(m.group(0))
            last_end = m.end()
            continue

        if info['left_pad'] and piece.endswith(" "):
            piece = piece[:-1]
        parts.append(piece)
        parts.append(info['text'])
        pending_right_strip = info['right_pad']
        last_end = m.end()

    tail = text[last_end:]
    if pending_right_strip and tail.startswith(" "):
        tail = tail[1:]
    parts.append(tail)
    return "".join(parts)


def translate_quoted(quoted, translate_fn):
    """quoted — строка вида '"текст"'. Возвращает новую строку в кавычках
    с переведённым текстом, сохранив теги/подстановки/переносы строк."""
    inner = quoted[1:-1]
    if inner.strip() == '':
        return quoted
    protected, tokens = protect(inner)
    translated = translate_fn(protected)
    if translated is None:
        return quoted
    if tokens and not _all_tokens_present(translated, len(tokens)):
        # Переводчик потерял или повредил один из защищённых маркеров
        # (тег, подстановку [name] или %-форматирование). Использовать
        # такой перевод рискованно — Ren'Py может упасть с ошибкой на
        # сломанном "%" или подстановке. Оставляем оригинал как есть —
        # это будет обнаружено и переведено заново при следующем запуске
        # (та же логика, что и при сетевой ошибке перевода).
        return quoted
    # На всякий случай экранируем случайно попавшие в перевод кавычки
    # (сами экранированные последовательности сейчас скрыты под
    # маркерами и вернутся как есть только на этапе restore).
    translated = re.sub(r'(?<!\\)"', r'\\"', translated)
    final_inner = restore(translated, tokens)
    return '"' + final_inner + '"'


def _all_tokens_present(text, count):
    found = {int(m.group(1)) for m in _RE_TOKEN.finditer(text)}
    return all(i in found for i in range(count))


def find_translatable(lines):
    """Итератор мест для перевода в файле. Ничего не переводит и не
    меняет lines — только находит и описывает места. Общая логика для
    process_lines() и collect_needed_texts(), чтобы не дублировать и не
    рассинхронизировать правила определения "нужен перевод"."""
    n = len(lines)
    i = 0
    while i < n:
        line = _strip_eol(lines[i])

        # --- блок old / new -------------------------------------------
        m_old = RE_OLD.match(line)
        if m_old:
            j = i + 1
            while j < n and _strip_eol(lines[j]).strip() == '':
                j += 1
            if j < n:
                m_new = RE_NEW.match(_strip_eol(lines[j]))
                if m_new:
                    yield {
                        'type': 'old_new',
                        'orig_quoted': m_old.group('str'),
                        'new_line_idx': j,
                        'new_indent': m_new.group('indent'),
                        'current_inner': m_new.group('str')[1:-1],
                        # Если "new" в точности совпадает с "old" — это
                        # либо свежесгенерированная заглушка-дубликат,
                        # либо след неудачного перевода (см. translate_quoted:
                        # при ошибке перевода в new подставляется оригинал
                        # без изменений). В обоих случаях считаем это
                        # "не переведено", чтобы другой движок мог
                        # перевести эту строку при следующем запуске.
                        'is_dup_of_original': m_new.group('str') == m_old.group('str'),
                    }
                    i = j + 1
                    continue

        # --- блок диалога (комментарий + строка для перевода) ----------
        m_c = RE_COMMENT_DLG.match(line)
        if m_c and i + 1 < n:
            m_d = RE_DLG.match(_strip_eol(lines[i + 1]))
            if (m_d
                    and m_d.group('indent') == m_c.group('indent')
                    and m_d.group('who') == m_c.group('who')):
                yield {
                    'type': 'dialogue',
                    'orig_quoted': m_c.group('str'),
                    'code_line_idx': i + 1,
                    'code_indent': m_d.group('indent'),
                    'who': m_d.group('who'),
                    'current_inner': m_d.group('str')[1:-1],
                    'is_dup_of_original': m_d.group('str') == m_c.group('str'),
                }
                i += 2
                continue

        i += 1


def _is_untranslated(entry, overwrite):
    if overwrite:
        return True
    return entry['current_inner'] == '' or entry['is_dup_of_original']


def extract_protected_text(quoted):
    """quoted — строка вида '"текст"'. Возвращает текст с защищёнными
    тегами/подстановками (как перед отправкой в переводчик), либо None,
    если переводить нечего (пустая строка)."""
    inner = quoted[1:-1]
    if inner.strip() == '':
        return None
    protected, _ = protect(inner)
    return protected


def collect_needed_texts(lines, overwrite=False):
    """Возвращает множество уникальных 'защищённых' текстов, которые
    нужно перевести в этом файле. Используется, чтобы заранее перевести
    всё одним пакетным проходом, прежде чем расставлять переводы по
    местам через process_lines()."""
    needed = set()
    for entry in find_translatable(lines):
        if _is_untranslated(entry, overwrite):
            protected = extract_protected_text(entry['orig_quoted'])
            if protected:
                needed.add(protected)
    return needed


def process_lines(lines, translate_fn, overwrite=False, stats=None, log=None):
    """lines — список строк файла (с переносами строк на конце, как из
    readlines()/splitlines(keepends=True)). Возвращает новый список строк.
    translate_fn(text) -> str, где text — защищённый текст без кавычек.
    Если translate_fn — это CachedTranslator с заранее прогретым кэшем
    (см. collect_needed_texts() + warm_batch()), эта функция работает
    практически мгновенно, без сетевых запросов.
    stats — dict со счётчиками translated/skipped (обновляется).
    log(msg) — необязательная функция логирования.
    """
    if stats is None:
        stats = {'translated': 0, 'skipped': 0}
    out = list(lines)

    for entry in find_translatable(out):
        if _is_untranslated(entry, overwrite):
            translated = translate_quoted(entry['orig_quoted'], translate_fn)
            if entry['type'] == 'old_new':
                eol = get_line_ending(out[entry['new_line_idx']])
                out[entry['new_line_idx']] = f"{entry['new_indent']}new {translated}{eol}"
            else:
                who_prefix = f"{entry['who']} " if entry['who'] else ""
                eol = get_line_ending(out[entry['code_line_idx']])
                out[entry['code_line_idx']] = f"{entry['code_indent']}{who_prefix}{translated}{eol}"
            stats['translated'] += 1
        else:
            stats['skipped'] += 1

    return out
