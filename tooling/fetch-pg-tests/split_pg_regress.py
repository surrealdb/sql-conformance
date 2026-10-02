#!/usr/bin/env python3
"""Split PostgreSQL regression-test scripts (src/test/regress/sql/*.sql) into
the statements psql would send to the server, and classify each statement
against the matching expected output (src/test/regress/expected/*.out).

The splitter mirrors psql's lexer (src/fe_utils/psqlscan.l) closely enough for
the regression suite:
  * '--' and nested '/* */' comments, standard '' strings, E'' strings with
    backslash escapes, "" quoted identifiers, $tag$ dollar quoting;
  * semicolons only terminate at paren depth 0 and outside the psql
    BEGIN ... END heuristic used for CREATE FUNCTION/PROCEDURE ... BEGIN ATOMIC;
  * backslash meta-commands consume the rest of the line; \\g, \\gx, \\gset,
    \\gexec, \\gdesc, \\crosstabview, \\watch, \\sendpipeline, \\parse send the
    buffer; \\r discards it; \\; splits a multi-statement send;
  * COPY ... FROM STDIN and \\copy ... FROM STDIN data blocks are skipped up to
    the '\\.' terminator;
  * psql variables (\\set, \\getenv, \\gset) are tracked and :name / :'name' /
    :"name" references to *defined* variables are substituted (the raw text is
    kept as well).

Output: JSON lines, one per statement:
  {file, line, end_line, sql_raw, sql, psql_var, terminator, expected}
where expected is one of ok | error | syntax_error | unaligned | shared_line.
"""
import json
import os
import re
import sys

IDENT_START = re.compile(r"[A-Za-z_\x80-￿]")
IDENT_CHAR = re.compile(r"[A-Za-z0-9_$\x80-￿]")
DOLLAR_TAG = re.compile(r"\$([A-Za-z_\x80-￿][A-Za-z0-9_\x80-￿]*)?\$")
COPY_FROM_STDIN = re.compile(r"^\s*copy\b.*\bfrom\s+std(in|out)\b", re.I | re.S)
PSQL_BUILTIN_VARS = ["LAST_ERROR_MESSAGE", "LAST_ERROR_SQLSTATE", "DBNAME", "USER", "HOST", "PORT",
                     "ENCODING", "VERSION", "VERSION_NAME", "VERSION_NUM", "SERVER_VERSION_NAME",
                     "SERVER_VERSION_NUM", "ROW_COUNT", "ERROR", "SQLSTATE", "SHELL_ERROR",
                     "SHELL_EXIT_CODE", "PIPELINE_SYNC_COUNT", "PIPELINE_COMMAND_COUNT",
                     "PIPELINE_RESULT_COUNT", "SERVICE"]
BSCOPY_FROM_STDIN = re.compile(r"\bfrom\s+(p?stdin)\b", re.I)
SCS_SET = re.compile(r"^\s*(?:set\s+(?:session\s+|local\s+)?standard_conforming_strings\s*(?:=|to)\s*('?\w+'?)|reset\s+standard_conforming_strings)\s*$", re.I)
GSET_LITERAL = re.compile(r"^\s*select\s+(?:(\$\w*\$)(.*?)\1|'((?:[^']|'')*)')\s+as\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", re.I | re.S)
SEND_CMDS = {"g", "gx", "gset", "gexec", "gdesc", "crosstabview", "watch",
             "sendpipeline", "parse"}
# meta-commands whose argument is the whole rest of the line (psql OT_WHOLE_LINE)
WHOLE_LINE_CMDS = {"copy", "!", "sf", "sf+", "sv", "sv+", "ef", "ev", "h", "help"}


