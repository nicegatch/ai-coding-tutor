# AI Tutor: a coding teacher that types with you

AI Tutor is a free AI that helps you **practise programming** in VS Code, Notepad, Notepad++
and other editors. Write a question or start coding, press **Ctrl + S + T**, and it types the
answer into your file letter by letter, like a person sitting next to you. Pause it at any
time and continue yourself. When you hand back to it, it first fixes your mistakes, then
carries on.

* **Runs 100% on your own PC.** No account, no internet needed after setup, and your code
  never leaves your computer.
* **No dashboard, no windows.** It waits quietly in the background until you press its keys.
* **Uses your hardware well.** On an NVIDIA graphics card (RTX 5060, 4060, 3050, ...) it
  runs on the card. Without one, it runs on the processor and RAM: a little slower, but it
  works on any PC.

It was made for college lab programs: C (arrays, sorting, searching, linked lists, binary
search trees ...), HTML/CSS/JavaScript web pages and forms, and SQL. It also handles C++, Java
and Python.

---

## Install (one time, one click)

1. Click the green **Code** button at the top of this page, then **Download ZIP**.
2. Open the ZIP and click **Extract all**.
3. Double-click **`setup.bat`** in the extracted folder.
   * If Windows says *"Windows protected your PC"*, click **More info**, then **Run anyway**.
4. Wait until it says **All done!** (5-30 minutes, mostly downloading the AI model). You
   don't need to click anything else.

That's it. AI Tutor now starts by itself every time you turn on your PC, and you never need
to run setup again. You can delete the downloaded folder.

Setup takes care of everything:

