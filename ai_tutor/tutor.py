"""
AI Tutor - background coding teacher for VS Code, Notepad and other editors.

  Ctrl+S+T  read the file in front, solve the question / finish the code, type it
  `         pause (your turn) / resume (AI checks your edits, fixes mistakes, continues)
  Ctrl+S+Q  stop the current session  (press twice when idle to close AI Tutor)
"""
import datetime
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback

from . import brain, winio
from .editor import (SPEED_MAX, SPEED_MIN, Editor, FocusLost, file_from_title, guess_language,
                     looks_like_code, normalize, plan_edit, speed_to_cps)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")

DEFAULT_CONFIG = {
    "model": "auto",                 # or e.g. "qwen2.5-coder:14b"
    "device": "auto",                # auto | gpu | cpu
    "start_hotkey": "ctrl+s+t",
    "stop_hotkey": "ctrl+s+q",       # stop what the AI is doing right now
    "close_hotkey": "ctrl+e+n",      # close the AI completely (frees GPU/RAM); Ctrl+S+T brings it back
    "pause_key": "`",
    "allowed_apps": ["Code.exe", "Code - Insiders.exe", "Notepad.exe", "notepad++.exe",
                     "sublime_text.exe", "devcpp.exe", "codeblocks.exe", "Cursor.exe",
                     "Windsurf.exe", "Antigravity.exe", "VSCodium.exe", "geany.exe",
                     "idea64.exe", "pycharm64.exe", "notepad2.exe"],
    "typing_style": "human",         # human (letter by letter) | line (fast, line by line)
    "typing_speed": 5,               # 1 (slow) .. 10 (fast); numpad + / - while the AI types
    "start_typing_early": True,      # start typing while the AI is still writing the rest
    "typos": True,                   # makes the odd typo and corrects it, like a person
    "typo_rate": 0.02,
    "typing_chunk": "line",          # only for typing_style "line": line | word
    "typing_ms_per_char": 14,
    "typing_min_gap_ms": 110,
    "temperature": 0.1,
    "context_tokens": 8192,
    "max_answer_tokens": 4096,
    "max_repairs": 2,
    "keep_model_loaded": "30m",
    "show_overlay": False,           # the status bubble in the corner (off: feels like a person)
    "verify_at_end": False,          # re-read (select all + copy) the file after typing to double-check it
    "sounds": False,                 # beeps on start / pause / resume / done
    "ollama_url": "http://127.0.0.1:11434",
}

IDLE, READING, THINKING, TYPING, PAUSED = "idle", "reading", "thinking", "typing", "paused"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
            cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
        except Exception as e:
            print(f"config.json ignored: {e}")
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    return cfg


_save_lock = threading.Lock()


def save_setting(key, value):
    """Remember a setting in config.json, keeping everything else the user wrote."""
    path = os.path.join(ROOT, "config.json")
    with _save_lock:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = dict(DEFAULT_CONFIG)
        data[key] = value
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


class QuestionMemory:
    """The AI removes the question from your file but remembers it here (per
    file name), so it still knows what you are solving when you resume or press
    Ctrl+S+T on that file again later."""

    def __init__(self):
        self.path = os.path.join(ROOT, "memory", "questions.json")
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def get(self, filename):
        if not filename:
            return None
        with self.lock:
            item = self._load().get(filename.lower())
        return item["question"] if item else None

    def put(self, filename, question):
        if not filename or not question:
            return
        with self.lock:
            data = self._load()
            data[filename.lower()] = {"question": question,
                                      "saved": f"{datetime.datetime.now():%Y-%m-%d %H:%M}"}
            if len(data) > 300:  # keep the newest 300
                data = dict(sorted(data.items(), key=lambda kv: kv[1]["saved"])[-300:])
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)


class Logger:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = os.path.join(LOG_DIR, "tutor.log")
        self.lock = threading.Lock()

    def __call__(self, msg):
        line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if sys.stdout:
            try:
                print(line, flush=True)
            except Exception:
                pass


def beep(cfg, kind):
    if not cfg.get("sounds"):
        return
    tones = {"start": [(880, 70)], "pause": [(660, 60), (440, 90)], "resume": [(440, 60), (660, 90)],
             "done": [(660, 70), (880, 70), (1100, 90)], "error": [(300, 220)],
             "close": [(880, 70), (660, 70), (440, 140)]}

    def play():
        try:
            import winsound
            for f, d in tones.get(kind, []):
                winsound.Beep(f, d)
        except Exception:
            pass
    threading.Thread(target=play, daemon=True).start()


