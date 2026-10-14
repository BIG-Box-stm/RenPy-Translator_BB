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

# Маркер-обёртка для защищённых кусков текста.
#
# История этого места (это уже третья итерация — сохраняю как memo):
# 1) Символы из приватной зоны Юникода (U+E000/U+E001) — некоторые
#    переводчики (особенно нейросетевые) выбрасывали незнакомые
#    управляющие символы при токенизации, оставляя голые цифры индекса
#    прямо в переводе ("0 1 2" вместо тегов).
# 2) ASCII-маркер "@@0@@" — работал с Google Translate (выглядит как
#    единое "слово" без пробелов), но на реальном LibreTranslate
#    (self-hosted нейросеть, например Argos Translate) вызвал МАССОВУЮ
#    порчу — судя по всему, нейросетевые модели, обученные на обычных
#    предложениях, воспринимают изолированный символьный "мусор" вроде
#    "@@0@@" как то, что не нужно копировать, и либо выбрасывают его,
#    либо портят при генерации.
# 3) Вариант по умолчанию сейчас — маркер только из букв и цифр (без "@"
#    и других символов), похожий на случайное "слово"/идентификатор.
#    Такие незнакомые "слова" (in-vocabulary-unknown) переводческие
#    модели почти всегда копируют как есть — это стандартное поведение
#    при переводе имён собственных, названий и т.п.
# 4) Так как единого маркера, идеально подходящего всем движкам, нет,
#    формат теперь можно менять из окна программы (поле "Разделитель")
#    без правки кода — см. parse_marker_template() и set_marker() ниже.
#    Формат задаётся шаблоном вида "Zqtx{N}xtqZ", где {N} — номер
#    защищённого куска.
_TOKEN_OPEN = "Zqtx"
_TOKEN_CLOSE = "xtqZ"
_RE_TOKEN = re.compile(re.escape(_TOKEN_OPEN) + r'(\d+)' + re.escape(_TOKEN_CLOSE))

DEFAULT_MARKER_TEMPLATE = "Zqtx{N}xtqZ"

# Режим "Без разделителя": теги/подстановки НЕ заменяются маркерами, в
# переводчик уходит исходная строка как есть ({b}, [name], %s и т.д.).
# Целостность проверяется не по маркерам, а прямым сравнением списка
# тегов/подстановок в оригинале и переводе (см. _structure_ok()).
NO_MARKER_LABEL = "Без разделителя"
_NO_MARKERS = False

# Куски, которые в режиме без разделителя НЕ сравниваются: экранированные
# кавычки/слэши (translate_quoted() сам экранирует "голые" кавычки) и
# "% x" с пробелом после процента (это обычный текст вроде "100% sure",
# а не формат-спецификатор).
_IGNORED_PIECES = ('\\"', '\\\\')
_RE_BRACE_TAG = re.compile(r'\{[^{}]*\}')


def set_no_markers():
    """Включает режим "без разделителя" для ВСЕХ последующих вызовов
    (вызывать до старта рабочего потока, как и set_marker())."""
    global _NO_MARKERS
    _NO_MARKERS = True


def is_no_markers():
    return _NO_MARKERS


def parse_marker_template(template):
    """Разбирает шаблон маркера вида 'Zqtx{N}xtqZ' на пару (начало, конец).
    {N} — место для номера защищённого куска. Бросает ValueError с
    понятным русским текстом, если шаблон непригоден: без {N}, с двумя
    {N}, с пробелами (переводчик разорвал бы маркер по пробелу) или без
    текста с одной из сторон (тогда соседние цифры из самой реплики
    нельзя было бы отличить от номера маркера)."""
    template = (template or "").strip()
    if template.count("{N}") != 1:
        raise ValueError(
            "Разделитель должен содержать ровно один {N} — это место, "
            "куда программа подставит номер защищённого куска. Например: "
            "Zqtx{N}xtqZ"
        )
    if any(ch.isspace() for ch in template):
        raise ValueError("В разделителе не должно быть пробелов.")
    open_part, close_part = template.split("{N}")
    if not open_part or not close_part:
        raise ValueError(
            "У разделителя должен быть текст и до, и после {N} — например "
            "Zqtx{N}xtqZ."
        )
    return open_part, close_part


