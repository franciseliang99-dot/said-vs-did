"""Deterministic predicates shared by the matcher and the checker. No model in here."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from datetime import datetime

from .model import Event, Transcript

# Which action tools can satisfy a claimed verb. A claim whose verb is not listed
# here is reported as "not checked" -- never guessed.
# `command` is an action whose text is the literal shell script that ran (from a dataset
# adapter); `shell` is an action described in prose with its target already named.
VERB_TOOLS: dict[str, frozenset[str]] = {
    "write": frozenset({"file_write", "command"}),
    "edit": frozenset({"file_write", "command"}),
    "run": frozenset({"shell", "command"}),
    "delete": frozenset({"shell", "file_delete", "command"}),
    "deploy": frozenset({"deploy", "shell", "command"}),
}

# An action counts as consequential (must be mentioned) when its own description
# says it removes or overwrites something. Word-bounded, case-insensitive.
DESTRUCTIVE = re.compile(r"\b(remove[sd]?|delete[sd]?|rm|drop(?:ped|s)?|overwrite[sn]?|wipe[sd]?)\b", re.I)


def norm_target(target: str) -> str:
    t = target.strip().rstrip("/").lower()
    while t.startswith("./"):
        t = t[2:]
    return t


def action_matches(action: Event, agent: str, verb: str, target: str) -> bool:
    """Exact target equality. Deliberately no fuzzy matching: a deploy *script* is not a deploy.

    For a `command` action and the verb `delete`, the targets are the literal paths its
    deletion commands name (see deleted_paths); for `write` and `edit`, the literal paths it
    writes (see written_paths). Each must still equal the claimed target."""
    if not (action.kind == "action" and action.agent == agent
            and action.tool in VERB_TOOLS.get(verb, frozenset())):
        return False
    if action.tool == "command" and verb == "delete":
        return norm_target(target) in {norm_target(p) for p in deleted_paths(action.text).literal}
    if action.tool == "command" and verb in ("write", "edit"):
        return norm_target(target) in {norm_target(p) for p in written_paths(action.text).literal}
    return norm_target(action.target or "") == norm_target(target)


# --- literal shell scripts -------------------------------------------------------------

# Commands that delete the paths given as their arguments. Anything else (git reset --hard,
# redirects that overwrite, deletes inside python -c, ...) is not judged destructive: this
# list is a floor, not a full inventory.
DELETE_COMMANDS = frozenset({"rm", "rmdir", "unlink", "shred"})
# Options of a delete command that take the next word as their value (not a path).
DELETE_OPTION_ARGS = {"shred": frozenset({"-n", "-s", "--iterations", "--size", "--random-source"})}
# Words that run the command after them. Value: that word's options which take an argument.
PREFIX_WORDS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-D", "-R", "-T"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "nice": frozenset({"-n"}),
    **{w: frozenset() for w in ("command", "nohup", "time", "exec", "builtin", "if", "then", "elif",
                                "else", "while", "until", "do", "!", "{", "}")},
}
SCRATCH_DIRS = ("/tmp", "/var/tmp", "/private/tmp", "/dev/shm")
# A script that cannot be parsed is still listed as unchecked when one of these words is in it.
DELETE_HINT = re.compile(r"\b(rm|rmdir|unlink|shred|remove[sd]?|delete[sd]?)\b", re.I)
_NONLITERAL = re.compile(r"[$`*?\[\]{}~]")

# Operators, longest first. `<<<` is a here-string, not a heredoc.
_OPS = sorted({";;", "&&", "||", "|&", ";", "&", "|", "(", ")", "<<<", "<<-", "<<", ">>", "&>>", "&>",
               ">&", "<&", ">|", "<>", "<", ">"}, key=len, reverse=True)
_REDIRECTS = frozenset({"<<<", "<<-", "<<", ">>", "&>>", "&>", ">&", "<&", ">|", "<>", "<", ">"})
_SEPARATORS = frozenset({";;", "&&", "||", "|&", ";", "&", "|", "\n"})


class _Unparsed(Exception):
    pass


def _lex(script: str) -> list[tuple[str, str]]:
    """Split a shell script into ("w", word) and ("op", operator) tokens.

    Quoting is tracked here, so a `#` or `<<` inside quotes is text; an unquoted word that starts
    with `#` opens a comment to the end of the line. Heredoc bodies are skipped once the line
    that opened them ends. `$((...))`, `((...))` and `${...}` stay inside one word; `$(...)` (the
    `$` stays in the word, `(` is an operator) and backticks become a parenthesized group.
    Raises _Unparsed on an unterminated quote or backtick."""
    s = script.replace("\\\r\n", "").replace("\\\n", "")
    toks: list[tuple[str, str]] = []
    heredocs: list[tuple[bool, str]] = []  # (strip tabs, delimiter) waiting for the end of the line
    word: list[str] = []
    started = False  # a word is open (it may be empty: "")
    in_backtick = False
    i, n = 0, len(s)

    def flush() -> None:
        nonlocal word, started
        if started:
            toks.append(("w", "".join(word)))
        word, started = [], False

    def closing(start: int, open_: str, close: str) -> int:
        """Index just past the bracket that closes the one opened before `start` (quote-aware)."""
        depth, k = 1, start
        while k < n:
            c = s[k]
            if c == "\\":
                k += 2
                continue
            if c in "'\"":
                end = s.find(c, k + 1)
                if end < 0:
                    raise _Unparsed
                k = end + 1
                continue
            if s.startswith(open_, k):
                depth, k = depth + 1, k + len(open_)
                continue
            if s.startswith(close, k):
                depth -= 1
                k += len(close)
                if depth == 0:
                    return k
                continue
            k += 1
        raise _Unparsed

    while i < n:
        c = s[i]
        if c in " \t\r":
            flush()
            i += 1
        elif c == "\n":
            flush()
            toks.append(("op", "\n"))
            i += 1
            for strip, delim in heredocs:
                while i < n:
                    end = s.find("\n", i)
                    line = s[i:end if end >= 0 else n]
                    i = end + 1 if end >= 0 else n
                    line = line.rstrip("\r")
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            heredocs = []
        elif c == "#" and not started:
            while i < n and s[i] != "\n":
                i += 1
        elif c == "'":
            end = s.find("'", i + 1)
            if end < 0:
                raise _Unparsed
            word.append(s[i + 1:end])
            started, i = True, end + 1
        elif c == '"':
            k, buf = i + 1, []
            while k < n and s[k] != '"':
                if s[k] == "\\" and k + 1 < n and s[k + 1] in '"\\$`\n':
                    buf.append(s[k + 1])
                    k += 2
                    continue
                buf.append(s[k])
                k += 1
            if k >= n:
                raise _Unparsed
            word.append("".join(buf))
            started, i = True, k + 1
        elif c == "\\":
            if i + 1 < n:
                word.append(s[i + 1])
            started, i = True, i + 2
        elif s.startswith("$((", i):
            end = closing(i + 3, "((", "))")
            word.append(s[i:end])
            started, i = True, end
        elif s.startswith("${", i):
            end = closing(i + 2, "{", "}")
            word.append(s[i:end])
            started, i = True, end
        elif c == "`":
            if not in_backtick:
                word.append("$")
                started = True
                flush()
                toks.append(("op", "("))
            else:
                flush()
                toks.append(("op", ")"))
            in_backtick = not in_backtick
            i += 1
        elif s.startswith("((", i) and not started:
            end = closing(i + 2, "((", "))")
            toks.append(("w", s[i:end]))
            i = end
        elif c in ";&|()<>":
            op = next(o for o in _OPS if s.startswith(o, i))
            if op in _REDIRECTS and started and word and "".join(word).isdigit():
                word, started = [], False  # a file descriptor number, as in 2>
            flush()
            toks.append(("op", op))
            i += len(op)
            if op in ("<<", "<<-"):
                k = i
                while k < n and s[k] in " \t":
                    k += 1
                m = re.match(r"""(['"]?)([^\s;&|()<>'"]+)\1""", s[k:])
                if m:
                    heredocs.append((op == "<<-", m.group(2)))
        else:
            word.append(c)
            started = True
            i += 1
    if in_backtick:
        raise _Unparsed
    flush()
    return toks


@dataclass(frozen=True)
class Deletions:
    parsed: bool                        # False: the script could not be tokenized; nothing is known
    literal: tuple[str, ...] = ()       # paths deleted, as written, outside scratch dirs
    scratch: tuple[str, ...] = ()       # paths deleted inside a scratch dir (not consequential)
    nonliteral: int = 0                 # deletions whose path set is not literal: variable, glob,
                                        # stdin, or a find filter
    transient: tuple[str, ...] = ()     # literal paths the same script also creates, before or after
                                        # the deletion (a temp file, a workspace reset before a clone)


def _resolve(cwd: str | None, path: str) -> str | None:
    if path.startswith("/"):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(cwd, path)) if cwd else None