# =========================================================================== keyboard routing


def _parse_chord(text):
    parts = [p.strip().lower() for p in text.split("+")]
    if len(parts) != 3 or parts[0] != "ctrl":
        raise ValueError(f"Hotkey must look like ctrl+<key>+<key>, got {text!r}")
    return winio.vk_from_name(parts[1]), winio.vk_from_name(parts[2])


class KeyRouter:
    """Decides, inside the keyboard hook, which key presses belong to AI Tutor.

    Ctrl+S+T needs care: Ctrl+S alone is "Save" (and opens a Save-As dialog in
    a new file), so the S is held back for a moment. If T follows, it was our
    hotkey and the S is dropped; otherwise Ctrl+S is replayed unchanged.
    """
    GRACE = 0.35  # seconds after releasing S during which T still counts
    SPEED_KEYS = {0x6B: +1, 0x6D: -1}  # numpad +  /  numpad -

    def __init__(self, tutor, cfg):
        self.tutor = tutor
        self.chords = {}
        for action, key in (("start", cfg["start_hotkey"]), ("stop", cfg["stop_hotkey"]),
                            ("close", cfg.get("close_hotkey", "ctrl+e+n"))):
            self.chords[_parse_chord(key)] = action
        self.hold_keys = {k1 for k1, _ in self.chords}
        self.pause_vk = winio.vk_from_name(cfg["pause_key"])
        self.accept_injected = bool(cfg.get("_test_accept_injected"))  # automated tests only
        self.down = set()
        self.swallowed = set()
        self.pending = None          # held-back key (S)
        self.pending_held = False
        self.pending_token = 0
        self.lock = threading.Lock()
        self.inject_q = queue.Queue()
        threading.Thread(target=self._injector, name="injector", daemon=True).start()

    # ------------------------------------------------------------------ helpers
    def _injector(self):
        while True:
            events = self.inject_q.get()
            try:
                winio.send_key_events(events)
            except Exception:
                pass

    def _mods(self):
        ctrl = any(v in self.down for v in winio.CTRL_VKS)
        others = any(v in self.down for v in (winio.SHIFT_VKS | winio.ALT_VKS | winio.WIN_VKS))
        return ctrl, others

    def _flush_pending(self, extra=()):
        """Give the held-back key to the app after all (Ctrl+S = Save)."""
        vk = self.pending
        self.pending = None
        events = [(vk, False)]
        if not self.pending_held:
            events.append((vk, True))
        self.inject_q.put(events + list(extra))

    def _grace_expired(self, token):
        with self.lock:
            if self.pending is not None and self.pending_token == token and not self.pending_held:
                self._flush_pending()

    # ------------------------------------------------------------------ hook entry
    def handle(self, vk, is_down, injected, extra):
        if injected and (extra == winio.INJECT_MARK or not self.accept_injected):
            return False  # our own (or another tool's) synthetic keys
        with self.lock:
            if is_down:
                first = vk not in self.down
                self.down.add(vk)
                return self._on_down(vk, first)
            self.down.discard(vk)
            return self._on_up(vk)

    def _on_down(self, vk, first):
        ctrl, others = self._mods()
        if self.pending is not None:
            if vk == self.pending:
                return True                          # auto-repeat of the held key
            chord = (self.pending, vk)
            if ctrl and not others and chord in self.chords:
                if self.pending_held:
                    self.swallowed.add(self.pending)
                self.pending = None
                self.swallowed.add(vk)
                self.tutor.on_hotkey(self.chords[chord])
                return True
            if vk in winio.CTRL_VKS:
                return False
            self._flush_pending(extra=[(vk, False)])  # not our chord: replay S, then this key
            return True

        if vk in self.hold_keys and ctrl and not others:
            self.pending, self.pending_held = vk, True
            self.pending_token += 1
            return True

        state = self.tutor.state
        if vk in self.SPEED_KEYS and not ctrl and not others and state in (READING, THINKING, TYPING):
            self.swallowed.add(vk)
            self.tutor.change_speed(self.SPEED_KEYS[vk])
            return True
        if vk == self.pause_vk and not ctrl and not others and state != IDLE:
            self.swallowed.add(vk)
            if first:
                self.tutor.on_hotkey("pause")
            return True

        if state in (READING, TYPING):
            # keyboard is locked while the AI types, so keys can't land mid-line
            self.swallowed.add(vk)
            if first and vk not in winio.MODIFIER_VKS:
                self.tutor.on_locked_key()
            return True
        if state == THINKING and vk not in winio.MODIFIER_VKS:
            self.tutor.on_user_activity()
        return False

    def _on_up(self, vk):
        if self.pending is not None and vk == self.pending:
            self.pending_held = False
            token = self.pending_token
            threading.Timer(self.GRACE, self._grace_expired, args=(token,)).start()
            return True
        if self.pending is not None and vk in winio.CTRL_VKS and not self._mods()[0]:
            self._flush_pending(extra=[(vk, True)])   # Ctrl let go: it was a normal Ctrl+S
            return True
        if vk in self.swallowed:
            self.swallowed.discard(vk)
            return True
        return False

    def on_mouse(self, msg):
        self.tutor.on_mouse_click()


