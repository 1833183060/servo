from __future__ import print_function, unicode_literals

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

from collections import defaultdict

from .. import localpaths

from manifest.sourcefile import SourceFile
from six import iteritems
from six.moves import range

here = os.path.abspath(os.path.split(__file__)[0])

ERROR_MSG = """You must fix all errors; for details on how to fix them, see
https://github.com/w3c/web-platform-tests/blob/master/docs/lint-tool.md

However, instead of fixing a particular error, it's sometimes
OK to add a line to the lint.whitelist file in the root of the
web-platform-tests directory to make the lint tool ignore it.

For example, to make the lint tool ignore all '%s'
errors in the %s file,
you could add the following line to the lint.whitelist file.

%s:%s"""

def all_git_paths(repo_root):
    command_line = ["git", "ls-tree", "-r", "--name-only", "HEAD"]
    output = subprocess.check_output(command_line, cwd=repo_root)
    for item in output.split("\n"):
        yield item


def check_path_length(repo_root, path):
    if len(path) + 1 > 150:
        return [("PATH LENGTH", "/%s longer than maximum path length (%d > 150)" % (path, len(path) + 1), None)]
    return []

def set_type(error_type, errors):
    return [(error_type,) + error for error in errors]

def parse_whitelist_file(filename):
    """
    Parse the whitelist file at `filename`, and return the parsed structure.
    """

    data = defaultdict(lambda:defaultdict(set))

    with open(filename) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [item.strip() for item in line.split(":")]
            if len(parts) == 2:
                parts.append(None)
            else:
                parts[-1] = int(parts[-1])

            error_type, file_match, line_number = parts
            data[file_match][error_type].add(line_number)

    return data


def filter_whitelist_errors(data, path, errors):
    """
    Filter out those errors that are whitelisted in `data`.
    """

    whitelisted = [False for item in range(len(errors))]

    for file_match, whitelist_errors in iteritems(data):
        if fnmatch.fnmatch(path, file_match):
            for i, (error_type, msg, path, line) in enumerate(errors):
                if "*" in whitelist_errors:
                    whitelisted[i] = True
                elif error_type in whitelist_errors:
                    allowed_lines = whitelist_errors[error_type]
                    if None in allowed_lines or line in allowed_lines:
                        whitelisted[i] = True

    return [item for i, item in enumerate(errors) if not whitelisted[i]]

class Regexp(object):
    pattern = None
    file_extensions = None
    error = None
    _re = None

    def __init__(self):
        self._re = re.compile(self.pattern)

    def applies(self, path):
        return (self.file_extensions is None or
                os.path.splitext(path)[1] in self.file_extensions)

    def search(self, line):
        return self._re.search(line)

class TrailingWhitespaceRegexp(Regexp):
    pattern = b"[ \t\f\v]$"
    error = "TRAILING WHITESPACE"
    description = "Whitespace at EOL"

class TabsRegexp(Regexp):
    pattern = b"^\t"
    error = "INDENT TABS"
    description = "Tabs used for indentation"

class CRRegexp(Regexp):
    pattern = b"\r$"
    error = "CR AT EOL"
    description = "CR character in line separator"

class W3CTestOrgRegexp(Regexp):
    pattern = b"w3c\-test\.org"
    error = "W3C-TEST.ORG"
    description = "External w3c-test.org domain used"

class Webidl2Regexp(Regexp):
    pattern = b"webidl2\.js"
    error = "WEBIDL2.JS"
    description = "Legacy webidl2.js script used"

class ConsoleRegexp(Regexp):
    pattern = b"console\.[a-zA-Z]+\s*\("
    error = "CONSOLE"
    file_extensions = [".html", ".htm", ".js", ".xht", ".html", ".svg"]
    description = "Console logging API used"

class PrintRegexp(Regexp):
    pattern = b"print(?:\s|\s*\()"
    error = "PRINT STATEMENT"
    file_extensions = [".py"]
    description = "Print function used"

regexps = [item() for item in
           [TrailingWhitespaceRegexp,
            TabsRegexp,
            CRRegexp,
            W3CTestOrgRegexp,
            Webidl2Regexp,
            ConsoleRegexp,
            PrintRegexp]]

def check_regexp_line(repo_root, path, f):
    errors = []

    applicable_regexps = [regexp for regexp in regexps if regexp.applies(path)]

    for i, line in enumerate(f):
        for regexp in applicable_regexps:
            if regexp.search(line):
                errors.append((regexp.error, regexp.description, path, i+1))

    return errors

