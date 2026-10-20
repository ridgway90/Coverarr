#!/usr/bin/env python3
"""Fail the build on non-ASCII characters that can hide malicious code.

Three attack classes, none of which are visible in a normal diff review:

  * Bidirectional controls (CVE-2021-42574, "Trojan Source") reorder how a
    line renders without changing how it parses, so a reviewer reads one
    thing and the compiler runs another.
  * Zero-width and invisible characters split or pad identifiers - RM<ZWSP>DIR
    and RMDIR look identical and are different variables. A UTF-8 BOM is the
    same problem at file scope.
  * Homoglyph letters from other scripts substitute for Latin ones - Cyrillic
    small a (U+0430) is pixel-identical to Latin 'a' (U+0061) in most fonts,
    so a name spelled with one is a different identifier that looks the same.

Typography this repo legitimately uses - box-drawing separators in comment
banners, arrows, em-dashes, the pound sign, check marks, emoji - is allowed
and reported only under --verbose.

Usage:
    python3 scripts/scan-unicode.py [path ...]   # default: repo root
    python3 scripts/scan-unicode.py --verbose    # also list allowed non-ASCII
    python3 scripts/scan-unicode.py --self-test  # prove the detector works

Exit code is 1 if anything blocking is found, else 0.

If a non-ASCII letter is ever genuinely needed (translated UI strings, say),
widen ALPHABETIC_SCRIPTS deliberately rather than deleting this check - the
value here is that nothing gets in unnoticed.

Shared with PEMForge; Coverarr additions are marked "Coverarr:".
"""
import argparse
import os
import sys
import unicodedata

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "obj", "bin",
    "venv", ".venv", "pw-browsers", ".mypy_cache", ".pytest_cache",
    # Coverarr: build output and tool caches (the built frontend is minified
    # third-party code, and backend/coverarr/static is a copy of it).
    "dist", "static", ".ruff_cache",
}

# Explicit ranges rather than a category check: these are format characters
# (category Cf) that sit alongside perfectly ordinary ones like the emoji
# variation selector, so the category alone doesn't separate them.
BIDI_CONTROLS = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | {
    0x200E, 0x200F, 0x061C,
}
INVISIBLE = {
    0x200B,  # zero-width space
    0x200C,  # zero-width non-joiner
    0x200D,  # zero-width joiner
    0xFEFF,  # BOM / zero-width no-break space
    0x2060,  # word joiner
    0x180E,  # Mongolian vowel separator
    0x00AD,  # soft hyphen
    # Coverarr: these slip past the category check below - the separators are
    # category Z, the grapheme joiner M - yet are just as invisible.
    0x2028,  # line separator (also ends a line in JavaScript strings)
    0x2029,  # paragraph separator
    0x034F,  # combining grapheme joiner
    0x2061, 0x2062, 0x2063, 0x2064,  # invisible math operators
}
# Letters from these scripts have no business in this codebase and are the
# raw material for homoglyph substitution.
ALPHABETIC_SCRIPTS = (
    "CYRILLIC", "GREEK", "COPTIC", "CJK", "HIRAGANA", "KATAKANA", "HANGUL",
    "ARABIC", "HEBREW", "THAI", "DEVANAGARI", "ARMENIAN", "GEORGIAN",
    "CHEROKEE", "FULLWIDTH", "HALFWIDTH",
)


def classify(ch):
    """-> (blocking: bool, reason: str)."""
    cp = ord(ch)
    if cp in BIDI_CONTROLS:
        return True, "bidirectional control (Trojan Source)"
    if cp in INVISIBLE:
        return True, "invisible / zero-width character"
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return True, "unnamed control character"
    for script in ALPHABETIC_SCRIPTS:
        if name.startswith(script):
            return True, "%s letter (homoglyph risk)" % script.title()
    if unicodedata.category(ch).startswith("C"):
        return True, "control character"
    return False, name


def iter_text_files(roots):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                yield os.path.join(dirpath, name)


