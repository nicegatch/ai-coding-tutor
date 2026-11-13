"""
Talks to whatever editor is in front (VS Code, Notepad, Notepad++, ...).

Reading : Ctrl+A, Ctrl+C  -> exact text of the whole file (better than OCR,
          which gets brackets / semicolons / indentation wrong).
Writing : the answer is "typed" by pasting it line by line. Pasting is exact,
          while real key presses get mangled by VS Code's auto-indent,
          auto-closing brackets/tags and IntelliSense pop-ups.
"""
import difflib
import random
import re
import time

from . import winio

# --------------------------------------------------------------------------- text helpers


def normalize(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


LANG_BY_EXT = {
    "c": "c", "h": "c", "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp",
    "java": "java", "py": "python", "js": "javascript", "html": "html", "htm": "html",
    "css": "css", "sql": "sql", "txt": None, "md": None,
}
_FILE_RE = re.compile(r"([\w\-. ()\[\]]+?\.(" + "|".join(LANG_BY_EXT) + r"))\b", re.I)


def file_from_title(title):
    """'● quicksort.c - Lab - Visual Studio Code' -> ('quicksort.c', 'c')."""
    title = (title or "").lstrip("●*• ").strip()
    m = _FILE_RE.search(title)
    if not m:
        return None, None
    name = m.group(1).strip()
    return name, LANG_BY_EXT.get(m.group(2).lower())


def guess_language(doc):
    """Best guess when the file has no extension (e.g. an untitled Notepad tab)."""
    d = doc.lower()
    if "#include" in d:
        return "cpp" if ("iostream" in d or "cout" in d or "using namespace" in d) else "c"
    if re.search(r"<\s*(html|body|form|div|p|h1|table|input)\b", d) or re.search(
            r"\b(html|web ?page|hyperlink|registration form|css|tag)\b", d):
        return "html"
    if re.search(r"\b(sql|database|query|queries|create table|select \*|insert into)\b", d):
        return "sql"
    if re.search(r"\bpublic\s+static\s+void\s+main\b|\bjava\b", d):
        return "java"
    if re.search(r"\bpython\b|^\s*def \w+\(|print\(", d, re.M):
        return "python"
    if re.search(r"\bjavascript\b|\bjs\b", d):
        return "javascript"
    if re.search(r"\bc\+\+\b|\bcpp\b", d):
        return "cpp"
    # Lab default: data-structure programs ("WAP to ...") are written in C
    return "c"


def plan_edit(doc, target, min_new_block=3):
    """Decide how to turn `doc` into `target` with plain paste operations.

    Returns (paste_text, type_text, fixes):
      paste_text : what the document must contain *instantly* (the student's
                   part with mistakes already fixed). None = no change needed,
                   keep the document and just go to the end.
      type_text  : what is then typed at the end, line by line.
      fixes      : list of human-readable "Line n: old -> new" changes made to
                   the student's existing text.
    """
    doc = normalize(doc)
    target = normalize(target)
    if target.startswith(doc):
        return None, target[len(doc):], []

    a = doc.splitlines(keepends=True)
    b = target.splitlines(keepends=True)
    ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        # A "replace" that also adds many lines is usually "fix the student's
        # lines, then continue": split it into a fix part and a new part.
        k = i2 - i1
        if tag == "replace" and (j2 - j1) - k >= min_new_block:
            sim = sum(difflib.SequenceMatcher(None, a[i1 + n].strip(), b[j1 + n].strip()).ratio()
                      for n in range(k)) / k
            if sim >= 0.5:
                ops.append(("replace", i1, i2, j1, j1 + k))
                ops.append(("insert", i2, i2, j1 + k, j2))
                continue
        ops.append((tag, i1, i2, j1, j2))

    # Where does the "new" part (that should be typed visibly) start?
    split_line = len(b)
    for tag, i1, i2, j1, j2 in ops:
        if tag in ("insert", "replace") and (j2 - j1) - (i2 - i1) >= min_new_block:
            split_line = j1
            break
    else:
        if ops and ops[-1][0] == "insert":
            split_line = ops[-1][3]

    fixes = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        old = "".join(a[i1:i2]).strip()
        new = "".join(b[j1:j2]).strip()
        if tag == "insert" and j1 >= split_line:
            continue  # brand new code, not a fix
        if tag == "insert":
            fixes.append(f"Line {i1 + 1}: added `{_short(new)}`")
        elif tag == "delete":
            fixes.append(f"Line {i1 + 1}: removed `{_short(old)}`")
        elif old != new:
            fixes.append(f"Line {i1 + 1}: `{_short(old)}` -> `{_short(new)}`")
        elif "".join(a[i1:i2]) != "".join(b[j1:j2]):
            fixes.append(f"Line {i1 + 1}: fixed spacing / indentation")

    paste_text = "".join(b[:split_line])
    type_text = "".join(b[split_line:])
    if paste_text == doc:
        paste_text = None
    return paste_text, type_text, fixes


def _short(s, n=60):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def looks_like_code(doc):
    """False when the document is only a question in plain words."""
    return bool(re.search(r"[;{}<>=#]|\w\s*\(.*\)", doc))


# --------------------------------------------------------------------------- human-like typing

SPEED_MIN, SPEED_MAX = 1, 10
PASTE_GAP = 0.07  # never change the clipboard sooner than this after a paste


def speed_to_cps(level):
    """Speed level 1..10 -> characters per second (2 .. 40, level 5 is about 8)."""
    level = max(SPEED_MIN, min(SPEED_MAX, int(level)))
    return 2.0 * (20 ** ((level - 1) / 9))


_UNIT_RE = re.compile(r"\n[ \t]*|.", re.S)


def human_units(text):
    """Split text into ('key', ch) units typed as real key presses (letters,
    digits, _ and spaces: these never trigger editor auto-features) and
    ('paste', s) units for everything else. A newline is pasted together with
    the next line's indentation, so the editor's auto-indent can't double it,
    and brackets / quotes / < > are pasted so auto-closing can't add extras."""
    raw = []
    for m in _UNIT_RE.finditer(text):
        s = m.group()
        if len(s) == 1 and ((s.isascii() and s.isalnum()) or s in "_ "):
            raw.append(("key", s))
        else:
            raw.append(("paste", s))
    # VS Code's HTML support reacts to a lone ">", "/" or "=" landing in the
    # file (even pasted) by adding </tag> or "". Glue such characters to the
    # next one so the editor never sees them arrive on their own.
    out, i = [], 0
    while i < len(raw):
        kind, s = raw[i]
        while kind == "paste" and s[-1] in _GLUE and i + 1 < len(raw):
            i += 1
            s += raw[i][1]
        out.append((kind, s))
        i += 1
    return out


_GLUE = set(">/=\"'")


def find_line_insert(doc, target, max_lines=6):
    """If `target` == doc[:pos] + inserted_whole_lines + doc[pos:] + more,
    return (pos, inserted_lines); else None."""
    common = 0
    for x, y in zip(doc, target):
        if x != y:
            break
        common += 1
    pos = doc.rfind("\n", 0, common) + 1           # start of the line where they differ
    if not target.startswith(doc[:pos]):
        return None
    tail, rest = doc[pos:], target[pos:]
    j = rest.find(tail) if tail else -1
    if j <= 0:
        return None
    ins = rest[:j]
    if not ins.endswith("\n") or ins.count("\n") > max_lines:
        return None
    return pos, ins


def next_unit(rest, final):
    """The next unit to type from `rest`, or None if we must wait for more
    text (so a newline is always sent together with the next line's indent,
    and a glue character together with the character after it)."""
    if not rest:
        return None
    units = human_units(rest[:400])
    if not units:
        return None
    kind, s = units[0]
    if not final and len(s) == len(rest):
        # this unit reaches the very end of what has been written so far:
        # it may still grow (indent of a line not written yet, glued char)
        if s.startswith("\n") or s[-1] in _GLUE:
            return None
    return kind, s


_KEY_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]


