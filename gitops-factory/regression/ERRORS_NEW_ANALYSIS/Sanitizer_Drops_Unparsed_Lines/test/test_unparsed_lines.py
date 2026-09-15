"""
Regression tests for ERRORS_NEW_ANALYSIS/Sanitizer_Drops_Unparsed_Lines.

error.txt is unusually precise about what is and is not the defect:

  > "THE BEHAVIOUR IS CORRECT BY DESIGN AND SHOULD NOT BE 'FIXED' THE OBVIOUS
  >  WAY. […] Do not make it emit unparsed lines into shareable output."

  > "THE ACTUAL DEFECT IS THE BLIND SPOT, not the omission. 3 lines out of 120
  >  are invisible in every shareable artifact. […] the count is surfaced
  >  precisely so someone acts on it. Nobody has."

  > "ACTION: read the 3 unparsed lines directly from the INTERNAL report […]
  >  determine why they failed to parse, and either extend the parser to cover
  >  their shape or confirm they are truly uninteresting."

So: the omission stays (asserted below), and the blind spot is closed by reading
the lines. In this repo's report there were FOUR, and all four were the report's
own header text — not findings:

    '         renamed on collision) — expected, not a loss.'
    '         source, or a field the conversion LOST or CHANGED.'
    'Fields the render ADDED are counted but not listed: a Helm chart filling in a'
    'dangling references (rename lost a reference): 0'

Three shapes, none of them an error record:
  * two WRAPPED legend sentences — the report wraps at ~78 columns, so the
    second half arrives indented, with no keyword of its own and no severity;
  * one legend sentence whose opening words ("Fields the render ADDED") do not
    match the hint written for the summary row ("fields added");
  * one numeric SUMMARY COUNTER, prose-colon-integer.

The parser now accounts for all three, and the point of doing so is not tidiness.
A counter that never reads zero is a counter nobody acts on — which is exactly
what error.txt observed. With these classified, `lines unparsed` is a real
signal, and `test_the_repos_own_report_has_NO_unparsed_lines` turns the next
novel shape into a failing test instead of a number in a header.
"""
import importlib.util
import pathlib
import sys

import pytest

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
SANITIZER_PY = GITOPS_DIR / "sanitize_fidelity_report.py"
REPORT = GITOPS_DIR / "output-showcase" / "report" / "fidelity_report.txt"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


san = _load(SANITIZER_PY, "sanitize_fidelity_report")


# The exact lines that were unparsed, kept verbatim as the regression corpus.
THE_FOUR_LINES = [
    "         renamed on collision) — expected, not a loss.",
    "         source, or a field the conversion LOST or CHANGED.",
    "Fields the render ADDED are counted but not listed: a Helm chart filling in a",
    "dangling references (rename lost a reference): 0",
]


# ---------------------------------------------------------------------------
# 1. The blind spot, closed
# ---------------------------------------------------------------------------

def test_the_repos_own_report_has_NO_unparsed_lines():
    """The test that makes the counter mean something. It fails the day the
    converter emits a line shape the sanitizer has never seen — which, per
    error.txt, is the most likely place a genuinely new problem shows up:

      "unparsed lines are, by definition, the ones with a shape the parser did
       not anticipate. Novel error formats are more likely to be genuinely new
       problems than the 117 that fit known patterns."
    """
    assert REPORT.is_file(), "run run_all.py first"
    result = san.parse(REPORT.read_text(encoding="utf-8"))
    assert result.unparsed == [], "\n".join(repr(x) for x in result.unparsed)


@pytest.mark.parametrize("line", THE_FOUR_LINES)
def test_each_of_the_four_lines_is_now_classified_as_header_text(line):
    after = line[:1].isspace()   # the wrapped ones only make sense in context
    assert san._is_preamble(line, after_preamble=after), repr(line)


def test_every_non_blank_line_is_still_accounted_for():
    """The accounting invariant the tool is built on: records + preamble +
    unparsed == every non-blank line. Classifying more lines as preamble must
    not make any line vanish from the count."""
    text = REPORT.read_text(encoding="utf-8")
    result = san.parse(text)
    assert result.total_lines == len([l for l in text.splitlines() if l.strip()])


# ---------------------------------------------------------------------------
# 2. The honesty mechanism must survive being made quieter
# ---------------------------------------------------------------------------

def test_a_genuinely_novel_shape_is_STILL_unparsed():
    """The failure mode of this folder's own fix: a rule broad enough to swallow
    everything would read zero forever and be worse than the blind spot it
    replaced."""
    result = san.parse("CRITICAL >>> Deployment/api exploded in a brand new way\n")
    assert len(result.unparsed) == 1