def check_parsed(repo_root, path, f):
    source_file = SourceFile(repo_root, path, "/")

    errors = []

    if source_file.name_is_non_test or source_file.name_is_manual:
        return []

    if source_file.markup_type is None:
        return []

    if source_file.root is None:
        return [("PARSE-FAILED", "Unable to parse file", path, None)]

    if len(source_file.timeout_nodes) > 1:
        errors.append(("MULTIPLE-TIMEOUT", "More than one meta name='timeout'", path, None))

    for timeout_node in source_file.timeout_nodes:
        timeout_value = timeout_node.attrib.get("content", "").lower()
        if timeout_value != "long":
            errors.append(("INVALID-TIMEOUT", "Invalid timeout value %s" % timeout_value, path, None))

    if source_file.testharness_nodes:
        if len(source_file.testharness_nodes) > 1:
            errors.append(("MULTIPLE-TESTHARNESS",
                           "More than one <script src='/resources/testharness.js'>", path, None))

        testharnessreport_nodes = source_file.root.findall(".//{http://www.w3.org/1999/xhtml}script[@src='/resources/testharnessreport.js']")
        if not testharnessreport_nodes:
            errors.append(("MISSING-TESTHARNESSREPORT",
                           "Missing <script src='/resources/testharnessreport.js'>", path, None))
        else:
            if len(testharnessreport_nodes) > 1:
                errors.append(("MULTIPLE-TESTHARNESSREPORT",
                               "More than one <script src='/resources/testharnessreport.js'>", path, None))

        for element in source_file.variant_nodes:
            if "content" not in element.attrib:
                errors.append(("VARIANT-MISSING",
                               "<meta name=variant> missing 'content' attribute", path, None))
            else:
                variant = element.attrib["content"]
                if variant != "" and variant[0] not in ("?", "#"):
                    errors.append(("MALFORMED-VARIANT",
                               "%s <meta name=variant> 'content' attribute must be the empty string or start with '?' or '#'" % path, None))

        seen_elements = {"timeout": False,
                         "testharness": False,
                         "testharnessreport": False}
        required_elements = [key for key, value in {"testharness": True,
                                                    "testharnessreport": len(testharnessreport_nodes) > 0,
                                                    "timeout": len(source_file.timeout_nodes) > 0}.items()
                             if value]

        for elem in source_file.root.iter():
            if source_file.timeout_nodes and elem == source_file.timeout_nodes[0]:
                seen_elements["timeout"] = True
                if seen_elements["testharness"]:
                    errors.append(("LATE-TIMEOUT",
                                   "<meta name=timeout> seen after testharness.js script", path, None))

            elif elem == source_file.testharness_nodes[0]:
                seen_elements["testharness"] = True

            elif testharnessreport_nodes and elem == testharnessreport_nodes[0]:
                seen_elements["testharnessreport"] = True
                if not seen_elements["testharness"]:
                    errors.append(("EARLY-TESTHARNESSREPORT",
                                   "testharnessreport.js script seen before testharness.js script", path, None))

            if all(seen_elements[name] for name in required_elements):
                break

    return errors

def output_errors_text(errors):
    for error_type, description, path, line_number in errors:
        pos_string = path
        if line_number:
            pos_string += " %s" % line_number
        print("%s: %s %s" % (error_type, pos_string, description))

def output_errors_json(errors):
    for error_type, error, path, line_number in errors:
        print(json.dumps({"path": path, "lineno": line_number,
                          "rule": error_type, "message": error}))

def output_error_count(error_count):
    if not error_count:
        return

    by_type = " ".join("%s: %d" % item for item in error_count.items())
    count = sum(error_count.values())
    if count == 1:
        print("There was 1 error (%s)" % (by_type,))
    else:
        print("There were %d errors (%s)" % (count, by_type))

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*",
                        help="List of paths to lint")
    parser.add_argument("--json", action="store_true",
                        help="Output machine-readable JSON format")
    return parser.parse_args()

def main():
    repo_root = localpaths.repo_root
    args = parse_args()
    paths = args.paths if args.paths else all_git_paths(repo_root)
    return lint(repo_root, paths, args.json)

def lint(repo_root, paths, output_json):
    error_count = defaultdict(int)
    last = None

    whitelist = parse_whitelist_file(os.path.join(repo_root, "lint.whitelist"))

    if output_json:
        output_errors = output_errors_json
    else:
        output_errors = output_errors_text

    def run_lint(path, fn, last, *args):
        errors = filter_whitelist_errors(whitelist, path, fn(repo_root, path, *args))
        if errors:
            last = (errors[-1][0], path)

        output_errors(errors)
        for error_type, error, path, line in errors:
            error_count[error_type] += 1
        return last

    for path in paths:
        abs_path = os.path.join(repo_root, path)
        if not os.path.exists(abs_path):
            continue
        for path_fn in path_lints:
            last = run_lint(path, path_fn, last)

        if not os.path.isdir(abs_path):
            with open(abs_path) as f:
                for file_fn in file_lints:
                    last = run_lint(path, file_fn, last, f)
                    f.seek(0)

    if not output_json:
        output_error_count(error_count)
        if error_count:
            print(ERROR_MSG % (last[0], last[1], last[0], last[1]))
    return sum(error_count.itervalues())

path_lints = [check_path_length]
file_lints = [check_regexp_line, check_parsed]

if __name__ == "__main__":
    error_count = main()
    if error_count > 0:
        sys.exit(1)
