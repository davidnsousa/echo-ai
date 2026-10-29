#!/usr/bin/env python3
"""Ask Claude about the currently selected text (X11).

A background process keeps track of selections. `echo-ai --show` (bind it to
a key such as Super+I in your window manager) makes it read the highlighted
text (PRIMARY selection, or Ctrl+C for apps like OnlyOffice that don't set
it), print it to the terminal in foreground mode (the log only gets its
length) and show it in a GTK popup (response area: WebKit, echo-ai.html) where you can send it to Claude Code (`claude -p`) with a
preset action or your own question; replies render as Markdown with
highlighted, copyable code blocks.

  echo-ai            run in the foreground (output in this terminal)
  echo-ai --start    start the background process, no popup
  echo-ai --show     show the popup (starts the background process if needed)
  echo-ai --stop     stop the background process
  echo-ai --uninstall, -R
                     remove echo-ai (the installed command only)
"""
import ctypes
import ctypes.util
import datetime
import os
import re
import socket
import subprocess
import sys

ASSISTANT_NAME = "Echo"  # shown in the popup; Claude answers as this name

xlib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
xlib.XOpenDisplay.restype = ctypes.c_void_p
xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
xlib.XDefaultRootWindow.restype = ctypes.c_ulong
xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
xlib.XStringToKeysym.restype = ctypes.c_ulong
xlib.XStringToKeysym.argtypes = [ctypes.c_char_p]
xlib.XKeysymToKeycode.restype = ctypes.c_ubyte
xlib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
xlib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
xlib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
xlib.XPending.argtypes = [ctypes.c_void_p]
xlib.XConnectionNumber.argtypes = [ctypes.c_void_p]
xlib.XGetInputFocus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
                                ctypes.POINTER(ctypes.c_int)]
xlib.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
xlib.XInternAtom.restype = ctypes.c_ulong
xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
xlib.XGetSelectionOwner.restype = ctypes.c_ulong
xlib.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
xlib.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char_p]


class XErrorEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("display", ctypes.c_void_p),
                ("resourceid", ctypes.c_ulong), ("serial", ctypes.c_ulong),
                ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
                ("minor_code", ctypes.c_ubyte)]


ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(XErrorEvent))


@ERROR_HANDLER
def on_x_error(_display, event):
    # Xlib's default handler exits the process; e.g. a window that closed
    # before we focus it (paste_into) isn't worth dying for.
    debug(f"X error {event.contents.error_code} (request {event.contents.request_code})")
    return 0


xlib.XSetErrorHandler(on_x_error)


TERMINALS = {"alacritty", "kitty", "xterm", "urxvt", "st", "st-256color", "foot", "wezterm",
             "org.wezfurlong.wezterm", "gnome-terminal-server", "konsole", "xfce4-terminal",
             "terminator", "tilix", "sakura", "lxterminal", "terminology", "ghostty"}
# Editors whose Ctrl+C copies the current line when nothing is highlighted.
COPY_LINE_EDITORS = {"code", "code-oss", "codium", "vscodium", "cursor", "sublime_text",
                     "jetbrains-idea", "jetbrains-pycharm", "jetbrains-webstorm",
                     "jetbrains-clion", "jetbrains-goland", "jetbrains-rider", "zed"}
CLIENT_MASK = ~0x1FFFFF  # XIDs from one X connection share these bits (Xorg default)
DEBUG = bool(os.environ.get("ECHO_AI_DEBUG"))


def debug(*parts):
    if DEBUG:
        print("[debug]", *parts, file=sys.stderr, flush=True)


def x_atom(display, name):
    return xlib.XInternAtom(display, name.encode(), False)


def active_window():
    """(window id, lowercase WM_CLASS names) of the WM's active window."""
    out = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], capture_output=True, text=True)
    m = re.search(r"0x[0-9a-f]+", out.stdout)
    if not m:
        return 0, set()
    win = int(m.group(), 16)
    out = subprocess.run(["xprop", "-id", hex(win), "WM_CLASS"], capture_output=True, text=True)
    return win, {c.lower() for c in re.findall(r'"([^"]*)"', out.stdout)}


def wait_for_key_release(display, timeout=1.5):
    """Wait until no key is held (the shortcut that ran --show), so a fake Ctrl+C isn't
    seen as e.g. Super+Ctrl+C."""
    import time

    keys = ctypes.create_string_buffer(32)
    deadline = time.time() + timeout
    while time.time() < deadline:
        xlib.XQueryKeymap(display, keys)
        if not any(keys.raw):
            return
        time.sleep(0.02)


def press_ctrl(display, letter):
    """Send Ctrl+<letter> to whatever window has focus, via the XTest extension."""
    xtst = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xtst") or "libXtst.so.6")
    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                       ctypes.c_ulong]
    ctrl = xlib.XKeysymToKeycode(display, xlib.XStringToKeysym(b"Control_L"))
    key = xlib.XKeysymToKeycode(display, xlib.XStringToKeysym(letter.encode()))
    for code, down in ((ctrl, True), (key, True), (key, False), (ctrl, False)):
        xtst.XTestFakeKeyEvent(display, code, down, 0)
    xlib.XSync(display, False)


