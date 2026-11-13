"""
The AI part: picks the right model for this computer, runs it through Ollama
(NVIDIA GPU when there is one, otherwise the CPU + RAM), builds the prompts
and double-checks every answer with a real compiler before it is typed.
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

from . import winio

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# --------------------------------------------------------------------------- hardware


def detect_hardware():
    """Returns dict(gpu_name, vram_mb, ram_gb, cpu_threads)."""
    info = {"gpu_name": None, "vram_mb": 0, "ram_gb": 0.0, "cpu_threads": os.cpu_count() or 4}
    info["ram_gb"] = round(winio.get_total_ram_gb()[0], 1)
    smi = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        out = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        best = None
        for line in out.stdout.strip().splitlines():
            name, mem = [x.strip() for x in line.rsplit(",", 1)]
            if best is None or int(mem) > best[1]:
                best = (name, int(mem))
        if best:
            info["gpu_name"], info["vram_mb"] = best
    except Exception:
        pass
    return info


MODEL_LADDER = {
    # name               : (download GB, VRAM (MB) needed to run fully on the GPU, RAM (GB) needed on CPU)
    "qwen2.5-coder:32b": (20.0, 22000, 40),
    "qwen2.5-coder:14b": (9.0, 11000, 20),
    "qwen2.5-coder:7b": (4.7, 5000, 10),
    "qwen2.5-coder:3b": (1.9, 2500, 5),
}


def choose_model(hw, cfg):
    """Pick the most accurate model this computer runs at a usable speed.

    NVIDIA card (any RTX / GTX, e.g. RTX 5060 8 GB): the biggest model that
    fits completely in the card's memory; a small card (4 GB) still runs the
    7B model, sharing the rest with RAM, which is faster than CPU alone.
    No NVIDIA card: the model runs on the CPU from RAM. 7B is the best fit:
    bigger models are too slow on a CPU to keep up with typing."""
    if cfg.get("model", "auto") != "auto":
        return cfg["model"]
    vram, ram = hw["vram_mb"], hw["ram_gb"]
    if hw["gpu_name"] and cfg.get("device", "auto") != "cpu":
        if vram >= 22000 and ram >= 24:
            return "qwen2.5-coder:32b"      # RTX 3090 / 4090 / 5090 class
        if vram >= 11000:
            return "qwen2.5-coder:14b"      # 12-16 GB cards (RTX 3060 12GB, 4070, 5070 Ti ...)
        if vram >= 3500 and ram >= 8:
            return "qwen2.5-coder:7b"       # 4-8 GB cards (RTX 3050, 4060, 5060 ...)
        return "qwen2.5-coder:3b"
    # CPU + RAM only (slower, but works on any PC)
    if ram >= 10:
        return "qwen2.5-coder:7b"
    return "qwen2.5-coder:3b"


def fallback_model(ollama, wanted):
    """If the chosen model isn't downloaded (e.g. the hardware changed), use the
    best one that is."""
    if ollama.has_model(wanted):
        return wanted
    for name in MODEL_LADDER:
        if ollama.has_model(name):
            return name
    return None


def device_label(hw, cfg, vram_used=None):
    if cfg.get("device") == "cpu" or not hw["gpu_name"]:
        return f"CPU + RAM ({hw['ram_gb']:.0f} GB)"
    label = f"{hw['gpu_name'].replace('NVIDIA ', '').replace('GeForce ', '')} ({hw['vram_mb'] / 1024:.0f} GB)"
    return label


# --------------------------------------------------------------------------- ollama


class Ollama:
    def __init__(self, cfg, log):
        self.base = cfg.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
        self.cfg = cfg
        self.log = log

    def _req(self, path, payload=None, timeout=10):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=timeout)

    def alive(self):
        try:
            with self._req("/api/version", timeout=3) as r:
                return json.load(r).get("version")
        except Exception:
            return None

    def ensure_running(self, wait=40):
        if self.alive():
            return True
        exe = shutil.which("ollama") or os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
        if not os.path.exists(exe):
            raise RuntimeError("Ollama is not installed. Run setup.bat first.")
        self.log(f"starting ollama server: {exe}")
        subprocess.Popen([exe, "serve"], creationflags=NO_WINDOW | 0x00000008,  # DETACHED_PROCESS
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + wait
        while time.time() < end:
            if self.alive():
                return True
            time.sleep(0.5)
        raise RuntimeError("Ollama server did not start")

    def has_model(self, model):
        try:
            with self._req("/api/tags") as r:
                names = {m["name"] for m in json.load(r).get("models", [])}
        except Exception:
            return False
        return model in names or (model + ":latest") in names

    def pull(self, model, progress=print):
        with self._req("/api/pull", {"model": model, "stream": True}, timeout=None) as r:
            last = ""
            for line in r:
                ev = json.loads(line)
                if "error" in ev:
                    raise RuntimeError(ev["error"])
                status = ev.get("status", "")
                if ev.get("total"):
                    pct = 100 * ev.get("completed", 0) / ev["total"]
                    status = f"{status} {pct:5.1f}%"
                if status != last:
                    progress(status)
                    last = status

    def gpu_share(self, model):
        """Fraction of the loaded model that sits in GPU memory (None = not loaded)."""
        try:
            with self._req("/api/ps") as r:
                for m in json.load(r).get("models", []):
                    if m.get("name", "").startswith(model) and m.get("size"):
                        return m.get("size_vram", 0) / m["size"]
        except Exception:
            pass
        return None

    def options(self):
        opts = {"temperature": self.cfg.get("temperature", 0.1), "top_p": 0.9,
                "num_ctx": self.cfg.get("context_tokens", 8192),
                "num_predict": self.cfg.get("max_answer_tokens", 4096),
                "seed": 7, "repeat_penalty": 1.05}
        if self.cfg.get("_force_cpu"):
            opts["num_gpu"] = 0
        return opts

    def warm_up(self, model):
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": False,
                   "keep_alive": self.cfg.get("keep_model_loaded", "30m"),
                   "options": dict(self.options(), num_predict=1)}
        with self._req("/api/chat", payload, timeout=600) as r:
            r.read()

    def unload(self, model):
        """Free the GPU / RAM the model is using."""
        with self._req("/api/generate", {"model": model, "keep_alive": 0}, timeout=30) as r:
            r.read()

    def chat(self, model, messages, should_cancel=lambda: False, on_token=None, on_text=None):
        """Streams the reply. Returns the full text, or None if cancelled."""
        payload = {"model": model, "messages": messages, "stream": True,
                   "keep_alive": self.cfg.get("keep_model_loaded", "30m"),
                   "options": self.options()}
        parts, n = [], 0
        with self._req("/api/chat", payload, timeout=900) as r:
            for line in r:
                if should_cancel():
                    return None
                ev = json.loads(line)
                if "error" in ev:
                    raise RuntimeError(ev["error"])
                piece = ev.get("message", {}).get("content", "")
                if piece:
                    parts.append(piece)
                    n += 1
                    if on_token:
                        on_token(n)
                    if on_text:
                        on_text("".join(parts))
                if ev.get("done"):
                    break
        return "".join(parts)


# --------------------------------------------------------------------------- prompts

FENCE = {"c": "c", "cpp": "cpp", "java": "java", "python": "python", "javascript": "javascript",
         "html": "html", "css": "css", "sql": "sql"}
COMMENT = {"c": "/* ... */", "cpp": "// ...", "java": "/* ... */", "javascript": "// ...",
           "css": "/* ... */", "python": "# ...", "html": "<!-- ... -->", "sql": "-- ..."}
LANG_NAMES = {"c": "C", "cpp": "C++", "java": "Java", "python": "Python",
              "javascript": "JavaScript", "html": "HTML (with CSS/JavaScript inside)",
              "css": "CSS", "sql": "SQL (MySQL)"}

SYSTEM_PROMPT = """You are AI Tutor, an expert programming teacher. A college student practises lab programs with you: C programs (arrays, searching, sorting such as quick sort and heap sort, linked lists, circular and doubly linked lists, binary search trees, menu-driven programs), HTML/CSS/JavaScript web pages and forms, SQL queries, and sometimes C++, Java or Python.

