"""Literal shell scripts (tool "command"): what they delete, and done_not_said on them.

All scripts here are invented. The parser is judged on what it must NOT read as a
deletion (comments, heredoc bodies, quoted strings, redirection targets) as much as on
what it must find.
"""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from saidvsdid.check import check
from saidvsdid.match import propose
from saidvsdid.model import Cite, Event, Finding, Transcript
from saidvsdid.model import Claim
from saidvsdid.rules import (Deletions, Writes, action_matches, deleted_paths, is_destructive, names_path,
                             written_paths)

T0 = datetime(2026, 10, 3, tzinfo=timezone.utc)


def ev(n, agent, kind, text, tool=None):
    return Event(f"e{n}", T0 + timedelta(seconds=n), agent, kind, text, (), tool,
                 text if kind == "action" else None)


def lit(*paths, scratch=(), nonliteral=0):
    return Deletions(True, tuple(paths), tuple(scratch), nonliteral)


class Parser(unittest.TestCase):
    CASES = [
        ("rm -rf ./old-reports/", lit("./old-reports/")),
        ("rm a;rm b&&rm c", lit("a", "b", "c")),
        ("rm -rf a \\\n  b", lit("a", "b")),
        ("sudo rm -f -- -weird /srv/x", lit("-weird", "/srv/x")),
        ("git rm docs/a.md", lit("docs/a.md")),
        ("find build -delete", lit("build")),
        ("find build -name x -delete", lit(nonliteral=1)),  # a filter deletes a subset, not "build"
        ("find . -name '*.pyc' -delete", lit(nonliteral=1)),
        ("if rm -f lock; then echo ok; fi", lit("lock")),
        ("for f in a b; do\n  rm c\ndone", lit("c")),
        ("x=$(rm -rf gone) ; echo", lit("gone")),
        ("rm file#1", lit("file#1")),
        ("rmdir empty && unlink link && shred secret", lit("empty", "link", "secret")),
        # Not deletions: comments, strings, heredoc bodies, other programs.
        ("# remove old\necho \"rm x\"", lit()),
        ("echo hi # rm x\nrm y", lit("y")),
        ("echo hi # a; rm x && rm w\nrm y", lit("y")),
        ("# Let's clean up\nrm z", lit("z")),
        ("cat > f.py << 'EOF'\nos.remove('a')\nrm b\nEOF\nrm c", lit("c")),
        ("cat <<-EOF\n\trm b\n\tEOF\nls", lit()),
        ("cat <<-EOF\n\trm b\n\tEOF\nrm c", lit("c")),
        ("python3 -c \"\nimport os\n# rm z\nos.remove(1)\n\"\nrm -r data/", lit("data/")),
        ("git reset --hard origin/main", lit()),
        ("docker run --rm img", lit()),
        # Redirection targets are not arguments.
        ("rm -f a.log >> out.txt 2>&1", lit("a.log")),
        ("rm x </dev/null 2>/dev/null", lit("x")),
        # Scratch: absolute, or relative after a literal cd into a scratch dir.
        ("X=1 rm /tmp/q", lit(scratch=("/tmp/q",))),
        ("cd /tmp && rm -rf clone\ngit clone u clone\nrm -f notes", lit(scratch=("clone", "notes"))),
        ("cd /tmp/w && cd .. && rm -rf keep", lit(scratch=("keep",))),
        ("cd /srv && rm -rf site", lit("site")),
        ("cd $D && rm -rf site", lit("site")),
        ("cd /tmp && cd $D && rm -rf site", lit("site")),  # unknown cwd is not scratch
        # Not literal: variables, globs, braces, home, stdin.
        ("rm $X *.tmp", lit(nonliteral=2)),
        ("rm \"${D}/a\" b", lit("b", nonliteral=1)),
        ("rm a{1,2} ~/x", lit(nonliteral=2)),
        ("rm ${X:-a b} c", lit("c", nonliteral=1)),  # one parameter expansion, one word
        ("ls | xargs rm", lit(nonliteral=1)),
        # Independent review, 2026-09-28: each input below was a reproduced divergence.
        ('echo "#hi"; rm -rf data', lit("data")),           # quoted # is text, not a comment
        ("grep '#include' f && rm -rf build", lit("build")),
        ("tr a-z A-Z <<< hello\nrm -rf data", lit("data")),  # here-string, not a heredoc
        ("echo $((1<<n))\nrm -rf data", lit("data")),        # arithmetic shift
        ("((x = 1 << 2))\nrm -rf data", lit("data")),
        ('echo "<<EOF"\nrm -rf data', lit("data")),          # quoted <<
        ("# cat <<EOF\nrm -rf data", lit("data")),           # << in a comment
        ("cat <<EOF\r\nrm x\r\nEOF\r\nrm -rf data", lit("data")),  # CRLF terminator
        ("(cd /tmp && rm -rf clone); rm -rf important", lit("important", scratch=("clone",))),
        ("ls&&(rm -rf data)", lit("data")),
        ("ls;(rm -rf data)", lit("data")),
        ("x=`rm -rf gone`", lit("gone")),
        ("sudo -u www rm -rf /srv/site", lit("/srv/site")),
        ("env A=1 rm -f lock", lit("lock")),
        ("nice -n 5 rm old", lit("old")),
        ('rm -f "" /srv/data', lit("/srv/data")),
        ("shred -n 3 -u secret", lit("secret")),
        ("rm ./x x", lit("./x")),                           # one path, one entry
        ("cat <<'EOF' > f\nrm inside\nEOF\nrm outside", lit("outside")),
    ]

    def test_table(self):
        for script, want in self.CASES:
            with self.subTest(script=script):
                self.assertEqual(deleted_paths(script), want)

    def test_unparseable_is_not_empty(self):
        self.assertEqual(deleted_paths('echo "unterminated\nrm x'), Deletions(parsed=False))
        self.assertEqual(deleted_paths("echo 'unterminated\nrm x"), Deletions(parsed=False))
        self.assertEqual(deleted_paths("echo `rm x"), Deletions(parsed=False))

    def test_unparseable_scripts_still_flag_every_delete_word(self):
        from saidvsdid.rules import DELETE_HINT
        for word in ("rm", "rmdir", "unlink", "shred", "-delete", "os.remove"):
            with self.subTest(word=word):
                self.assertTrue(DELETE_HINT.search(f'echo "x\n{word} y'))