def xclip_out(selection):
    try:
        out = subprocess.run(["xclip", "-o", "-selection", selection],
                             capture_output=True, timeout=2)
        return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


TEXT_TARGETS = ("UTF8_STRING", "text/plain")
IMAGE_TARGETS = {"image/png": "png", "image/jpeg": "jpg"}
PRIVATE_DIR = os.path.join("/tmp", f"echo-ai-{os.getuid()}")  # clipboard images, usage cache


def clipboard_targets():
    try:
        return subprocess.run(["xclip", "-o", "-selection", "clipboard", "-t", "TARGETS"],
                              capture_output=True, timeout=2).stdout.decode().split()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def clipboard_bytes(target):
    try:
        return subprocess.run(["xclip", "-o", "-selection", "clipboard", "-t", target],
                              capture_output=True, timeout=2).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return b""


def save_clipboard():
    """(target, bytes) of the clipboard's current content, text or image, or None."""
    targets = clipboard_targets()
    for target in (*TEXT_TARGETS, *IMAGE_TARGETS):
        if target in targets:
            return target, clipboard_bytes(target)
    return None


def clipboard_image():
    """Path of a file holding the clipboard image, or None if it doesn't hold one.

    Only when there's no text too: apps like LibreOffice put a picture of copied
    text on the clipboard as well, and that shouldn't be attached.
    """
    targets = clipboard_targets()
    if any(t in targets for t in TEXT_TARGETS):
        return None
    for target, ext in IMAGE_TARGETS.items():
        if target in targets:
            data = clipboard_bytes(target)
            if not data:
                return None
            import hashlib

            os.makedirs(PRIVATE_DIR, mode=0o700, exist_ok=True)
            # Named by content, so the same image is reused rather than piling up.
            path = os.path.join(PRIVATE_DIR, f"clipboard-{hashlib.sha1(data).hexdigest()[:12]}.{ext}")
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(data)
            return path
    return None


def copy_from_app(display):
    """Press Ctrl+C in the active app and read what it put on the clipboard.

    This is what's highlighted *right now* ("" if nothing is): unlike PRIMARY,
    it notices when you've clicked away, and it also works for apps
    (OnlyOffice) that never set PRIMARY. The previous clipboard content,
    text or image, is put back afterwards.
    """
    import time

    previous = save_clipboard()
    clipboard = x_atom(display, "CLIPBOARD")
    # Hand the clipboard to xclip first, so we can tell when the app takes it over.
    subprocess.run(["xclip", "-selection", "clipboard"], input=b"")
    placeholder = xlib.XGetSelectionOwner(display, clipboard)
    wait_for_key_release(display)
    press_ctrl(display, "c")
    copied = ""
    start = time.time()
    while time.time() - start < 0.5:  # apps that copy at all do it in ~0.1s
        owner = xlib.XGetSelectionOwner(display, clipboard)
        if owner not in (0, placeholder):
            time.sleep(0.05)
            copied = xclip_out("clipboard")
            break
        time.sleep(0.03)
    debug(f"ctrl+c: placeholder owner {placeholder:#x}, new owner {owner:#x} "
          f"after {time.time() - start:.2f}s, copied {copied[:60]!r}")
    if previous:
        target, data = previous
        subprocess.run(["xclip", "-selection", "clipboard", "-t", target], input=data)
    return copied


def read_selection(fresh, last_shown):
    """The text highlighted in the active window ("" if nothing is).

    Apps keep owning PRIMARY after you click away, without telling anyone, so
    PRIMARY can't say whether anything is still highlighted. So ask the app
    itself with Ctrl+C, which copies nothing when nothing is highlighted.
    Except where Ctrl+C can't be used (terminals: SIGINT; editors: copies the
    current line): there use PRIMARY, but only if it changed since we last
    used it (fresh) and isn't just the text we showed last time re-claimed
    (apps re-claim PRIMARY when they get focus back).
    """
    display = xlib.XOpenDisplay(None)
    try:
        owner = xlib.XGetSelectionOwner(display, x_atom(display, "PRIMARY"))
        active, classes = active_window()
        same_app = bool(owner) and owner & CLIENT_MASK == active & CLIENT_MASK
        primary = xclip_out("primary") if fresh and owner else ""
        if fresh and primary and primary == last_shown:
            fresh = False  # the same old selection re-claimed, not a new one
        debug(f"PRIMARY owner {owner:#x} (changed since last use: {fresh}), "
              f"active {active:#x} {sorted(classes)}, same client: {same_app}")
        if active and not classes & (TERMINALS | COPY_LINE_EDITORS):
            return copy_from_app(display)
        # Some terminals (Alacritty) own PRIMARY from a separate X connection,
        # so the client check can't vouch for them; trust any fresh PRIMARY there.
        if not active or classes & TERMINALS:
            return primary if fresh else ""
        return primary if fresh and same_app else ""
    finally:
        xlib.XCloseDisplay(display)


