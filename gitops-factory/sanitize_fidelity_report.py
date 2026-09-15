#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sanitize and summarize a fidelity-checker report for sharing outside the org.

Reads `report/fidelity_report.txt` (as written by convert_to_universal_chart.py
--verify-fidelity), and writes one short, anonymized file per error category:

    out/error_field_lost.txt
    out/error_field_changed.txt
    out/error_missing_object.txt
    ...

Three properties matter, in this order:

1. NOTHING REAL LEAKS. Every namespace, resource name and source filename is
   replaced with a stable alias. The writer re-scans its own output and REFUSES
   to write a file that still contains a real identifier — a leak is a hard
   failure, never a warning. See `Anonymizer.assert_clean`.

2. NOTHING IS SILENTLY DROPPED. Every non-blank line of the input is either
   parsed into a record or recorded as unparsed. The totals are printed and
   written into the reports, so a parser that quietly skipped 400 lines is
   visible rather than reassuring.

3. IT FITS ON A PHONE SCREEN. Thousands of identical errors collapse into a
   count, the affected resources grouped by kind, and one worked example.

Usage
-----
    python sanitize_fidelity_report.py --input report/fidelity_report.txt --out-dir shareable/

    # reproducible aliases (same input -> same fake names), for diffing two runs
    python sanitize_fidelity_report.py -i report.txt -o out/ --seed 42

    # keep the raw values in the example diffs instead of masking literals
    python sanitize_fidelity_report.py -i report.txt -o out/ --keep-literals
