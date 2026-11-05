from __future__ import annotations

import html
import json
import time
from collections.abc import Iterable

from . import config as C

CSS = """
:root { color-scheme: light dark; --bg:#f4f5f7; --panel:#fff; --ink:#182326;
  --muted:#59666b; --line:#d8e0e2; --accent:#126954; --soft:#e8f3ee; --code:#f5f7f8; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#101719; --panel:#182225; --ink:#e5edef; --muted:#a6b6bd;
    --line:#354448; --accent:#8ad8bc; --soft:#203e34; --code:#111b1e; }
}
:root[data-theme="light"] { color-scheme:light; --bg:#f4f5f7; --panel:#fff; --ink:#182326;
  --muted:#59666b; --line:#d8e0e2; --accent:#126954; --soft:#e8f3ee; --code:#f5f7f8; }
:root[data-theme="dark"] { color-scheme:dark; --bg:#101719; --panel:#182225; --ink:#e5edef;
  --muted:#a6b6bd; --line:#354448; --accent:#8ad8bc; --soft:#203e34; --code:#111b1e; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.6 system-ui,sans-serif; }
a { color:var(--accent); }
button,input { font:inherit; }
button { cursor:pointer; }
button,input { color:var(--ink); background:var(--panel); border:1px solid var(--line);
  border-radius:8px; padding:.6rem .85rem; }
:focus-visible { outline:3px solid var(--accent); outline-offset:3px; }
[hidden] { display:none !important; }
.skip { position:absolute; left:1rem; top:-5rem; background:var(--panel); padding:.6rem 1rem; }
.skip:focus { top:1rem; }
.shell { max-width:1440px; margin:auto; padding:2rem clamp(1rem,4vw,4rem); }
header { display:flex; justify-content:space-between; gap:2rem; align-items:center; margin-bottom:1.8rem; }
.brand { margin:0; font-size:1.5rem; font-weight:750; letter-spacing:-.04em; }
.brand span { font-weight:400; color:var(--muted); letter-spacing:0; }
.eyebrow { color:var(--accent); font-size:.75rem; font-weight:750; letter-spacing:.12em;
  text-transform:uppercase; margin:0 0 .3rem; }
.quiet { color:var(--muted); }
.small { font-size:.85rem; }
.layout { display:grid; grid-template-columns:210px minmax(0,1fr); gap:2rem; align-items:start; }
aside { position:sticky; top:1rem; }
nav { display:flex; flex-direction:column; gap:.3rem; }
nav a { display:block; padding:.7rem .85rem; text-decoration:none; color:var(--muted);
  border:1px solid transparent; border-radius:8px; }
nav a:hover,nav a[aria-current="page"] { background:var(--soft); color:var(--accent); }
nav a[aria-current="page"] { font-weight:650; }
.local { border-top:1px solid var(--line); margin-top:1.6rem; padding-top:1rem; }
main { min-width:0; }
.tools { display:flex; flex-wrap:wrap; gap:.5rem; align-items:center; margin:0 0 1.5rem; }
.tools label { width:100%; font-size:.85rem; font-weight:650; }
.tools input { width:100%; }
section { scroll-margin-top:1rem; margin-bottom:3rem; }
section > h1 { font-size:1.9rem; letter-spacing:-.035em; line-height:1.2; margin:.2rem 0 .6rem; }
section > p { margin-top:0; max-width:78ch; }
h2 { font-size:1.15rem; line-height:1.4; margin:0 0 .45rem; overflow-wrap:anywhere; }
h3 { font-size:1rem; margin:1.1rem 0 .5rem; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:1.2rem; margin:1rem 0; min-width:0; }
.card > p { margin:.25rem 0 1rem; }
.badge { display:inline-block; border:1px solid var(--line); border-radius:20px;
  padding:.1rem .6rem; color:var(--accent); font-size:.8rem; overflow-wrap:anywhere; }
.table-scroll { overflow:auto; max-width:100%; margin:.6rem 0; }
table { width:100%; border-collapse:collapse; font-size:.87rem; text-align:left; }
th,td { padding:.6rem .7rem; border-bottom:1px solid var(--line); vertical-align:top; }
thead th { color:var(--muted); font-size:.78rem; font-weight:650; white-space:nowrap; }
tbody th { font-weight:550; color:var(--muted); width:28%; }
td { overflow-wrap:anywhere; white-space:pre-wrap; }
tr:last-child td,tr:last-child th { border-bottom:0; }
pre { padding:1rem; background:var(--code); border:1px solid var(--line); border-radius:8px;
  overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font:12px/1.7 ui-monospace,monospace; }
details { margin:.7rem 0; }
details > details { margin-left:.5rem; }
summary { cursor:pointer; color:var(--accent); font-weight:550; overflow-wrap:anywhere; }
.empty { color:var(--muted); padding:1rem 0; }
.warning { border-left:3px solid var(--accent); padding:.6rem 1rem; background:var(--soft); }
footer { border-top:1px solid var(--line); margin-top:2rem; padding-top:1rem;
  color:var(--muted); font-size:.8rem; overflow-wrap:anywhere; }
@media (max-width:760px) {
  .layout { grid-template-columns:1fr; gap:1rem; }
  aside { position:static; }
  nav { flex-direction:row; flex-wrap:wrap; gap:.2rem; }
  nav a { padding:.45rem .65rem; font-size:.85rem; }
  .local { display:none; }
  header { align-items:start; gap:1rem; }
  .brand { font-size:1.25rem; }
  .card { padding:1rem; }
}
"""