def focused_window():
    """The window that has keyboard focus right now (0 if unknown)."""
    display = xlib.XOpenDisplay(None)
    if not display:
        return 0
    win, revert = ctypes.c_ulong(), ctypes.c_int()
    xlib.XGetInputFocus(display, ctypes.byref(win), ctypes.byref(revert))
    xlib.XCloseDisplay(display)
    return win.value if win.value > 1 else 0  # 0 = None, 1 = PointerRoot


def paste_into(window, content):
    """Put content on the clipboard, focus window and press Ctrl+V there.

    The app still has the text highlighted, so the paste replaces it.
    """
    import time

    subprocess.run(["xclip", "-selection", "clipboard"], input=content.encode())
    display = xlib.XOpenDisplay(None)
    time.sleep(0.25)  # let the WM settle after the popup unmaps
    if window:
        xlib.XSetInputFocus(display, window, 2, 0)  # RevertToParent, CurrentTime
        xlib.XSync(display, False)
        time.sleep(0.1)
    debug(f"paste: target {window:#x}, focused now {focused_window():#x}")
    press_ctrl(display, "v")
    xlib.XCloseDisplay(display)


popups = []


def report(text, image=None):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # The text itself only goes to a terminal (foreground mode), never into the log file.
    shown = text if sys.stdout.isatty() else f"popup ({len(text)} chars selected)"
    print(f"[{stamp}] {shown}" + (" [+ clipboard image]" if image else ""), flush=True)
    # Separate process so the window never blocks the main loop.
    popup = subprocess.Popen([sys.executable, __file__, "--popup", *(["--image", image] if image else [])],
                             stdin=subprocess.PIPE)
    popup.stdin.write(text.encode())
    popup.stdin.close()
    popups[:] = [p for p in popups if p.poll() is None] + [popup]  # reap closed ones


USAGE_FILE = os.path.join(PRIVATE_DIR, "usage.json")
# Plan limit windows Claude Code reports with every request: name -> (label, length in s)
LIMIT_WINDOWS = {"five_hour": ("5-hour", 5 * 3600), "seven_day": ("Weekly", 7 * 86400)}


def save_usage(rate_limit_info):
    import json
    import time

    windows = rate_limit_info.get("unifiedWindows")
    if not windows:
        return
    os.makedirs(PRIVATE_DIR, mode=0o700, exist_ok=True)
    with open(USAGE_FILE, "w") as f:
        json.dump({"fetched_at": time.time(), "windows": windows}, f)


def load_usage():
    import json

    try:
        with open(USAGE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def when(ts):
    """A reset time as 18:30 today, else Sat 18:30."""
    moment = datetime.datetime.fromtimestamp(ts)
    if moment.date() == datetime.date.today():
        return moment.strftime("%H:%M")
    return moment.strftime("%a %H:%M")


def describe_limit(window, length, fetched_at, now):
    """(fraction used, summary text) for one limit window, with a pace estimate.

    The pace estimate assumes you keep using it as fast as you have since the
    window started (reset time minus its length): if that would hit 100%
    before the reset, say roughly when.
    """
    used, resets = window.get("utilization", 0), window.get("resetsAt", 0)
    if resets <= now:
        return 0.0, "reset since last check"
    text = f"{used:.0%} used · resets {when(resets)}"
    elapsed = fetched_at - (resets - length)
    if used >= 1:
        return 1.0, text + " · limit reached"
    if used > 0 and elapsed > length * 0.02:
        full_at = (resets - length) + elapsed / used
        text += (" · on pace to last" if full_at >= resets
                 else f" · at this pace, runs out ~{when(full_at)}")
    return used, text


UI_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "echo-ai.html")
CODE_STYLE = "github-dark"  # any Pygments style

# Quick actions: button label -> instruction sent to Claude with the selection.
PRESETS = {
    "Explain": "Explain this clearly and concisely.",
    "Summarize": "Summarize this in a few sentences.",
    "Fix grammar": "Fix the grammar and spelling. Reply with only the corrected text.",
    "Simplify": "Rewrite this in simpler, plainer language.",
    "Translate to English": "Translate this to English. Reply with only the translation.",
    "Reply": "Draft a short, friendly reply to this message.",
}
FORMATTING = ("Be concise. Use Markdown when it helps (lists, tables, fenced code blocks with "
              "a language tag).")
IDENTITY = (f"You are {ASSISTANT_NAME}, a desktop assistant powered by Claude (made by "
            f"Anthropic); call yourself {ASSISTANT_NAME} if it comes up. ")
SYSTEM_PROMPT = (IDENTITY + "You're answering from a small desktop popup about text the user highlighted "
                 "and/or files they attached. " + FORMATTING + " When asked to rewrite, fix or "
                 "translate text, reply with only the resulting text and no Markdown: it may be "
                 "pasted straight back into the user's document.")