# =========================================================================== the tutor


class _Cancelled(Exception):
    pass


class SolveJob(threading.Thread):
    """Runs the AI in the background so typing can start while it is still writing."""

    def __init__(self, tutor, doc):
        super().__init__(name="solve", daemon=True)
        self.tutor, self.doc = tutor, doc
        self.plan, self.question = tutor.plan, tutor.question
        self.filename, self.lang = tutor.editor.filename, tutor.lang
        self.raw, self.result, self.error = "", None, None
        self.done, self.cancelled = threading.Event(), threading.Event()
        self.processed = False

    def run(self):
        t = self.tutor
        try:
            self.result = brain.solve(t.ollama, t.model, self.doc, self.filename, self.lang, plan=self.plan,
                                      should_cancel=self.cancelled.is_set,
                                      max_repairs=t.cfg.get("max_repairs", 2), log=t.log,
                                      question=self.question, on_text=self._on_text)
        except Exception as e:
            self.error = e
        finally:
            self.done.set()

    def _on_text(self, raw):
        self.raw = raw

    def target(self, min_lines=12):
        """-> (best code so far or None, finished)."""
        if self.done.is_set():
            return (self.result.code if self.result is not None else None), True
        code, _ = brain.code_from_stream(self.raw, self.question, min_lines)
        return code, False


