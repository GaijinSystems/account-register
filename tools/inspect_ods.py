#!/usr/bin/env python3
"""Print the structure of a LibreOffice .ods workbook with the values masked.

The output covers sheet names, column types, formulas (cell references kept,
literal numbers and strings masked), merged/hidden cells, comments and
anomalies. That is enough to design an importer without revealing amounts,
descriptions or notes. Standard library only; nothing is written.

Usage:
    python3 inspect_ods.py WORKBOOK.ods [--header-rows N] > structure.txt

Review the output before sharing it. It shows sheet names, the text of the
first N rows of each sheet (default 1, meant for column headings), and any
single-character cell values (such as the "x" cleared marks).
"""
import argparse
import collections
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

NS = {
    'office': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0',
    'table': 'urn:oasis:names:tc:opendocument:xmlns:table:1.0',
    'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0',
    'style': 'urn:oasis:names:tc:opendocument:xmlns:style:1.0',
}

MAX_CONTENT_BYTES = 200 * 1024 * 1024
MAX_EXPAND = 10000   # cap when materialising repeated rows/cells that have content
MAX_LIST = 15        # cap on example lists in the output


def q(prefix, name):
    return '{%s}%s' % (NS[prefix], name)


T_CELL, T_COVERED = q('table', 'table-cell'), q('table', 'covered-table-cell')
T_ROW, T_COL = q('table', 'table-row'), q('table', 'table-column')
ROW_GROUPS = {q('table', n) for n in ('table-header-rows', 'table-row-group', 'table-rows')}
COL_GROUPS = {q('table', n) for n in ('table-header-columns', 'table-column-group', 'table-columns')}

LOOKS_MONEY = re.compile(r'^\(?\s*-?\s*\$\s*-?\d[\d,]*(\.\d+)?\s*\)?$|^\(?-?\d[\d,]*\.\d\d\)?$')
LOOKS_NUMBER = re.compile(r'^-?\d[\d,]*(\.\d+)?$')
LOOKS_DATE = re.compile(r'^\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}$')


def col_letter(i):
    s, i = '', i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def addr(row, col):
    return '%s%d' % (col_letter(col), row + 1)


def shape(text):
    """Display text with every run of digits replaced by '#'."""
    return re.sub(r'\d+', '#', text)


def para_text(el):
    out = [el.text or '']
    for child in el:
        if child.tag == q('text', 's'):
            out.append(' ' * int(child.get(q('text', 'c'), '1')))
        elif child.tag == q('text', 'tab'):
            out.append('\t')
        elif child.tag == q('text', 'line-break'):
            out.append('\n')
        elif child.tag != q('office', 'annotation'):
            out.append(para_text(child))
        out.append(child.tail or '')
    return ''.join(out)


def cell_info(cell):
    text = '\n'.join(para_text(p) for p in cell.findall(q('text', 'p')))
    vtype = cell.get(q('office', 'value-type'))
    formula = cell.get(q('table', 'formula'))
    if vtype is None and formula is None and text == '':
        return None
    return {
        'kind': vtype or 'string',
        'formula': formula,
        'text': text,
        'date': cell.get(q('office', 'date-value')),
    }


def norm_ref(part, row):
    """'.H3' in row index 3 (sheet row 4) -> '.H{r-1}'. Absolute rows stay '$n'
    and references to other sheets keep their real row number."""
    sheet, dot, cell = part.rpartition('.')
    m = re.fullmatch(r'(\$?)([A-Z]+)(\$?)(\d+)', cell)
    if not m:
        return part
    cabs, col, rabs, r = m.groups()
    if sheet:  # another sheet: relative offsets mean nothing, show the real row
        rs = rabs + r
    elif rabs:
        rs = '$' + r
    else:
        off = int(r) - (row + 1)
        rs = '{r%+d}' % off if off else '{r}'
    return '%s%s%s%s%s' % (sheet, dot, cabs, col, rs)


