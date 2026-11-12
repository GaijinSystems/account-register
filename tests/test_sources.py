"""File detection, the .xlsx reader and tolerant CSV reading (sources.py)."""
import codecs
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from helpers import checking_rows, cur, s, write_ods, write_xlsx
from register.core import money, sources

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class Dir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def file(self, name, data):
        path = self.dir / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def zipped(self, name, parts):
        path = self.dir / name
        with zipfile.ZipFile(path, "w") as z:
            for part, data in parts.items():
                z.writestr(part, data)
        return path


class DetectTest(Dir):
    def test_supported_kinds_by_content_not_name(self):
        write_ods(self.dir / "book.xlsx", {"S": [[s("a")]]})          # wrong extension on purpose
        write_xlsx(self.dir / "book.ods", {"S": [[s("a")]]})
        self.assertEqual(sources.detect(self.dir / "book.xlsx"), "ods")
        self.assertEqual(sources.detect(self.dir / "book.ods"), "xlsx")
        self.assertEqual(sources.detect(self.file("data.txt", "a,b\n1,2\n")), "csv")
        self.assertEqual(sources.detect(self.file("pk.csv", "PKG,amount\nx,1\n")), "csv")
        self.assertEqual(sources.detect(self.file("u16.csv", "a\tb\n".encode("utf-16"))), "csv")

    def test_unsupported_files_say_what_to_do(self):
        cases = {
            "old.xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100, ".xls"),
            "statement.pdf": (b"%PDF-1.7\n", "PDF"),
            "bank.xls": (b"<html><table><tr><td>1</td></tr></table></html>", "web page"),
            "x2003.xml": (b'<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet">',
                          "Excel 2003 XML"),
            "blob.bin": (b"\x01\x02\x00\x03", "binary"),
        }
        for name, (data, words) in cases.items():
            with self.assertRaises(sources.SourceError, msg=name) as cm:
                sources.detect(self.file(name, data))
            self.assertIn(words, str(cm.exception))
        zips = {
            "doc.docx": ({"word/document.xml": "<w/>"}, "Word"),
            "b.xlsb": ({"xl/workbook.bin": "x"}, ".xlsb"),
            "n.numbers": ({"Index/Document.iwa": "x"}, "Numbers"),
            "t.odt": ({"mimetype": "application/vnd.oasis.opendocument.text"}, "OpenDocument text"),
            "a.zip": ({"readme.txt": "hi"}, "zip archive"),
        }
        for name, (parts, words) in zips.items():
            with self.assertRaises(sources.SourceError, msg=name) as cm:
                sources.detect(self.zipped(name, parts))
            self.assertIn(words, str(cm.exception))

    def test_password_protected_ods(self):
        path = self.zipped("locked.ods", {
            "mimetype": "application/vnd.oasis.opendocument.spreadsheet",
            "META-INF/manifest.xml": '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:'
                                     'manifest:1.0"><manifest:encryption-data/></manifest:manifest>',
            "content.xml": "\x8f\x01garbage"})
        with self.assertRaises(sources.SourceError) as cm:
            sources.read_source(path)
        self.assertIn("password-protected", str(cm.exception))