def neighbor_key(ch, rnd=random):
    low = ch.lower()
    for row in _KEY_ROWS:
        i = row.find(low)
        if i >= 0:
            n = rnd.choice([row[j] for j in (i - 1, i + 1) if 0 <= j < len(row)])
            return n.upper() if ch.isupper() else n
    return None


def human_delay(kind, s, prev, cps, rnd=random):
    """Pause after typing unit `s`, like a person: uneven rhythm, a breath at
    the end of each line, a longer think before a new block."""
    d = (1.0 / cps) * rnd.lognormvariate(0, 0.35)
    slow = (8.0 / cps) ** 0.7          # thinking pauses shrink when you speed it up
    if s.startswith("\n"):
        d += rnd.uniform(0.15, 0.40) * slow
        if prev == "{" or prev.startswith("\n"):
            d += rnd.uniform(0.25, 0.70) * slow
    elif s == " ":
        d *= 1.25
    elif s in ";,)":
        d += rnd.uniform(0.02, 0.10) * slow
    if rnd.random() < 0.025:
        d += rnd.uniform(0.15, 0.55) * slow  # small hesitation
    return d


def split_chunks(text, mode="line"):
    if mode == "word":
        return [c for c in re.findall(r"\S+\s*|\s+", text) if c]
    return text.splitlines(keepends=True)