Your code must be 100% correct: complete, compiles without errors or warnings, and gives the right output on the first run.

Style rules:
- Simple, standard, textbook style that the student can write in a lab record and explain in a viva.
- C: standard C with <stdio.h>, <stdlib.h>, <string.h> only. Use int main() with return 0. Read input with scanf and print clear prompts. Never use conio.h, getch() or clrscr(). Check malloc results and free memory you allocate.
- HTML: a complete <!DOCTYPE html> page with <html lang="en">, <head> (<meta charset="UTF-8">, <title>) and <body>. Close every tag. Put CSS in a <style> block and JavaScript in a <script> block unless the student already uses separate files.
- SQL: MySQL syntax, every statement ends with a semicolon, a short -- comment above each statement.
- Add short, helpful comments. No long explanations inside the code."""

START_TEMPLATE = """FILE: {filename}    LANGUAGE: {language}
{question_block}
This is the student's document exactly as it is right now (between the markers):
<<<DOCUMENT
{doc}
DOCUMENT>>>
{plan_block}
Your job:
1. Work out what the student wants: the question (written in the document, or given above) and any code the student has already written.
2. Return the COMPLETE final version of the document with the full, correct answer.
3. REMOVE THE QUESTION from the document. The final document must contain only the program itself: no question text, not even as a comment. Put the question in the QUESTION line of your reply instead.
4. Keep every line of code the student wrote that is already correct EXACTLY as it is: same names, spacing and order. Change only what is wrong.
5. Fix every mistake in the student's code: syntax errors, wrong logic, wrong output, missing parts.
6. Finish the program so that it completely solves the question. If there is no question at all, work out what the student is building from their code and finish it as a complete, runnable program (for C/C++/Java this always includes main()).
7. Do not add comments like "Fixed" or "Added" to the code. Explain your fixes only in the FIXES list.