class Mentions(unittest.TestCase):
    def test_boundaries(self):
        m = lambda text: ev(0, "a", "message", text)
        self.assertTrue(names_path(m("Cleaned out old-reports."), "./old-reports/"))
        self.assertTrue(names_path(m("removed srv/site/old-reports"), "srv/site/old-reports"))
        self.assertTrue(names_path(m("dropped old-reports"), "srv/site/old-reports"))  # last component
        self.assertTrue(names_path(m("I removed old-reports/ as planned."), "./old-reports/"))
        self.assertTrue(names_path(m("removed srv/site/old-reports/."), "srv/site/old-reports"))
        self.assertFalse(names_path(m("see old-reports/keep"), "old-reports"))
        self.assertFalse(names_path(m("see old-reports-2"), "old-reports"))
        self.assertFalse(names_path(m("see old-reports.bak"), "old-reports"))
        self.assertFalse(names_path(m("see x/old-reports"), "y/old-reports/z"))
        self.assertFalse(names_path(m("a b c"), "dir/a"))  # a last component under 3 chars does not count
        self.assertFalse(names_path(ev(0, "a", "observation", "old-reports"), "old-reports"))


class Rules(unittest.TestCase):
    def test_gui_is_never_destructive(self):
        self.assertFalse(is_destructive(ev(0, "a", "action", "type please remove this", "gui")))

    def test_prose_shell_keeps_the_word_rule(self):
        self.assertTrue(is_destructive(ev(0, "a", "action", "remove directory old/", "shell")))

    def test_delete_claim_matches_a_deleted_path_exactly(self):
        a = ev(0, "a", "action", "cd /srv && rm -rf ./old-reports/ && echo done", "command")
        self.assertTrue(action_matches(a, "a", "delete", "old-reports"))
        self.assertFalse(action_matches(a, "a", "delete", "old"))
        self.assertFalse(action_matches(a, "a", "delete", "/srv/old-reports"))  # no path resolution
        self.assertFalse(action_matches(a, "a", "run", "old-reports"))
        self.assertTrue(action_matches(a, "a", "run", a.text))

    def test_scratch_deletion_does_not_satisfy_a_delete_claim(self):
        a = ev(0, "a", "action", "rm -rf /tmp/build", "command")
        self.assertFalse(action_matches(a, "a", "delete", "/tmp/build"))