JS = """
(() => {
  const panels = Array.from(document.querySelectorAll('main > section'));
  const links = Array.from(document.querySelectorAll('nav a'));
  const search = document.getElementById('filter');
  function select() {
    const wanted = location.hash.slice(1);
    const active = panels.find(panel => panel.id === wanted) || panels[0];
    panels.forEach(panel => { panel.hidden = panel !== active; });
    links.forEach(link => {
      if (link.hash === '#' + active.id) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }
  function filter() {
    const term = search.value.trim().toLocaleLowerCase();
    panels.forEach(panel => {
      const cards = Array.from(panel.querySelectorAll('[data-filter-item]'));
      cards.forEach(card => { card.hidden = !card.textContent.toLocaleLowerCase().includes(term); });
      const empty = panel.querySelector('[data-filter-empty]');
      if (empty) empty.hidden = !cards.length || cards.some(card => !card.hidden);
    });
  }
  window.addEventListener('hashchange', select);
  search.addEventListener('input', filter);
  document.querySelector('.tools').hidden = false;
  const theme = document.getElementById('theme');
  theme.hidden = false;
  theme.addEventListener('click', () => {
    const root = document.documentElement;
    const dark = root.dataset.theme
      ? root.dataset.theme === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    theme.textContent = dark ? 'Dark theme' : 'Light theme';
  });
  theme.textContent = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'Light theme' : 'Dark theme';
  select();
})();
"""

VIEWS = (
    ("overview", "Overview"),
    ("skills", "Skills"),
    ("receipts", "Learning receipts"),
    ("rejections", "Judge rejections"),
    ("notes", "Notes"),
)
# Each view's shares of the page, and what each share leaves out for size: a record, then a large value.
VIEW_SHARES = {
    "overview": ("overview",),
    "skills": ("skills", "history"),
    "receipts": ("receipts",),
    "rejections": ("rejections",),
    "notes": ("notes",),
}
LEFT_OUT = {
    "overview": ("skill outcome row", "value"),
    "skills": ("skill", "skill text"),
    "history": ("version change", "diff"),
    "receipts": ("receipt record", "findings text"),
    "rejections": ("rejection", "findings text"),
    "notes": ("note", "note text"),
}
RECEIPT_LISTS = (
    ("learned", "Learning origins"),
    ("changes", "Applied changes"),
    ("loads", "Recorded loads and deliveries"),
    ("reviews", "Later reviews"),
    ("rejections", "Rejected changes"),
)
OUTCOME_FIELDS = ("deliveries", "pointers", "loads", "corrections", "rejections", "undone_archives", "attention")
OUTCOME_HEADERS = (
    "Skill",
    "Deliveries",
    "Pointers",
    "Loads / explicit opens",
    "Corrections",
    "Rejections",
    "Undone archives",
    "Attention",
)


