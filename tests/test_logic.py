"""Offline tests (no model, no editor needed):  python -m unittest discover tests"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_tutor import brain, winio  # noqa: E402
from ai_tutor.editor import file_from_title, guess_language, plan_edit  # noqa: E402
from ai_tutor import tutor as T  # noqa: E402


class TestPlanEdit(unittest.TestCase):
    def test_question_only_types_after_it(self):
        doc = "/* WAP to check odd or even */"
        target = doc + "\n#include <stdio.h>\nint main() {\n    return 0;\n}\n"
        paste, typ, fixes = plan_edit(doc, target)
        self.assertIsNone(paste)
        self.assertEqual(doc + typ, target)
        self.assertEqual(fixes, [])

    def test_student_mistake_is_fixed_then_rest_typed(self):
        doc = "#include <stdio.h>\nint main() {\n    int n\n    scanf(\"%d\", n);\n"
        target = ("#include <stdio.h>\nint main() {\n    int n;\n    scanf(\"%d\", &n);\n"
                  "    if (n % 2 == 0)\n        printf(\"Even\");\n    else\n        printf(\"Odd\");\n"
                  "    return 0;\n}\n")
        paste, typ, fixes = plan_edit(doc, target)
        self.assertEqual(paste + typ, target)
        self.assertTrue(paste.endswith("&n);\n"))
        self.assertEqual(len(fixes), 1)
        self.assertIn("scanf", fixes[0])

    def test_missing_function_in_middle_is_typed_visibly(self):
        doc = "#include <stdio.h>\n\nint main() {\n    swap();\n}\n"
        target = ("#include <stdio.h>\n\nvoid swap() {\n    int a = 1;\n    int b = 2;\n}\n\n"
                  "int main() {\n    swap();\n}\n")
        paste, typ, fixes = plan_edit(doc, target)
        self.assertEqual(paste + typ, target)
        self.assertTrue(typ.lstrip().startswith("void swap"))
        self.assertEqual(fixes, [])

    def test_identical(self):
        paste, typ, fixes = plan_edit("int x;\n", "int x;\n")
        self.assertIsNone(paste)
        self.assertEqual(typ, "")

    def test_crlf(self):
        paste, typ, _ = plan_edit("a\r\nb\r\n", "a\nb\nc\n")
        self.assertIsNone(paste)
        self.assertEqual(typ, "c\n")


class TestHumanTyping(unittest.TestCase):
    def test_units_rebuild_text_exactly(self):
        from ai_tutor.editor import human_units
        text = 'int main() {\n    printf("Hi\\n");\n\n    return 0;\n}\n<p class="x">a</p>\n'
        units = human_units(text)
        self.assertEqual("".join(s for _, s in units), text)
        keys = {s for k, s in units if k == "key"}
        self.assertTrue(keys <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ "))
        self.assertIn(("paste", "\n    "), units)       # newline + indent in one go
        self.assertIn(("paste", "{"), units)            # brackets never typed as keys
        for _, s in units:                               # no lone > / = " ' (VS Code HTML triggers)
            self.assertFalse(len(s) == 1 and s in ">/=\"'", s)

    def test_next_unit_waits_for_more_text(self):
        from ai_tutor.editor import next_unit
        self.assertIsNone(next_unit("\n", False))            # indent of next line not written yet
        self.assertEqual(next_unit("\n", True), ("paste", "\n"))
        self.assertEqual(next_unit("\n    int", False), ("paste", "\n    "))
        self.assertIsNone(next_unit("=", False))              # glue char needs its neighbour
        self.assertEqual(next_unit('="x"', False), ("paste", '="x'))
        self.assertEqual(next_unit("ab", False), ("key", "a"))

    def test_stream_parser(self):
        raw = "QUESTION: WAP odd even\n```c\n/* WAP odd even */\n" + "".join(f"int a{i};\n" for i in range(12))
        code, closed = brain.code_from_stream(raw + "int b")
        self.assertFalse(closed)
        self.assertTrue(code.startswith("int a0;"))           # question removed, partial line held back
        self.assertTrue(code.endswith("int a11;\n"))
        self.assertEqual(brain.code_from_stream(raw[:40]), (None, False))
        code, closed = brain.code_from_stream(raw + "\n```\nFIXES: none")
        self.assertTrue(closed)

    def test_find_line_insert(self):
        from ai_tutor.editor import find_line_insert
        typed = "#include <stdio.h>\n\nint main() {\n    int *a = malloc(4);\n"
        fixed = "#include <stdio.h>\n#include <stdlib.h>\n\nint main() {\n    int *a = malloc(4);\n    free(a);\n"
        self.assertEqual(find_line_insert(typed, fixed), (19, "#include <stdlib.h>\n"))
        self.assertIsNone(find_line_insert("int a;\nint b;\n", "int a;\nint c;\n"))   # a change, not an insert

    def test_speed_levels(self):
        from ai_tutor.editor import speed_to_cps
        self.assertAlmostEqual(speed_to_cps(1), 2.0)
        self.assertAlmostEqual(speed_to_cps(10), 40.0)
        self.assertTrue(6 < speed_to_cps(5) < 9)
        self.assertEqual(speed_to_cps(99), 40.0)


class TestParsing(unittest.TestCase):
    def test_parse_reply(self):
        reply = ("QUESTION: WAP to check odd or even\n```c\n#include <stdio.h>\nint main() {\n    printf(\"```\");\n"
                 "    return 0;\n}\n```\nFIXES:\n- Line 3: missing semicolon after int n\n")
        code, lang, fixes, q = brain.parse_reply(reply)
        self.assertEqual(q, "WAP to check odd or even")
        self.assertEqual(lang, "c")
        self.assertTrue(code.startswith("#include"))
        self.assertTrue(code.endswith("}\n"))
        self.assertEqual(fixes, ["Line 3: missing semicolon after int n"])

    def test_parse_reply_no_fixes(self):
        code, lang, fixes, q = brain.parse_reply("QUESTION: none\n```html\n<p>hi</p>\n```\nFIXES: none")
        self.assertEqual((code, lang, fixes, q), ("<p>hi</p>\n", "html", [], None))

    def test_strip_question(self):
        q = "WAP to implement heap sort"
        self.assertEqual(brain.strip_question("/*\nWAP to implement heap sort\n*/\n\n#include <stdio.h>\n", q),
                         "#include <stdio.h>\n")
        self.assertEqual(brain.strip_question("// heap sort\nint a;\n", q), "// heap sort\nint a;\n")
        self.assertEqual(brain.strip_question("<!DOCTYPE html>\n<!-- Create a web page -->\n<html>\n",
                                              "Create a web page"), "<!DOCTYPE html>\n<html>\n")

    def test_titles(self):
        self.assertEqual(file_from_title("● quick sort.c - Lab - Visual Studio Code"), ("quick sort.c", "c"))
        self.assertEqual(file_from_title("*form.html - Notepad"), ("form.html", "html"))
        self.assertEqual(file_from_title("Untitled - Notepad"), (None, None))
        self.assertEqual(file_from_title("db.sql - Notepad++"), ("db.sql", "sql"))

    def test_guess_language(self):
        self.assertEqual(guess_language("WAP to implement heap sort"), "c")
        self.assertEqual(guess_language("Create a student registration form with CSS"), "html")
        self.assertEqual(guess_language("Create a database in SQL and perform the queries CREATE, INSERT"), "sql")


class TestCheckers(unittest.TestCase):
    def test_gcc_finds_errors(self):
        tool, problems = brain.check_code("#include <stdio.h>\nint main(){ int n\n scanf(\"%d\", n); return 0; }\n", "c")
        if tool is None:
            self.skipTest("gcc not installed")
        self.assertTrue(problems)

    def test_gcc_clean(self):
        tool, problems = brain.check_code("#include <stdio.h>\nint main(){ int n; scanf(\"%d\", &n); printf(\"%d\", n); return 0; }\n", "c")
        if tool is None:
            self.skipTest("gcc not installed")
        self.assertEqual(problems, [])

    def test_html(self):
        _, problems = brain.check_code("<html><body><div><p>x</p></body></html>", "html")
        self.assertTrue(any("div" in p for p in problems))
        _, ok = brain.check_code("<!DOCTYPE html><html><head><title>t</title></head><body><br><img src=a.png></body></html>", "html")
        self.assertEqual(ok, [])

    def test_js_inside_html(self):
        tool, problems = brain.check_code("<html><body><script>function f( { }</script></body></html>", "html")
        if not __import__("shutil").which("node"):
            self.skipTest("node not installed")
        self.assertTrue(any("script" in p for p in problems))


class FakeTutor:
    def __init__(self):
        self.state = T.IDLE
        self.hotkeys = []
        self.activity = 0

    def on_hotkey(self, a):
        self.hotkeys.append(a)

    def on_locked_key(self):
        pass

    def on_user_activity(self):
        self.activity += 1

    def change_speed(self, d):
        self.hotkeys.append(f"speed{d:+d}")


class TestKeyRouter(unittest.TestCase):
    CTRL, S, TK, Q, BT, A = 0xA2, 0x53, 0x54, 0x51, 0xC0, 0x41

    def make(self):
        tutor = FakeTutor()
        r = T.KeyRouter(tutor, dict(T.DEFAULT_CONFIG))
        r.injected = []
        r.inject_q.put = lambda ev: r.injected.append(ev)  # capture instead of sending
        return tutor, r

    def keys(self, r, seq):
        return [r.handle(vk, down, False, 0) for vk, down in seq]

    def test_ctrl_s_t_fires_and_swallows(self):
        tutor, r = self.make()
        out = self.keys(r, [(self.CTRL, 1), (self.S, 1), (self.TK, 1), (self.TK, 0), (self.S, 0), (self.CTRL, 0)])
        self.assertEqual(tutor.hotkeys, ["start"])
        self.assertEqual(out, [False, True, True, True, True, False])
        self.assertEqual(r.injected, [])  # nothing replayed -> no Save dialog

    def test_plain_ctrl_s_still_saves(self):
        tutor, r = self.make()
        self.keys(r, [(self.CTRL, 1), (self.S, 1), (self.S, 0)])
        time.sleep(r.GRACE + 0.15)
        self.assertEqual(r.injected, [[(self.S, False), (self.S, True)]])
        self.assertEqual(tutor.hotkeys, [])

    def test_ctrl_released_quickly_replays_in_order(self):
        tutor, r = self.make()
        self.keys(r, [(self.CTRL, 1), (self.S, 1), (self.S, 0), (self.CTRL, 0)])
        self.assertEqual(r.injected, [[(self.S, False), (self.S, True), (self.CTRL, True)]])

    def test_tap_s_then_t_within_grace(self):
        tutor, r = self.make()
        self.keys(r, [(self.CTRL, 1), (self.S, 1), (self.S, 0), (self.TK, 1), (self.TK, 0), (self.CTRL, 0)])
        self.assertEqual(tutor.hotkeys, ["start"])
        time.sleep(r.GRACE + 0.15)
        self.assertEqual(r.injected, [])

    def test_ctrl_s_other_key(self):
        tutor, r = self.make()
        self.keys(r, [(self.CTRL, 1), (self.S, 1), (self.A, 1)])
        self.assertEqual(r.injected, [[(self.S, False), (self.A, False)]])

    def test_stop_chord(self):
        tutor, r = self.make()
        self.keys(r, [(self.CTRL, 1), (self.S, 1), (self.Q, 1), (self.Q, 0), (self.S, 0), (self.CTRL, 0)])
        self.assertEqual(tutor.hotkeys, ["stop"])

    def test_close_chord(self):
        tutor, r = self.make()
        E, N = 0x45, 0x4E
        out = self.keys(r, [(self.CTRL, 1), (E, 1), (N, 1), (N, 0), (E, 0), (self.CTRL, 0)])
        self.assertEqual(tutor.hotkeys, ["close"])
        self.assertEqual(out, [False, True, True, True, True, False])
        self.keys(r, [(self.CTRL, 1), (E, 1), (E, 0), (self.CTRL, 0)])   # plain Ctrl+E still works
        self.assertEqual(r.injected, [[(E, False), (E, True), (self.CTRL, True)]])

    def test_backtick_only_during_session(self):
        tutor, r = self.make()
        self.assertEqual(self.keys(r, [(self.BT, 1), (self.BT, 0)]), [False, False])  # idle: types `
        tutor.state = T.TYPING
        self.assertEqual(self.keys(r, [(self.BT, 1), (self.BT, 0)]), [True, True])
        self.assertEqual(tutor.hotkeys, ["pause"])
        tutor.state = T.PAUSED
        self.assertEqual(self.keys(r, [(self.A, 1), (self.A, 0)]), [False, False])  # you can type while paused
        self.keys(r, [(self.BT, 1), (self.BT, 0)])
        self.assertEqual(tutor.hotkeys, ["pause", "pause"])

    def test_keyboard_locked_while_typing(self):
        tutor, r = self.make()
        tutor.state = T.TYPING
        self.assertEqual(self.keys(r, [(self.A, 1), (self.A, 0)]), [True, True])

    def test_numpad_speed_keys(self):
        tutor, r = self.make()
        self.assertEqual(self.keys(r, [(0x6B, 1), (0x6B, 0)]), [False, False])   # idle: normal +
        tutor.state = T.TYPING
        self.assertEqual(self.keys(r, [(0x6B, 1), (0x6B, 0), (0x6D, 1), (0x6D, 0)]), [True] * 4)
        self.assertEqual(tutor.hotkeys, ["speed+1", "speed-1"])
        tutor.state = T.PAUSED
        self.assertEqual(self.keys(r, [(0x6D, 1), (0x6D, 0)]), [False, False])   # your turn: normal -

    def test_injected_keys_ignored(self):
        tutor, r = self.make()
        tutor.state = T.TYPING
        self.assertFalse(r.handle(self.A, True, True, winio.INJECT_MARK))


class TestHardware(unittest.TestCase):
    def test_choose_model(self):
        cfg = {"model": "auto", "device": "auto"}
        self.assertEqual(brain.choose_model({"gpu_name": "RTX 4050", "vram_mb": 6141, "ram_gb": 16}, cfg), "qwen2.5-coder:7b")
        self.assertEqual(brain.choose_model({"gpu_name": "RTX 4080", "vram_mb": 12282, "ram_gb": 32}, cfg), "qwen2.5-coder:14b")
        self.assertEqual(brain.choose_model({"gpu_name": None, "vram_mb": 0, "ram_gb": 16}, cfg), "qwen2.5-coder:7b")
        self.assertEqual(brain.choose_model({"gpu_name": None, "vram_mb": 0, "ram_gb": 8}, cfg), "qwen2.5-coder:3b")
        self.assertEqual(brain.choose_model({"gpu_name": "RTX 5060", "vram_mb": 8151, "ram_gb": 16}, cfg), "qwen2.5-coder:7b")
        self.assertEqual(brain.choose_model({"gpu_name": "RTX 3050", "vram_mb": 4096, "ram_gb": 8}, cfg), "qwen2.5-coder:7b")
        self.assertEqual(brain.choose_model({"gpu_name": "RTX 5090", "vram_mb": 32607, "ram_gb": 64}, cfg), "qwen2.5-coder:32b")
        self.assertEqual(brain.choose_model({"gpu_name": "RTX 4050", "vram_mb": 6141, "ram_gb": 16},
                                            {"model": "auto", "device": "cpu"}), "qwen2.5-coder:7b")


if __name__ == "__main__":
    unittest.main()