# With nothing highlighted the popup is just a general-purpose assistant.
ASSISTANT_PROMPT = IDENTITY + "You're a helpful assistant in a small desktop popup. " + FORMATTING

INSTALL_HELP = """**Claude Code CLI is not installed** (no `claude` command found).

Install it with the native installer:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

or with npm (Node.js 18+):

```bash
npm install -g @anthropic-ai/claude-code
```

Then run `claude` once in a terminal to sign in, and open echo-ai again.
"""


def find_claude():
    import shutil

    found = shutil.which("claude")
    if found:
        return found
    # A daemon started from .xinitrc may not have ~/.local/bin on PATH.
    for path in ("~/.local/bin/claude", "~/.claude/local/claude", "~/.npm-global/bin/claude"):
        path = os.path.expanduser(path)
        if os.access(path, os.X_OK):
            return path
    return None


def markdown_renderer():
    """(render(markdown) -> html, css for code highlighting). Degrades to <pre> without libs."""
    import html

    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return (lambda md: f"<pre style='white-space:pre-wrap'>{html.escape(md)}</pre>"), ""
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound

        formatter = HtmlFormatter(nowrap=True, style=CODE_STYLE)
        css = formatter.get_style_defs("pre")
        # Keep our own code-block background instead of the style's.
        css = "\n".join(l for l in css.splitlines() if not l.startswith("pre {"))

        def highlight_code(code, lang, _attrs):
            try:
                return highlight(code, get_lexer_by_name(lang), formatter) if lang else ""
            except ClassNotFound:
                return ""  # markdown-it escapes it as plain text
    except ImportError:
        css, highlight_code = "", None
    md = MarkdownIt("commonmark", {"highlight": highlight_code}).enable(["table", "strikethrough"])
    return md.render, css


def pick_files(parent, start):
    """The desktop's native (GTK) file chooser; returns the chosen paths."""
    from gi.repository import Gtk

    dialog = Gtk.FileChooserDialog(title="Attach files", transient_for=parent,
                                   action=Gtk.FileChooserAction.OPEN, modal=True)
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Attach", Gtk.ResponseType.ACCEPT)
    dialog.set_default_response(Gtk.ResponseType.ACCEPT)
    dialog.set_select_multiple(True)
    dialog.set_current_folder(start)
    dialog.set_default_size(900, 600)
    dialog.set_keep_above(True)
    picked = dialog.get_filenames() if dialog.run() == Gtk.ResponseType.ACCEPT else []
    dialog.destroy()
    return picked


GTK_CSS = b"""
.heading { font-weight: bold; font-size: 12pt; }
.dim { opacity: 0.6; }
.card { background-color: @theme_base_color; border: 1px solid @borders; border-radius: 10px; }
.mono { font-family: "JetBrainsMonoNL Nerd Font", monospace; }
button.chip { font-weight: normal; padding: 4px 12px; }
.status-busy { color: @theme_selected_bg_color; }
.status-ok { color: #57e389; }
levelbar trough { min-height: 6px; min-width: 110px; }
levelbar block.filled.ok { background-color: @theme_selected_bg_color; }
levelbar block.filled.warn { background-color: #f8c33a; }
levelbar block.filled.crit { background-color: #ff7b63; }
.status-error { color: #ff7b63; }
"""