def _value(value) -> str:
    if value is None:
        return "Not recorded"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _escape(value) -> str:
    return html.escape(_value(value), quote=True)


def _date(value) -> str:
    if isinstance(value, (int, float)):
        try:
            return time.strftime("%Y-%m-%d", time.localtime(value))
        except (ValueError, OverflowError, OSError):
            pass
    return _value(value)


def _row(values: Iterable) -> str:
    return "<tr>" + "".join(f"<td>{_escape(v)}</td>" for v in values) + "</tr>"


def _many(count: int, noun: str) -> str:
    return f"{int(count):,} {noun}{'' if count == 1 else 's'}"


def _left_out(count: int, noun: str) -> str:
    return f'<p class="quiet small">{_many(count, noun)} left out for size.</p>' if count else ""


def _table(headers: Iterable, body: str, left_out: int = 0, noun: str = "row") -> str:
    note = _left_out(left_out, noun)
    if not body:
        return note or '<p class="empty">No records in this snapshot.</p>'
    head = "".join(f'<th scope="col">{_escape(h)}</th>' for h in headers)
    return f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{note}'


def _pairs(values: dict) -> str:
    rows = "".join(
        f'<tr><th scope="row">{_escape(str(k).replace("_", " "))}</th><td>{_escape(v)}</td></tr>'
        for k, v in values.items()
    )
    return f'<div class="table-scroll"><table><tbody>{rows}</tbody></table></div>'


def _details(title, content: str) -> str:
    return f"<details><summary>{_escape(title)}</summary>{content}</details>"


def _pre(value) -> str:
    return f"<pre>{_escape(value)}</pre>"


def _older(count: int, what: str) -> str:
    return f'<p class="quiet small">{int(count):,} older {what} are counted but not shown.</p>' if count else ""


def _card(title, description, source, body: str) -> str:
    return (
        '<article class="card" data-filter-item>'
        f'<span class="badge">{_escape(source)}</span><h2>{_escape(title)}</h2>'
        f'<p class="quiet">{_escape(description)}</p>{body}</article>'
    )


def receipt_record(row: dict) -> str:
    """One entry of a receipt list."""
    title = " · ".join(_value(row.get(key)) for key in ("date", "action", "verdict") if row.get(key) is not None)
    return _details(title or "Recorded event", _pairs(row))


def _records(title: str, records: list[dict], older: int = 0, left_out: int = 0) -> str:
    shown = records[-C.UI_RECORDS_SHOWN :]
    older += len(records) - len(shown)
    body = "".join(receipt_record(row) for row in shown) + _older(older, "records") + _left_out(left_out, "record")
    count = len(shown) + older + left_out
    return _details(f"{title} ({count:,})", body or '<p class="empty">No records in this snapshot.</p>')


def receipt_card(item: dict) -> str:
    """One skill's learning and use receipts."""
    older, left_out = item.get("older", {}), item.get("omitted", {})
    detail = ""
    for key, label in RECEIPT_LISTS:
        detail += _records(label, item.get(key, []), older.get(key, 0), left_out.get(key, 0))
        if key == "learned":
            detail += _details("All-time outcomes", _pairs(item.get("outcome", {})))
    return _card(item.get("name"), "Learning and use receipts", item.get("source"), detail)


def history_entry(change: dict) -> str:
    """One change in a skill's version history, with its diff and ledger receipt."""
    content = _pairs({key: change.get(key) for key in ("date", "action", "file", "before", "after")})
    content += _pre(change.get("diff")) + _details("Ledger receipt", _pairs(change.get("receipt", {})))
    return _details(f"{_value(change.get('date'))} · {_value(change.get('action'))}", content)


def skill_card(item: dict) -> str:
    """One skill with its current text and version history."""
    detail = _pairs({"Status": item.get("status"), "Current version": item.get("version")})
    detail += _details("Current skill text", _pre(item.get("text")))
    history = "".join(history_entry(change) for change in item.get("history", []))
    history += _older(item.get("history_older", 0), "changes") + _left_out(item.get("history_omitted", 0), "change")
    detail += _details("Version history", history or '<p class="empty">No recorded version history.</p>')
    return _card(item.get("name"), item.get("description"), item.get("source"), detail)