class Tutor:
    def __init__(self, cfg, overlay, log):
        self.cfg, self.overlay, self.log = cfg, overlay, log
        self.state = IDLE
        self.cmds = queue.Queue()
        self.cancel = threading.Event()
        self.pause_req = threading.Event()
        self.mouse_clicked = False
        self.user_touched = False
        self.ready = False
        self.model = None
        self.hw = None
        self.ollama = brain.Ollama(cfg, log)
        self.editor = None
        self.plan = None             # complete solution the AI is working towards
        self.question = None         # the question, kept here once it is removed from the file
        self.job = None              # the AI's answer being written in the background
        self.last_solution = None
        self.questions = QuestionMemory()
        self.lang = None
        self.expected_doc = None     # what the document should look like right now
        self.saved_clipboard = None
        self.dormant = False
        self._last_progress = 0.0
        self._teach_text, self._typing_kind = "", "type"

    # ------------------------------------------------------------------ events from the hook thread
    def on_hotkey(self, action):
        self.log(f"hotkey: {action} (state={self.state})")
        if action == "start":
            if self.state != IDLE:
                self.cancel.set()
            self.cmds.put("start")
        elif action == "stop":
            if self.state != IDLE:
                self.cancel.set()
                self.cmds.put("stop")
        elif action == "close":
            if self.state != IDLE:
                self.cancel.set()
            self.cmds.put("sleep")
        elif action == "pause":
            if self.state == PAUSED:
                self.cmds.put("resume")
            elif self.state in (READING, THINKING, TYPING):
                self.pause_req.set()
                if self.state == THINKING:
                    self.overlay.show("⏸ OK, your turn. I'll keep the answer ready.\nPress ` when you want me to continue.", "pause")

    def on_mouse_click(self):
        if self.state == TYPING:
            self.mouse_clicked = True
        elif self.state == THINKING:
            self.user_touched = True

    def on_user_activity(self):
        self.user_touched = True

    def on_locked_key(self):
        self.overlay.show("⌨ Keyboard is locked while I type.\nPress ` to pause and take over.", "type")

    def change_speed(self, delta):
        """Numpad + / - : called from the keyboard hook, so keep it quick."""
        level = max(SPEED_MIN, min(SPEED_MAX, int(self.cfg.get("typing_speed", 5)) + delta))
        self.cfg["typing_speed"] = level
        bar = "▮" * level + "▯" * (SPEED_MAX - level)
        if self.state == TYPING:
            self.overlay.show(self._typing_text(), self._typing_kind)
        else:
            self.overlay.show(f"⚡ Typing speed {bar} {level}/10 (~{speed_to_cps(level):.0f} letters/s)", "info", 2.5)
        threading.Thread(target=save_setting, args=("typing_speed", level), daemon=True).start()

    def _typing_text(self):
        level = int(self.cfg.get("typing_speed", 5))
        bar = "▮" * level + "▯" * (SPEED_MAX - level)
        speed = (f"speed {bar} {level}/10  (numpad + / −)" if self.cfg.get("typing_style", "human") == "human"
                 else "")
        return f"{self._teach_text}✍ Typing…  ` = pause and take over\n{speed}".rstrip()

    # ------------------------------------------------------------------ startup
    def init_ai(self):
        try:
            self.overlay.show("AI Tutor: starting…", "info")
            self.hw = brain.detect_hardware()
            self.model = brain.choose_model(self.hw, self.cfg)
            if self.cfg.get("device") == "cpu" or not self.hw["gpu_name"]:
                self.cfg["_force_cpu"] = True
            self.log(f"hardware: {self.hw} -> model {self.model}, force_cpu={self.cfg.get('_force_cpu', False)}")
            self.ollama.ensure_running()
            self.model = brain.fallback_model(self.ollama, self.model) or self.model
            if not self.ollama.has_model(self.model):
                self.overlay.show(f"AI Tutor: model {self.model} is not downloaded.\nRun setup.bat once, then start AI Tutor again.", "error")
                self.log(f"model {self.model} missing")
                return
            self.overlay.show(f"AI Tutor: loading {self.model} …", "info")
            self.ollama.warm_up(self.model)
            share = self.ollama.gpu_share(self.model)
            where = brain.device_label(self.hw, self.cfg)
            if share is not None and self.hw["gpu_name"] and not self.cfg.get("_force_cpu"):
                where += f" · {share * 100:.0f}% on GPU"
            self.where = where
            self.ready = True
            self.log(f"ready on {where}")
            self.overlay.show(f"✅ AI Tutor ready  ·  {self.model}  ·  {where}\n"
                              f"Ctrl+S+T = help me   ·   ` = pause / resume   ·   Ctrl+E+N = close", "ok", 7)
        except Exception as e:
            self.log("init failed: " + traceback.format_exc())
            self.overlay.show(f"AI Tutor could not start: {e}", "error")

    # ------------------------------------------------------------------ controller thread
    def run(self):
        while True:
            cmd = self.cmds.get()
            try:
                if cmd == "exit":
                    self.overlay.show("AI Tutor closed. Bye!", "info", 2)
                    time.sleep(1.5)
                    self.overlay.quit()
                    return
                if cmd == "sleep":
                    self.go_to_sleep()
                elif cmd == "stop":
                    if self.job is not None:
                        self.job.cancelled.set()
                    self.cancel.clear()
                    self.end_session("⏹ Stopped.", "info")
                elif cmd == "start":
                    self.cancel.clear()
                    self.pause_req.clear()
                    self.start_session()
                elif cmd == "resume" and self.state == PAUSED:
                    self.pause_req.clear()
                    self.resume_session()
            except FocusLost:
                self.enter_paused("focus")
            except Exception as e:
                self.log("error: " + traceback.format_exc())
                beep(self.cfg, "error")
                self.end_session(f"⚠ Something went wrong: {e}", "error")

    # ------------------------------------------------------------------ session steps
    def _app_allowed(self, proc):
        allowed = [a.lower() for a in self.cfg.get("allowed_apps", [])]
        return "*" in allowed or proc.lower() in allowed

    def _release_keys(self):
        winio.wait_keys_released(list(winio.MODIFIER_VKS) + [winio.vk_from_name(k) for k in "stqen"]
                                 + [winio.vk_from_name(self.cfg["pause_key"])])
        time.sleep(0.05)

    def go_to_sleep(self):
        """Ctrl+E+N: close the AI completely. The model is unloaded from the GPU /
        RAM and nothing runs; only the tiny key listener stays, so that
        Ctrl+S+T can bring it back."""
        if self.job is not None:
            self.job.cancelled.set()
        self.cancel.clear()
        self.pause_req.clear()
        self.state = IDLE
        self._restore_clipboard()
        self.plan = None
        self.dormant = True
        self.ready = False
        beep(self.cfg, "close")
        self.overlay.show("AI Tutor closed. Ctrl+S+T starts it again.", "info", 3)
        try:
            if self.model:
                self.ollama.unload(self.model)
        except Exception as e:
            self.log(f"unload failed: {e}")
        self.log("closed by Ctrl+E+N (model unloaded); waiting for Ctrl+S+T")

    def wake(self):
        self.log("waking up: loading the model again")
        self.overlay.show(f"AI Tutor: starting again ({self.model})…", "info")
        self.ollama.ensure_running()
        self.ollama.warm_up(self.model)
        self.dormant = False
        self.ready = True
        self.log("awake")

    def start_session(self):
        hwnd = winio.foreground_window()
        proc = winio.window_process_name(hwnd)
        if not self._app_allowed(proc):
            beep(self.cfg, "error")
            self.overlay.show(f"AI Tutor only types into code editors (VS Code, Notepad, …).\n"
                              f"This window is {proc or 'unknown'} — add it to allowed_apps in config.json if you want.", "info", 6)
            return
        beep(self.cfg, "start")
        if self.dormant:
            self.wake()
        end = time.time() + 180
        while not self.ready and time.time() < end and not self.cancel.is_set():
            time.sleep(0.2)                       # still loading after Windows start-up
        if not self.ready:
            beep(self.cfg, "error")
            return
        self.editor = Editor(hwnd, self.cfg, self.log)
        self.state = READING
        self.overlay.show("👀 Reading your file…", "info")
        self._release_keys()
        self.saved_clipboard = winio.get_clipboard_text()
        doc = self.editor.read_all()
        self.log(f"start in {proc} '{self.editor.title}' ({len(doc)} chars)")
        if not doc.strip():
            beep(self.cfg, "error")
            self.end_session("The file is empty. Type a question (or some code) first, then press Ctrl+S+T.", "info", 6)
            return
        self.lang = self.editor.lang or guess_language(doc)
        self.plan = None
        self.question = self.questions.get(self.editor.filename)
        self.think_and_apply(doc)

    def resume_session(self):
        hwnd = winio.foreground_window()
        if hwnd != self.editor.hwnd:
            self.overlay.show("Click back into the file I was working on, then press ` again.\n"
                              "(Or press Ctrl+S+T to start fresh on this window.)", "pause")
            return
        name = self.editor.filename
        if name and file_from_title(winio.window_title(hwnd))[0] not in (None, name):
            self.overlay.show(f"You switched to another file. Go back to {name} and press `,\n"
                              f"or press Ctrl+S+T to start fresh on this file.", "pause")
            return
        self.state = READING
        beep(self.cfg, "resume")
        self._release_keys()
        self.saved_clipboard = winio.get_clipboard_text()
        doc = self.editor.read_all()
        job = self.job
        if job is not None and not job.processed:
            # the AI was still writing its answer when you paused
            if doc == self.expected_doc:
                self.log("resume: no edits by student, continuing the answer being written")
                self.apply(doc, None, job.target)
                return
            # you changed the file: that answer is out of date; what it had so far
            # becomes the guide for the new one
            partial, _ = job.target(min_lines=1)
            job.cancelled.set()
            self.plan = partial or self.plan
            self.log("resume: student edited the file while the answer was being written, re-planning")
            self.think_and_apply(doc)
            return
        if job is not None and not self._finish_job(job):
            self.state = IDLE
            return
        if self.plan and doc == self.expected_doc:
            self.log("resume: no edits by student, continuing plan")
            paste_text, _, _ = plan_edit(doc, self.plan)
            plan = self.plan
            self.apply(doc, paste_text, lambda: (plan, True))
        else:
            self.log("resume: student edited the file, re-planning")
            self.think_and_apply(doc)

    def think_and_apply(self, doc):
        """Ask the AI, and start typing as soon as its first lines are written:
        it writes much faster than a person types, so the rest of the answer
        arrives while the first lines are being typed."""
        self.state = THINKING
        self.user_touched = False
        if self.job is not None:
            self.job.cancelled.set()
        job = SolveJob(self, doc)
        self.job = job
        job.start()
        stream = self.cfg.get("typing_style", "human") == "human" and self.cfg.get("start_typing_early", True)
        student_lines = len(brain.strip_question(doc, self.question).splitlines())
        need = max(12, student_lines + 3) if looks_like_code(brain.strip_question(doc, self.question)) else 12
        self.expected_doc = doc
        t0 = time.time()
        while True:
            if self.cancel.is_set():
                job.cancelled.set()
                self.log("thinking cancelled")
                self.state = IDLE
                return
            if self.pause_req.is_set() or self.user_touched:
                self.enter_paused("typed" if self.user_touched else "pause")   # the AI keeps writing meanwhile
                return
            target, final = job.target(min_lines=need)
            if final:
                if not self._finish_job(job):
                    self.state = IDLE
                    return
                target = self.plan
                break
            if stream and target is not None:
                break
            time.sleep(0.05)
        self.log(f"start typing after {time.time() - t0:.1f}s ({'finished answer' if final else 'answer still being written'})")
        paste_text, type_text, _ = plan_edit(doc, target)
        if final and paste_text is None and not type_text.strip():
            self.end_session("✅ Your code already looks correct and complete. Nice work!", "ok", 6)
            return
        self.apply(doc, paste_text, job.target)

    def _finish_job(self, job):
        """When the AI has finished (and compile-checked) its answer: remember it,
        remember the question, write the mistakes journal. Returns False if the
        job was cancelled."""
        if job.processed:
            return job.result is not None
        job.done.wait()
        job.processed = True
        if job.error is not None:
            raise job.error
        sol = job.result
        if sol is None:
            return False
        self.log(f"solution: {len(sol.code)} chars, {sol.tokens} tokens in {sol.seconds:.1f}s, "
                 f"checker={sol.check_tool}, problems={sol.problems}")
        self.plan = sol.code
        self.lang = sol.lang or self.lang
        if sol.question:
            self.log(f"question remembered: {sol.question}")
            self.question = sol.question
            self.questions.put(self.editor.filename, sol.question)
        # mistakes are judged on your code only: removing the question isn't one
        student = brain.strip_question(job.doc, self.question)
        _, _, diff_fixes = plan_edit(student, sol.code)
        if not looks_like_code(student):
            diff_fixes, sol.fixes = [], []
        self.journal(job.doc, sol, diff_fixes)
        self.last_solution = sol
        return True

    def apply(self, doc, paste_text, get_target):
        self.state = TYPING
        self.mouse_clicked = False
        self.editor.ensure_front()
        if paste_text is not None:
            self.editor.replace_all(paste_text)
            base = paste_text
        else:
            self.editor.go_end()
            base = doc
        self.expected_doc = base
        self.log(f"typing (pasted fixed part: {paste_text is not None})")
        self._teach_text, self._typing_kind = "", "type"
        self.overlay.show(self._typing_text(), self._typing_kind)

        def should_stop():
            if self.cancel.is_set():
                return "cancel"
            if self.pause_req.is_set():
                return "pause"
            if self.mouse_clicked:
                return "mouse"
            return None

        def progress(now):
            self.expected_doc = now

        def target():
            t, final = get_target()
            if final and self.job is not None and not self.job.processed:
                if not self._finish_job(self.job):
                    raise _Cancelled()
            return t, final

        try:
            if self.cfg.get("typing_style", "human") == "human":
                _, reason = self.editor.type_stream(base, target, should_stop, progress)
            else:
                full = self._wait_final(target)
                if full.startswith(base):
                    rest = full[len(base):]
                else:
                    self.editor.replace_all(full)
                    base, rest = full, ""
                n, reason = self.editor.type_text(rest, should_stop, lambda k: progress(base + rest[:k]))
        except _Cancelled:
            reason = "cancel"
        if reason == "cancel":
            if self.job is not None:
                self.job.cancelled.set()
            self.state = IDLE
            return
        if reason:
            self.enter_paused(reason)
            return
        final_text = self.plan
        # Optional double-check: read the file back and repair anything the editor
        # changed. Off by default because it visibly selects the whole file; any
        # difference is picked up anyway the next time you press ` or Ctrl+S+T.
        if self.cfg.get("verify_at_end"):
            seen = self.editor.read_all()
            if seen.rstrip() != final_text.rstrip():
                self.log("verification mismatch; replacing whole document with the exact answer")
                self.editor.replace_all(final_text)
                self.editor.go_end()
        self.expected_doc = final_text
        sol = getattr(self, "last_solution", None)
        note = ""
        if sol is not None and sol.check_tool:
            note = (f"\nChecked with {sol.check_tool}: no errors." if not sol.problems else
                    f"\n⚠ {sol.check_tool} still reports {len(sol.problems)} problem(s) — please review.")
        beep(self.cfg, "done")
        self.end_session("✅ Done! Read through it, then try writing it yourself." + note, "ok", 8)

    def _wait_final(self, target):
        while True:
            t, final = target()
            if final:
                return t
            if self.cancel.is_set():
                raise _Cancelled()
            time.sleep(0.05)

    # ------------------------------------------------------------------ state helpers
    def enter_paused(self, reason):
        self.state = PAUSED
        self.pause_req.clear()
        self.mouse_clicked = False
        self._restore_clipboard()
        msg = {
            "pause": "⏸ Paused — your turn to code.",
            "typed": "⏸ You started typing, so I'm waiting — your turn.",
            "mouse": "⏸ You clicked in the editor, so I paused — your turn.",
            "focus": "⏸ Paused because the editor is no longer in front.",
        }.get(reason, "⏸ Paused.")
        beep(self.cfg, "pause")
        self.overlay.show(msg + "\nPress ` when you want me to check your work and continue.", "pause")
        self.log(f"paused ({reason})")

    def end_session(self, msg, kind, seconds=5):
        self.log("session end: " + msg.replace("\n", " | "))
        self.state = IDLE
        self._restore_clipboard()
        self.overlay.show(msg, kind, seconds)

    def _restore_clipboard(self):
        if self.saved_clipboard is not None:
            try:
                winio.set_clipboard_text(self.saved_clipboard)
            except Exception:
                pass
            self.saved_clipboard = None

    def journal(self, doc, sol, diff_fixes, note=None):
        """Mistakes journal: a running record of what you got wrong, to revise from."""
        if not (sol.fixes or diff_fixes or note):
            return
        path = os.path.join(LOG_DIR, "mistakes_journal.md")
        lines = [f"\n## {datetime.datetime.now():%Y-%m-%d %H:%M} · {self.editor.filename or 'untitled'}"
                 f" ({brain.LANG_NAMES.get(self.lang, self.lang or '?')})"]
        if self.question:
            lines.append(f"**Question:** {self.question}")
        if note:
            lines.append(f"_{note}_")
        if sol.fixes:
            lines.append("**What was wrong (tutor's explanation):**")
            lines += [f"- {f}" for f in sol.fixes]
        if diff_fixes:
            lines.append("**Exact changes made to your code:**")
            lines += [f"- {f}" for f in diff_fixes]
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# =========================================================================== entry points


