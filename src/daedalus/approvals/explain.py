"""Read the code (or the command) and say what it will actually do.

The complaint this answers, in the owner's words: *"the permission prompt gives
me no explanation."* `code` used to ask for approval with the line "run python
code on this machine" — true of `print(1)` and of `shutil.rmtree("~")` alike, so
the prompt carried no information and the owner learned to click through it. A
prompt that cannot be read is worse than no prompt: it trains the habit that
spends the trust.

**Deterministic, and never a model.** SPEC §6 decides the tier from the call,
never from a model's opinion of it; the explanation is held to the same rule.
Asking a model "is this safe?" would be a round-trip an approval cannot afford,
would let the code's own text argue for itself, and would put the owner's
source in a request body. This module reads the text with `ast` where it can
and with patterns where it must, and reports only what it found — including
when it found nothing, which is stated as *unreadable*, never as safe.

What it produces is the short list the prompt shows above the code block:

    what this will do
      • writes /home/apollo/.daedalus/notes.md
      • sends a request to https://api.example.com
      • runs `git push origin main`

Each line is a fact lifted from the source: a path that appears in a write, a
host that appears in a URL, a command that is literally there. Nothing is
inferred about intent, and nothing is scored — the *level* is
`classify.py`'s job; this is the sentence that makes the level mean something.
"""

from __future__ import annotations

import ast
import re
import shlex
from typing import Iterable

__all__ = ["MAX_LINES", "NOTHING_FOUND", "UNREADABLE", "effects"]

#: Enough to characterise an action; a prompt longer than this is a wall.
MAX_LINES = 6

#: What is said when nothing recognisable was found. It is deliberately not
#: reassuring: an unreadable action is exactly the one to be careful with.
UNREADABLE = "could not be read — assume it can do anything you can"

#: …and what is said when the text parsed cleanly but named no file, host or
#: command. Quieter, and still not a promise: it runs as the owner either way.
NOTHING_FOUND = "nothing recognisable — no file, host or command named; it still runs as you"

# Calls whose *name* is enough to know the effect.
_WRITE_CALLS = frozenset(
    {
        "write_text",
        "write_bytes",
        "writelines",
        "write",
        "dump",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "move",
        "rename",
        "replace",
        "makedirs",
        "mkdir",
        "touch",
        "truncate",
        "savefig",
        "to_csv",
        "to_json",
    }
)
_DELETE_CALLS = frozenset({"remove", "unlink", "rmdir", "removedirs", "rmtree", "unlink_all"})
_NET_MODULES = ("requests", "httpx", "aiohttp", "urllib", "urllib3", "socket", "ftplib", "smtplib")
_RUN_CALLS = frozenset({"system", "popen", "run", "call", "check_call", "check_output", "spawn", "spawnl", "execv", "execvp"})
_DYNAMIC_CALLS = frozenset({"eval", "exec", "compile", "__import__", "execfile"})
_ENV_CALLS = frozenset({"getenv"})

# Bash builtins and the tools that mean the same thing in a command.
_SHELL_WRITERS = frozenset({"cp", "mv", "touch", "mkdir", "tee", "dd", "truncate", "install", "ln"})
#: Tools whose *first argument* decides whether they touch anything: `sed -n 1,5p f`
#: reads, `sed -i …` writes, and calling both "writes" is how a prompt starts
#: crying wolf.
_SHELL_CONDITIONAL_WRITERS = {"sed": ("-i", "--in-place"), "chmod": (), "chown": ()}
_SHELL_RUNNERS = frozenset({"uv", "npm", "pnpm", "yarn", "cargo", "go", "dotnet", "bun", "poetry"})
_SHELL_DELETERS = frozenset({"rm", "rmdir", "unlink", "shred", "truncate"})
_SHELL_NET = frozenset({"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "telnet", "ftp", "ping", "dig"})
_SHELL_INSTALL = frozenset({"pacman", "apt", "apt-get", "dnf", "yum", "zypper", "pip", "pip3", "uv", "npm", "pnpm", "yarn", "cargo", "gem", "go"})
_SHELL_SERVICE = frozenset({"systemctl", "service", "loginctl", "launchctl", "rc-service"})
_SHELL_PROCESS = frozenset({"kill", "pkill", "killall"})
_SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "python", "python3", "node", "perl", "ruby"})
_SECRET_HINTS = (".ssh", ".aws", ".gnupg", ".netrc", ".env", "master.key", "id_rsa", "credentials", ".dockercfg")

#: A literal that looks like somewhere on disk, or like somewhere on a network.
_PATH = re.compile(r"(~?/[^\s'\"():;,]+|\b[A-Za-z]:\\\\[^\s'\"]+)")
_URL = re.compile(r"\b(?:https?|ftp|wss?)://[^\s'\"<>]+")
_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?\b")