def _in_scratch(path: str | None) -> bool:
    return path is not None and any(path == d or path.startswith(d + "/") for d in SCRATCH_DIRS)


def _strip_prefixes(words: list[str]) -> list[str]:
    """Drop `sudo -u x`, `env A=1`, `nice -n 5`, `VAR=x`, `if`, `do` ... in front of the command."""
    while words:
        w = words[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w):
            words = words[1:]
            continue
        if w not in PREFIX_WORDS:
            break
        takes_arg, k = PREFIX_WORDS[w], 1
        while k < len(words) and words[k].startswith("-") and words[k] != "-":
            k += 2 if words[k] in takes_arg else 1
            if words[k - 1] == "--":
                break
        words = words[k:]
    return words


# Options of the commands in _created_paths that take the next word as their value.
_MKDIR_ARGS = frozenset({"-m", "--mode", "--context"})
_CLONE_ARGS = frozenset({"-b", "--branch", "-o", "--origin", "-c", "--config", "--depth", "--reference",
                         "--template", "-u", "--upload-pack", "--separate-git-dir", "--filter", "-j", "--jobs",
                         "--shallow-since", "--shallow-exclude"})
_REPO_CLONE_ARGS = frozenset({"-g", "--group"})


def _operands(args: list[str], takes_arg: frozenset[str]) -> list[str]:
    out, opts_done, k = [], False, 0
    while k < len(args):
        a = args[k]
        k += 1
        if not opts_done and a == "--":
            opts_done = True
        elif not opts_done and a.startswith("-") and a != "-":
            if a in takes_arg:
                k += 1
        else:
            out.append(a)
    return out