# --------------------------------------------------------------------------- editor control


class FocusLost(Exception):
    """The editor is no longer the window in front."""


class Editor:
    def __init__(self, hwnd, cfg, log):
        self.hwnd = hwnd
        self.cfg = cfg
        self.log = log
        self.title = winio.window_title(hwnd)
        self.process = winio.window_process_name(hwnd)
        self.filename, self.lang = file_from_title(self.title)

    # ---- safety
    def in_front(self):
        return winio.foreground_window() == self.hwnd

    def ensure_front(self):
        if not self.in_front():
            raise FocusLost()

    # ---- primitives
    def _paste(self, text):
        winio.set_clipboard_text(text.replace("\n", "\r\n"))
        time.sleep(0.015)
        winio.press_combo("ctrl", "v")

    def read_all(self):
        """Exact text of the whole document (selection is collapsed afterwards)."""
        self.ensure_front()
        before = winio.clipboard_seq()
        winio.press_combo("ctrl", "a")
        time.sleep(0.05)
        winio.press_combo("ctrl", "c")
        changed = False
        end = time.time() + 2.0
        while time.time() < end:
            if winio.clipboard_seq() != before:
                changed = True
                break
            time.sleep(0.02)
        text = ""
        if changed:
            time.sleep(0.03)
            text = normalize(winio.get_clipboard_text() or "")
        # collapse the selection so a stray key press can't wipe the file
        winio.press_combo("ctrl", "end")
        time.sleep(0.03)
        if not text.strip():
            text = ""
        return text

    def replace_all(self, text):
        self.ensure_front()
        winio.press_combo("ctrl", "a")
        time.sleep(0.05)
        if text:
            self._paste(text)
        else:
            winio.press_combo("delete")
        time.sleep(self._settle(text))

    def go_end(self):
        self.ensure_front()
        winio.press_combo("ctrl", "end")
        time.sleep(0.04)

    def type_text(self, text, should_stop, on_progress=None):
        """Paste `text` chunk by chunk at the cursor.

        should_stop() is polled between chunks; when it returns a reason the
        typing stops. Returns (typed_chars, stop_reason or None).
        """
        if self.cfg.get("typing_style", "human") == "human":
            return self._type_human(text, should_stop, on_progress)
        typed = 0
        chunks = split_chunks(text, self.cfg.get("typing_chunk", "line"))
        char_delay = self.cfg.get("typing_ms_per_char", 14) / 1000.0
        min_delay = self.cfg.get("typing_min_gap_ms", 110) / 1000.0
        for chunk in chunks:
            reason = should_stop()
            if reason:
                return typed, reason
            if not self.in_front():
                return typed, "focus"
            self._paste(chunk)
            typed += len(chunk)
            if on_progress:
                on_progress(typed)
            time.sleep(max(min_delay, min(0.5, len(chunk) * char_delay)))
        time.sleep(0.15)
        return typed, None

    def _type_human(self, text, should_stop, on_progress):
        """Fixed text (no streaming): same engine as type_stream."""
        done, reason = self.type_stream("", lambda: (text, True), should_stop,
                                        lambda d: on_progress(len(d)) if on_progress else None)
        return len(done), reason

    def type_stream(self, doc, get_target, should_stop, on_progress=None):
        """Type towards a target that may still be growing while the AI writes it.

        doc          : what the document holds right now (a prefix of the target)
        get_target() : -> (target_text or None, is_final). The text only grows,
                       except when the finished answer was corrected by the
                       compiler check; then what was already typed is fixed up.
        Character by character with a human rhythm; the speed is read live from
        cfg['typing_speed'] so numpad + / - take effect immediately.
        Returns (document_text_now, stop_reason or None)."""
        self._rnd = random.Random()
        self._prev, self._last_paste = "", 0.0
        while True:
            reason = should_stop()
            if reason:
                return doc, reason
            if not self.in_front():
                return doc, "focus"
            target, final = get_target()
            if target is None:
                time.sleep(0.05)
                continue
            if not target.startswith(doc):
                doc = self._correct(doc, target)
                if on_progress:
                    on_progress(doc)
                continue
            rest = target[len(doc):]
            unit = next_unit(rest, final)
            if unit is None:
                if final and not rest:
                    time.sleep(0.2)
                    return doc, None
                time.sleep(0.05)            # typing caught up with the AI's writing
                continue
            self._emit(*unit)
            doc += unit[1]
            if on_progress:
                on_progress(doc)

    def _emit(self, kind, s):
        """Type one unit like a person (with the odd typo), then pause naturally."""
        rnd, prev = self._rnd, self._prev
        cps = speed_to_cps(self.cfg.get("typing_speed", 5))
        if kind == "key":
            if (self.cfg.get("typos", True) and s.isalpha() and prev.isalpha() and cps < 20
                    and rnd.random() < self.cfg.get("typo_rate", 0.02)):
                wrong = neighbor_key(s, rnd)
                if wrong:                       # oops... notice it, fix it
                    winio.type_char(wrong)
                    time.sleep(rnd.uniform(0.15, 0.4))
                    winio.press_combo("backspace")
                    time.sleep(rnd.uniform(0.08, 0.18))
            winio.type_char(s)
        else:
            wait = PASTE_GAP - (time.time() - self._last_paste)
            if wait > 0:
                time.sleep(wait)
            self._paste(s)
            self._last_paste = time.time()
        time.sleep(human_delay(kind, s, prev, cps, rnd))
        self._prev = s

    def _correct(self, doc, target):
        """The checked answer differs from what was already typed (e.g. the
        compiler check added a missing #include). If whole lines were only
        added higher up, go there and type them, like a teacher would;
        otherwise fix the typed part in one go."""
        found = find_line_insert(doc, target)
        if found:
            pos, ins = found
            line = doc[:pos].count("\n")
            before = doc[:pos].split("\n")
            if line <= 60 and all(len(l) < 70 for l in before):   # short lines: arrow keys are exact
                self.log(f"going back to line {line + 1} to add {ins.count(chr(10))} line(s)")
                time.sleep(self._rnd.uniform(0.3, 0.7))
                winio.press_combo("ctrl", "home")
                time.sleep(0.15)
                for _ in range(line):
                    winio.press_combo("down")
                    time.sleep(0.03)
                time.sleep(0.2)                      # column 1 is kept while moving down
                for kind, s in human_units(ins):
                    self._emit(kind, s)
                time.sleep(self._rnd.uniform(0.2, 0.5))
                winio.press_combo("ctrl", "end")
                time.sleep(0.15)
                return doc[:pos] + ins + doc[pos:]
        paste, _, _ = plan_edit(doc, target)
        if paste is None:
            paste = ""
        self.log(f"answer changed while typing; correcting {len(doc)} typed chars")
        self.replace_all(paste)
        return paste

    @staticmethod
    def _settle(text):
        return 0.15 + min(0.6, len(text) / 20000.0)