class XlsxTest(Dir):
    def sheet(self, cells, styles=None, workbook_pr="", strings=None):
        """A one-sheet workbook from raw <sheetData> XML."""
        rels = ('<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'worksheet" Target="/xl/worksheets/s.xml"/>'
                '<Relationship Id="r2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'styles" Target="styles.xml"/>'
                '<Relationship Id="r3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'sharedStrings" Target="strings.xml"/>')
        parts = {
            "xl/workbook.xml": '<workbook xmlns="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                               'relationships">%s<sheets><sheet name="S" r:id="r1"/></sheets></workbook>'
                               % (MAIN, workbook_pr),
            "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                                          'relationships">%s</Relationships>' % rels,
            "xl/worksheets/s.xml": '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (MAIN, cells),
            "xl/styles.xml": '<styleSheet xmlns="%s">%s</styleSheet>' % (MAIN, styles or ""),
            "xl/strings.xml": '<sst xmlns="%s">%s</sst>' % (MAIN, strings or ""),
        }
        return sources.read_xlsx(self.zipped("t.xlsx", parts))["S"]

    def test_values(self):
        styles = ('<numFmts><numFmt numFmtId="170" formatCode="[Red]&quot;day &quot;dd/mm/yyyy"/>'
                  '<numFmt numFmtId="171" formatCode="mm:ss"/></numFmts>'
                  '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="170"/><xf numFmtId="171"/>'
                  '<xf numFmtId="20"/></cellXfs>')
        strings = ('<si><t>plain</t></si>'
                   '<si><r><t>rich </t></r><r><rPr/><t>text</t></r><rPh><t>PHONETIC</t></rPh></si>'
                   '<si><t>line_x000D_break _x005F_x0041_</t></si>')
        g = self.sheet(
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
            '<row r="2"><c r="A2"><v>84.120000000000005</v></c><c r="B2" s="1"><v>45662</v></c>'
            '<c r="C2" s="2"><v>45662.75</v></c><c r="D2" s="3"><v>0.5</v></c><c r="E2" s="4"><v>0.5</v></c></row>'
            '<row><c t="b"><v>1</v></c><c t="e"><v>#REF!</v></c><c t="inlineStr"><is><t>inline</t></is></c>'
            '<c t="str"><f>A1&amp;"!"</f><v>plain!</v></c><c s="1"/><c><f>1+1</f></c></row>',
            styles=styles, strings=strings)
        text = {sources.ref(*k): (v.kind, v.text) for k, v in g.items()}
        self.assertEqual(text, {
            "A1": ("string", "plain"), "B1": ("string", "rich text"), "C1": ("string", "line\rbreak _x0041_"),
            "A2": ("float", "84.12"), "B2": ("date", "2025-01-05"), "C2": ("date", "2025-01-05"),
            "D2": ("float", "0.5"), "E2": ("float", "0.5"),
            "A3": ("boolean", "TRUE"), "B3": ("string", "#REF!"), "C3": ("string", "inline"),
            "D3": ("string", "plain!")})
        self.assertEqual(g[(1, 0)].value, "84.12")

    def test_1904_dates_and_the_1900_leap_day(self):
        g = self.sheet('<row r="1"><c r="A1" s="1"><v>0</v></c><c r="B1" s="1"><v>59</v></c>'
                       '<c r="C1" s="1"><v>60</v></c><c r="D1" s="1"><v>61</v></c></row>',
                       styles='<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>')
        self.assertEqual([g[(0, c)].kind for c in range(4)], ["float", "date", "float", "date"])
        self.assertEqual((g[(0, 1)].date, g[(0, 3)].date), ("1900-02-28", "1900-03-01"))
        g = self.sheet('<row r="1"><c r="A1" s="1"><v>0</v></c></row>', workbook_pr='<workbookPr date1904="1"/>',
                       styles='<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>')
        self.assertEqual(g[(0, 0)].date, "1904-01-01")

    def test_shared_formulas_are_expanded(self):
        g = self.sheet('<row r="2"><c r="C2"><f>C1+B2</f><v>2</v></c></row>'
                       '<row r="3"><c r="C3"><f t="shared" ref="C3:C5" si="0">C2+B3-$A$1</f><v>3</v></c></row>'
                       '<row r="4"><c r="C4"><f t="shared" si="0"/><v>4</v></c></row>'
                       '<row r="5"><c r="C5"><f t="shared" si="0"/><v>5</v></c></row>')
        self.assertEqual([g[(r, 2)].formula for r in range(1, 5)],
                         ["of:=[.C1]+[.B2]", "of:=[.C2]+[.B3]-[.$A$1]", "of:=[.C3]+[.B4]-[.$A$1]",
                          "of:=[.C4]+[.B5]-[.$A$1]"])
        self.assertEqual(sources.odf_formula("'My sheet'!H5+LOG10(B2)+SUM(A1:A3)"),
                         "of:=[$'My sheet'.H5]+LOG10([.B2])+SUM([.A1]:[.A3])")

    def test_hostile_or_broken_files(self):
        bad = [
            '<row r="1"><c r="A1" t="s"><v>7</v></c></row>',                       # no such shared string
            '<row r="1"><c r="XFE1"><v>1</v></c></row>',                            # beyond the last column
        ]
        for cells in bad:
            with self.assertRaises(sources.SourceError, msg=cells):
                self.sheet(cells)
        with self.assertRaises(sources.SourceError):
            sources.read_xlsx(self.zipped("d.xlsx", {
                "xl/workbook.xml": '<!DOCTYPE x [<!ENTITY a "aaaa">]><workbook xmlns="%s"/>' % MAIN}))
        with self.assertRaises(sources.SourceError):
            sources.read_xlsx(self.zipped("e.xlsx", {"xl/workbook.xml": "<workbook"}))
        with mock.patch.object(sources, "MAX_CELLS", 3), self.assertRaises(sources.SourceError):
            self.sheet('<row r="1">%s</row>' % ('<c><v>1</v></c>' * 4))

    def test_same_cells_as_ods(self):
        rows = checking_rows("100.00", [(1066, "2025-01-05", "Rent", "1000.00", "x", None, None, s("note")),
                                        (None, "2025-01-06", "Pay", None, None, "2150.00", None, None)])
        rows[3][7] = cur("1250.00", formula="of:=[.H3]-[.D4]+[.F4]")
        write_ods(self.dir / "a.ods", {"2025": rows})
        write_xlsx(self.dir / "a.xlsx", {"2025": rows}, shared_strings=False)
        ods = sources.read_ods(self.dir / "a.ods")["2025"]
        xlsx = sources.read_xlsx(self.dir / "a.xlsx")["2025"]
        self.assertEqual(sorted(ods), sorted(xlsx))
        for k in ods:
            a, b = ods[k], xlsx[k]
            self.assertEqual((a.date, a.formula), (b.date, b.formula), k)
            if a.value is not None:
                self.assertEqual(Decimal(a.value), Decimal(b.value), k)
            if a.kind == "string":
                self.assertEqual(a.text, b.text, k)


class CsvTest(Dir):
    def test_encoding_delimiter_and_excel_hint(self):
        path = self.file("eu.csv", "sep=;\r\nDatum;Omschrijving;Bedrag\r\n05.01.2025;Bäckerei;-4,50\r\n"
                                   .encode("cp1252"))
        (grid,), info = (lambda g, i: ([g], i))(*sources.read_csv(path))
        self.assertEqual(info, {"encoding": "cp1252", "delimiter": ";"})
        self.assertEqual(grid[(2, 1)].text, "Bäckerei")   # rows keep the file's line numbers
        self.assertEqual(grid.widths, {1: 3, 2: 3})
        path = self.file("u16.txt", "a\tb\n1\t2,5\n".encode("utf-16"))
        grid, info = sources.read_csv(path)
        self.assertEqual((info["delimiter"], grid[(1, 1)].text), ("\t", "2,5"))
        grid, info = sources.read_csv(self.file("u8.csv", codecs.BOM_UTF8 + "é,b\n".encode()))
        self.assertEqual((info["encoding"], grid[(0, 0)].text), ("utf-8-sig", "é"))
        with self.assertRaises(sources.SourceError):
            sources.read_csv(self.file("bad.csv", b"\xff\xfe"[:1] + b"abc"), encoding="utf-8")
        with self.assertRaises(sources.SourceError):
            sources.read_csv(self.file("q.csv", 'a,"b\n'))

    def test_shape_is_checked_from_the_data_on(self):
        text = "Account,Checking\n\nDate,Amount,Balance,Note\n01/02/25,1.00,2.00,\n01/03/25,1.00,3.00,,,\n"
        grid, _ = sources.read_csv(self.file("bank.csv", text))
        self.assertIsNone(sources.check_widths(grid, 3))   # the preamble may have any shape; ",," extras are empty
        grid, _ = sources.read_csv(self.file("bank.csv", text + "01/04/25,$1,000.00,3.00,x\n"))
        msg = sources.check_widths(grid, 3)
        self.assertIn("line(s) 6 differ", msg)
        self.assertIn("repair_split_money", msg)


class ParseLooseTest(unittest.TestCase):
    def test_more_ways_of_writing_amounts(self):
        cases = {("84.12-", "."): "-84.12", ("+5", "."): "5", ("1 234.56", "."): "1234.56",
                 ("1 234,56 €", ","): "1234.56", ("1.234,56", ","): "1234.56", ("-84,12", ","): "-84.12",
                 ("€ -3,5", ","): "-3.5", ("1'000.25", "."): "1000.25", ("−7.00", "."): "-7.00",
                 ("(£12.00)", "."): "-12.00", ("1.234", ","): "1234"}
        for (text, decimal), want in cases.items():
            self.assertEqual(money.parse_loose(text, decimal), Decimal(want), text)
        for bad in ("-", "--5", "5--", "$$5", "1,234 567", "1.234,56", "5.", "+-5"):
            with self.assertRaises(ValueError, msg=bad):
                money.parse_loose(bad)
        with self.assertRaises(ValueError):
            money.parse_loose("1,5", ";")


if __name__ == "__main__":
    unittest.main()