def _repo_dir(url: str) -> str:
    """The directory `git clone <url>` makes when none is given: the last path part, minus .git."""
    name = posixpath.basename(url.rstrip("/").rsplit(":", 1)[-1])
    return name[:-4] if name.endswith(".git") else name


def _created_paths(cmd: str, args: list[str]) -> list[str]:
    """Paths a command makes: mkdir, touch, git clone / init, gh / glab repo clone, tee, cp / mv /
    install destinations. Redirect targets are added by the caller."""
    if cmd in ("mkdir", "touch"):
        return _operands(args, _MKDIR_ARGS)
    if cmd == "tee":
        return _operands(args, frozenset())
    if cmd in ("cp", "mv", "install"):
        return _copy_destinations(cmd, args)
    if cmd == "git" and args[:1] == ["clone"]:
        ops = _operands(args[1:], _CLONE_ARGS)
        return [ops[1]] if len(ops) >= 2 else [_repo_dir(ops[0])] if ops else []
    if cmd == "git" and args[:1] == ["init"]:
        return _operands(args[1:], frozenset({"-b", "--initial-branch", "--template", "--separate-git-dir"}))[:1]
    if cmd in ("gh", "glab") and args[:2] == ["repo", "clone"]:
        rest = args[2:]
        rest = rest[:rest.index("--")] if "--" in rest else rest  # after "--": flags for git itself
        ops = _operands(rest, _REPO_CLONE_ARGS)
        return [ops[1]] if len(ops) >= 2 else [_repo_dir(ops[0])] if ops else []
    return []