def norm_formula(f, row):
    """Make a formula comparable across rows and strip its literal values."""
    if f is None:
        return None
    f = re.sub(r'^(of|oooc|msoxl):', '', f)
    out = []
    for m in re.finditer(r'\[[^\]]*\]|"(?:[^"]|"")*"|[^\["]+|.', f):
        tok = m.group(0)
        if tok.startswith('[') and tok.endswith(']'):
            out.append('[' + ':'.join(norm_ref(p, row) for p in tok[1:-1].split(':')) + ']')
        elif tok.startswith('"'):
            out.append('"T"')
        else:
            out.append(re.sub(r'(?<![A-Za-z_.\d])\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?', 'N', tok))
    return ''.join(out)


def describe(info, row, reveal_text):
    """One cell as a short masked description."""
    kind, f = info['kind'], norm_formula(info['formula'], row)
    if kind == 'string':
        if reveal_text or len(info['text']) <= 1:
            d = 'str %r' % info['text']
        else:
            d = 'str'
            if LOOKS_MONEY.match(info['text'].strip()):
                d += '(looks like money: %r)' % shape(info['text'].strip())
            elif LOOKS_DATE.match(info['text'].strip()):
                d += '(looks like a date: %r)' % shape(info['text'].strip())
            elif LOOKS_NUMBER.match(info['text'].strip()):
                d += '(looks like a number: %r)' % shape(info['text'].strip())
    else:
        d = '%s %r' % (kind, shape(info['text']))
    if f:
        d += ' =' + f.lstrip('=')
    return d


def read_sheet(table):
    hidden_cols, ci = [], 0
    for el in table.iter():
        if el.tag == T_COL:
            n = int(el.get(q('table', 'number-columns-repeated'), '1'))
            vis = el.get(q('table', 'visibility'), 'visible')
            if vis != 'visible':
                hidden_cols.append((ci, n, vis))
            ci += n

    cells, hidden_rows, merges, notes, covered_content = {}, [], [], [], []

    def rows(el):
        for child in el:
            if child.tag == T_ROW:
                yield child
            elif child.tag in ROW_GROUPS:
                yield from rows(child)

    ri = 0
    for row in rows(table):
        rrep = int(row.get(q('table', 'number-rows-repeated'), '1'))
        vis = row.get(q('table', 'visibility'), 'visible')
        rowcells, c = [], 0
        for cell in row:
            if cell.tag not in (T_CELL, T_COVERED):
                continue
            crep = int(cell.get(q('table', 'number-columns-repeated'), '1'))
            info = cell_info(cell)
            if cell.find(q('office', 'annotation')) is not None:
                notes.append((ri, c))
            cs = int(cell.get(q('table', 'number-columns-spanned'), '1'))
            rs = int(cell.get(q('table', 'number-rows-spanned'), '1'))
            if cs > 1 or rs > 1:
                merges.append((ri, c, rs, cs))
            if info and cell.tag == T_COVERED:
                covered_content.append((ri, c))
            elif info:
                for k in range(min(crep, MAX_EXPAND)):
                    rowcells.append((c + k, info))
            c += crep
        if rowcells:
            for k in range(min(rrep, MAX_EXPAND)):
                for cc, info in rowcells:
                    cells[(ri + k, cc)] = info
        if vis != 'visible':
            hidden_rows.append((ri, rrep, vis))
        ri += rrep
    return cells, hidden_cols, hidden_rows, merges, notes, covered_content


def fmt_ranges(items):
    """[(start, count, vis)] of column indexes -> 'G, K:M (filter)'."""
    out = []
    for start, n, vis in items:
        s = col_letter(start) if n == 1 else '%s:%s' % (col_letter(start), col_letter(start + n - 1))
        out.append(s + ('' if vis == 'collapse' else ' (%s)' % vis))
    return ', '.join(out)


def capped(seq):
    seq = list(seq)
    s = ', '.join(seq[:MAX_LIST])
    return s + (' ... (+%d more)' % (len(seq) - MAX_LIST) if len(seq) > MAX_LIST else '')