Reply in exactly this format and nothing else:
QUESTION: <the student's question in one line, copied as written; "none" if there is no question>
```{fence}
<the complete final document, without the question>
```
FIXES:
- <one short line for each mistake you fixed in the student's code: what was wrong and why>
Write "FIXES: none" if the student's code had no mistakes or there was no code yet."""

QUESTION_BLOCK = """
The student's question for this file (they asked it earlier; it has already been removed from the file):
<<<QUESTION
{question}
QUESTION>>>
If the document now contains a different, new question, the new one is what the student wants.
"""

PLAN_BLOCK = """
Before the student took over, you were typing this complete solution:
<<<YOUR_PLAN
{plan}
YOUR_PLAN>>>
The student paused you and then edited the document. Keep the student's correct changes (even if they differ from your plan), fix their mistakes, and finish the program.
"""

REPAIR_TEMPLATE = """I checked your answer with {tool} and it reported these problems:

{errors}

Fix them. Change only the lines needed to fix these problems and keep every other line exactly the same. Reply again in exactly the same format (QUESTION line, one ```{fence} code block, then FIXES:)."""


def build_messages(doc, filename, lang, plan=None, question=None):
    language = LANG_NAMES.get(lang, "unknown - work it out from the question")
    fence = FENCE.get(lang, "")
    plan_block = PLAN_BLOCK.format(plan=plan.rstrip("\n")) if plan else ""
    question_block = QUESTION_BLOCK.format(question=question.strip()) if question else ""
    user = START_TEMPLATE.format(filename=filename or "untitled", language=language,
                                 doc=doc.rstrip("\n") or "(empty)", plan_block=plan_block,
                                 question_block=question_block, fence=fence or "<language>")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