def show_window(text, files=()):
    import json
    import threading

    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")  # else gi picks Gdk 4.0, which clashes with Gtk 3
    gi.require_version("WebKit2", "4.1")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, WebKit2

    source_window = focused_window()  # read before our window appears and takes focus
    debug(f"popup: source window {source_window:#x}")
    # Nothing highlighted: no selection card, presets or Replace; just a plain assistant.
    has_selection = bool(text.strip())
    if not has_selection:
        text = ""
    claude = find_claude()
    render_md, code_css = markdown_renderer()
    state = {"proc": None, "answer": "", "done": False, "paste": False, "files": list(files),
             "render_pending": False, "last": None}

    css = Gtk.CssProvider()
    css.load_from_data(GTK_CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    win = Gtk.Window(title=ASSISTANT_NAME)
    win.set_type_hint(Gdk.WindowTypeHint.DIALOG)  # tiling WMs (pytwm/dwm) float dialogs
    win.set_keep_above(True)
    win.set_resizable(False)
    win.set_position(Gtk.WindowPosition.CENTER)
    page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=16)
    page.set_size_request(760, -1)
    win.add(page)

    def label(markup_text, *classes, **kw):
        lbl = Gtk.Label(label=markup_text, xalign=0, **kw)
        for c in classes:
            lbl.get_style_context().add_class(c)
        return lbl

    def button(text_, on_click, *classes, hint=None):
        btn = Gtk.Button(label=text_)
        if hint:  # the hotkey, dimmed after the label
            box = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
            box.pack_start(Gtk.Label(label=text_), False, False, 0)
            box.pack_start(label(hint, "dim"), False, False, 0)
            btn.remove(btn.get_child())
            btn.add(box)
        for c in classes:
            btn.get_style_context().add_class(c)
        btn.connect("clicked", lambda _b: on_click())
        return btn

    def header(title, top=0):
        row = Gtk.Box(spacing=10, margin_top=top)
        row.pack_start(label(title, "heading"), False, False, 0)
        page.pack_start(row, False, False, 0)
        return row

    def card(child, height):
        frame = Gtk.Frame()
        frame.get_style_context().add_class("card")
        frame.set_size_request(-1, height)
        frame.add(child)
        return frame

    def flash(btn, text_):
        original = btn.get_label()
        btn.set_label(text_)
        GLib.timeout_add(1200, lambda: (btn.set_label(original), False)[1])

    def clipboard(content):
        # xclip keeps owning the clipboard after this window is gone.
        subprocess.run(["xclip", "-selection", "clipboard"], input=content.encode())

    # --- Selection -------------------------------------------------------
    if has_selection:
        lines = text.splitlines()
        row = header("Selection")
        row.pack_end(label(f"{len(text)} chars · {len(lines)} line{'s' * (len(lines) != 1)}",
                           "dim"), False, False, 0)
        # A plain label, not a TextView: selecting in a TextView would claim PRIMARY, so the
        # source app could drop its highlight and Replace would insert instead of replace.
        sel_view = Gtk.Label(label=text, xalign=0, yalign=0, wrap=True, selectable=False,
                             margin_start=12, margin_end=12, margin_top=10, margin_bottom=10)
        sel_view.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        sel_view.get_style_context().add_class("mono")
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(sel_view)
        page.pack_start(card(scroller, 110), False, False, 0)

    # --- Ask ---------------------------------------------------------------
    header(f"Ask {ASSISTANT_NAME}", top=8 if has_selection else 0)
    if has_selection:
        presets = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                              row_spacing=6, homogeneous=False, max_children_per_line=10)
        for n, (name, instruction) in enumerate(PRESETS.items(), 1):
            presets.add(button(name, lambda i=instruction: ask(i), "chip",
                               hint=f"Alt+{n}" if n <= 9 else None))
        page.pack_start(presets, False, False, 0)

    ask_row = Gtk.Box(spacing=8)
    ask_row.pack_start(button("+ Files", lambda: attach(), "chip", hint="Alt+F"), False, False, 0)
    entry = Gtk.Entry(placeholder_text="Ask anything about the selection…" if has_selection
                      else "Ask about the clipboard image…" if files else f"Ask {ASSISTANT_NAME} anything…")
    entry.connect("activate", lambda _e: ask_custom())
    ask_row.pack_start(entry, True, True, 0)
    ask_row.pack_start(button("Ask", lambda: ask_custom(), "suggested-action"), False, False, 0)
    page.pack_start(ask_row, False, False, 0)

    files_row = Gtk.Box(spacing=6)
    page.pack_start(files_row, False, False, 0)

    def render_files():
        for child in files_row.get_children():
            child.destroy()
        if not state["files"]:
            files_row.pack_start(label("No files attached", "dim"), False, False, 0)
        for path in state["files"][:4]:
            name = ("Clipboard image" if path.startswith(PRIVATE_DIR + os.sep)
                    else os.path.basename(path))
            chip = button(f"{name}  ✕",
                          lambda p=path: (state["files"].remove(p), render_files()), "chip")
            chip.set_tooltip_text(path)
            thumb = thumbnail(path)
            if thumb:
                chip.set_image(thumb)
                chip.set_always_show_image(True)
            files_row.pack_start(chip, False, False, 0)
        if len(state["files"]) > 4:
            files_row.pack_start(label(f"+{len(state['files']) - 4} more", "dim"), False, False, 0)
        files_row.show_all()

    def thumbnail(path, height=20):
        if os.path.splitext(path)[1].lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            return None
        try:
            return Gtk.Image.new_from_pixbuf(
                GdkPixbuf.Pixbuf.new_from_file_at_scale(path, -1, height, True))
        except GLib.Error:
            return None

    def attach():
        start = os.path.dirname(state["files"][-1]) if state["files"] else os.path.expanduser("~")
        for path in pick_files(win, start):
            if path not in state["files"]:
                state["files"].append(path)
        render_files()
        entry.grab_focus()

    def ask_custom():
        question = entry.get_text().strip()
        if question:
            ask(question)

    # --- Response (the only HTML part) --------------------------------------
    row = header("Response", top=8)
    status = label("")
    row.pack_start(status, False, False, 0)
    copy_btn = button("Copy", lambda: (clipboard(state["answer"]), flash(copy_btn, "Copied ✓")),
                      "flat")
    row.pack_end(copy_btn, False, False, 0)

    content = WebKit2.UserContentManager()
    content.register_script_message_handler("echo_ai")
    view = WebKit2.WebView.new_with_user_content_manager(content)
    view.set_background_color(Gdk.RGBA(0, 0, 0, 0))  # let the GTK card show through
    page.pack_start(card(view, 340 if has_selection else 460), True, True, 0)

    def set_status(text_, kind=None):
        ctx = status.get_style_context()
        for k in ("busy", "ok", "error"):
            ctx.remove_class(f"status-{k}")
        if kind:
            ctx.add_class(f"status-{kind}")
        status.set_text(("● " if kind == "busy" else "") + text_)

    def js(function, *args):
        call = f"{function}({', '.join(json.dumps(a) for a in args)})"
        view.evaluate_javascript(call, -1, None, None, None, None, None)

    def theme_colors():
        # Hand the GTK theme's colours to the page so the HTML matches the widgets.
        ctx = win.get_style_context()

        def color(name, alpha=None):
            found, rgba = ctx.lookup_color(name)
            if not found:
                return None
            r, g, b = (round(c * 255) for c in (rgba.red, rgba.green, rgba.blue))
            return f"rgba({r}, {g}, {b}, {rgba.alpha if alpha is None else alpha})"

        colors = {"fg": color("theme_fg_color"), "muted": color("theme_fg_color", 0.6),
                  "accent": color("theme_selected_bg_color"), "error": color("error_color")}
        return {k: v for k, v in colors.items() if v}

    def on_message(_manager, result):
        msg = json.loads(result.get_js_value().to_string())
        if msg["action"] == "ready":
            js("init", {"colors": theme_colors(), "pygmentsCss": code_css})
            if not claude:
                js("setResponse", render_md(INSTALL_HELP), False)
                set_status("not installed", "error")
            else:
                js("setPlaceholder", "Pick an action or type a question." if has_selection else
                   "Type a question and press Enter. Use + Files to add files as context.")
        elif msg["action"] == "copy":
            clipboard(msg["text"])
        elif msg["action"] == "close":
            escape()

    content.connect("script-message-received::echo_ai", on_message)

    def on_policy(_view, decision, kind):
        # Links in responses open in the default browser, never inside the popup.
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            action = decision.get_navigation_action()
            if action.get_navigation_type() == WebKit2.NavigationType.LINK_CLICKED:
                subprocess.Popen(["xdg-open", action.get_request().get_uri()])
                decision.ignore()
                return True
        return False

    view.connect("decide-policy", on_policy)

    # --- Plan usage --------------------------------------------------------
    usage_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6)
    page.pack_start(usage_box, False, False, 0)

    def render_usage():
        import time

        for child in usage_box.get_children():
            child.destroy()
        usage = load_usage()
        if not usage:
            usage_box.pack_start(label("Plan usage appears after your first question.", "dim"),
                                 False, False, 0)
        else:
            for key, (name, length) in LIMIT_WINDOWS.items():
                if key not in usage["windows"]:
                    continue
                used, summary = describe_limit(usage["windows"][key], length,
                                               usage["fetched_at"], time.time())
                row = Gtk.Box(spacing=10)
                row.pack_start(label(name, "dim", width_chars=7), False, False, 0)
                bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER)
                for offset in ("low", "high", "full"):
                    bar.remove_offset_value(offset)
                for offset, value in (("ok", 0.75), ("warn", 0.9), ("crit", 1.0)):
                    bar.add_offset_value(offset, value)
                bar.set_value(min(used, 1))
                row.pack_start(bar, False, False, 0)
                row.pack_start(label(summary, "dim"), False, False, 0)
                usage_box.pack_start(row, False, False, 0)
            usage_box.set_tooltip_text(
                "Your Claude plan's limits, shared by all Claude apps and Claude Code. "
                f"Updated {when(usage['fetched_at'])} (with each question you ask here).")
        usage_box.show_all()

    render_usage()

    # --- Footer ----------------------------------------------------------
    footer = Gtk.Box(spacing=8, margin_top=6)
    footer.pack_start(label("Enter to ask · Ctrl+R to retry · Esc to stop, again to close",
                            "dim"), False, False, 0)
    footer.pack_end(button("Close", lambda: close(), "flat"), False, False, 0)
    footer.pack_end(button("Copy & close", lambda: copy_and_close(), "flat", hint="Ctrl+⇧C"),
                    False, False, 0)
    if has_selection:
        footer.pack_end(button("Replace selection", lambda: replace(), "suggested-action",
                               hint="Ctrl+↵"), False, False, 0)
    page.pack_start(footer, False, False, 0)

    # --- Claude Code -------------------------------------------------------
    def stop():
        proc = state["proc"]
        if proc and proc.poll() is None:
            proc.kill()
        state["proc"] = None

    def ask(instruction):
        if not claude:
            return
        stop()  # a new question replaces one still running
        state["answer"], state["done"], state["last"] = "", False, instruction
        js("setResponse", "", True)
        set_status("thinking…", "busy")
        files = list(state["files"])
        # No tools at all, unless files are attached: then only Read, and only their folders.
        tools = ["--tools", ""]
        if files:
            tools = ["--tools", "Read", "--allowedTools", "Read"]
            for folder in sorted({os.path.dirname(f) for f in files}):
                tools += ["--add-dir", folder]
        proc = subprocess.Popen(
            [claude, "-p", "--output-format", "stream-json", "--verbose",
             "--include-partial-messages", *tools, "--strict-mcp-config",
             "--no-session-persistence", "--append-system-prompt",
             SYSTEM_PROMPT if has_selection else ASSISTANT_PROMPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=os.path.expanduser("~"), text=True)
        state["proc"] = proc
        threading.Thread(target=read_stream, args=(proc, build_prompt(instruction, files)),
                         daemon=True).start()

    def build_prompt(instruction, files):
        prompt = instruction + "\n"
        if text:
            prompt += f"\n<selected_text>\n{text}\n</selected_text>\n"
        if files:
            prompt += "\nAttached files for context (read them with the Read tool):\n"
            prompt += "".join(f"- {f}\n" for f in files)
        return prompt

    def read_stream(proc, prompt):
        """Worker thread: feed the prompt, hand stream-json events to the GTK main loop."""
        def emit(kind, payload=None):
            GLib.idle_add(on_event, proc, kind, payload)

        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            wrote_text, final = False, None
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "stream_event":
                    event = msg["event"]
                    block = event.get("content_block", {})
                    delta = event.get("delta", {})
                    if block.get("type") == "tool_use":
                        emit("tool", block.get("name"))
                    elif block.get("type") == "text" and wrote_text:
                        emit("delta", "\n\n")  # new paragraph after a tool call
                    elif delta.get("type") == "text_delta":
                        wrote_text = True
                        emit("delta", delta["text"])
                elif msg.get("type") == "rate_limit_event":
                    emit("limits", msg.get("rate_limit_info", {}))
                elif msg.get("type") == "result":
                    if msg.get("is_error"):
                        emit("error", str(msg.get("result") or "Claude returned an error."))
                        return
                    final = msg
            code = proc.wait()
            err = proc.stderr.read().strip()
            if code:
                emit("error", err or f"claude exited with code {code}")
            else:
                emit("done", final)
        except (OSError, ValueError):
            pass  # process was killed (window closed or question replaced)

    def render():
        # Re-rendering the whole Markdown is cheap; batch it so fast streams stay smooth.
        state["render_pending"] = False
        js("setResponse", render_md(state["answer"]), not state["done"])
        return False

    def on_event(proc, kind, payload):
        if proc is not state["proc"]:
            return False  # output from a question that was replaced
        if kind == "delta":
            state["answer"] += payload
            set_status("writing…", "busy")
            if not state["render_pending"]:
                state["render_pending"] = True
                GLib.timeout_add(60, render)
        elif kind == "tool":
            set_status("reading files…", "busy")
        elif kind == "limits":
            save_usage(payload)
            render_usage()
        elif kind == "done":
            if payload and payload.get("result"):
                # Just the final answer, without "let me read…" lead-ins.
                state["answer"] = payload["result"]
            state["done"] = True
            render()
            set_status(f"✓ done{token_summary(payload)}", "ok")
        else:
            js("setError", payload)
            set_status("error", "error")
        return False

    def token_summary(result):
        usage = (result or {}).get("usage") or {}
        sent = sum(usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                             "cache_read_input_tokens"))
        if not sent:
            return ""
        fmt = lambda n: f"{n / 1000:.1f}k" if n >= 1000 else str(n)
        cost = result.get("total_cost_usd")
        status.set_tooltip_text(f"≈ ${cost:.3f} at API list prices; on a Claude plan it counts "
                                "towards the limits below instead." if cost else None)
        return f" · {fmt(sent)} tokens in, {fmt(usage.get('output_tokens', 0))} out"

    def close():
        stop()
        win.destroy()

    def copy_and_close():
        # The response if there is one, otherwise the original selection.
        clipboard(state["answer"] if state["done"] else text)
        close()

    def replace():
        if not state["done"]:
            set_status("wait for a finished response first", "error")
            return
        state["paste"] = True  # done after the main loop, once the popup is really gone
        close()

    def escape():
        # A reply still coming in is stopped first; a second Esc closes.
        if state["proc"] and state["proc"].poll() is None:
            stop()
            set_status("stopped", "error")
        else:
            close()

    def retry():
        if state["last"]:
            ask(state["last"])

    def on_key(_w, event):
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        key = Gdk.keyval_to_lower(event.keyval)
        ctrl, alt = Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.MOD1_MASK
        shift = Gdk.ModifierType.SHIFT_MASK
        presets = list(PRESETS.values())
        if key == Gdk.KEY_Escape and not mods:
            escape()
        elif mods == alt and has_selection and Gdk.KEY_1 <= key < Gdk.KEY_1 + min(len(presets), 9):
            ask(presets[key - Gdk.KEY_1])
        elif mods == alt and key == Gdk.KEY_f:
            attach()
        elif mods == ctrl and key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and has_selection:
            replace()
        elif mods == ctrl | shift and key == Gdk.KEY_c:
            copy_and_close()
        elif mods == ctrl and key == Gdk.KEY_r:
            retry()
        else:
            return False
        return True

    win.connect("key-press-event", on_key)
    win.connect("destroy", lambda _w: Gtk.main_quit())
    render_files()
    with open(UI_HTML) as f:
        view.load_html(f.read(), "file:///")
    win.show_all()
    win.present()
    entry.grab_focus()
    Gtk.main()
    if state["paste"]:
        paste_into(source_window, state["answer"].strip())