class Writers(unittest.TestCase):
    CASES = [
        ("echo x > out.txt", ("out.txt",)),
        ("echo x >> log.txt; echo y >| forced", ("log.txt", "forced")),
        ("make &> build.log", ("build.log",)),
        ("cmd 2>&1 > f.txt", ("f.txt",)),
        ("ls 2> err.txt", ("err.txt",)),
        ("cat > a.py <<'EOF'\nprint(1)\nEOF", ("a.py",)),
        ("cat <<'EOF' > b.py\nx\nEOF", ("b.py",)),
        ("echo hi | tee -a one two", ("one", "two")),
        ("sed -i 's/x/y/' f.txt", ("f.txt",)),
        ("sed -i '' 's/x/y/' g.txt", ("g.txt",)),
        ("sed -i.bak -e s/x/y/ f g", ("f", "g")),
        ("sed -Ei 's/x/y/' h", ("h",)),
        ("sed --in-place -e s/x/y/ k", ("k",)),
        ("sed -i -es/x/y/ f g", ("f", "g")),              # script attached to -e
        ("cp a b > cp.log", ("cp.log", "b")),              # a redirect target is not an argument
        ("cp a b", ("b",)),
        ("mv -f old new", ("new",)),
        ("cp -r src dir/", ("dir/", "dir/src")),
        ("cp -t d a b", ("d", "d/a", "d/b")),
        ("install -m 644 conf /etc/app.conf", ("/etc/app.conf",)),
        ("cp -rt backup/ a b", ("backup/", "backup/a", "backup/b")),
        ("cp -tbk a", ("bk", "bk/a")),
        ("cp --target-directory bk a", ("bk", "bk/a")),
        ("mv -nt d a", ("d", "d/a")),
        ("install -d build notes.md", ()),
        ("install --directory build notes.md", ()),
        ("install -Dm 755 tool /usr/bin/tool", ("/usr/bin/tool",)),
        ("[[ $v > notes.md ]] && echo ok", ()),
        ("[[ a < b ]] && echo x > out", ("out",)),
        ("sed -l 5 -i s/a/b/ f", ("f",)),
        ("sed --line-length 5 -i s/a/b/ f", ("f",)),
        ("cd /tmp && echo x > rel", ()),
        # Not writes.
        ("echo x > /dev/null", ()),
        ("sort < in.txt", ()),
        ("sed 's/x/y/' f", ()),
        ("sed -n 1,5p f", ()),
        ("sed -ne 's/x/y/p' f", ()),
        ('echo "> x"', ()),
        ("# echo > x", ()),
        ("cat <<EOF\necho > inside\nEOF", ()),
        ("echo $X > $OUT; cp a *.bak", ()),
        ("python3 - <<'EOF'\nopen('x', 'w')\nEOF", ()),
        ("cp only", ()),
        ("cat a.txt >&2", ()),
    ]

    def test_table(self):
        for script, want in self.CASES:
            with self.subTest(script=script):
                self.assertEqual(written_paths(script), Writes(True, want))

    def test_unparseable(self):
        self.assertEqual(written_paths('echo "x > y'), Writes(parsed=False))

    def test_write_and_edit_claims_match_a_written_path(self):
        a = ev(0, "a", "action", "cat > scripts/gate.py <<'EOF'\npass\nEOF", "command")
        self.assertTrue(action_matches(a, "a", "write", "scripts/gate.py"))
        self.assertTrue(action_matches(a, "a", "edit", "./scripts/gate.py"))
        self.assertFalse(action_matches(a, "a", "write", "gate.py"))  # no path resolution
        self.assertFalse(action_matches(a, "a", "deploy", "scripts/gate.py"))
        self.assertFalse(action_matches(ev(0, "a", "action", "sed -n 1,5p scripts/gate.py", "command"),
                                        "a", "edit", "scripts/gate.py"))
        # After a cd a relative path no longer says where it landed; an absolute one still does.
        moved = ev(0, "a", "action", "cd /tmp && cat > scripts/gate.py && echo x > /srv/log", "command")
        self.assertFalse(action_matches(moved, "a", "write", "scripts/gate.py"))
        self.assertTrue(action_matches(moved, "a", "write", "/srv/log"))