def report_sheet(idx, table, hidden, header_rows, out):
    name = table.get(q('table', 'name'))
    cells, hidden_cols, hidden_rows, merges, notes, covered = read_sheet(table)
    flags = []
    if hidden:
        flags.append('hidden sheet')
    if table.get(q('table', 'protected')) == 'true':
        flags.append('protected')
    out('=== Sheet %d: %r%s' % (idx, name, ' [%s]' % ', '.join(flags) if flags else ''))
    if not cells:
        out('  (empty)\n')
        return
    max_r = max(r for r, _ in cells)
    max_c = max(c for _, c in cells)
    out('  used range: A1:%s  (%d rows, %d columns)' % (addr(max_r, max_c), max_r + 1, max_c + 1))
    used = {r for r, _ in cells}
    blank = [str(r + 1) for r in range(max_r + 1) if r not in used]
    out('  blank rows inside range: %s' % (capped(blank) if blank else 'none'))
    out('  hidden columns: %s' % (fmt_ranges(hidden_cols) or 'none'))
    hr = [(r, n, v) for r, n, v in hidden_rows if r <= max_r]
    out('  hidden rows: %s' % (capped(
        ('%d' % (r + 1) if n == 1 else '%d-%d' % (r + 1, r + n)) + ('' if v == 'collapse' else ' (%s)' % v)
        for r, n, v in hr) if hr else 'none'))
    out('  merged cells: %s' % (capped(
        '%s:%s' % (addr(r, c), addr(r + rs - 1, c + cs - 1)) for r, c, rs, cs in merges) if merges else 'none'))
    if covered:
        out('  content hidden under merged cells: %s' % capped(addr(r, c) for r, c in covered))
    out('  comments: %s' % (capped(addr(r, c) for r, c in notes) if notes else 'none'))

    out('  -- first %d row(s), text shown --' % header_rows)
    for r in range(min(header_rows, max_r + 1)):
        row = sorted((c, i) for (rr, c), i in cells.items() if rr == r)
        out('  row %d: %s' % (r + 1, '; '.join(
            '%s=%s' % (col_letter(c), describe(i, r, True)) for c, i in row) or '(empty)'))

    data_rows = [r for r in sorted(used) if r >= header_rows]
    if not data_rows:
        out('')
        return
    out('  -- column profile, rows %d-%d --' % (data_rows[0] + 1, max_r + 1))
    anomalies = []
    for c in range(max_c + 1):
        col = [(r, cells[(r, c)]) for r in data_rows if (r, c) in cells]
        if not col:
            out('  %s: empty' % col_letter(c))
            continue
        kinds = collections.Counter(i['kind'] for _, i in col)
        parts = ['%d %s' % (n, k) for k, n in kinds.most_common()]
        years = sorted({i['date'][:4] for _, i in col if i['date']})
        if years:
            parts.append('years %s' % (years[0] if len(years) == 1 else '%s-%s' % (years[0], years[-1])))
        shapes = collections.Counter(shape(i['text']) for _, i in col if i['kind'] != 'string')
        if shapes:
            parts.append('displayed as ' + ', '.join('%r x%d' % s for s in shapes.most_common(4)))
        singles = collections.Counter(i['text'] for _, i in col if i['kind'] == 'string' and len(i['text']) <= 1)
        if singles:
            parts.append('single chars ' + ', '.join('%r x%d' % s for s in singles.most_common(4)))
        money_txt = [r for r, i in col if i['kind'] == 'string' and len(i['text']) > 1
                     and LOOKS_MONEY.match(i['text'].strip())]
        date_txt = [r for r, i in col if i['kind'] == 'string' and len(i['text']) > 1
                    and LOOKS_DATE.match(i['text'].strip())]
        num_txt = [r for r, i in col if i['kind'] == 'string' and len(i['text']) > 1
                   and LOOKS_NUMBER.match(i['text'].strip()) and r not in money_txt]
        for label, rows_ in (('money', money_txt), ('dates', date_txt), ('plain numbers', num_txt)):
            if rows_:
                parts.append('%d text cell(s) that look like %s (%s)' % (
                    len(rows_), label, capped(addr(r, c) for r in rows_)))
        out('  %s: %s' % (col_letter(c), '; '.join(parts)))

        formulas = collections.Counter()
        first_at = {}
        for r, i in col:
            f = norm_formula(i['formula'], r)
            if f:
                formulas[f] += 1
                first_at.setdefault(f, r)
        for f, n in formulas.most_common(5):
            out('      formula x%d (first at %s): %s' % (n, addr(first_at[f], c), f))
        if len(formulas) > 5:
            out('      ... %d more distinct formulas' % (len(formulas) - 5))
        n_formula = sum(formulas.values())
        if n_formula * 2 > len(col):
            main, n_main = formulas.most_common(1)[0]
            for r, i in col:
                f = norm_formula(i['formula'], r)
                if f is None:
                    anomalies.append('%s: typed %s where column %s is mostly formulas' % (
                        addr(r, c), i['kind'], col_letter(c)))
                elif f != main and n_main * 2 > n_formula:
                    anomalies.append('%s: differs from the usual %s formula: %s' % (
                        addr(r, c), col_letter(c), f))

    out('  -- formula anomalies --')
    for a in anomalies[:MAX_LIST * 2]:
        out('  ' + a)
    if len(anomalies) > MAX_LIST * 2:
        out('  ... (+%d more)' % (len(anomalies) - MAX_LIST * 2))
    if not anomalies:
        out('  none')

    sample = data_rows[:3] + [r for r in data_rows[-3:] if r not in data_rows[:3]]
    out('  -- first and last data rows (masked) --')
    for r in sample:
        row = sorted((c, i) for (rr, c), i in cells.items() if rr == r)
        out('  row %d: %s' % (r + 1, '; '.join('%s=%s' % (col_letter(c), describe(i, r, False)) for c, i in row)))
    out('')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('workbook')
    ap.add_argument('--header-rows', type=int, default=1,
                    help='number of top rows per sheet whose text is shown (default 1)')
    args = ap.parse_args()

    try:
        with zipfile.ZipFile(args.workbook) as z:
            info = z.getinfo('content.xml')
            if info.file_size > MAX_CONTENT_BYTES:
                sys.exit('content.xml is unexpectedly large (%d bytes)' % info.file_size)
            root = ET.fromstring(z.read('content.xml'))
    except (zipfile.BadZipFile, KeyError):
        sys.exit('%s is not an .ods workbook (save it from Calc as "ODF Spreadsheet (.ods)")'
                 % args.workbook)

    hidden_styles = set()
    for st in root.iter(q('style', 'style')):
        if st.get(q('style', 'family')) == 'table':
            for tp in st.findall(q('style', 'table-properties')):
                if tp.get(q('table', 'display')) == 'false':
                    hidden_styles.add(st.get(q('style', 'name')))

    out = print
    sheet_el = root.find('.//' + q('office', 'spreadsheet'))
    tables = sheet_el.findall(q('table', 'table'))
    out('Workbook: %d sheet(s): %s\n' % (len(tables), ', '.join(repr(t.get(q('table', 'name'))) for t in tables)))
    for n, t in enumerate(tables, 1):
        report_sheet(n, t, t.get(q('table', 'style-name')) in hidden_styles, args.header_rows, out)

    named = sheet_el.find(q('table', 'named-expressions'))
    if named is not None and len(named):
        out('=== Named ranges / expressions')
        for el in named:
            target = el.get(q('table', 'cell-range-address')) or norm_formula(el.get(q('table', 'expression')), 0)
            out('  %s -> %s' % (el.get(q('table', 'name')), target))


if __name__ == '__main__':
    main()