def effects(source: str, *, language: str = "") -> tuple[str, ...]:
    """The concrete things this code or command would do, or `(UNREADABLE,)`."""
    text = str(source or "")
    if not text.strip():
        return ()
    name = (language or "").lower()
    if name == "python" or (not name and _looks_like_python(text)):
        found = _python_effects(text)
    elif name in ("bash", "sh", "shell", "zsh") or (not name and not _looks_like_python(text)):
        found = _shell_effects(text)
    elif name == "node" or name == "javascript":
        found = _node_effects(text)
    else:
        found = _shell_effects(text)
    unique = list(dict.fromkeys(found))
    if not unique:
        # "Nothing found" is a different sentence in a script than in a command:
        # a script that only computes still runs with the owner's privileges, and
        # saying "unreadable" about `print(1)` teaches the owner to distrust the
        # line that matters.
        return (NOTHING_FOUND,)
    return tuple(unique[:MAX_LINES])


# -- python ------------------------------------------------------------------


def _looks_like_python(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped.startswith(("import ", "from ", "def ", "class ", "print(", "@"))
    return False


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _literal(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
        return str(node.value)
    # `Path("notes.md")` — the target is inside the constructor, so a receiver
    # that is itself a call still names the file.
    if isinstance(node, ast.Call) and node.args:
        return _literal(node.args[0])
    if isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str) and part.value:
                return part.value
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return " ".join(_literal(e) for e in node.elts if _literal(e))
    return ""


def _python_effects(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _shell_effects(text) if " " in text.splitlines()[0] else []
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        leaf = name.rsplit(".", 1)[-1]
        # `Path("notes.md").write_text(...)` names its target on the receiver,
        # not in the call — without this the line read "writes something on disk"
        # for the most common write in the whole language.
        receiver = _literal(node.func.value) if isinstance(node.func, ast.Attribute) else ""
        joined = " ".join(part for part in (receiver, *(_literal(a) for a in node.args)) if part)
        mode = ""
        if leaf in ("open", "file") and len(node.args) > 1:
            mode = _literal(node.args[1])
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = _literal(kw.value)

        if name.startswith(_NET_MODULES):
            found.append(f"sends a request to {_target(joined) or 'a network host'}")
        elif leaf in _RUN_CALLS and ("subprocess" in name or name.startswith("os.") or not name.count(".")):
            found.append(f"runs `{_command(node) or 'a command'}`")
        elif leaf in _DYNAMIC_CALLS:
            found.append("evaluates code it builds at runtime")
        elif leaf in ("open", "file") and mode and set(mode) & set("wax+"):
            found.append(f"writes {_path(joined) or 'a file'}")
        elif leaf in ("open", "file"):
            found.append(f"reads {_path(joined) or 'a file'}")
        elif leaf in _DELETE_CALLS or (leaf in ("remove", "rmdir") and name.startswith("os.")):
            found.append(f"deletes {_path(joined) or 'something on disk'}")
        elif leaf in _WRITE_CALLS:
            found.append(f"writes {_path(joined) or 'something on disk'}")
        elif name.startswith("os.environ") or leaf in _ENV_CALLS or leaf in ("putenv",):
            if leaf in _ENV_CALLS or name.startswith("os.environ"):
                found.append("reads environment variables")
    # A secret-looking path is worth its own line even when the write did not
    # name it — it is the thing the owner must notice.
    for hint in _SECRET_HINTS:
        if hint in text:
            found.append(f"touches something that looks like credentials ({hint})")
            break
    return found


def _command(node: ast.Call) -> str:
    for arg in node.args:
        text = _literal(arg)
        if text:
            return " ".join(text.split())[:120]
    for kw in node.keywords:
        if kw.arg in ("args", "cmd", "command"):
            text = _literal(kw.value)
            if text:
                return " ".join(text.split())[:120]
    return ""


# -- node --------------------------------------------------------------------


def _node_effects(text: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"fs\.(writeFile|appendFile|writeFileSync|appendFileSync|mkdir|mkdirSync)\s*\(([^)]*)", text):
        found.append(f"writes {_path(match.group(2)) or 'a file'}")
    for match in re.finditer(r"fs\.(unlink|unlinkSync|rm|rmSync|rmdir|rmdirSync)\s*\(([^)]*)", text):
        found.append(f"deletes {_path(match.group(2)) or 'something on disk'}")
    for match in re.finditer(r"\b(fetch|axios\.(get|post|put|delete)|https?\.request)\s*\(([^)]*)", text):
        found.append(f"sends a request to {_target(_literal_text(match.group(3))) or 'a network host'}")
    if "child_process" in text or re.search(r"\bexecSync?\s*\(", text):
        inner = re.search(r"\bexec(?:Sync)?\s*\(\s*[`'\"]([^`'\"]+)", text)
        found.append(f"runs `{(inner.group(1) if inner else 'a command')[:120]}`")
    return found


def _literal_text(raw: str) -> str:
    match = re.search(r"[`'\"]([^`'\"]+)[`'\"]", raw or "")
    return match.group(1) if match else (raw or "").strip()


# -- shell -------------------------------------------------------------------


#: The shape that installs other people's code in one line: a fetch piped into
#: an interpreter. It is worth its own sentence because neither half says it —
#: `curl … | bash` is *download and execute*, and `bash` alone looks harmless.
_PIPE_TO_SHELL = re.compile(
    r"\b(curl|wget)\b[^|;\n]*(?:\||>\s*\S+\s*&&\s*\S*)\s*(?:sudo\s+)?(bash|sh|zsh|python3?|node)\b"
)


def _shell_effects(text: str, *, depth: int = 0) -> list[str]:
    found: list[str] = []
    for match in _PIPE_TO_SHELL.finditer(text):
        target = _target(text[match.start() :]) or "a remote host"
        found.append(f"downloads a script from {target} and runs it")
    for command in _segments(text):
        found.extend(_one_command(command, depth=depth))
    return found


def _segments(text: str) -> list[str]:
    """Split a shell line into the simple commands it runs, in order."""
    parts = re.split(r"\|\||&&|;|\||\n|\$\(|`", str(text or ""))
    return [p.strip() for p in parts if p.strip()]


def _one_command(command: str, *, depth: int = 0) -> list[str]:
    found: list[str] = []
    try:
        words = shlex.split(command, comments=False, posix=True)
    except ValueError:
        words = command.split()
    if not words:
        return found
    index = 0
    if words[0] == "sudo":
        found.append("asks for root (sudo)" + (f" to run `{' '.join(words[1:])[:100]}`" if len(words) > 1 else ""))
        index = 1
        if index < len(words) and words[index].startswith("-"):
            index += 1
    if index >= len(words):
        return found
    head = words[index].rsplit("/", 1)[-1]
    tail = words[index + 1 :]
    joined = " ".join(tail)
    redirect = re.search(r">>?\s*(\S+)", command)
    if redirect:
        found.append(f"writes {_path(redirect.group(1))}")

    if head in _SHELL_DELETERS:
        found.append(f"deletes {_targets(tail)}")
    elif head in ("find",) and "-delete" in tail:
        found.append(f"deletes {_targets(tail)}")
    elif head in _SHELL_RUNNERS and tail and tail[0] == "run":
        # `uv run pytest` / `npm run build` run a command the project already
        # defines. Reporting that as an install is simply false, and the owner
        # sees this line on every turn of ordinary work.
        found.append(f"runs the project command `{' '.join(tail[1:])[:100] or head}`")
    elif head in _SHELL_INSTALL:
        found.append(f"installs software with `{head}`")
    elif head in _SHELL_NET:
        found.append(f"reaches the network (`{head}` → {_target(joined) or 'a remote host'})")
    elif head in _SHELL_SERVICE:
        found.append(f"changes a system service ({head} {' '.join(tail)[:60]}".rstrip() + ")")
    elif head in _SHELL_PROCESS:
        found.append(f"stops a process ({head} {' '.join(tail)[:60]}".rstrip() + ")")
    elif head == "git" and tail and tail[0] in ("push", "clone", "fetch", "pull", "submodule"):
        found.append(f"reaches a git remote (`git {tail[0]}`)")
    elif head in _SHELL_WRITERS:
        found.append(f"writes {_targets(tail)}")
    elif head in _SHELL_CONDITIONAL_WRITERS:
        if head == "sed":
            # `sed -n 1,5p f` only prints; `sed -i …` rewrites the file. The
            # in-place flag is the whole difference, and the file is the last
            # word that is not a flag — the expression in between is not a path.
            if any(flag in tail for flag in _SHELL_CONDITIONAL_WRITERS["sed"]):
                named = next((t for t in reversed(tail) if not t.startswith("-")), "")
                found.append(f"edits the text of {_path(named) or named or 'a file'}")
        elif head in ("chmod", "chown"):
            found.append(f"changes who can access {_targets(tail)}")
        else:
            found.append(f"writes {_targets(tail)}")
    elif head in _SHELL_INTERPRETERS and "-c" in tail and depth == 0:
        inner = " ".join(tail[tail.index("-c") + 1 :])
        found.extend(_shell_effects(inner, depth=1))
    elif head in _SHELL_INTERPRETERS and depth == 0 and any(t.endswith((".sh", ".py", ".js")) for t in tail):
        found.append(f"runs the script {next(t for t in tail if t.endswith(('.sh', '.py', '.js')))}")
    if any(hint in command for hint in _SECRET_HINTS):
        found.append("touches something that looks like credentials")
    return found


def _targets(words: Iterable[str]) -> str:
    paths = [_path(w) for w in words if _path(w)]
    return ", ".join(dict.fromkeys(paths))[:160] or "something on disk"


def _path(text: str) -> str:
    match = _PATH.search(str(text or ""))
    return match.group(1)[:120] if match else ""


def _target(text: str) -> str:
    url = _URL.search(str(text or ""))
    if url:
        return url.group(0)[:120]
    host = _HOST.search(str(text or ""))
    return host.group(0)[:120] if host else ""