"""
from __future__ import annotations

import argparse
import pathlib
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Input format
# --------------------------------------------------------------------------
# The report is a flat text file. The lines that matter look like:
#
#   === platform-ops — 18 raw objects vs 18 rendered ===
#     ERROR  CronJob/nightly-report LOST at spec.schedule: raw had '46 2 * * 6'
#     ERROR  Deployment/api-gateway CHANGED at spec.replicas: raw=3 render=1
#     ERROR  ClusterRole/log-agent-reader (from log-agent.yaml): cluster-scoped, but rendered by NO release
#     ERROR  Service/billing: rendered by 'billing' but present in NO raw manifest
#     INFO   ServiceAccount/db-migrate: created by the chart ...
#
# Everything else (banner, legend, the numeric summary) is preamble and is not
# a per-object record.

NS_HEADER = re.compile(r"^===\s+(?P<ns>\S+)\s+.*===\s*$")

# `  SEVERITY  Kind/name[ (from file.yaml)][ VERB at path]: message`
RECORD = re.compile(
    r"^\s*(?P<severity>ERROR|INFO|WARN(?:ING)?)\s+"
    r"(?P<kind>[A-Za-z][A-Za-z0-9]*)/(?P<name>[^\s:(]+)"
    r"(?:\s+\(from\s+(?P<source>[^)]+)\))?"
    r"(?:\s+(?P<verb>LOST|CHANGED|ADDED)\s+at\s+(?P<path>[^\s:]+))?"
    r"\s*:?\s*(?P<message>.*)$"
)


@dataclass
class Record:
    """One parsed report line."""
    severity: str
    namespace: str
    kind: str
    name: str
    source: str | None
    verb: str | None
    path: str | None
    message: str
    raw_line: str


# --------------------------------------------------------------------------
# Categorisation
# --------------------------------------------------------------------------
# A category is what the reader wants one file about. Field losses are split by
# the LEAF of the field path, because "45 objects lost metadata.annotations" and
# "3 objects lost spec.schedule" are different problems that happen to share a
# verb — lumping them together is exactly the summarisation that hides things.

_MESSAGE_CATEGORIES = (
    ("missing_object", ("rendered by no release", "rendered by NO release")),
    ("extra_object", ("present in no raw manifest", "present in NO raw manifest",
                      "no raw source")),
    ("render_failure", ("render failed", "failed to render", "template failed")),
    ("relocated", ("relocated to", "copied into release")),
)


def categorise(rec: Record) -> str:
    """Stable, filename-safe category for one record."""
    if rec.verb in ("LOST", "CHANGED", "ADDED"):
        leaf = (rec.path or "").rstrip(".").split(".")[-1] or "field"
        leaf = re.sub(r"\[[^\]]*\]", "", leaf) or "field"
        return _slug(f"field_{rec.verb.lower()}_{leaf}")
    low = rec.message.lower()
    for cat, needles in _MESSAGE_CATEGORIES:
        if any(n.lower() in low for n in needles):
            return cat
    return "other_info" if rec.severity == "INFO" else "other_error"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "unknown"


# --------------------------------------------------------------------------
# Anonymization
# --------------------------------------------------------------------------

_WORDS = (
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho",
    "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
)

# Value literals that could be secrets even after names are replaced: long
# base64-ish blobs, hex digests, bearer-looking tokens.
_SECRETY = re.compile(
    r"\b(?:[A-Za-z0-9+/]{32,}={0,2}|[0-9a-f]{32,}|sha256:[0-9a-f]{16,})\b")


class Anonymizer:
    """Stable real-name -> fake-name mapping for one run.

    Consistency is the point: `billing-app` must become the same alias every
    time it appears, in every file. The mapping lives for the life of the
    object, so one Anonymizer per run gives one coherent set of reports that can
    be read side by side.

    Aliases carry the KIND so a reader can still follow the shape of the problem
    ("two Deployments and a Service") without learning any real name.
    """

    def __init__(self, seed: int | None = None):
        # A random seed by default so two runs of the same input do NOT produce
        # the same aliases -- an alias that is stable across runs is a weak
        # identifier for anyone collecting several reports over time. Pass a
        # seed explicitly when you deliberately want reproducibility.
        self._rng = random.Random(seed)
        self._map: dict[tuple[str, str], str] = {}
        self._used: set[str] = set()
        self._counter: Counter = Counter()

    # -- mapping ---------------------------------------------------------
    def alias(self, category: str, real: str) -> str:
        """The alias for `real` within `category` ('ns', a Kubernetes kind, ...).

        Idempotent: calling it again with the same pair returns the same alias.
        """
        real = (real or "").strip()
        if not real:
            return real
        key = (category, real)
        if key in self._map:
            return self._map[key]
        prefix = _slug(category)[:16] or "obj"
        while True:
            word = _WORDS[self._counter[category] % len(_WORDS)]
            self._counter[category] += 1
            candidate = f"{prefix}-{word}-{self._rng.randrange(100, 1000)}"
            if candidate not in self._used:
                break
        self._used.add(candidate)
        self._map[key] = candidate
        return candidate

    def namespace(self, real: str) -> str:
        return self.alias("ns", real)

    def resource(self, kind: str, real: str) -> str:
        return self.alias(kind or "resource", real)

    def source_file(self, real: str) -> str:
        return self.alias("srcfile", real)

    # -- text scrubbing --------------------------------------------------
    @property
    def real_names(self) -> list[str]:
        """Every real identifier registered so far, longest first.

        Longest-first is not cosmetic: replacing `log-agent` before
        `log-agent-reader` would corrupt the longer name into
        `<alias>-reader` and leak the `-reader` suffix.
        """
        return sorted({real for _cat, real in self._map}, key=len, reverse=True)

    def scrub(self, text: str, mask_literals: bool = True) -> str:
        """Replace every known real identifier in free text with its alias.

        Applied to messages and example diffs, where names appear inside prose
        and inside values rather than in a parsed field.
        """
        if not text:
            return text
        for real in self.real_names:
            if real not in text:
                continue
            # One real name can be registered under several categories (a
            # Service and a Deployment sharing a name). Any alias is fine --
            # they all point at the same real string, which is what must go.
            alias = next(v for (c, r), v in self._map.items() if r == real)
            text = re.sub(rf"(?<![\w.-]){re.escape(real)}(?![\w-])", alias, text)
        if mask_literals:
            text = _SECRETY.sub("<REDACTED>", text)
        return text

    def assert_clean(self, text: str) -> None:
        """Raise if any real identifier survived. Called before every write.

        This is the property the whole tool exists for, so it is enforced
        rather than trusted: a leak raises and no file is produced.
        """
        leaked = [r for r in self.real_names
                  if re.search(rf"(?<![\w.-]){re.escape(r)}(?![\w-])", text)]
        if leaked:
            raise LeakError(
                "refusing to write: real identifiers survived sanitization: "
                + ", ".join(sorted(leaked)[:10]))


class LeakError(RuntimeError):
    """A real identifier survived into output. Always fatal."""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

@dataclass
class ParseResult:
    records: list[Record] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    preamble_lines: int = 0

    @property
    def total_lines(self) -> int:
        return len(self.records) + len(self.unparsed) + self.preamble_lines


def parse(text: str) -> ParseResult:
    """Parse the report into records, accounting for every non-blank line.

    A line is one of: a namespace header, a record, preamble (banner, legend,
    the numeric summary), or unparsed. Unparsed lines are kept and counted --
    the tool reports how many it could not read rather than presenting a tidy
    summary of an unknown fraction of the input.
    """
    result = ParseResult()
    namespace = "(none)"
    after_preamble = False
    for line in text.splitlines():
        if not line.strip():
            continue
        header = NS_HEADER.match(line)
        if header:
            namespace = header.group("ns")
            result.preamble_lines += 1
            after_preamble = True
            continue
        match = RECORD.match(line)
        if match and match.group("kind"):
            g = match.groupdict()
            result.records.append(Record(
                severity="WARN" if g["severity"].startswith("WARN") else g["severity"],
                namespace=namespace,
                kind=g["kind"],
                name=g["name"].rstrip(":"),
                source=g["source"],
                verb=g["verb"],
                path=g["path"],
                message=(g["message"] or "").strip(),
                raw_line=line,
            ))
            after_preamble = False
            continue
        if _is_preamble(line, after_preamble):
            result.preamble_lines += 1
            after_preamble = True
        else:
            result.unparsed.append(line)
            after_preamble = False
    return result


_PREAMBLE_HINTS = (
    "round-trip fidelity report", "legend:", "info ", "error ",
    "objects matched", "of those, relocated", "fields lost", "fields changed",
    "fields added", "objects in raw", "objects rendered", "render failures",
    "fields the chart derives", "own metadata labels", "kubernetes default",
    # The RUN IDENTITY block the converter stamps on every report. It is what
    # lets two reports say whether they describe the same run at all, so it is
    # header, not data — and it carries no identifier to anonymize: the hashes
    # are content digests and the paths are the operator's own.
    "run identity", "run id:", "converter:", "input:", "output:",
    "compare two fidelity reports", "run id means", "moved between them",
    "numbers are not evidence", "this report by hand", "in this same run",
    "error breakdown by category",
    # Opens a wrapped legend sentence. Listed because it does NOT start with
    # "fields added" — the sentence is "Fields the render ADDED are counted but
    # not listed", and the hint above matches the summary row, not the prose.
    "fields the render added",
)

# A row of the converter's numeric summary: prose, a colon, an integer.
# "dangling references (rename lost a reference): 0" is one of these, and it was
# being counted as UNPARSED — see below.
_SUMMARY_COUNTER = re.compile(r"^[a-z][a-z0-9 ,'()/&+-]*:\s*\d+$")

# A record always leads with its severity. Anything else that arrives INDENTED
# directly after header text is the wrap of the sentence above it.
_SEVERITY_LEAD = re.compile(r"^\s*(ERROR|WARN\w*|INFO)\b")

# One row of the converter's generated category breakdown: "  extra_object  16".
# Matched by shape rather than by listing every category name, because the
# categories are derived from field paths and are therefore open-ended.
_BREAKDOWN_ROW = re.compile(r"^(?:field_(?:lost|changed|added)_[a-z0-9_]+|missing_object|"
                            r"extra_object|relocated|render_failure|other_error)\s+\d+$")


def _is_preamble(line: str, after_preamble: bool = False) -> bool:
    """Is this line the report's own header text rather than a finding?

    `after_preamble` says the previous accounted line was header text. It is what
    lets a WRAPPED sentence be recognised as the continuation it is — the report
    wraps its legend at ~78 columns, so half of a legend sentence arrives as an
    indented line with no severity token and no keyword of its own.

    Getting this right is the whole point of folder 26. The unparsed COUNT is the
    tool's honesty mechanism — "a parser that quietly skipped 400 lines is
    visible rather than reassuring" — but a counter that never reads zero is one
    nobody acts on. Every line it flags should be a shape the parser genuinely
    did not anticipate, because those are the ones most likely to be new
    problems.
    """
    low = line.strip().lower()
    if _BREAKDOWN_ROW.match(low):
        return True
    if _SUMMARY_COUNTER.match(low):
        return True
    if after_preamble and line[:1].isspace() and not _SEVERITY_LEAD.match(line):
        return True
    return any(low.startswith(h) or h in low for h in _PREAMBLE_HINTS)


# --------------------------------------------------------------------------
# Summarizing + writing
# --------------------------------------------------------------------------

def _example_diff(rec: Record, anon: Anonymizer, mask: bool) -> list[str]:
    """A two-line sanitized before/after for one record, when it has one."""
    msg = anon.scrub(rec.message, mask)
    raw = re.search(r"raw(?:\s+had|=)\s*(.+?)(?:\s+render=|$)", msg)
    ren = re.search(r"render(?:ed)?\s*=\s*(.+)$", msg)
    if raw or ren:
        return [f"- Raw:      {rec.path or '(object)'}: {raw.group(1).strip() if raw else '(absent)'}",
                f"+ Rendered: {rec.path or '(object)'}: {ren.group(1).strip() if ren else '(absent)'}"]
    return [f"  {msg}"] if msg else []


# --------------------------------------------------------------------------
# What each category actually MEANS
# --------------------------------------------------------------------------
# A list of anonymized names is not a diagnosis. Nobody can act on
# "serviceaccount-alpha-214". What a reader needs is what the error means, why
# it happens, what to do about it and whether it is worth their time -- none of
# which requires knowing any real name.
#
# Every entry below comes from the 36-defect analysis campaign: these are the
# causes that were actually found and fixed, not guesses.
#
#   (what it means, likely cause, what to do, severity)

_UNKNOWN = (
    "An error type this tool does not have a note for yet.",
    "Unknown - the category was derived from the message text.",
    "Open the FULL report (internal only) and read a few of these lines.",
    "UNKNOWN",
)

CATEGORY_GUIDE: dict[str, tuple[str, str, str, str]] = {
    "missing_object": (
        "An object exists in the raw manifests but NO release renders it. "
        "After migrating, it simply will not be on the cluster.",
        "Usually the object was renamed (so it renders under a different name), "
        "or it belongs to a release the conversion did not produce.",
        "READ EVERY ONE. Check the 'relocated' category first - if the same "
        "count appears there, these are the same objects seen twice and nothing "
        "is actually lost.",
        "HIGH",
    ),
    "extra_object": (
        "A release renders an object that NO raw manifest declares. It WILL be "
        "created on the cluster.",
        "Most often the chart creating something by default - a ServiceAccount "
        "per release (serviceAccount.create defaults to true). Can also be the "
        "other half of a rename, where the same object appears under a new name.",
        "If the kinds below are all ServiceAccount, this is the chart's default "
        "and is harmless - set serviceAccount.create=false to stop it. Any OTHER "
        "kind here needs reading: something is being invented.",
        "LOW if ServiceAccount only, otherwise HIGH",
    ),
    "relocated": (
        "An object moved to a different release than the file it came from - a "
        "shared release, or the cluster-scoped one.",
        "By design. Objects declared in a namespace-shared file move to that "
        "'shared' release; cluster-scoped objects move to 'cluster-shared'.",
        "Nothing. This is informational and confirms the layout worked.",
        "NONE",
    ),
    "render_failure": (
        "A release could not be rendered at all by Helm.",
        "A values file the chart's schema rejects, or a template error.",
        "READ EVERY ONE. Nothing from this release will deploy.",
        "CRITICAL",
    ),
    "field_lost_labels": (
        "Labels present on the raw object are missing from the rendered one.",
        "The converter did not capture metadata.labels, or the template did not "
        "render them. Was the single highest-count defect in the original report.",
        "Mostly cosmetic (team/environment tags) EXCEPT "
        "argocd.argoproj.io/instance - losing that makes ArgoCD stop managing "
        "the object while it stays on the cluster. Check the FIELDS list below.",
        "LOW, unless argocd.argoproj.io/* appears",
    ),
    "field_lost_annotations": (
        "Annotations present on the raw object are missing from the rendered one.",
        "Same shape as labels: not captured, or not rendered.",
        "Check WHICH annotations. Safe to lose: kubectl last-applied, "
        "deployment.kubernetes.io/revision, openshift.io/*. NOT safe: "
        "argocd.argoproj.io/sync-wave (reorders your whole deploy), "
        "prometheus.io/scrape (metrics stop silently), checksum/config "
        "(pods stop restarting on config change).",
        "LOW for server-generated ones, HIGH for the four named above",
    ),
    "field_lost_resources": (
        "A container's resources block is missing from the rendered output.",
        "Almost always the raw value was an EMPTY map: `resources: {}`.",
        "If raw was {}, this is NOISE - an empty map and an absent key are "
        "identical to Kubernetes. If raw had actual limits/requests, it is real "
        "and the pod will run unbounded.",
        "NONE if raw was {}, otherwise HIGH",
    ),
    "field_lost_securitycontext": (
        "A securityContext is missing from the rendered output.",
        "Either the raw value was an empty map, or a per-job securityContext "
        "was not carried across.",
        "If raw was {} it is noise. If it had runAsUser/fsGroup or "
        "capabilities, the workload loses its hardening - read it.",
        "NONE if raw was {}, otherwise HIGH",
    ),
    "field_lost_serviceaccount": (
        "The deprecated `serviceAccount` pod field is missing.",
        "Kubernetes treats it as a deprecated alias for serviceAccountName and "
        "populates both. The chart renders only the modern key.",
        "Nothing - as long as serviceAccountName carries the same value, which "
        "it does unless that also appears in a 'changed' category.",
        "NONE",
    ),
    "field_lost_progressdeadlineseconds": (
        "A Deployment's progressDeadlineSeconds is missing.",
        "Not captured by the converter.",
        "Harmless if the raw value was 600 - that IS the Kubernetes default. "
        "Any other value is a real behaviour change in rollout failure timing.",
        "NONE if 600, otherwise MEDIUM",
    ),
    "field_lost_sessionaffinity": (
        "A Service's sessionAffinity is missing.",
        "The converter deliberately drops the value `None`.",
        "Harmless if the raw value was None - that is the Kubernetes default. "
        "`ClientIP` going missing is a real break in sticky sessions.",
        "NONE if None, otherwise HIGH",
    ),
    "field_changed_image": (
        "A container image differs between raw and rendered.",
        "Commonly a @sha256 digest replaced by a mutable tag, so a DIFFERENT "
        "binary can be deployed than the one that was running.",
        "READ EVERY ONE. This decides what code runs.",
        "CRITICAL",
    ),
    "field_changed_schedule": (
        "A CronJob's schedule differs between raw and rendered.",
        "Two CronJobs collapsing onto one values key, so one carried the "
        "other's schedule.",
        "READ EVERY ONE. The job fires at the wrong time, silently.",
        "HIGH",
    ),
    "field_changed_servicename": (
        "A StatefulSet's governing Service name differs.",
        "The name was replaced with the release fullname instead of the raw "
        "headless Service.",
        "READ EVERY ONE. Only a headless Service publishes per-pod DNS, so "
        "clustered apps cannot form a cluster without it.",
        "CRITICAL",
    ),
    "field_changed_serviceaccountname": (
        "The ServiceAccount a pod runs as differs from the raw manifest.",
        "The name was forced to match the release name.",
        "READ EVERY ONE. The pod references a ServiceAccount that may not "
        "exist - it will not start, and its RBAC does not apply.",
        "CRITICAL",
    ),
    "field_changed_replicas": (
        "The replica count differs.",
        "Expected when an HPA owns the count - the chart omits replicas so the "
        "autoscaler is not fought.",
        "Harmless if this release has an HPA. Otherwise the app runs at the "
        "wrong scale.",
        "NONE with an HPA, otherwise MEDIUM",
    ),
}


def guide_for(category: str) -> tuple[str, str, str, str]:
    """Explanation for a category, with sensible fallbacks by shape."""
    if category in CATEGORY_GUIDE:
        return CATEGORY_GUIDE[category]
    if category.startswith("field_lost_"):
        field = category[len("field_lost_"):].replace("_", " ")
        return (
            f"The field `{field}` is present in the raw manifest and missing "
            f"from the rendered output.",
            "Either the converter did not capture it, or no template renders it.",
            "Check whether the raw value was an empty map or the Kubernetes "
            "default for this field - if so it is noise. Otherwise it is a real "
            "loss and the FIELDS list below says exactly where.",
            "UNKNOWN - depends on the raw value",
        )
    if category.startswith("field_changed_"):
        field = category[len("field_changed_"):].replace("_", " ")
        return (
            f"The field `{field}` has a DIFFERENT value in the rendered output "
            f"than in the raw manifest.",
            "The conversion rewrote the value, often a name being replaced with "
            "a release-derived one.",
            "READ THESE. A changed value is almost always more serious than a "
            "missing one - something is actively wrong rather than absent.",
            "HIGH",
        )
    return _UNKNOWN


def summarize(category: str, recs: list[Record], anon: Anonymizer,
              mask_literals: bool = True, max_per_kind: int = 8) -> str:
    """Render one category into a short, sanitized report."""
    by_kind: dict[str, list[str]] = defaultdict(list)
    namespaces: set[str] = set()
    for r in recs:
        by_kind[r.kind].append(anon.resource(r.kind, r.name))
        namespaces.add(anon.namespace(r.namespace))

    means, cause, action, severity = guide_for(category)

    def wrap(text: str, indent: str = "  ") -> list[str]:
        import textwrap
        return textwrap.wrap(text, width=72, initial_indent=indent,
                             subsequent_indent=indent) or [indent.rstrip()]

    out = [
        f"ERROR CATEGORY: {category.replace('_', ' ').title()}",
        f"SEVERITY:       {severity}",
        f"OCCURRENCES:    {len(recs)}  across {len(namespaces)} namespace(s)",
        "",
        "WHAT IT MEANS:",
        *wrap(means),
        "",
        "LIKELY CAUSE:",
        *wrap(cause),
        "",
        "WHAT TO DO:",
        *wrap(action),
        "",
        "AFFECTED KINDS:",
    ]
    # Counts, not a list of meaningless aliases. The aliases are kept only so a
    # reader can correlate the SAME object across two categories; they are not
    # the point and they do not lead.
    for kind in sorted(by_kind, key=lambda k: -len(set(by_kind[k]))):
        names = sorted(set(by_kind[kind]))
        out.append(f"- {kind}: {len(names)} object(s)")
        if len(names) <= max_per_kind:
            out.append(f"    ids: {', '.join(names)}")
        else:
            out.append(f"    ids: {', '.join(names[:max_per_kind])} (+{len(names) - max_per_kind} more)")

    paths = Counter(r.path for r in recs if r.path)
    if paths:
        out += ["", "EXACT FIELDS (this is what to check against the list above):"]
        out += [f"- {anon.scrub(p, mask_literals)}  ({n}x)" for p, n in paths.most_common(5)]

    example = _example_diff(recs[0], anon, mask_literals)
    if example:
        out += ["", "EXAMPLE DIFF (Sanitized):"] + example

    body = "\n".join(out) + "\n"
    anon.assert_clean(body)
    return body


def write_reports(result: ParseResult, out_dir: pathlib.Path, anon: Anonymizer,
                  mask_literals: bool = True) -> dict[str, pathlib.Path]:
    """Write one file per category, plus an index. Returns category -> path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[Record]] = defaultdict(list)
    for rec in result.records:
        grouped[categorise(rec)].append(rec)

    # Register every real identifier BEFORE writing anything, so assert_clean
    # can see names that appear only inside another record's prose.
    for rec in result.records:
        anon.namespace(rec.namespace)
        anon.resource(rec.kind, rec.name)
        if rec.source:
            anon.source_file(rec.source)

    written: dict[str, pathlib.Path] = {}
    for category, recs in sorted(grouped.items()):
        path = out_dir / f"error_{category}.txt"
        path.write_text(summarize(category, recs, anon, mask_literals), encoding="utf-8")
        written[category] = path

    index = [
        "SANITIZED FIDELITY SUMMARY",
        "=" * 40,
        f"records parsed:   {len(result.records)}",
        f"lines unparsed:   {len(result.unparsed)}",
        f"categories:       {len(grouped)}",
        "",
        "CATEGORIES:",
    ]
    index += [f"- {c:<34} {len(r):>6} occurrence(s)"
              for c, r in sorted(grouped.items(), key=lambda kv: -len(kv[1]))]
    if result.unparsed:
        index += ["", "WARNING: some lines could not be parsed and are NOT summarized",
                  "         above. They are omitted entirely rather than shown,",
                  "         because an unparsed line cannot be safely sanitized."]
    body = "\n".join(index) + "\n"
    anon.assert_clean(body)
    idx = out_dir / "index.txt"
    idx.write_text(body, encoding="utf-8")
    written["_index"] = idx
    return written


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", required=True, help="raw fidelity_report.txt")
    ap.add_argument("-o", "--out-dir", required=True, help="directory for sanitized reports")
    ap.add_argument("--seed", type=int, default=None,
                    help="fix the alias seed for reproducible output (default: random)")
    ap.add_argument("--keep-literals", action="store_true",
                    help="do not mask base64/hex blobs in example diffs")
    a = ap.parse_args(argv)

    src = pathlib.Path(a.input)
    if not src.is_file():
        print(f"error: no such file: {src}", file=sys.stderr)
        return 2

    result = parse(src.read_text(encoding="utf-8", errors="replace"))
    anon = Anonymizer(seed=a.seed)
    try:
        written = write_reports(result, pathlib.Path(a.out_dir), anon,
                                mask_literals=not a.keep_literals)
    except LeakError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1

    print(f"parsed {len(result.records)} record(s); "
          f"{len(result.unparsed)} line(s) unparsed")
    for cat, path in sorted(written.items()):
        print(f"  {path}")
    if result.unparsed:
        print(f"\nWARNING: {len(result.unparsed)} line(s) were not parsed and are "
              f"NOT in the summaries. Review the input format before sharing.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