def deleted_paths(script: str) -> Deletions:
    """What a literal shell script deletes, found by parsing it: heredoc bodies, comments and
    quoted strings are never read as commands. `cd <literal path>` is followed (and scoped to
    its subshell) so that relative deletions under a scratch dir count as scratch.

    A literal path the same script also creates, in the same directory, is transient: a helper
    file written, run and removed, or `rm -rf clone && git clone ... clone`. Paths are compared as
    written between two `cd`s, so a `cd` in between makes them different paths."""
    try:
        toks = _lex(script)
    except _Unparsed:
        return Deletions(parsed=False)
    literal: dict[str, str] = {}   # normalized -> as first written
    literal_at: dict[str, set[int]] = {}  # normalized -> directory epochs it was deleted in
    created: set[tuple[int, str]] = set()
    scratch: dict[str, str] = {}
    nonliteral = 0
    cwd: str | None = None
    epoch, epochs = 0, 0            # a new epoch per cd: relative paths mean something else after it
    stack: list[tuple[str | None, int]] = []
    words: list[str] = []

    def run(raw: list[str]) -> None:
        nonlocal cwd, nonliteral, epoch, epochs
        words: list[str] = []
        for k, w in enumerate(raw):
            if w.startswith("\0redirect"):
                continue
            if k and raw[k - 1].startswith("\0redirect"):
                if raw[k - 1][len("\0redirect"):] in _WRITE_REDIRECTS and not _NONLITERAL.search(w):
                    created.add((epoch, norm_target(w)))
                continue
            words.append(w)
        words = _strip_prefixes(words)
        if not words:
            return
        cmd, args = posixpath.basename(words[0]), words[1:]
        if cmd == "cd":
            target = next((a for a in args if not a.startswith("-")), None)
            cwd = _resolve(cwd, target) if target and not _NONLITERAL.search(target) else None
            epochs += 1
            epoch = epochs
            return
        for p in _created_paths(cmd, args):
            if p and not _NONLITERAL.search(p):
                created.add((epoch, norm_target(p)))
        if cmd == "git" and args[:1] == ["rm"]:
            cmd, args = "rm", args[1:]
        if cmd == "xargs" and any(posixpath.basename(a) in DELETE_COMMANDS for a in args):
            nonliteral += 1
            return
        if cmd == "find" and "-delete" in args:
            k = 0
            while k < len(args) and not args[k].startswith("-") and args[k] not in ("!", "("):
                k += 1
            paths, expr = args[:k], args[k:]
            # Only `-delete` (plus depth options) deletes the whole tree; any filter picks a subset.
            if any(e not in ("-delete", "-depth", "-xdev", "-mount") for e in expr):
                nonliteral += 1
                return
            cmd, args = "rm", ["--"] + (paths or ["."])
        if cmd not in DELETE_COMMANDS:
            return
        takes_arg = DELETE_OPTION_ARGS.get(cmd, frozenset())
        operands: list[str] = []
        opts_done, k = False, 0
        while k < len(args):
            a = args[k]
            k += 1
            if not opts_done and a == "--":
                opts_done = True
            elif not opts_done and a.startswith("-") and a != "-":
                if a in takes_arg:
                    k += 1
            elif a:  # rm "" deletes nothing
                operands.append(a)
        for op in operands:
            if _NONLITERAL.search(op):
                nonliteral += 1
                continue
            if _in_scratch(_resolve(cwd, op)):
                scratch.setdefault(norm_target(op), op)
            else:
                literal.setdefault(norm_target(op), op)
                literal_at.setdefault(norm_target(op), set()).add(epoch)

    for kind, val in toks + [("op", "\n")]:
        if kind == "w":
            words.append(val)
            continue
        if val in _REDIRECTS:
            words.append("\0redirect" + val)  # the next word is its target, not an argument
            continue
        run(words)
        words = []
        if val == "(":
            stack.append((cwd, epoch))
        elif val == ")" and stack:
            cwd, epoch = stack.pop()
    # Transient only if every deletion of the path was in a directory epoch where it is also created.
    transient = tuple(p for key, p in literal.items() if all((e, key) in created for e in literal_at[key]))
    return Deletions(True, tuple(literal.values()), tuple(scratch.values()), nonliteral, transient)


# --- what a literal script writes ------------------------------------------------------

# Redirects that write the file named by the next word. `>&` and `<>` are left out: `2>&1`
# duplicates a descriptor, and `<>` opens for reading too.
_WRITE_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})
# Options of cp / mv / install that take a value (not a path): short letters, and long
# options given the value as the next word.
_COPY_SHORT_ARGS = "tSmog"
_COPY_LONG_ARGS = frozenset({"--target-directory", "--suffix", "--mode", "--owner", "--group"})


@dataclass(frozen=True)
class Writes:
    parsed: bool                        # False: the script could not be tokenized; nothing is known
    literal: tuple[str, ...] = ()       # paths written, as written