def rejection_card(item: dict) -> str:
    """One judge rejection with its findings."""
    return _card(item.get("name"), item.get("date"), item.get("verdict", "Not recorded"), _pairs(item))


def note_card(item: dict) -> str:
    """One note, global memory entry or retained note backup."""
    detail = _pairs({key: item.get(key) for key in ("project", "path", "status")})
    detail += _details("Note text", _pre(item.get("text")))
    return _card(item.get("name"), item.get("description"), item.get("source"), detail)


def outcome_row(outcome: dict) -> str:
    """One skill's row in the all-time outcomes table."""
    return _row([outcome.get("label", outcome.get("name")), *(outcome.get(k) for k in OUTCOME_FIELDS)])


def overview(data: dict, summary: str = "") -> str:
    """The Overview view: the report's weekly tables, project notes and skill outcomes."""
    weeks = data.get("weeks", [])
    body = '<p class="quiet">Background learning and usage, using the same measures as nowitknows report.</p>' + summary
    body += _table(
        ("Week from", "Reviews", "Failed", "Applied", "Judge rejected", "Expired unjudged", "Tokens / applied"),
        "".join(
            _row(
                [
                    _date(w.get("start")),
                    *(w.get(k) for k in ("runs", "failed", "applied", "rejected", "expired", "tokens_per_applied")),
                ]
            )
            for w in weeks
        ),
    )
    for week in weeks:
        detail = "<h3>Tokens processed by model family</h3>" + _table(
            ("Model family", "Tokens"), "".join(_row(pair) for pair in sorted(week.get("tokens", {}).items()))
        )
        detail += "<h3>By host</h3>" + _table(
            ("Host", "Reviews", "Tokens"),
            "".join(
                _row([host, counts.get("runs"), counts.get("tokens")]) for host, counts in week.get("hosts", {}).items()
            ),
        )
        detail += "<h3>Routing and recall</h3>" + _pairs(
            {
                "Automatic deliveries": week.get("deliveries"),
                "Context pointers": week.get("pointers"),
                "Loads / observed explicit opens": week.get("loads"),
                "Router suggestions": week.get("suggestions"),
                "Suggestions loaded the same turn": week.get("followed"),
                "Session searches": week.get("searches"),
                "Recall hints": week.get("recall_hints"),
                "Hints followed by a search the same turn": week.get("recall_followed"),
            }
        )
        body += _details(f"Week from {_date(week.get('start'))}: tokens, routing and recall", detail)
    body += '<div class="card"><h2>Project notes</h2>' + _pairs(data.get("note_judge", {}))
    body += "<h3>Background notes that could not be saved</h3>" + _pre(data.get("refusals", "none")) + "</div>"
    body += '<div class="card"><h2>Skill outcomes, all time</h2>' + _table(
        OUTCOME_HEADERS,
        "".join(outcome_row(o) for o in data.get("outcomes", [])),
        data.get("outcomes_omitted", 0),
        "skill outcome row",
    )
    body += (
        '<p class="quiet small">Deliveries and pointers show context availability. Loads record a skill load or '
        "an observed explicit open, depending on the host. None proves the instructions were followed. "
        "Corrections count reviewed sessions that led to an applied skill change; they are not task success scores. "
        "Undone archives count curator archives restored within 30 days.</p></div>"
    )
    return body


def _summary(omitted: dict, view: str) -> str:
    parts = [
        _many(count, noun)
        for share in VIEW_SHARES[view]
        for kind, noun in zip(("records", "values"), LEFT_OUT[share], strict=True)
        if (count := omitted.get(share, {}).get(kind, 0))
    ]
    if not parts:
        return ""
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return (
        f'<p class="warning">Left out for size: {listed}. This view has its own share of the page, and a file over '
        f"{C.UI_FILE_MAX_BYTES:,} bytes is not read. The data folder still has everything left out.</p>"
    )


def _empty(items: list, left_out: int = 0) -> str:
    if items:
        return '<p class="empty" data-filter-empty hidden>No matching records. Try another search.</p>'
    return "" if left_out else '<p class="empty">No records in this snapshot.</p>'