_COMMENT_MARKS = re.compile(r"^\s*(/\*+|\*/|\*|//+|<!--|-->|--|#)\s*|\s*(\*/|-->)\s*$")


def _plain(line):
    prev = None
    while prev != line:
        prev, line = line, _COMMENT_MARKS.sub("", line)
    return " ".join(line.lower().split())


def strip_question(code, question, look=10):
    """Safety net: remove the question if the model left it in (often as a comment)."""
    if not question:
        return code
    q = _plain(question)
    if len(q) < 8:
        return code
    lines = code.split("\n")
    drop = set()
    for i, line in enumerate(lines[:look]):
        p = _plain(line)
        if len(p) >= max(8, 0.6 * len(q)) and (p in q or difflib.SequenceMatcher(None, p, q).ratio() > 0.8):
            drop.add(i)
    if not drop:
        return code
    lo, hi = min(drop), max(drop)
    # take the comment's own opening / closing lines with it
    while lo > 0 and lines[lo - 1].strip() in ("/*", "/**", "<!--", "--", "//", "#"):
        lo -= 1
    while hi + 1 < len(lines) and lines[hi + 1].strip() in ("*/", "-->"):
        hi += 1
    for k in range(lo, hi + 1):
        if k in drop or not _plain(lines[k]):
            drop.add(k)
    kept = [l for k, l in enumerate(lines) if k not in drop]
    while kept and not kept[0].strip():
        kept.pop(0)
    return "\n".join(kept)


_LANG_ALIASES = {"c": "c", "h": "c", "cpp": "cpp", "c++": "cpp", "cc": "cpp", "java": "java",
                 "python": "python", "py": "python", "javascript": "javascript", "js": "javascript",
                 "html": "html", "htm": "html", "css": "css", "sql": "sql", "mysql": "sql"}


def parse_reply(reply):
    """-> (code, fence_lang, fixes_list, question). code is None if no code block found."""
    lines = reply.replace("\r\n", "\n").split("\n")
    start = next((i for i, l in enumerate(lines) if l.strip().startswith("```")), None)
    question = None
    for l in lines[:start if start is not None else len(lines)]:
        s = l.strip().lstrip("*# ").strip()
        if s.upper().startswith("QUESTION"):
            q = s.split(":", 1)[1].strip().strip("*").strip() if ":" in s else ""
            if q and q.lower().strip(".") not in ("none", "n/a", "no question"):
                question = q
    if start is None:
        return None, None, [], question
    tag = lines[start].strip()[3:].strip().lower()
    fixes_at = next((i for i in range(len(lines) - 1, start, -1)
                     if lines[i].strip().upper().startswith("FIXES")), len(lines))
    end = next((i for i in range(fixes_at - 1, start, -1) if lines[i].strip() == "```"), None)
    if end is None:
        end = fixes_at
    code = "\n".join(lines[start + 1:end]).rstrip() + "\n"
    fixes = []
    for l in lines[fixes_at:]:
        s = l.strip()
        if s.upper().startswith("FIXES"):
            s = s.split(":", 1)[1].strip() if ":" in s else ""
        s = s.lstrip("-*• ").strip()
        low = s.lower()
        if (s and low not in ("none", "none.", "no mistakes", "n/a") and "already correct" not in low
                and "question" not in low):
            fixes.append(s)
    return code, _LANG_ALIASES.get(tag), fixes, question