def scan(roots):
    """-> (findings, allowed_counts, files_scanned)."""
    findings, allowed, scanned = [], {}, 0
    for path in iter_text_files(roots):
        try:
            raw = open(path, "rb").read()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:          # binary
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        if raw.startswith(b"\xef\xbb\xbf"):
            findings.append((path, 1, 1, "U+FEFF", "file starts with a UTF-8 BOM", ""))
            text = text[1:]  # Coverarr: already reported; don't flag it twice below
        for lineno, line in enumerate(text.split("\n"), 1):
            for col, ch in enumerate(line, 1):
                if ord(ch) < 128:
                    continue
                blocking, reason = classify(ch)
                if blocking:
                    findings.append((path, lineno, col, "U+%04X" % ord(ch),
                                     reason, line.strip()[:120]))
                else:
                    allowed["U+%04X %s" % (ord(ch), reason)] = \
                        allowed.get("U+%04X %s" % (ord(ch), reason), 0) + 1
    return findings, allowed, scanned


def self_test():
    """Prove each detector fires, so a clean run means something."""
    # Built from escapes, not literals: a checker that trips its own check
    # is a checker nobody can leave switched on.
    cases = [
        ("Cyrillic homoglyph", "\u0430dmin = 1"),
        ("bidi override",      "var ok = false; /*\u202e } \u2066if (ok)\u2069 */"),
        ("zero-width space",   "RM\u200bDIR=/tmp"),
        ("CJK letter",         "$\u6bcf = 1"),
        ("BOM",                "\ufeffprint('x')"),
        # Coverarr additions:
        ("line separator",     "// note\u2028code()"),
        ("tag character",      "token\U000e0041"),
        ("fullwidth Latin",    "\uff41dmin = 1"),
    ]
    ok = True
    for label, sample in cases:
        caught = [classify(c)[1] for c in sample if ord(c) > 127 and classify(c)[0]]
        status = "detected" if caught else "MISSED"
        if not caught:
            ok = False
        print("  %-20s %s %s" % (label, status, caught[:1]))
    # Coverarr: plus the typography this repo's UI uses (ellipsis, times sign,
    # curly apostrophe, accented Latin, emoji with its variation selector).
    benign = ("# \u2500\u2500 banner \u2192 \u00a340 \u2713"
              " Choose zip\u2026 400\u00d7600 World\u2019s Am\u00e9lie \u2764\ufe0f")
    leaked = [c for c in benign if ord(c) > 127 and classify(c)[0]]
    print("  %-20s %s" % ("benign typography",
                          "allowed" if not leaked else "FALSE POSITIVE %s" % leaked))
    return ok and not leaked


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=["."], help="files or directories")
    ap.add_argument("--verbose", action="store_true",
                    help="also list the allowed non-ASCII characters in use")
    ap.add_argument("--self-test", action="store_true",
                    help="check the detectors fire on known-bad samples")
    args = ap.parse_args()

    if args.self_test:
        print("Detector self-test:")
        ok = self_test()
        print("\nself-test:", "PASS" if ok else "FAIL")
        return 0 if ok else 1

    findings, allowed, scanned = scan(args.paths or ["."])

    if findings:
        print("Found %d suspicious character(s):\n" % len(findings))
        for path, line, col, cp, reason, ctx in findings:
            print("  %s:%d:%d" % (path, line, col))
            print("      %s  %s" % (cp, reason))
            if ctx:
                print("      %s" % ctx)
            # Coverarr: inline annotations on the PR diff when run in Actions.
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print("::error file=%s,line=%d,col=%d::%s %s"
                      % (os.path.normpath(path), line, col, cp, reason))
        print("\nThese can hide code that reads differently than it runs.")
        print("If one is genuinely needed, widen ALPHABETIC_SCRIPTS in %s"
              % os.path.relpath(__file__))
        return 1

    print("Scanned %d text files - no suspicious characters." % scanned)
    if args.verbose and allowed:
        print("\nAllowed non-ASCII in use:")
        for key, count in sorted(allowed.items(), key=lambda kv: -kv[1]):
            print("  %5d  %s" % (count, key))
    return 0


if __name__ == "__main__":
    # Die on SIGPIPE the way any other Unix filter does. Without this,
    # `scan-unicode.py | head` raises BrokenPipeError and exits non-zero with a
    # traceback - which in CI reads as "the scan failed" when all that happened
    # is a reader closed the pipe early.
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError):
        pass  # no SIGPIPE on Windows
    sys.exit(main())