def set_marker(open_part, close_part):
    """Меняет формат защитных маркеров для ВСЕХ последующих вызовов
    protect()/restore()/tokens_preserved() и т.д. Вызывать нужно до
    начала перевода (из окна программы это делается перед запуском
    рабочего потока), а не посреди него: уже закэшированные переводы
    содержат маркеры старого формата, и с другим форматом они просто не
    будут найдены в кэше по ключу — то есть безвредно, но переведутся
    заново."""
    global _TOKEN_OPEN, _TOKEN_CLOSE, _RE_TOKEN, _NO_MARKERS
    _NO_MARKERS = False
    _TOKEN_OPEN = open_part
    _TOKEN_CLOSE = close_part
    _RE_TOKEN = re.compile(re.escape(open_part) + r'(\d+)' + re.escape(close_part))


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
    if _NO_MARKERS:
        return text, []
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
    if not needs_translation(quoted):
        return quoted
    protected, tokens = protect(inner)
    translated = translate_fn(protected)
    if translated is None:
        return quoted
    if _NO_MARKERS and ("\n" not in translated and "\r" not in translated):
        # Режим "без разделителя": теги остались в тексте как есть,
        # вместо проверки маркеров сравниваем списки тегов/подстановок
        # в оригинале и переводе.
        translated = re.sub(r'(?<!\\)"', r'\\"', translated)
        if _ends_with_odd_backslash(translated):
            # Одиночный "\" в конце съел бы закрывающую кавычку строки.
            return quoted
        if not tokens_preserved(protected, translated):
            return quoted
        if markers_collapsed(protected, translated):
            return quoted
        return '"' + translated + '"'
    if "\n" in translated or "\r" in translated:
        # Переводчик вернул перевод из нескольких физических строк (в
        # основном встречается у LLM-переводчиков вроде Translate Gemma,
        # которые иногда путаются и переносят часть ответа на новую
        # строку). Строка в кавычках .rpy не может занимать больше одной
        # физической строки файла — если записать такое как есть, файл
        # перестанет парситься и игра не запустится. Оставляем оригинал.
        return quoted
    if tokens and not _all_tokens_present(translated, len(tokens)):
        # Переводчик потерял или повредил один из защищённых маркеров
        # (тег, подстановку [name] или %-форматирование). Использовать
        # такой перевод рискованно — Ren'Py может упасть с ошибкой на
        # сломанном "%" или подстановке. Оставляем оригинал как есть —
        # это будет обнаружено и переведено заново при следующем запуске
        # (та же логика, что и при сетевой ошибке перевода).
        return quoted
    if tokens and markers_collapsed(protected, translated):
        # Все маркеры на месте (проверка выше это подтвердила), но
        # какая-то пара тегов, между которыми в оригинале был текст,
        # оказалась в переводе слипшейся — то есть текст, который они
        # должны оборачивать, куда-то съехал. Как правило это
        # проявляется как "{b}{color=...}{/color}{/b}" вместо
        # "{b}{color=...}текст{/color}{/b}": видимый текст остался
        # снаружи тегов. Тоже отбрасываем как неудачный перевод.
        return quoted
    # На всякий случай экранируем случайно попавшие в перевод кавычки
    # (сами экранированные последовательности сейчас скрыты под
    # маркерами и вернутся как есть только на этапе restore).
    translated = re.sub(r'(?<!\\)"', r'\\"', translated)
    final_inner = restore(translated, tokens)
    return '"' + final_inner + '"'


def _ends_with_odd_backslash(text):
    n = len(text) - len(text.rstrip("\\"))
    return n % 2 == 1


def _token_matches(text):
    """Все "защищённые" куски текста: маркеры (обычный режим) либо сами
    теги/подстановки (режим без разделителя)."""
    return list((_RE_PROTECT if _NO_MARKERS else _RE_TOKEN).finditer(text))


def _strip_tokens(text):
    return (_RE_PROTECT if _NO_MARKERS else _RE_TOKEN).sub('', text)


def _pieces(text):
    """Режим без разделителя: список тегов/подстановок строки, без тех,
    что сознательно не сравниваются (см. _IGNORED_PIECES)."""
    result = []
    for m in _RE_PROTECT.finditer(text):
        p = m.group(0)
        if p in _IGNORED_PIECES or p.startswith('% '):
            continue
        result.append(p)
    return result