class NoOverlay:
    """Nothing on screen (the default). run() just keeps the program alive."""

    def __init__(self):
        self._stop = threading.Event()

    def show(self, *a, **k):
        pass

    def hide(self):
        pass

    def quit(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(0.5):
            pass


def _print_hw(cfg):
    hw = brain.detect_hardware()
    model = brain.choose_model(hw, cfg)
    print(f"GPU : {hw['gpu_name'] or 'none (NVIDIA not found)'}" + (f"  {hw['vram_mb']} MB" if hw["gpu_name"] else ""))
    print(f"RAM : {hw['ram_gb']} GB   CPU threads: {hw['cpu_threads']}")
    print(f"Runs on: {brain.device_label(hw, cfg)}")
    print(f"Model : {model}")
    return hw, model


def setup(cfg):
    """One-time setup step (called by setup.bat): pick the model for this PC and download it."""
    print()
    print("  Checking your computer...")
    hw = brain.detect_hardware()
    model = brain.choose_model(hw, cfg)
    if hw["gpu_name"]:
        print(f"  Graphics card : {hw['gpu_name']} ({hw['vram_mb'] / 1024:.0f} GB)  -> the AI will run on it")
    else:
        print("  Graphics card : no NVIDIA card found  -> the AI will run on the processor and RAM")
        print("                  (works fine, just a bit slower)")
    print(f"  Memory (RAM)  : {hw['ram_gb']:.0f} GB")
    size = brain.MODEL_LADDER.get(model, ("?",))[0]
    print(f"  AI model      : {model}  ({size} GB)")
    print()
    o = brain.Ollama(cfg, lambda m: None)
    o.ensure_running()
    if o.has_model(model):
        print("  The AI model is already downloaded.")
    else:
        models_dir = os.environ.get("OLLAMA_MODELS") or os.path.join(os.path.expanduser("~"), ".ollama")
        free_gb = shutil.disk_usage(os.path.splitdrive(models_dir)[0] + "\\").free / 1024 ** 3
        if isinstance(size, float) and free_gb < size + 1:
            print(f"  Not enough free disk space: need about {size + 1:.0f} GB, only {free_gb:.1f} GB free.")
            print("  Free up some space and run setup again.")
            return 2
        print(f"  Downloading the AI model ({size} GB). This happens only once and can take a while...")
        last = [-1]

        def show(status):
            m = re.search(r"([\d.]+)%", status)
            if m and status.startswith("pulling"):
                pct = int(float(m.group(1)))
                if pct != last[0]:
                    last[0] = pct
                    print(f"\r  [{'#' * (pct // 4)}{'.' * (25 - pct // 4)}] {pct:3d}%", end="", flush=True)
            elif status in ("verifying sha256 digest", "success"):
                print(f"\r  [{'#' * 25}] 100%  {status}" + " " * 10, flush=True)
        o.pull(model, progress=show)
        print()
    print("  Testing the AI once...")
    if hw["gpu_name"] is None or cfg.get("device") == "cpu":
        cfg["_force_cpu"] = True
    t = time.time()
    o.warm_up(model)
    share = o.gpu_share(model)
    where = "graphics card" if share and share > 0.05 else "processor + RAM"
    print(f"  OK - loaded in {time.time() - t:.0f}s, running on the {where}.")
    return 0


def selftest(cfg, path):
    """Solve a file without any editor: python run.py --solve file.c"""
    hw, model = _print_hw(cfg)
    if hw["gpu_name"] is None or cfg.get("device") == "cpu":
        cfg["_force_cpu"] = True
    with open(path, encoding="utf-8") as f:
        doc = normalize(f.read())
    name = os.path.basename(path)
    lang = file_from_title(name)[1] or guess_language(doc)
    o = brain.Ollama(cfg, print)
    o.ensure_running()
    sol = brain.solve(o, model, doc, name, lang, on_progress=None, max_repairs=cfg.get("max_repairs", 2))
    print(f"--- {sol.tokens} tokens in {sol.seconds:.1f}s ({sol.tokens / max(sol.seconds, 0.1):.1f} tok/s)  "
          f"lang={sol.lang} checker={sol.check_tool} problems={sol.problems}")
    print("--- QUESTION:", sol.question)
    print("--- FIXES:", sol.fixes)
    print(sol.code)
    paste_text, type_text, fixes = plan_edit(doc, sol.code)
    print("--- diff fixes:", fixes)
    return sol


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = load_config()
    if "--model" in argv:
        cfg["model"] = argv[argv.index("--model") + 1]
    if "--test-keys" in argv:
        cfg["_test_accept_injected"] = True
    if "--setup" in argv:
        sys.exit(setup(cfg))
    if "--hw" in argv:
        _print_hw(cfg)
        return
    if "--solve" in argv:
        return selftest(cfg, argv[argv.index("--solve") + 1])

    mutex = winio.single_instance()
    if mutex is None:
        print("AI Tutor is already running.")
        return
    winio.set_dpi_aware()
    log = Logger()
    with open(os.path.join(LOG_DIR, "tutor.pid"), "w") as f:
        f.write(str(os.getpid()))
    log(f"AI Tutor starting (pid {os.getpid()})")

    if cfg.get("show_overlay"):
        try:
            from .overlay import Overlay
            overlay = Overlay(True)
        except Exception as e:  # e.g. Python installed without tkinter
            log(f"status bubble unavailable: {e}")
            overlay = NoOverlay()
    else:
        overlay = NoOverlay()
    tutor = Tutor(cfg, overlay, log)
    router = KeyRouter(tutor, cfg)
    hooks = winio.HookThread(router.handle, router.on_mouse)
    hooks.start()
    hooks.ready.wait(5)
    if hooks.error:
        log(f"keyboard hook failed: {hooks.error}")
        overlay.show(f"AI Tutor: could not install the keyboard hook ({hooks.error})", "error")
    threading.Thread(target=tutor.init_ai, name="init", daemon=True).start()
    threading.Thread(target=tutor.run, name="controller", daemon=True).start()
    try:
        overlay.run()
    finally:
        hooks.stop()
        log("AI Tutor stopped")
        try:
            os.remove(os.path.join(LOG_DIR, "tutor.pid"))
        except OSError:
            pass