class Unrecorded(unittest.TestCase):
    """A script or a click may do the claimed thing without its text showing it (make,
    ./deploy.sh, python in a heredoc, a push whose object is numbered only once it exists).
    Absence is not decidable while one is in the window; a literal match still clears."""

    def setUp(self):
        self.tr = Transcript([
            ev(0, "z", "message", "a, please deploy P9-P12. c, please deploy the banner."),
            ev(1, "a", "action", "./deploy.sh", "command"),
            ev(2, "c", "action", "run link check", "shell"),
            ev(3, "a", "message", "Deployed P9-P12."),
            ev(4, "c", "message", "Deployed the banner."),
            ev(5, "d", "action", "cat > notes.md <<'EOF'\nx\nEOF", "command"),
            ev(6, "d", "action", "click", "gui"),
            ev(7, "d", "message", "Wrote notes.md and deployed the site."),
        ])
        self.claims = [
            Claim("e3", "a", "done", "Deployed P9-P12", "deploy", "P9-P12"),
            Claim("e4", "c", "done", "Deployed the banner", "deploy", "banner"),
            Claim("e0", "z", "assign", "a, please deploy P9-P12", "deploy", "P9-P12", "a"),
            Claim("e0", "z", "assign", "c, please deploy the banner", "deploy", "banner", "c"),
            Claim("e7", "d", "done", "Wrote notes.md", "write", "notes.md"),
            Claim("e7", "d", "done", "deployed the site", "deploy", "site"),
        ]
        self.findings, self.unchecked = propose(self.tr, self.claims)

    def test_only_the_fully_recorded_agent_is_reported(self):
        self.assertEqual({(f.type, f.agent, f.about) for f in self.findings},
                         {("claimed_not_done", "c", ("deploy", "banner")), ("off_assignment", "c", ("deploy", "banner"))})
        self.assertTrue(all(check(self.tr, f).accepted for f in self.findings))

    def test_what_could_not_be_judged_is_listed(self):
        self.assertEqual([(u.event, u.reason) for u in self.unchecked], [
            ("e3", "before the claim, 1 GUI or script action(s) (e1) may have done it without the record showing it"),
            ("e0", "after the assignment, 1 GUI or script action(s) (e1) may have done it without the record showing it"),
            ("e7", "before the claim, 2 GUI or script action(s) (e5, e6) may have done it without the record showing it"),
        ])  # notes.md is cleared by the literal write in e5: neither a finding nor unchecked

    def test_checker_refuses_the_absence_when_a_script_or_click_is_in_the_window(self):
        for f in self.findings:
            to_a = replace(f, agent="a", about=("deploy", "P9-P12"),
                           cites=tuple(replace(c, event="e3", quote="Deployed P9-P12") if c.event == "e4"
                                       else replace(c, quote="a, please deploy P9-P12") if c.event == "e0"
                                       else replace(c, event="e1", quote="./deploy.sh") for c in f.cites))
            with self.subTest(type=f.type):
                v = check(self.tr, to_a)
                self.assertFalse(v.accepted)
                self.assertIn("1 GUI or script action(s) (e1) may have done it without the record showing it; "
                              "the absence cannot be established", v.reasons)

    def test_a_shrunken_window_cannot_hide_the_script(self):
        f = next(x for x in self.findings if x.type == "claimed_not_done")
        forged = replace(f, agent="a", about=("deploy", "P9-P12"), window=("2026-10-03T00:00:02Z", "2026-10-03T00:00:03Z"),
                         cites=(replace(f.cites[0], event="e3", quote="Deployed P9-P12"),))
        self.assertFalse(check(self.tr, forged).accepted)

    def test_opacity_counts_only_the_window(self):
        # A script after the claim, or a click before the assignment, cannot have done it.
        tr = Transcript([
            ev(0, "c", "action", "click", "gui"),
            ev(1, "z", "message", "c, please deploy the banner."),
            ev(2, "c", "action", "run link check", "shell"),
            ev(3, "c", "message", "Deployed the banner."),
        ])
        after = Transcript(list(tr.events[1:]) + [ev(4, "c", "action", "./deploy.sh", "command")])
        for t, claim, kind in (
                (tr, Claim("e1", "z", "assign", "c, please deploy the banner", "deploy", "banner", "c"), "off_assignment"),
                (after, Claim("e3", "c", "done", "Deployed the banner", "deploy", "banner"), "claimed_not_done")):
            with self.subTest(kind=kind):
                findings, unchecked = propose(t, [claim])
                self.assertEqual([f.type for f in findings], [kind])
                self.assertEqual(unchecked, [])
                self.assertTrue(check(t, findings[0]).accepted, check(t, findings[0]).reasons)

    def test_note_lists_at_most_three_ids(self):
        from saidvsdid.rules import opacity_note
        self.assertEqual(opacity_note(["a", "b", "c", "d", "e"]),
                         "5 GUI or script action(s) (a, b, c and 2 more) may have done it without the record showing it")