def _structure_ok(source, translated):
    """Режим без разделителя: набор тегов/подстановок в переводе совпадает
    с оригиналом по количеству и составу, а фигурные теги {..} ещё и
    идут в том же порядке (вложенность {b}{color}...{/color}{/b} важна).
    Порядок [переменных] и %-подстановок не проверяется — в русском они
    вполне могут законно меняться местами."""
    ps, pt = _pieces(source), _pieces(translated)
    if sorted(ps) != sorted(pt):
        return False
    return [p for p in ps if p.startswith('{')] == [p for p in pt if p.startswith('{')]


def _adjacent_brace_pairs(text):
    pairs = set()
    prev_end = None
    idx = 0
    for m in _RE_BRACE_TAG.finditer(text):
        if prev_end is not None and text[prev_end:m.start()].strip() == "":
            pairs.add((idx - 1, idx))
        prev_end = m.end()
        idx += 1
    return pairs


def boundary_text(text):
    """(текст до первого защищённого куска, текст после последнего)."""
    matches = _token_matches(text)
    if not matches:
        return text, ""
    return text[:matches[0].start()], text[matches[-1].end():]


_MIN_BOUNDARY_CHARS = 3


def looks_truncated(source_protected, translated):
    """True, если текст до первого тега/маркера или после последнего в
    оригинале был содержательным, а в переводе пропал целиком (модель
    перевела только то, что внутри тегов). Общая проверка для Translate
    Gemma и Gemma 4."""
    src_lead, src_trail = boundary_text(source_protected)
    tr_lead, tr_trail = boundary_text(translated)
    if len(src_lead.strip()) >= _MIN_BOUNDARY_CHARS and not tr_lead.strip():
        return True
    if len(src_trail.strip()) >= _MIN_BOUNDARY_CHARS and not tr_trail.strip():
        return True
    return False


def _all_tokens_present(text, count):
    found = {int(m.group(1)) for m in _RE_TOKEN.finditer(text)}
    return all(i in found for i in range(count))


def _adjacent_marker_pairs(text):
    """Возвращает множество пар (idx1, idx2) — номеров маркеров, которые
    в text идут вплотную друг за другом без единого не-пробельного
    символа между ними (в порядке появления). Используется
    markers_collapsed() для сравнения взаимного расположения маркеров до
    и после перевода."""
    pairs = set()
    prev_idx = None
    prev_end = None
    for m in _RE_TOKEN.finditer(text):
        idx = int(m.group(1))
        if prev_idx is not None and text[prev_end:m.start()].strip() == "":
            pairs.add((prev_idx, idx))
        prev_idx = idx
        prev_end = m.end()
    return pairs


def markers_collapsed(source_protected, translated):
    """True, если в translated образовалась "слипшаяся" пара маркеров
    (без текста между ними), которой не было в source_protected — то
    есть переводчик оторвал теги от текста, который они должны
    оборачивать. Типичный сбой у LLM-переводчиков (например Translate
    Gemma): все маркеры формально на месте (tokens_preserved() говорит
    "ок"), но фраза внутри {b}...{/b}/{color=...}...{/color} и т.п.
    съехала наружу, а сами теги слиплись друг с другом в конце строки.
    Дополняет _all_tokens_present() — та проверяет только сам факт
    присутствия маркеров, эта — их взаимное расположение."""
    if _NO_MARKERS:
        return bool(_adjacent_brace_pairs(translated) - _adjacent_brace_pairs(source_protected))
    return bool(_adjacent_marker_pairs(translated) - _adjacent_marker_pairs(source_protected))


def _has_translatable_content(protected):
    """True, если в защищённом тексте (после protect()) осталась хоть
    одна буква/цифра ВНЕ маркеров — то есть реально есть что
    переводить. Строка вроде "{w}{nw}" (только теги, без единого
    слова) после protect() превращается в один маркер или несколько
    маркеров подряд — переводить там нечего."""
    stripped = _strip_tokens(protected)
    return any(ch.isalnum() for ch in stripped)