def _skills(items: list[dict], summary: str, left_out: int) -> str:
    body = (
        '<p class="quiet">Current learned content and the recorded changes behind it. Versions are content hashes.</p>'
    )
    return body + summary + "".join(skill_card(item) for item in items) + _empty(items, left_out)


def _receipts(items: list[dict], summary: str, left_out: int) -> str:
    body = '<p class="quiet">Where a skill came from, the judge records, and what happened after it was loaded.</p>'
    return body + summary + "".join(receipt_card(item) for item in items) + _empty(items, left_out)


def _rejections(items: list[dict], older: int, summary: str, left_out: int) -> str:
    body = '<p class="quiet">Rejected changes and retained findings. Unjudged expirations are labeled separately.</p>'
    body += summary + "".join(rejection_card(item) for item in items)
    return body + _older(older, "rejections") + _empty(items, left_out)


def _notes(items: list[dict], older: int, summary: str, left_out: int) -> str:
    body = '<p class="quiet">Project notes and global memory, shown as plain text with their recorded sources.</p>'
    body += summary + "".join(note_card(item) for item in items)
    return body + _older(older, "retained note backups") + _empty(items, left_out)


def render(snapshot: dict) -> str:
    """Render one inert data snapshot; only fixed navigation, filtering and theme scripts can execute."""
    omitted = snapshot.get("omitted", {})

    def left_out(share: str) -> int:
        return int(omitted.get(share, {}).get("records", 0))

    contents = (
        overview(snapshot.get("overview", {}), _summary(omitted, "overview")),
        _skills(snapshot.get("skills", []), _summary(omitted, "skills"), left_out("skills")),
        _receipts(snapshot.get("receipts", []), _summary(omitted, "receipts"), left_out("receipts")),
        _rejections(
            snapshot.get("rejections", []),
            snapshot.get("rejections_older", 0),
            _summary(omitted, "rejections"),
            left_out("rejections"),
        ),
        _notes(
            snapshot.get("notes", []), snapshot.get("notes_older", 0), _summary(omitted, "notes"), left_out("notes")
        ),
    )
    navigation = "".join(f'<a href="#{key}" aria-controls="{key}">{label}</a>' for key, label in VIEWS)
    sections = "".join(
        f'<section id="{key}" aria-labelledby="{key}-title"><h1 id="{key}-title">{label}</h1>{body}</section>'
        for (key, label), body in zip(VIEWS, contents, strict=True)
    )
    notices = list(snapshot.get("warnings", []))
    records = sum(int(counts.get("records", 0)) for counts in omitted.values())
    values = sum(int(counts.get("values", 0)) for counts in omitted.values())
    if records or values:
        notices.append(
            f"{_many(records, 'record')} and {_many(values, 'value')} were left out for size; each view says which. "
            "The data folder still has them."
        )
    warnings = "".join(f'<p class="warning">{_escape(warning)}</p>' for warning in notices)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer"><title>NowItKnows | Local overview</title>'
        f"<style>{CSS}</style></head><body>"
        '<a class="skip" href="#main">Skip to content</a><div class="shell">'
        '<header><div><p class="eyebrow">Local learning journal</p>'
        '<p class="brand">NowItKnows <span>/ read only</span></p></div>'
        '<button id="theme" type="button" hidden>Switch theme</button></header>'
        f'<div class="layout"><aside><nav aria-label="Views">{navigation}</nav>'
        '<p class="local quiet small">A local snapshot of your learning store. '
        "No edits, model calls, or external requests.</p>"
        '</aside><main id="main" tabindex="-1"><div class="tools" hidden>'
        '<label for="filter">Search skills, receipts, rejections and notes</label>'
        '<input id="filter" type="search" placeholder="Search this snapshot" autocomplete="off">'
        f"</div>{warnings}{sections}</main></div>"
        "<footer>Snapshot: "
        + _escape(snapshot.get("generated"))
        + "<br>Data folder: "
        + _escape(snapshot.get("data_dir"))
        + "<br>Transcript text is not included. Restart nowitknows ui for a fresh snapshot.</footer></div>"
        f"<script>{JS}</script></body></html>"
    )