class Splitter:
    def __init__(self, text, fname):
        self.t = text
        self.n = len(text)
        self.fname = fname
        self.i = 0
        self.line = 1
        self.out = []
        # psql variables: name -> value (str) or None (defined, value unknown)
        self.vars = {k: None for k in PSQL_BUILTIN_VARS}
        self.cond = []          # \if stack: list of [parent_active, taken, active]
        self.copy_data_lines = set()
        self.reset_buf()
        self.pending_copy = 0
        self.scs = True  # standard_conforming_strings

    def reset_buf(self):
        self.buf = []           # list of (raw_piece, subst_piece)
        self.has_content = False
        self.paren = 0
        self.begin_depth = 0
        self.idents = []
        self.ident_count = 0
        self.start_line = None
        self.used_var = False

    # -- buffer helpers -------------------------------------------------
    def active(self):
        return all(c[2] for c in self.cond)

    def eval_cond(self, args):
        a = args.strip()
        m = re.match(r":['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?$", a)
        if m:
            a = self.vars.get(m.group(1)) or "false"
        return a.strip("'").lower() in ("true", "on", "yes", "1", "t", "y")

    def add(self, raw, subst=None, content=True):
        if not self.active():
            return
        if subst is None:
            subst = raw
        if content and not self.has_content and raw.strip():
            self.has_content = True
            self.start_line = self.line
        self.buf.append((raw, subst))

    def emit(self, terminator):
        raw = "".join(p[0] for p in self.buf).strip()
        sub = "".join(p[1] for p in self.buf).strip()
        if self.has_content and raw and self.active():
            rec = {
                "file": self.fname,
                "line": self.start_line,
                "end_line": self.line,
                "sql_raw": raw,
                "sql": sub,
                "psql_var": self.used_var,
                "terminator": terminator,
            }
            self.out.append(rec)
            m = SCS_SET.match(sub)
            if m:
                self.scs = m.group(1) is None or m.group(1).lower().strip("'") in ("on", "true")
            if COPY_FROM_STDIN.match(sub):
                self.pending_copy += 1
        self.reset_buf()

    def skip_copy_data(self):
        # Called at a real send point: skip one data block per pending COPY FROM STDIN.
        while self.pending_copy > 0:
            self.pending_copy -= 1
            self.skip_to_backslash_dot()

    def skip_to_backslash_dot(self):
        # advance to the start of the next line first
        nl = self.t.find("\n", self.i)
        if nl < 0:
            self.i = self.n
            return
        self.i = nl + 1
        self.line += 1
        while self.i < self.n:
            nl = self.t.find("\n", self.i)
            end = self.n if nl < 0 else nl
            ln = self.t[self.i:end]
            self.copy_data_lines.add(self.line)
            self.i = end + 1 if nl >= 0 else self.n
            self.line += 1
            if ln.rstrip("\r") == "\\.":
                return

    def adv(self, k):
        seg = self.t[self.i:self.i + k]
        self.line += seg.count("\n")
        self.i += k
        return seg

    # -- psql variable substitution ---------------------------------------
    def subst_var(self):
        """At ':' — try :'name', :"name", :name. Returns True if handled."""
        t, i = self.t, self.i
        if i + 1 < self.n and t[i + 1] == ":":
            return False  # '::' cast
        if i > 0 and t[i - 1] == ":":
            return False
        m = re.compile(r":\{\?([A-Za-z_][A-Za-z0-9_]*)\}").match(t, i)
        if m:
            self.used_var = True
            self.add(self.adv(m.end() - i), "TRUE" if m.group(1) in self.vars else "FALSE")
            return True
        m = re.compile(r":(['\"])([A-Za-z_][A-Za-z0-9_]*)\1").match(t, i)
        if m and m.group(2) in self.vars:
            val = self.vars[m.group(2)]
            if val is None:
                val = "psql_" + m.group(2)
            if m.group(1) == "'":
                rep = "'" + val.replace("'", "''") + "'"
            else:
                rep = '"' + val.replace('"', '""') + '"'
            self.used_var = True
            self.add(self.adv(m.end() - i), rep)
            return True
        m = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)").match(t, i)
        if m and m.group(1) in self.vars:
            val = self.vars[m.group(1)]
            if val is None:
                val = "0"
            self.used_var = True
            self.add(self.adv(m.end() - i), val)
            return True
        return False

    def eval_set_args(self, args):
        """Evaluate the value of '\\set name args...' (concatenation)."""
        out = []
        j = 0
        while j < len(args):
            c = args[j]
            if c.isspace():
                j += 1
                continue
            if c == "'":
                k = j + 1
                s = []
                while k < len(args):
                    if args[k] == "'" and k + 1 < len(args) and args[k + 1] == "'":
                        s.append("'")
                        k += 2
                    elif args[k] == "'":
                        k += 1
                        break
                    else:
                        s.append(args[k])
                        k += 1
                out.append("".join(s))
                j = k
                continue
            if c == ":":
                m = re.compile(r":['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?").match(args, j)
                if m:
                    v = self.vars.get(m.group(1))
                    out.append(v if v is not None else "psql_" + m.group(1))
                    j = m.end()
                    continue
            if c == "`":
                k = args.find("`", j + 1)
                out.append("shell_output")
                j = len(args) if k < 0 else k + 1
                continue
            k = j
            while k < len(args) and not args[k].isspace() and args[k] not in "':`":
                k += 1
            out.append(args[j:k] if k > j else args[j])
            j = max(k, j + 1)
        return "".join(out)

    # -- meta commands ------------------------------------------------------
    def meta(self):
        t = self.t
        j = self.i + 1
        if j < self.n and t[j] == ";":
            # '\;' : psql inserts a literal ';' and keeps accumulating, sending all at once.
            self.adv(2)
            self.emit("\\;")
            return
        if j < self.n and t[j] == "\\":
            self.adv(2)
            return
        k = j
        while k < self.n and not t[k].isspace() and t[k] != "\\":
            k += 1
        cmd = t[j:k]
        nl = t.find("\n", k)
        eol = self.n if nl < 0 else nl
        if cmd in WHOLE_LINE_CMDS:
            end = eol
        else:
            # psql argument lexing: quoted args may contain backslashes; an unquoted
            # backslash starts the next meta-command on the same line.
            end = k
            while end < eol:
                ch = t[end]
                if ch in "'\"`":
                    q = ch
                    end += 1
                    while end < eol and t[end] != q:
                        if q == "'" and t[end] == "\\":
                            end += 1
                        end += 1
                    end += 1
                    continue
                if ch == "\\":
                    break
                end += 1
            end = min(end, eol)
        args = t[k:end]
        self.i = end  # stay before the newline / next meta-command
        if cmd == "if":
            pa = self.active()
            v = pa and self.eval_cond(args)
            self.cond.append([pa, v, v])
            return
        if cmd == "elif" and self.cond:
            c = self.cond[-1]
            v = c[0] and not c[1] and self.eval_cond(args)
            c[2] = v
            c[1] = c[1] or v
            return
        if cmd == "else" and self.cond:
            c = self.cond[-1]
            c[2] = c[0] and not c[1]
            c[1] = True
            return
        if cmd == "endif" and self.cond:
            self.cond.pop()
            return
        if not self.active():
            return
        if cmd in ("q", "quit"):
            self.i = self.n
            return
        if cmd in SEND_CMDS:
            if cmd == "gset":
                prefix = args.strip()
                names = re.findall(r"\bAS\s+(\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)", "".join(p[1] for p in self.buf), re.I)
                for nm in names:
                    self.vars[prefix + nm.strip('"')] = None
                body = "".join(p[1] for p in self.buf)
                lm = GSET_LITERAL.match(body)
                if lm:
                    val = lm.group(2) if lm.group(2) is not None else lm.group(3).replace("''", "'")
                    self.vars[prefix + lm.group(4)] = val
            self.emit("\\" + cmd)
            self.skip_copy_data()
        elif cmd == "r" or cmd == "reset":
            self.reset_buf()
        elif cmd == "set":
            parts = args.strip().split(None, 1)
            if parts:
                self.vars[parts[0]] = self.eval_set_args(parts[1]) if len(parts) > 1 else ""
        elif cmd == "unset":
            parts = args.split()
            if parts:
                self.vars.pop(parts[0], None)
        elif cmd == "getenv":
            parts = args.split()
            if parts:
                self.vars[parts[0]] = "/psql_env/" + (parts[1] if len(parts) > 1 else parts[0])
        elif cmd == "copy":
            if BSCOPY_FROM_STDIN.search(args):
                self.skip_to_backslash_dot()
                return
        # any other meta command: ignored, buffer kept

    # -- main loop ----------------------------------------------------------
    def run(self):
        t = self.t
        while self.i < self.n:
            c = t[self.i]
            nxt = t[self.i + 1] if self.i + 1 < self.n else ""
            if c == "-" and nxt == "-":
                nl = t.find("\n", self.i)
                end = self.n if nl < 0 else nl
                seg = t[self.i:end]
                if self.has_content:
                    self.add(seg, content=False)
                self.i = end
                continue
            if c == "/" and nxt == "*":
                depth = 0
                k = self.i
                while k < self.n:
                    if t.startswith("/*", k):
                        depth += 1
                        k += 2
                    elif t.startswith("*/", k):
                        depth -= 1
                        k += 2
                        if depth == 0:
                            break
                    else:
                        k += 1
                seg = self.adv(k - self.i)
                # psql keeps C-style comments in the buffer; they do not make it non-empty
                # for our purposes unless followed by real content.
                if self.has_content:
                    self.add(seg, content=False)
                else:
                    self.buf.append((seg, seg))
                continue
            if c == "'":
                prev = t[self.i - 1] if self.i > 0 else ""
                prev2 = t[self.i - 2] if self.i > 1 else ""
                ext = (prev in "eE" and not (prev2 and IDENT_CHAR.match(prev2))) or not self.scs
                k = self.i + 1
                while k < self.n:
                    if ext and t[k] == "\\":
                        k += 2
                        continue
                    if t[k] == "'":
                        if k + 1 < self.n and t[k + 1] == "'":
                            k += 2
                            continue
                        k += 1
                        break
                    k += 1
                self.add(self.adv(k - self.i))
                continue
            if c == '"':
                k = self.i + 1
                while k < self.n:
                    if t[k] == '"':
                        if k + 1 < self.n and t[k + 1] == '"':
                            k += 2
                            continue
                        k += 1
                        break
                    k += 1
                self.add(self.adv(k - self.i))
                continue
            if c == "$":
                prev = t[self.i - 1] if self.i > 0 else ""
                m = None if (prev and IDENT_CHAR.match(prev)) else DOLLAR_TAG.match(t, self.i)
                if m:
                    tag = m.group(0)
                    close = t.find(tag, m.end())
                    k = self.n if close < 0 else close + len(tag)
                    self.add(self.adv(k - self.i))
                    continue
                self.add(self.adv(1))
                continue
            if c == ":" and self.subst_var():
                continue
            if c == "\\":
                self.meta()
                continue
            if not self.active() and c in "();":
                self.adv(1)
                continue
            if c == "(":
                self.paren += 1
                self.add(self.adv(1))
                continue
            if c == ")":
                if self.paren > 0:
                    self.paren -= 1
                self.add(self.adv(1))
                continue
            if c == ";":
                if self.paren == 0 and self.begin_depth == 0:
                    self.adv(1)
                    self.emit(";")
                    self.skip_copy_data()
                    continue
                self.add(self.adv(1))
                continue
            if IDENT_START.match(c):
                k = self.i + 1
                while k < self.n and IDENT_CHAR.match(t[k]):
                    k += 1
                word = t[self.i:k]
                if self.active():
                    self.track_ident(word)
                self.add(self.adv(k - self.i))
                continue
            if c.isdigit():
                k = self.i + 1
                while k < self.n and (t[k].isalnum() or t[k] in "._"):
                    k += 1
                self.add(self.adv(k - self.i))
                continue
            if c.isspace():
                if self.has_content:
                    self.add(self.adv(1), content=False)
                else:
                    self.adv(1)
                continue
            self.add(self.adv(1))
        if self.has_content:
            self.emit("EOF")
        return self.out

    def track_ident(self, word):
        # Mirrors psqlscan.l's CREATE [OR REPLACE] {FUNCTION|PROCEDURE} ... BEGIN ... END heuristic.
        lw = word.lower()
        if self.ident_count == 0:
            self.idents = []
        if lw in ("create", "function", "procedure", "or", "replace"):
            if len(self.idents) == self.ident_count and self.ident_count < 4:
                self.idents.append(lw[0])
        self.ident_count += 1
        ids = self.idents + ["\0"] * 4
        if (ids[0] == "c" and (ids[1] in ("f", "p") or (ids[1] == "o" and ids[2] == "r" and ids[3] in ("f", "p"))) and self.paren == 0):
            if lw == "begin":
                self.begin_depth += 1
            elif lw == "case":
                if self.begin_depth >= 1:
                    self.begin_depth += 1
            elif lw == "end":
                if self.begin_depth > 0:
                    self.begin_depth -= 1