def code_from_stream(raw, question=None, min_lines=12):
    """The code typed so far by the model, while it is still writing.

    Only complete lines are returned (a line never changes once it has its
    newline), trailing blank lines are held back, and nothing is returned
    until there are `min_lines` lines, so the question safety-net (which looks
    at the first lines) gives a stable result. -> (code or None, closed)."""
    lines = raw.replace("\r\n", "\n").split("\n")[:-1]   # drop the unfinished last line
    start = next((i for i, l in enumerate(lines) if l.strip().startswith("```")), None)
    if start is None:
        return None, False
    for l in lines[:start]:
        t = l.strip().lstrip("*# ").strip()
        if t.upper().startswith("QUESTION") and ":" in t:
            q = t.split(":", 1)[1].strip().strip("*").strip()
            if q and q.lower().strip(".") not in ("none", "n/a", "no question"):
                question = q
    code_lines, closed = [], False
    for l in lines[start + 1:]:
        if l.strip() == "```":
            closed = True
            break
        code_lines.append(l)
    if not closed and len(code_lines) < min_lines:
        return None, False
    code = strip_question("\n".join(code_lines) + "\n", question)
    code = code.rstrip() + "\n" if code.strip() else ""
    return code, closed


# --------------------------------------------------------------------------- checks

_IMPORTANT_C_WARNINGS = ("implicit declaration", "format", "uninitialized", "control reaches end",
                         "return type", "suggest parentheses", "makes pointer from integer",
                         "makes integer from pointer", "incompatible", "no return statement",
                         "array subscript", "missing terminating", "comparison between pointer")


def _run(cmd, cwd=None, timeout=30):
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout,
                       creationflags=NO_WINDOW, errors="replace")
    return p.returncode, (p.stdout + p.stderr)


def _check_c_like(code, compiler, ext):
    exe = shutil.which(compiler)
    if not exe:
        return None, []
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "main" + ext)
        with open(src, "w", encoding="utf-8") as f:
            f.write(code)
        _, out = _run([exe, "-fsyntax-only", "-Wall", src], cwd=d)
    problems = []
    for line in out.splitlines():
        low = line.lower()
        if " error:" in low or ("warning:" in low and any(k in low for k in _IMPORTANT_C_WARNINGS)):
            problems.append(line.replace(src, "main" + ext).strip())
    return compiler, problems


def _check_java(code):
    if not shutil.which("javac"):
        return None, []
    m = re.search(r"public\s+(?:final\s+)?class\s+(\w+)", code)
    name = m.group(1) if m else "Main"
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, name + ".java")
        with open(src, "w", encoding="utf-8") as f:
            f.write(code)
        _, out = _run(["javac", "-d", d, src], cwd=d, timeout=60)
    return "javac", [l.replace(src, name + ".java") for l in out.splitlines() if "error" in l.lower()]


def _check_js(code, label="node --check"):
    node = shutil.which("node")
    if not node:
        return None, []
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "script.js")
        with open(src, "w", encoding="utf-8") as f:
            f.write(code)
        rc, out = _run([node, "--check", src], cwd=d)
    if rc == 0:
        return label, []
    return label, [l for l in out.splitlines() if l.strip() and not l.startswith("    at ")][:8]


def _check_python(code):
    try:
        compile(code, "main.py", "exec")
        return "Python compiler", []
    except SyntaxError as e:
        return "Python compiler", [f"main.py line {e.lineno}: {e.msg}"]