def test_a_real_record_is_never_swallowed_by_the_continuation_rule():
    """The boundary that matters: records are indented too, and every one of them
    follows the namespace header, so `after_preamble` is true when they arrive.

    Asserted through `parse` rather than `_is_preamble`, because `parse` tries
    the RECORD pattern FIRST and that ordering is the actual guarantee.
    (`_is_preamble` alone answers True for a record line — the hint list has
    carried a bare `"error "` entry since before this folder, for the legend. It
    is unreachable for records and is left alone.)"""
    text = ("=== acme-prod — 2 raw objects vs 2 rendered ===\n"
            "  ERROR  Deployment/api LOST at spec.x: raw had 'y'\n"
            "  WARNING  Service/api CHANGED at spec.y: raw='a' render='b'\n")
    result = san.parse(text)
    assert len(result.records) == 2
    assert result.unparsed == []


def test_the_continuation_rule_only_applies_after_header_text():
    """Without the context flag it would swallow any indented line anywhere."""
    line = "         renamed on collision) — expected, not a loss."
    assert san._is_preamble(line, after_preamble=True)
    assert not san._is_preamble(line, after_preamble=False)


def test_a_record_resets_the_continuation_context():
    """An indented prose line following a RECORD is not a legend wrap, so it must
    still be reported."""
    text = ("=== ns — 1 raw objects vs 1 rendered ===\n"
            "  ERROR  Deployment/api LOST at spec.x: raw had 'y'\n"
            "         something the parser has never seen\n")
    assert len(san.parse(text).unparsed) == 1


def test_the_summary_counter_rule_needs_a_number():
    """Shape-based, so it cannot become 'any line with a colon'."""
    assert san._is_preamble("dangling references (rename lost a reference): 0")
    assert not san._is_preamble("something odd here: not a number")


# ---------------------------------------------------------------------------
# 3. What must NOT change — the omission itself
# ---------------------------------------------------------------------------

def test_unparsed_lines_are_still_kept_out_of_shareable_output(tmp_path):
    """error.txt is explicit that the omission is correct and must stay:
    "an unparsed line cannot be anonymized reliably, so omitting it is the safe
    choice. Do not make it emit unparsed lines into shareable output." """
    secret = "CRITICAL >>> Deployment/customer-billing-prod exploded"
    text = ("=== acme-prod — 1 raw objects vs 1 rendered ===\n"
            "  ERROR  Deployment/api LOST at spec.x: raw had 'y'\n"
            + secret + "\n")
    result = san.parse(text)
    assert len(result.unparsed) == 1
    anon = san.Anonymizer(seed=1)
    san.write_reports(result, tmp_path, anon, mask_literals=True)
    for f in tmp_path.rglob("*.txt"):
        body = f.read_text(encoding="utf-8")
        assert "customer-billing-prod" not in body
        assert secret not in body


def test_the_count_is_still_reported_when_it_is_nonzero(tmp_path):
    """The warning is the mechanism. Closing the blind spot must not remove it."""
    text = ("=== acme-prod — 1 raw objects vs 1 rendered ===\n"
            "  ERROR  Deployment/api LOST at spec.x: raw had 'y'\n"
            "CRITICAL >>> a shape nobody anticipated\n")
    san.write_reports(san.parse(text), tmp_path, san.Anonymizer(seed=1), mask_literals=True)
    index = (tmp_path / "index.txt").read_text(encoding="utf-8")
    assert "lines unparsed:   1" in index
    assert "could not be parsed" in index


def test_zero_unparsed_does_not_print_a_warning(tmp_path):
    text = ("=== acme-prod — 1 raw objects vs 1 rendered ===\n"
            "  ERROR  Deployment/api LOST at spec.x: raw had 'y'\n")
    san.write_reports(san.parse(text), tmp_path, san.Anonymizer(seed=1), mask_literals=True)
    index = (tmp_path / "index.txt").read_text(encoding="utf-8")
    assert "lines unparsed:   0" in index
    assert "could not be parsed" not in index


# ---------------------------------------------------------------------------
# 4. Scale check error.txt asks for
# ---------------------------------------------------------------------------

def test_the_reports_index_summarizes_this_runs_report_only():
    """error.txt: "this index summarizes a SUBSET, not the full 1,313-line
    report. Confirm which run and which namespace tree it covers before comparing
    its counts against anything else." The run identity block folder 1 added is
    what answers that, and it is carried in the report this test reads."""
    text = REPORT.read_text(encoding="utf-8")
    assert "run id" in text.lower()