def classify(sql_lines, out_lines, stmts, copy_lines):
    """Align echoed input lines in the .out file and attach the server response
    (ok / error / syntax_error) to each statement by its end line."""
    echo = {}
    p = 0
    nonempty = [j for j, l in enumerate(sql_lines, 1) if l.strip() and j not in copy_lines]
    unaligned = 0
    for j in nonempty:
        target = sql_lines[j - 1].rstrip("\n")
        found = -1
        lim = min(len(out_lines), p + 4000)
        for k in range(p, lim):
            if out_lines[k].rstrip("\n") == target:
                found = k
                break
        if found < 0:
            unaligned += 1
            continue
        echo[j] = found
        p = found + 1
    aligned = sorted(echo)
    end_counts = {}
    for s in stmts:
        end_counts[s["end_line"]] = end_counts.get(s["end_line"], 0) + 1
    for s in stmts:
        L = s["end_line"]
        if L not in echo:
            s["expected"] = "unaligned"
            continue
        # next aligned line after L
        nxt = None
        for j in aligned:
            if j > L:
                nxt = j
                break
        a = echo[L] + 1
        b = echo[nxt] if nxt is not None else len(out_lines)
        region = out_lines[a:b]
        syn = any(r.startswith("ERROR:  syntax error") for r in region)
        err = any(r.startswith("ERROR:") for r in region)
        if syn:
            s["expected"] = "syntax_error"
        elif err:
            s["expected"] = "error"
        else:
            s["expected"] = "ok"
        if end_counts[L] > 1:
            s["shared_line"] = True
        msgs = [r.rstrip("\n") for r in region if r.startswith("ERROR:")]
        if msgs:
            s["error_msgs"] = msgs[:3]
    return unaligned, len(nonempty)


def main():
    regress = sys.argv[1]
    out_path = sys.argv[2]
    sql_dir = os.path.join(regress, "sql")
    exp_dir = os.path.join(regress, "expected")
    total_unaligned = total_lines = 0
    n = 0
    with open(out_path, "w") as fo:
        for fn in sorted(os.listdir(sql_dir)):
            if not fn.endswith(".sql"):
                continue
            path = os.path.join(sql_dir, fn)
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            sp = Splitter(text, fn[:-4])
            stmts = sp.run()
            exp = os.path.join(exp_dir, fn[:-4] + ".out")
            if os.path.exists(exp):
                with open(exp, encoding="utf-8", errors="replace") as f:
                    out_lines = f.readlines()
                u, tl = classify(text.split("\n"), out_lines, stmts, sp.copy_data_lines)
                total_unaligned += u
                total_lines += tl
            else:
                for s in stmts:
                    s["expected"] = "no_expected_file"
            for s in stmts:
                fo.write(json.dumps(s, ensure_ascii=False) + "\n")
                n += 1
    print(f"statements={n} sql_nonempty_lines={total_lines} unaligned_lines={total_unaligned}", file=sys.stderr)


if __name__ == "__main__":
    main()