class _TagChecker(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
            "track", "wbr", "param", "keygen", "command"}
    OPTIONAL_END = {"p", "li", "td", "th", "tr", "option", "thead", "tbody", "tfoot", "dt", "dd",
                    "colgroup", "optgroup", "rt", "rp"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.problems, self.scripts, self._in_script = [], [], [], False

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        self.stack.append((tag, self.getpos()[0]))
        if tag == "script" and not any(k == "src" for k, _ in attrs):
            self._in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if tag == "script":
            self._in_script = False
        if not any(t == tag for t, _ in self.stack):
            self.problems.append(f"line {self.getpos()[0]}: closing </{tag}> has no matching <{tag}>")
            return
        while self.stack:
            t, line = self.stack.pop()
            if t == tag:
                break
            if t not in self.OPTIONAL_END:
                self.problems.append(f"line {line}: <{t}> is never closed (expected </{t}> before </{tag}>)")

    def handle_data(self, data):
        if self._in_script and self.scripts:
            self.scripts[-1] += data


def _check_html(code):
    p = _TagChecker()
    try:
        p.feed(code)
        p.close()
    except Exception as e:  # pragma: no cover
        return "HTML checker", [str(e)]
    problems = list(p.problems)
    for t, line in p.stack:
        if t not in _TagChecker.OPTIONAL_END:
            problems.append(f"line {line}: <{t}> is never closed")
    for js in p.scripts:
        if js.strip():
            _, js_problems = _check_js(js)
            problems += [f"in <script>: {x}" for x in js_problems]
    return "an HTML/JavaScript checker", problems


def _completeness(code, lang):
    if lang in ("c", "cpp") and not re.search(r"\bmain\s*\(", code):
        return ["The program has no main() function, so it is incomplete and cannot run."]
    if lang == "java" and not re.search(r"static\s+void\s+main\s*\(", code):
        return ["The program has no main method, so it cannot run."]
    if lang == "html" and "</html>" not in code.lower():
        return ["The page is not a complete HTML document (missing </html>)."]
    return []


def check_code(code, lang):
    """-> (tool_name or None, [problems]). Empty list = looks correct."""
    tool, problems = _check_code(code, lang)
    missing = _completeness(code, lang)
    if missing:
        return tool or "a completeness check", missing + problems
    return tool, problems


def _check_code(code, lang):
    try:
        if lang == "c":
            return _check_c_like(code, "gcc", ".c")
        if lang == "cpp":
            return _check_c_like(code, "g++", ".cpp")
        if lang == "java":
            return _check_java(code)
        if lang == "python":
            return _check_python(code)
        if lang == "javascript":
            return _check_js(code)
        if lang == "html":
            return _check_html(code)
    except Exception:
        return None, []
    return None, []


# --------------------------------------------------------------------------- solve


class Solution:
    def __init__(self, code, lang, fixes, check_tool, problems, seconds, tokens, question=None):
        self.code, self.lang, self.fixes, self.question = code, lang, fixes, question
        self.check_tool, self.problems = check_tool, problems
        self.seconds, self.tokens = seconds, tokens


def solve(ollama, model, doc, filename, lang, plan=None, should_cancel=lambda: False,
          on_progress=None, max_repairs=2, log=print, question=None, on_text=None):
    """Ask the model for the finished document, compile-check it, repair if needed."""
    messages = build_messages(doc, filename, lang, plan, question)
    t0 = time.time()
    total_tokens = [0]

    def tok(n):
        total_tokens[0] += 1
        if on_progress:
            on_progress(total_tokens[0], time.time() - t0)

    code = fence_lang = None
    fixes, problems, tool = [], [], None
    for attempt in range(max_repairs + 1):
        reply = ollama.chat(model, messages, should_cancel, tok, on_text if attempt == 0 else None)
        if reply is None:
            return None
        code, fence_lang, new_fixes, new_q = parse_reply(reply)
        if attempt == 0:
            fixes = new_fixes
        question = new_q or question
        if code is not None and code.strip():
            code = strip_question(code, question).rstrip() + "\n"
        if code is None:
            log("model reply had no code block; asking again")
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": "Reply with the QUESTION line, then the complete document inside one ``` code block, then FIXES:."}]
            continue
        lang_now = fence_lang or lang
        tool, problems = check_code(code, lang_now)
        log(f"attempt {attempt + 1}: lang={lang_now} checker={tool} problems={len(problems)} {problems[:4]}")
        if not problems:
            break
        if attempt < max_repairs:
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": REPAIR_TEMPLATE.format(
                             tool=tool, errors="\n".join(problems[:15]),
                             fence=FENCE.get(lang_now, ""))}]
    if code is None:
        raise RuntimeError("The model did not return any code")
    return Solution(code, fence_lang or lang, fixes, tool, problems, time.time() - t0, total_tokens[0],
                    question)