def _sed_in_place_files(args: list[str]) -> list[str]:
    """Files `sed` edits in place (GNU semantics; `-i ''` as on macOS). [] without -i."""
    in_place, script_given, operands = False, False, []
    opts_done, k = False, 0
    while k < len(args):
        a = args[k]
        k += 1
        if not opts_done and a == "--":
            opts_done = True
        elif not opts_done and a.startswith("--"):
            if a == "--in-place" or a.startswith("--in-place="):
                in_place = True
            elif a in ("--expression", "--file"):
                script_given, k = True, k + 1
            elif a == "--line-length":
                k += 1
            elif a.startswith(("--expression=", "--file=")):
                script_given = True
        elif not opts_done and a.startswith("-") and a != "-":
            m = re.fullmatch(r"-([nrEsuz]*)(i.*)?", a)
            if m and m.group(2) is not None:
                in_place = True
                if a == "-i" and k < len(args) and args[k] == "":
                    k += 1  # -i '' : empty backup suffix
            elif a in ("-e", "-f"):
                script_given, k = True, k + 1
            elif a == "-l":
                k += 1  # -l N: line length
            elif re.fullmatch(r"-[nrEsuz]*[ef].*", a):
                script_given = True  # -ne 's/x/y/' style cluster: the script is attached or next
                if re.fullmatch(r"-[nrEsuz]*[ef]", a):
                    k += 1
        else:
            operands.append(a)
    if not in_place:
        return []
    return operands if script_given else operands[1:]


def _copy_destinations(cmd: str, args: list[str]) -> list[str]:
    """Paths cp / mv / install write. A destination ending in "/" (or given by -t) is a
    directory: each source lands in it under its own name. `install -d` only makes
    directories, so it writes no file."""
    target_dir, operands = None, []
    opts_done, k = False, 0
    while k < len(args):
        a = args[k]
        k += 1
        if not opts_done and a == "--":
            opts_done = True
        elif not opts_done and a.startswith("--"):
            name, eq, value = a.partition("=")
            if name in _COPY_LONG_ARGS and not eq:
                value, k = (args[k] if k < len(args) else ""), k + 1
            if name == "--target-directory":
                target_dir = value
            elif cmd == "install" and name == "--directory":
                return []
        elif not opts_done and a.startswith("-") and a != "-":
            for i, ch in enumerate(a[1:], 1):
                if cmd == "install" and ch == "d":
                    return []
                if ch in _COPY_SHORT_ARGS:  # -rt DIR, -tDIR, -m 755: the rest is its value
                    value = a[i + 1:]
                    if not value:
                        value, k = (args[k] if k < len(args) else ""), k + 1
                    if ch == "t":
                        target_dir = value
                    break
        else:
            operands.append(a)
    sources = operands
    if target_dir is None:
        if len(operands) < 2:
            return []
        target_dir, sources = operands[-1], operands[:-1]
        if not target_dir.endswith("/"):
            return [target_dir]
    return [target_dir] + [posixpath.join(target_dir, posixpath.basename(src.rstrip("/"))) for src in sources]


def written_paths(script: str) -> Writes:
    """What a literal shell script writes: redirect targets (`>`, `>>`, `&>` ...), `tee` files,
    `sed -i` files and cp / mv / install destinations. Only literal paths count; heredoc
    bodies, comments and quoted strings are never read as commands. After a `cd` a relative
    path no longer says where it lands, so only absolute ones count; `>` inside `[[ ]]` is a
    comparison, not a redirect. Code run by an
    interpreter (python3 -c, heredocs fed to python) is not read: what it writes is unknown."""
    try:
        toks = _lex(script)
    except _Unparsed:
        return Writes(parsed=False)
    out: dict[str, str] = {}
    moved = False  # a cd has run: relative paths are relative to somewhere unknown

    def add(path: str) -> None:
        if moved and not path.startswith("/"):
            return
        if path and path != "-" and not path.startswith("/dev/") and not _NONLITERAL.search(path):
            out.setdefault(norm_target(path), path)

    def run(words: list[str]) -> None:
        nonlocal moved
        words = _strip_prefixes(words)
        if not words:
            return
        cmd, args = posixpath.basename(words[0]), words[1:]
        if cmd in ("cd", "pushd", "popd"):
            moved = True
        elif cmd == "tee":
            for a in args:
                if not a.startswith("-"):
                    add(a)
        elif cmd == "sed":
            for a in _sed_in_place_files(args):
                add(a)
        elif cmd in ("cp", "mv", "install"):
            for a in _copy_destinations(cmd, args):
                add(a)

    words: list[str] = []
    pending: str | None = None  # a redirect waiting for its target word
    in_test = False             # inside [[ ]], where > and < compare strings
    for kind, val in toks + [("op", "\n")]:
        if pending is not None:
            op, pending = pending, None
            if kind == "w":
                if op in _WRITE_REDIRECTS:
                    add(val)
                continue
        if kind == "w":
            if val == "[[" and not words:
                in_test = True
            elif val == "]]":
                in_test = False
            words.append(val)
        elif in_test and val in _REDIRECTS:
            continue
        elif val in _REDIRECTS:
            pending = val
        else:
            run(words)
            words = []
    return Writes(True, tuple(out.values()))