def needs_translation(quoted):
    """True, если в этой кавычечной строке вообще есть что переводить.
    Пустые строки и строки, состоящие только из тегов/подстановок
    (например "{w}{nw}" или один голый "[name]") возвращают False —
    их не нужно (и, как выяснилось на практике, вредно) отправлять
    переводчику: на реальном LibreTranslate/Argos Translate замечено,
    что такая "пустышка" (текст без единого настоящего слова) в общей
    пачке сбивает у движка определение границ предложений и он
    сшивает её перевод с соседней строкой пачки — портится не только
    сама пустышка, но и нормальная строка рядом с ней."""
    inner = quoted[1:-1]
    if inner.strip() == '':
        return False
    protected, _ = protect(inner)
    return _has_translatable_content(protected)


def tokens_preserved(source_protected, translated):
    """True, если translated содержит как минимум все защитные маркеры
    (см. _TOKEN_OPEN/_TOKEN_CLOSE), которые были в source_protected. Нужна движкам перевода
    (gtranslate/deepl_translate/libretranslate_translate), чтобы не
    закэшировать результат пакетного перевода, где сервис вернул
    правильное КОЛИЧЕСТВО строк, но при этом потерял/повредил маркер
    внутри одной из них — такой результат нельзя запоминать в кэше:
    иначе испорченный перевод будет навсегда "залипать" и
    воспроизводиться при каждом следующем запуске, даже после полной
    переустановки перевода (сам файл .rpy можно очистить, а кэш —
    отдельный файл, и порча в нём переживёт эту очистку)."""
    if translated is None:
        return False
    if _NO_MARKERS:
        return _structure_ok(source_protected, translated)
    needed = {int(m.group(1)) for m in _RE_TOKEN.finditer(source_protected)}
    if not needed:
        return True
    found = {int(m.group(1)) for m in _RE_TOKEN.finditer(translated)}
    return needed <= found


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
    если переводить нечего (пустая строка или только теги/подстановки —
    см. needs_translation())."""
    if not needs_translation(quoted):
        return None
    protected, _ = protect(quoted[1:-1])
    return protected


def count_pending(lines, overwrite=False):
    """Сколько строк в этом файле реально предстоит отправить на
    перевод (не переведены и в них есть что переводить). Нужен
    контекстному движку (Translate Gemma), у которого нет общего
    предварительного сбора уникальных текстов, — чтобы заранее знать
    общее число строк для индикатора прогресса и оценки времени. Те же
    правила, что и в process_lines_contextual(), поэтому число
    совпадает с тем, сколько раз позже будет вызван переводчик."""
    count = 0
    for entry in find_translatable(lines):
        if _is_untranslated(entry, overwrite) and needs_translation(entry['orig_quoted']):
            count += 1
    return count


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


def _failed_msg(orig_quoted, file_name):
    msg = "Не удалось перевести (оставлено как есть): {0!r}".format(orig_quoted[:80])
    if file_name:
        msg += " в файле {0}".format(file_name)
    return msg


def process_lines(lines, translate_fn, overwrite=False, stats=None, log=None,
                  file_name=None):
    """lines — список строк файла (с переносами строк на конце, как из
    readlines()/splitlines(keepends=True)). Возвращает новый список строк.
    translate_fn(text) -> str, где text — защищённый текст без кавычек.
    Если translate_fn — это CachedTranslator с заранее прогретым кэшем
    (см. collect_needed_texts() + warm_batch()), эта функция работает
    практически мгновенно, без сетевых запросов.
    stats — dict со счётчиками translated/skipped/failed (обновляется).
    log(msg) — необязательная функция логирования.
    """
    if stats is None:
        stats = {'translated': 0, 'skipped': 0, 'failed': 0}
    stats.setdefault('failed', 0)
    stats.setdefault('nothing_to_translate', 0)
    log = log or (lambda msg: None)
    out = list(lines)

    for entry in find_translatable(out):
        if _is_untranslated(entry, overwrite):
            if not needs_translation(entry['orig_quoted']):
                # Строка состоит только из тегов/подстановок — переводить
                # нечего, и отправлять такое переводчику вредно (см.
                # needs_translation()). Не трогаем строку и не считаем
                # это ни успехом, ни неудачей.
                stats['nothing_to_translate'] += 1
                continue
            translated = translate_quoted(entry['orig_quoted'], translate_fn)
            if translated == entry['orig_quoted']:
                # translate_quoted() откатился на исходный текст (переводчик
                # не ответил, либо повредил защищённый маркер) — строка
                # фактически осталась непереведённой. НЕ записываем её и не
                # считаем успехом: иначе программа рапортует "переведено",
                # хотя в файле всё ещё английский текст, и при каждом
                # следующем запуске эта же строка будет находиться заново
                # (что выглядит как "зацикливание"). Оставляем как есть —
                # find_translatable() снова найдёт её при следующем запуске,
                # когда, например, переводчик будет доступнее.
                stats['failed'] += 1
                log(
                    _failed_msg(entry['orig_quoted'], file_name)
                )
                continue
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


def process_lines_contextual(lines, translate_line_fn, resolve_speaker_fn,
                              overwrite=False, stats=None, log=None,
                              file_name=None):
    """Аналог process_lines(), но для контекстных движков (Translate
    Gemma): строки обрабатываются строго по порядку сверху вниз, с
    переносимым состоянием "предыдущая реплика/перевод". Это состояние
    ведётся ТОЛЬКО между диалоговыми строками (entries типа 'dialogue') —
    блоки old/new (пункты меню/интерфейса) переводятся без контекста и
    не входят в цепочку вообще (это чужеродные UI-строки, а не часть
    разговора).

    Уже переведённые диалоговые строки не перезаписываются (как обычно),
    но их СОБСТВЕННЫЙ текст всё равно становится контекстом для
    следующей строки — так "докручивание" перевода после остановки
    работает само собой, без отдельного файла состояния: что уже
    написано в файле, то и есть история разговора на этот момент.

    translate_line_fn(previous_source, previous_translation,
    protected_current_source, rules_name) -> translated_text | None —
    вызывается на каждую ЕЩЁ НЕ переведённую строку; previous_source/
    previous_translation — исходный (незащищённый) текст, пустые строки
    для old/new и для самой первой диалоговой строки файла.

    resolve_speaker_fn(who) -> str | None — определяет имя говорящего
    для поля RULES по значению entry['who'] (пустая строка → своё имя
    для рассказчика и т.п. — решает сама функция, см. character_names.py).
    """
    if stats is None:
        stats = {'translated': 0, 'skipped': 0, 'failed': 0}
    stats.setdefault('failed', 0)
    stats.setdefault('nothing_to_translate', 0)
    log = log or (lambda msg: None)
    out = list(lines)

    previous_source = ""
    previous_translation = ""

    for entry in find_translatable(out):
        is_dialogue = entry['type'] == 'dialogue'
        rules_name = resolve_speaker_fn(entry['who']) if is_dialogue else None

        if not _is_untranslated(entry, overwrite):
            stats['skipped'] += 1
            if is_dialogue:
                previous_source = entry['orig_quoted'][1:-1]
                previous_translation = entry['current_inner']
            continue

        if not needs_translation(entry['orig_quoted']):
            stats['nothing_to_translate'] += 1
            continue

        ctx_source = previous_source if is_dialogue else ""
        ctx_translation = previous_translation if is_dialogue else ""

        def _fn(protected_text, _s=ctx_source, _t=ctx_translation, _r=rules_name):
            return translate_line_fn(_s, _t, protected_text, _r)

        translated = translate_quoted(entry['orig_quoted'], _fn)

        if translated == entry['orig_quoted']:
            stats['failed'] += 1
            log(
                _failed_msg(entry['orig_quoted'], file_name)
            )
            if is_dialogue:
                # Реплика реально прозвучала (на английском) — не
                # выкидываем её из контекста совсем; за неимением
                # лучшего перевода используем тот же английский текст.
                orig_inner = entry['orig_quoted'][1:-1]
                previous_source = orig_inner
                previous_translation = orig_inner
            continue

        if entry['type'] == 'old_new':
            eol = get_line_ending(out[entry['new_line_idx']])
            out[entry['new_line_idx']] = f"{entry['new_indent']}new {translated}{eol}"
        else:
            who_prefix = f"{entry['who']} " if entry['who'] else ""
            eol = get_line_ending(out[entry['code_line_idx']])
            out[entry['code_line_idx']] = f"{entry['code_indent']}{who_prefix}{translated}{eol}"
        stats['translated'] += 1

        if is_dialogue:
            previous_source = entry['orig_quoted'][1:-1]
            previous_translation = translated[1:-1]

    return out