SOCKET_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or PRIVATE_DIR, "echo-ai.sock")
LOG_FILE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                        "echo-ai", "echo-ai.log")


def send(message):
    """Send a command to the background process; False if it isn't running."""
    with socket.socket(socket.AF_UNIX) as conn:
        try:
            conn.connect(SOCKET_PATH)
        except OSError:
            return False
        conn.sendall(message.encode())
    return True


def listen():
    """The socket --show and --stop talk to; exits if another one is already running."""
    if send("ping"):
        sys.exit("echo-ai: already running (echo-ai --stop to stop it)")
    os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o700, exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)  # left over from a process that didn't exit cleanly
    server = socket.socket(socket.AF_UNIX)
    server.bind(SOCKET_PATH)
    server.listen()
    return server


def start(then_show=False):
    """Start the background process unless it's running, and wait until it answers."""
    import time

    if send("ping"):
        return
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as log:
        subprocess.Popen([sys.executable, os.path.abspath(__file__)], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        if send("show fresh" if then_show else "ping"):
            return
        time.sleep(0.05)
    sys.exit(f"echo-ai: did not start - see {LOG_FILE}")


def main():
    display = xlib.XOpenDisplay(None)
    if not display:
        sys.exit("echo-ai: cannot open X display (is DISPLAY set?)")
    root = xlib.XDefaultRootWindow(display)
    server = listen()

    # Get told whenever any app takes PRIMARY, i.e. makes a new selection.
    xfixes = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xfixes") or "libXfixes.so.3")
    xfixes.XFixesSelectSelectionInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                  ctypes.c_ulong, ctypes.c_ulong]
    event_base, error_base = ctypes.c_int(), ctypes.c_int()
    xfixes.XFixesQueryExtension(ctypes.c_void_p(display), ctypes.byref(event_base),
                                ctypes.byref(error_base))
    xfixes.XFixesSelectSelectionInput(display, root, x_atom(display, "PRIMARY"),
                                      1)  # XFixesSetSelectionOwnerNotifyMask
    xlib.XSync(display, False)
    selection_notify = event_base.value  # XFixesSelectionNotify

    import select

    print(f"echo-ai: running, waiting for echo-ai --show (socket {SOCKET_PATH})", flush=True)
    event = ctypes.create_string_buffer(192)  # sizeof(XEvent)
    # Has PRIMARY changed since we last used it? Not known at startup, so check.
    fresh, last_shown = False, ""
    try:
        while True:
            # XPending first: Xlib may already have read events off the connection.
            while xlib.XPending(display):
                xlib.XNextEvent(display, event)
                if ctypes.c_int.from_buffer(event).value == selection_notify:
                    fresh = True
                    # XFixesSelectionNotifyEvent.owner sits at byte 48 on 64-bit.
                    debug(f"PRIMARY taken by {ctypes.c_ulong.from_buffer(event, 48).value:#x}")
            ready, _, _ = select.select([xlib.XConnectionNumber(display), server], [], [])
            if server not in ready:
                continue
            conn, _ = server.accept()
            with conn:
                conn.settimeout(1)
                try:
                    message = conn.recv(64).decode(errors="replace").split()
                except OSError:
                    continue
            if message[:1] == ["stop"]:
                print("echo-ai: stopping", flush=True)
                break
            if message[:1] == ["show"]:
                # "show fresh": --show just started us, so whatever is selected is new.
                fresh = fresh or "fresh" in message
                image = clipboard_image()  # before read_selection juggles the clipboard
                last_shown = read_selection(fresh, last_shown)
                report(last_shown, image)
                fresh = False
    finally:
        server.close()
        os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    command = sys.argv[1:2]
    if command == ["--popup"]:
        # --popup [--image PATH]: the popup, with the selection on stdin (internal)
        image = sys.argv[3:4] if sys.argv[2:3] == ["--image"] else []
        show_window(sys.stdin.read(), image)
    elif command == ["--start"]:
        start()
    elif command == ["--show"]:
        if not send("show"):
            start(then_show=True)
    elif command == ["--stop"]:
        if not send("stop"):
            print("echo-ai: not running")
    elif command in (["--help"], ["-h"]):
        print(__doc__.strip())
    elif command:
        sys.exit(f"echo-ai: unknown option {command[0]} (see echo-ai --help)")
    else:
        try:
            main()
        except KeyboardInterrupt:
            pass