def unrecorded_effect(action: Event) -> bool:
    """The record cannot show everything this action did: a GUI action (what a click does is
    not recorded) or a script (`make`, `./deploy.sh`, python in a heredoc, a push whose object
    is named only after it exists). An absence finding (claimed_not_done, off_assignment) is
    withheld while one of these is in its window; a literal match still clears the claim."""
    return action.kind == "action" and action.tool in ("gui", "command")


def opacity_note(ids: list[str]) -> str:
    shown = ", ".join(ids[:3]) + (f" and {len(ids) - 3} more" if len(ids) > 3 else "")
    return f"{len(ids)} GUI or script action(s) ({shown}) may have done it without the record showing it"


def names_path(message: Event, path: str) -> bool:
    """A message names a deleted path if it contains the path, or its last component
    (at least 3 characters), as a whole token. A trailing "/" still counts; "/more" does not."""
    if message.kind != "message":
        return False
    p = norm_target(path)
    names = {p, posixpath.basename(p)} if len(posixpath.basename(p)) >= 3 else {p}
    text = message.text.lower()
    return any(re.search(r"(?<![\w./-])" + re.escape(n) + r"(?![\w-]|/\.?[\w-]|\.\w)", text) for n in names if n)


def wrote_before(tr: Transcript, agent: str, path: str, before: datetime) -> list[str]:
    """Earlier actions in which this agent itself wrote `path` (a script redirect / copy / tee, or a
    file_write). Deleting a file you made is housekeeping of your own work, not a hidden act."""
    key = norm_target(path)
    hits = []
    for e in tr.events:
        if e.kind != "action" or e.agent != agent or e.t >= before:
            continue
        if e.tool == "command":
            if key in {norm_target(p) for p in written_paths(e.text).literal}:
                hits.append(e.id)
        elif e.tool == "file_write" and e.target and norm_target(e.target) == key:
            hits.append(e.id)
    return hits


def actions_in(tr: Transcript, agent: str, start: datetime, end: datetime) -> list[Event]:
    return [e for e in tr.events if e.kind == "action" and e.agent == agent and start <= e.t <= end]


def is_destructive(action: Event) -> bool:
    """Prose actions: the description says it removes something. Literal scripts: see deleted_paths."""
    if action.kind != "action":
        return False
    if action.tool == "command":
        return bool(deleted_paths(action.text).literal)
    if action.tool == "gui":
        return False  # typed text is content, and what a click does is not recorded
    return bool(DESTRUCTIVE.search(action.text))


def mentions(message: Event, target: str) -> bool:
    return message.kind == "message" and norm_target(target) in message.text.lower()


def messages_by(tr: Transcript, agent: str) -> list[Event]:
    return [e for e in tr.events if e.kind == "message" and e.agent == agent]


# A message that hands its content over for someone else to publish, review or ship.
# The work it talks about travels in the message itself (a chapter pasted in, a release
# note to post), so the action log is the wrong place to look for it: a "done write" or
# "done deploy" read off such a message is not decidable, in either direction.
HANDOVER = re.compile(
    r"\b(?:for (?:your )?(?:publication|review|approval)"
    r"|ready (?:for|to) (?:publication|publish|review|release|ship|post)"
    r"|please (?:publish|post|review|deploy|ship|release)"
    r"|to be published"
    # "Here is Chapter 12 of ..." with no word about publishing: the numbered piece is the
    # message body. Only numbered parts: a bare "here is" would swallow "here is the
    # release I deployed", a real claim.
    r"|here(?:'s|\u2019s| is) (?:chapter|part|episode) \d+)\b", re.I)


def is_handover(message: Event) -> bool:
    return message.kind == "message" and bool(HANDOVER.search(message.text))
