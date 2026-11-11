"""Recording-duration / interruption report for one exported research session.

Works on the CSV pair written by research_app.py ("Export selected session as plaintext CSV")
or by ``cloud_sync.py download``:

    python pc_tools/session_report.py data/exports/long_test.csv
    python pc_tools/session_report.py data/exports/long_test.csv --gap 2 --out data/exports/long_test_report.md

If ``<name>_events.csv`` sits next to the sample CSV it is read too (connections, interruptions).
The file is streamed, so multi-hour exports are fine.

What it reports, per signal: samples received, first/last arrival, the span it was active,
observed sample rate while active, and reception gaps (consecutive packets of that signal
arriving more than --gap seconds apart). It only describes what the laptop received. It cannot
tell whether the watch stopped measuring or the radio dropped data, and it never fills gaps.
"""
import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
import sys


def fmt_s(seconds):
    seconds = max(0.0, float(seconds))
    h, rem = divmod(int(round(seconds)), 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s)


def utc(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class SignalStats:
    def __init__(self):
        self.samples = 0
        self.packets = 0
        self.first_elapsed = self.last_elapsed = None
        self.first_utc = self.last_utc = None
        self.last_packet = None
        self.gaps = []          # (start_elapsed, length_s)
        self.segments = set()

    def add(self, row, gap_s):
        elapsed = float(row["recv_elapsed_s"])
        self.samples += 1
        self.segments.add(row.get("connection_segment", "0"))
        packet = row["packet_seq_host"]
        if packet == self.last_packet:
            return              # further samples of the same packet share its arrival time
        self.last_packet = packet
        self.packets += 1
        if self.first_elapsed is None:
            self.first_elapsed, self.first_utc = elapsed, float(row["recv_utc"])
        elif elapsed - self.last_elapsed > gap_s:
            self.gaps.append((self.last_elapsed, elapsed - self.last_elapsed))
        self.last_elapsed, self.last_utc = elapsed, float(row["recv_utc"])


def read_events(path):
    if not path.is_file():
        return None
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_report(csv_path, gap_s=2.0, events_path=None):
    csv_path = Path(csv_path)
    events_path = Path(events_path) if events_path else csv_path.with_name(csv_path.stem + "_events.csv")
    stats, session, rows = {}, None, 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows += 1
            session = session or row["session_id"]
            stats.setdefault(row["signal"], SignalStats()).add(row, gap_s)
    if not rows:
        raise SystemExit("No samples in %s" % csv_path)
    events = read_events(events_path)

    start = min(s.first_elapsed for s in stats.values())
    end = max(s.last_elapsed for s in stats.values())
    start_utc = min(s.first_utc for s in stats.values())
    lines = ["# Session report", "",
             "Session: `%s`  " % session,
             "File: `%s` (%d sample rows)  " % (csv_path.name, rows),
             "First sample: %s; data span (first to last sample, any signal): **%s**" % (utc(start_utc), fmt_s(end - start)),
             ""]
    if events is not None:
        kinds = [e["event"] for e in events]
        lines += ["Connection events (`%s`): %d connection(s), %d interruption(s)." % (
            events_path.name, kinds.count("connected"), kinds.count("connection_gap")), ""]
    else:
        lines += ["No events file found (`%s`); connection count unknown." % events_path.name, ""]

    lines += ["| Signal | Samples | Active from - to (elapsed) | Active span | Observed rate (samples/s over span) "
              "| Gaps > %gs | Longest gap | Time in gaps | Coverage of session span |" % gap_s,
              "|---|---|---|---|---|---|---|---|---|"]
    for name in sorted(stats):
        s = stats[name]
        span = s.last_elapsed - s.first_elapsed
        in_gaps = sum(g for _, g in s.gaps)
        rate = s.samples / span if span > 0 else float("nan")
        coverage = (span - in_gaps) / (end - start) * 100 if end > start else 100.0
        lines.append("| %s | %d | %s - %s | %s | %.2f | %d | %s | %s | %.1f %% |" % (
            name, s.samples, fmt_s(s.first_elapsed - start), fmt_s(s.last_elapsed - start), fmt_s(span),
            rate, len(s.gaps), fmt_s(max((g for _, g in s.gaps), default=0)), fmt_s(in_gaps), coverage))

    lines += ["", "## Gaps (first 30 per signal)", ""]
    for name in sorted(stats):
        gaps = stats[name].gaps
        if not gaps:
            lines.append("- %s: none longer than %g s" % (name, gap_s))
            continue
        shown = ", ".join("at %s for %.1f s" % (fmt_s(t - start), g) for t, g in gaps[:30])
        lines.append("- %s (%d): %s%s" % (name, len(gaps), shown, " ..." if len(gaps) > 30 else ""))
    if events:
        lines += ["", "## Connection events", "", "| Elapsed | Event | Details |", "|---|---|---|"]
        lines += ["| %s | %s | %s |" % (fmt_s(float(e["elapsed_s"])), e["event"], e["details"]) for e in events]
    lines += ["", "Coverage = (active span - time in gaps) / session span. It describes laptop reception only; "
              "a signal can stop because the watch measurement ended, not only because the link dropped."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", help="sample CSV exported from the recorder or cloud_sync.py download")
    p.add_argument("--events", help="events CSV (default: <csv name>_events.csv next to it)")
    p.add_argument("--gap", type=float, default=2.0, help="seconds without packets that count as a gap (default 2)")
    p.add_argument("--out", help="also write the report to this Markdown file")
    a = p.parse_args(argv)
    report = build_report(a.csv, a.gap, a.events)
    print(report)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(report, encoding="utf-8")
        print("Saved to " + a.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