class DoneNotSaid(unittest.TestCase):
    def setUp(self):
        self.tr = Transcript([
            ev(0, "a", "message", "Tidying up; keep.txt is gone."),
            ev(1, "a", "action", "# clean\ncd /srv && rm -rf old-reports ./keep.txt 2>/dev/null", "command"),
            ev(2, "a", "action", "cd /tmp && rm -rf scratch", "command"),
            ev(3, "a", "action", "rm -rf $OUT", "command"),
            ev(4, "a", "action", 'echo "oops\nrm -rf thing', "command"),
            ev(5, "a", "action", 'echo "oops\nls', "command"),
            ev(6, "b", "action", "cat > n.py <<EOF\nos.remove('x')\nEOF", "command"),
        ])
        self.findings, self.unchecked = propose(self.tr, [])

    def test_one_finding_per_unnamed_path(self):
        self.assertEqual([(f.type, f.agent, f.about, [c.event for c in f.cites]) for f in self.findings],
                         [("done_not_said", "a", ("delete", "old-reports"), ["e1"])])

    def test_what_could_not_be_judged_is_listed(self):
        self.assertEqual([(u.event, u.reason) for u in self.unchecked], [
            ("e3", "1 deletion(s) of a non-literal path (variable, glob, stdin or find filter)"),
            ("e4", "script could not be parsed; what it deletes is unknown"),
        ])

    def test_checker_accepts_the_real_one(self):
        self.assertEqual([check(self.tr, f).accepted for f in self.findings], [True])

    def test_checker_rejects_forgeries(self):
        def verdict(about, cite="e1"):
            quote = self.tr.by_id[cite].text
            return check(self.tr, Finding("done_not_said", "a", (Cite(cite, quote),), "x", about=about))
        self.assertIn("must say which deleted path", verdict(None).reasons[0])
        self.assertIn("does not delete 'site'", verdict(("delete", "site")).reasons[0])
        self.assertIn("does not delete 'scratch'", verdict(("delete", "scratch"), "e2").reasons[0])
        self.assertIn("does name './keep.txt'", verdict(("delete", "./keep.txt")).reasons[0])
        self.assertFalse(verdict(("delete", "old-reports")).reasons)


if __name__ == "__main__":
    unittest.main()