| Setup step | What it does |
|---|---|
| Python | Installs it (just for you, no admin needed) if it isn't already installed |
| [Ollama](https://ollama.com) | Installs it if missing. Ollama is the free program that runs the AI model on your PC |
| Your hardware | Detects your graphics card and RAM and picks the best AI model for them (table below) |
| AI model | Downloads the model (2-20 GB, only once) and tests it |
| Start-up | Makes AI Tutor start with Windows, adds *Start menu > AI Tutor*, and starts it |

### Which AI model does it pick?

| Your PC | Model | Download size | Speed |
|---|---|---|---|
| NVIDIA card with 22 GB or more (RTX 3090 / 4090 / 5090) | Qwen2.5-Coder 32B | 20 GB | fast, most accurate |
| NVIDIA card with 12-16 GB (RTX 3060 12GB, 4070, 5070 Ti ...) | Qwen2.5-Coder 14B | 9 GB | fast |
| NVIDIA card with 4-8 GB (RTX 3050, 4050, 4060, 5060 ...) | Qwen2.5-Coder 7B | 4.7 GB | fast |
| No NVIDIA card, 10 GB of RAM or more | Qwen2.5-Coder 7B (on the processor) | 4.7 GB | slower, same accuracy |
| No NVIDIA card, less than 10 GB of RAM | Qwen2.5-Coder 3B (on the processor) | 1.9 GB | slower |

**Needs:** Windows 10 or 11 (64-bit), about 15 GB of free disk space (Ollama about 5 GB, plus the model), and internet during setup.

---

## How to use it

| Keys | What happens |
|---|---|
| **Ctrl + S + T** | Hold Ctrl, press S, then T. The AI reads your file, removes the question, and types the solution, or fixes and finishes your half-written code. |
| **`** (key left of 1) | **Pause**: your turn to code. Press it again to **continue**: the AI checks what you changed, fixes your mistakes, then carries on. |
| **Numpad + / −** | While the AI types: faster or slower (10 levels, remembered). |
| **Ctrl + S + Q** | Stop what the AI is doing right now. |
| **Ctrl + E + N** | Close the AI completely and free the memory. **Ctrl + S + T** brings it back in a few seconds. |

**A typical practice session:**

1. Open a file such as `heapsort.c` and write the question, e.g. `WAP to implement heap sort`.
2. Try to write the program yourself.
3. Stuck? Press **Ctrl + S + T**. Within a few seconds the AI starts typing. It removes the
   question from the file (it remembers it), fixes what you wrote, and continues.
4. Press **`** whenever you want your turn back. Press **`** again when you want help.
5. Every mistake it fixes is written down, with an explanation, in
   `%LOCALAPPDATA%\AI-Tutor\logs\mistakes_journal.md`. Read it before your exam.

**Good to know:**

* A normal **Ctrl + S** still saves, and a normal **Ctrl + E** still works. AI Tutor only takes
  the key when T, Q or N follows straight after.
* The ` key belongs to AI Tutor only while it's helping you. The rest of the time it types a
  normal backtick.
* While the AI types, your keyboard is locked so your keys can't land in the middle of its
  code. Press ` to take over. Clicking in the editor or switching windows also pauses it.
* Before anything is typed, the answer is checked with a real compiler when your PC has one
  installed (`gcc`/`g++` for C/C++, `javac` for Java, `node` for JavaScript). Python and HTML
  are always checked. If the check finds a problem, the AI fixes it first, so a C compiler such
  as MinGW makes the answers more reliable.
* No AI is ever 100% correct. Always read the answer: that is how you learn.

---

## Uninstall

*Start menu > AI Tutor > Uninstall AI Tutor.* It asks whether to delete the AI model too, and it
saves your mistakes journal to *Documents* if you want to keep it.

## Settings (optional)

Open `%LOCALAPPDATA%\AI-Tutor\config.json` in Notepad. Save it, then use *Start menu > AI Tutor
> Start AI Tutor* (or restart your PC).

| Setting | Meaning |
|---|---|
| `start_hotkey`, `stop_hotkey`, `close_hotkey`, `pause_key` | the keys (`ctrl+<key>+<key>`, or a single key for pause) |
| `typing_speed` | 1-10 (numpad + / − change it while it types) |
| `typos` | `true`: makes the odd typo and corrects it, like a person |
| `typing_style` | `"human"` (letter by letter) or `"line"` (fast, a line at a time) |
| `start_typing_early` | `true`: starts typing while the AI is still writing the rest |
| `model`, `device` | `"auto"`, or force a model (e.g. `"qwen2.5-coder:14b"`) / `"cpu"` / `"gpu"` |
| `allowed_apps` | editors it may type into; add `"*"` for any window |
| `show_overlay`, `sounds` | a small status bubble and beeps (both off by default) |

## Troubleshooting

* **Nothing happens on Ctrl + S + T.** Click inside your file first. If you closed AI Tutor, use
  *Start menu > AI Tutor > Start AI Tutor*. Details are in `%LOCALAPPDATA%\AI-Tutor\logs\tutor.log`.
* **Antivirus warning.** AI Tutor watches for its shortcut keys and types into your editor,
  which some antivirus programs find suspicious. The full source code is in this repository.
* **Slow on a PC without a graphics card.** This is expected. Lower `typing_speed` so the AI
  never has to wait for itself.

---

## For developers

```
setup.bat               the one-click installer (runs installer/setup.ps1)
run.py                  entry point: python run.py [--setup | --hw | --solve <file>]
ai_tutor/tutor.py       hotkeys, sessions, pause/resume, question memory
ai_tutor/brain.py       hardware detection, model choice, prompts, compiler checks, streaming
ai_tutor/editor.py      reads the editor (Ctrl+A/C), human-like typing engine
ai_tutor/winio.py       Windows keyboard hook, key injection, clipboard (pure ctypes)
ai_tutor/overlay.py     optional status bubble (tkinter)
tests/                  python -m unittest discover -s tests
```

No pip packages are needed: it uses only the Python standard library and talks to Ollama at
`http://127.0.0.1:11434`.

**How it reads and types.** It reads your file with Ctrl+A / Ctrl+C. That is exact, unlike
screen OCR, and it also sees code that has scrolled out of view. When it types, letters,
digits and spaces are real key presses. Brackets, quotes, `<`, `>` and each newline with its
indentation are pasted instead. If they were typed, VS Code's auto-indent, auto-closing
brackets and tags, and suggestion pop-ups would add extra characters. Your clipboard is
restored afterwards, and its pastes are kept out of the Win+V clipboard history.
