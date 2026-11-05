from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from . import config as C
from .memory import TopicStore, parse_topic
from .state import atomic_write, link_target

STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "to",
        "of",
        "for",
        "in",
        "on",
        "with",
        "my",
        "this",
        "that",
        "it",
        "is",
        "be",
        "me",
        "i",
        "you",
        "we",
        "our",
        "your",
        "from",
        "by",
        "at",
        "as",
        "into",
        "get",
        "make",
        "do",
        "can",
        "please",
        "want",
        "need",
        "about",
        "some",
        "these",
        "those",
        "its",
        "how",
        "what",
        "why",
        "when",
        "where",
        "which",
        "who",
        "use",
        "using",
        "help",
        "let",
        "lets",
        "just",
        "now",
        "then",
        "also",
        "there",
        "their",
        "them",
        "was",
        "were",
        "are",
        "am",
        "has",
        "have",
        "had",
        "will",
        "would",
        "could",
        "should",
        "up",
        "out",
        "so",
        "if",
        "not",
        "no",
        "yes",
        "ok",
    ]
)
CACHE_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]  # code change invalidates caches
WEIGHTS = {"name": 3, "description": 2, "when_to_use": 2, "triggers": 2, "headings": 1}


def toks(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in STOP or len(w) < 2:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


def request_text(prompt: str) -> str:
    """What the user asked, for routing: pasted blocks, code fences and quoted lines are material, not the request."""
    text = re.sub(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", " ", prompt or "", flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def length_factor(query: str) -> float:
    """A long request shares words with many skills by chance, so its threshold grows with sqrt of its length."""
    return max(1.0, math.sqrt(len(set(toks(query))) / C.ROUTER_LONG_TOKENS))


def route_request(index: BM25, prompt: str, threshold: float, **kw) -> list[tuple[Doc, float]]:
    """The hook's routing: what the user asked, against a threshold raised for a long request."""
    request = request_text(prompt)
    return index.route(request, min_score=threshold * length_factor(request), **kw)


def frontmatter(text: str) -> tuple[dict, str]:
    """Top-level YAML scalars, including `|`/`>` block scalars and indented continuation lines."""
    m = re.match(r"^\ufeff?---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)  # a BOM, as Claude Code allows
    if not m:
        return {}, text
    meta: dict = {}
    key, block = None, None
    for line in m.group(1).splitlines():
        mm = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if mm:
            key, value = mm.group(1), mm.group(2).strip()
            block = value[:1] if value[:1] in ("|", ">") else None
            meta[key] = "" if block else value.strip("'\"")
        elif key and line[:1] in (" ", "\t") and line.strip():
            meta[key] = (meta[key] + ("\n" if block == "|" else " ") + line.strip()).strip()
    return meta, m.group(2)


@dataclass
class Doc:
    name: str
    description: str
    path: str
    kind: str
    fields: dict
    source: str = "local"


@dataclass(frozen=True)
class SkillRoots:
    plugins: tuple[str, ...] = ()
    system: tuple[str, ...] = ()
    personal: tuple[str, ...] = ()


def skill_roots(claude_home: Path, skills_home: Path, env: dict) -> SkillRoots:
    env = C.environment(env)
    home = C.home_dir(env)
    codex = Path(C.env("NOWITKNOWS_CODEX_HOME", env) or env.get("CODEX_HOME") or home / ".codex").expanduser()
    return SkillRoots(
        tuple(
            os.path.join(os.path.normcase(str(p.resolve())), "")
            for p in (claude_home / "plugins", codex / "plugins", home / ".agents/plugins")
        ),
        tuple(
            os.path.join(os.path.normcase(str(p.resolve())), "")
            for root in (skills_home, codex / "skills", home / ".agents/skills")  # Codex reads synced skills here
            for p in (root / "synced", root / ".system")
        ),
        (
            os.path.join(
                os.path.normcase(str(C.data_dir(env).resolve() / "skills")),
                "",
            ),
        ),
    )


def skill_source(
    path: Path, prefix: str = "", scope: str = "", *, roots: SkillRoots = SkillRoots(), real: Path | None = None
) -> str:
    """Resolve managed roots once per index, rather than treating unrelated ancestor names as provenance."""
    real = path.resolve() if real is None else real
    if not prefix and scope not in C.ROUTER_EXCLUDED_SOURCES and os.path.normcase(str(real)).startswith(roots.personal):
        return "local"
    if prefix or scope == "plugin" or os.path.normcase(str(real)).startswith(roots.plugins):
        return "plugin"
    if scope in C.ROUTER_EXCLUDED_SOURCES:
        return scope
    if (
        path.parent.name != "synced"
        and real.parent.name != "synced"
        and os.path.normcase(str(real.parent)).startswith(roots.system)
    ):
        return "system"
    return "local"


def _skill_doc(
    path: Path,
    prefix: str = "",
    triggers: dict | None = None,
    scope: str = "local",
    *,
    source: dict | None = None,
    cfg: C.Config | None = None,
) -> Doc | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if cfg is not None and cfg.host == "cursor":
        from .delivery import masked_delivery

        meta, body = frontmatter(masked_delivery(text, cfg))
    else:
        meta, body = frontmatter(text)
    if str(meta.get("disable-model-invocation", "")).lower() == "true":
        return None
    name = meta.get("name") or path.parent.name
    full = f"{prefix}{name}"
    # Without a description the router line would say nothing; the body's first line (often a title) says something.
    description = meta.get("description") or next(
        (t for l in body.splitlines() if (t := l.strip().lstrip("#").strip())), name
    )
    fields = {
        "name": name.replace("-", " "),
        "description": description,
        "when_to_use": meta.get("when_to_use", ""),
        "headings": " ".join(re.findall(r"^#+\s*(.+)$", body, re.M)),
        "triggers": " ".join(
            trigger_queries(trigger_entry(triggers or {}, full, source or trigger_source(path), text))
        ),
    }
    return Doc(full, description, str(path), "skill", fields, "plugin" if prefix else scope)


def _enabled_plugins(chain: list[dict]) -> dict:
    merged: dict = {}
    for s in reversed(chain):
        merged.update(s.get("enabledPlugins") or {})
    return merged


def plugin_skill_files(
    claude_home: Path, chain: list[dict], resolved: dict[Path, Path] | None = None
) -> list[tuple[Path, str]]:
    try:
        installed = json.loads((claude_home / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))[
            "plugins"
        ]
    except (OSError, ValueError, KeyError):
        return []
    enabled = _enabled_plugins(chain)
    out = []
    for pid, installs in installed.items():
        if enabled.get(pid) is False or pid not in enabled:
            continue
        for inst in installs if isinstance(installs, list) else [installs]:
            root = Path(inst.get("installPath", ""))
            try:
                pname = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
            except (OSError, ValueError, KeyError):
                pname = pid.split("@")[0]
            out += [(p, f"{pname}:") for p in find_skills(root / "skills", resolved=resolved)]
    return out


def _is_dir(entry: os.DirEntry) -> bool:
    """A symlink loop or an unreadable link skips that entry only, as Path.is_dir did."""
    try:
        return entry.is_dir()
    except OSError:
        return False


def find_skills(
    root: Path,
    seen: set | None = None,
    depth: int = 3,
    *,
    resolved: dict[Path, Path] | None = None,
    link_targets: dict[str, Path] | None = None,
    signatures: dict[Path, tuple] | None = None,
) -> list[Path]:
    """SKILL.md files under `root`, following symlinked folders as Claude Code does; each real folder once."""
    seen = set() if seen is None else seen
    out: list[Path] = []
    parents: dict[Path, Path] = {}

    def linked_folder(path: str) -> Path:
        raw = link_target(os.readlink(path))
        key = raw if os.path.isabs(raw) else os.path.join(os.path.dirname(path), raw)
        if link_targets is not None and key in link_targets:
            return link_targets[key]
        target = Path(raw)
        if not target.is_absolute():
            target = Path(path).parent / target
        if target.name == "..":
            return Path(path).resolve()
        parent = parents.get(target.parent)
        if parent is None:
            parent = parents[target.parent] = target.parent.resolve()
        real = parent / target.name
        real = real.resolve() if real.is_symlink() else real
        if link_targets is not None:
            link_targets[key] = real
        return real

    def walk(d: Path, real: Path, level: int) -> None:
        if real in seen:
            return
        seen.add(real)
        try:
            path = d / "SKILL.md"
            if signatures is not None:
                info = None
                linked = False
                try:
                    info = path.lstat()
                    linked = stat.S_ISLNK(info.st_mode)
                    if linked:
                        info = path.stat()
                except OSError:
                    pass
                found = info is not None and stat.S_ISREG(info.st_mode)
            else:
                found = path.is_file()
                linked = found and resolved is not None and path.is_symlink()
            if found:
                out.append(path)
                if resolved is not None:
                    resolved[path] = path.resolve() if linked else real / "SKILL.md"
                if signatures is not None:
                    signatures[path] = _generation(info)
                return
            if level >= depth:
                return
            with os.scandir(d) as it:  # entry types come with the listing: no stat per child on the prompt path
                children = sorted(
                    (
                        e
                        for e in it
                        if not e.name.startswith(".") and ((link_targets is not None and e.is_symlink()) or _is_dir(e))
                    ),
                    key=lambda e: os.path.normcase(e.name),
                )
        except OSError:
            return
        for e in children:
            try:
                child = linked_folder(e.path) if e.is_symlink() else real / e.name
            except (OSError, RuntimeError):
                continue
            if child not in seen:
                walk(d / e.name, child, level + 1)

    try:
        if root.is_dir():
            walk(root, root.resolve(), 0)
    except OSError:
        pass
    return out


def skill_files(
    skill_dirs: list[Path], claude_home: Path, chain: list[dict], *, resolved: dict[Path, Path] | None = None
) -> list[tuple[Path, str]]:
    seen: set = set()
    files = [(p, "") for d in skill_dirs for p in find_skills(d, seen, resolved=resolved)]
    return files + plugin_skill_files(claude_home, chain, resolved)


def home_skill_roots(cfg: C.Config) -> tuple[Path, ...]:
    return (*cfg.skill_link_homes, cfg.skills_home, C.home_dir(cfg.env) / ".agents/skills")


def home_skill(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def canonical_names(cfg: C.Config | None, paths) -> set[str]:
    """Only Nik-managed skills get precedence when a profile also holds user-authored skills."""
    if cfg is None:
        return set()
    library = cfg.skills_library.resolve()
    names = {path.parent.name for path in paths if path.parent.parent == library}
    if library == cfg.skills_home.resolve():
        from .migrate import _made

        names.intersection_update(_made(cfg.data / "skills_ledger.jsonl", cfg.host, library))
    return names


def skill_shadows(cfg: C.Config, project: Path | None = None) -> list[dict]:
    """Report foreign copies without changing them or counting links to the canonical folder."""
    home = C.home_dir(cfg.env)
    roots = [*cfg.skill_link_homes, home / ".agents/skills"]
    if project is not None:
        root = project.resolve()
        from .native import repo_root

        native_root = repo_root(root) or root
        for current in (root, *root.parents):
            roots.extend(current / host / "skills" for host in (".agents", ".claude", ".cursor"))
            if current == native_root:
                break
    paths = {path for root in roots for path in find_skills(root)}
    for catalog in (cfg.data / "native-skills").glob("*.json"):
        try:
            entries = json.loads(catalog.read_text(encoding="utf-8")).get("skills", [])
            paths.update(Path(item["path"]) for item in entries if isinstance(item, dict) and item.get("path"))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    managed = canonical_names(cfg, (p.resolve() for p in find_skills(cfg.skills_library)))
    home_roots = home_skill_roots(cfg)  # a repository's own namesake keeps its precedence, so it shadows nothing
    shadows = []
    for path in sorted(paths):
        if path.parent.name not in managed or not home_skill(path, home_roots):
            continue
        canonical = cfg.skills_library / path.parent.name / "SKILL.md"
        try:
            if canonical.is_file() and path.is_file() and path.resolve() != canonical.resolve():
                shadows.append({"name": path.parent.name, "path": str(path), "canonical": str(canonical)})
        except (OSError, RuntimeError):
            continue
    return shadows


def blocked_by_overrides(name: str, chain: list[dict]) -> bool:
    for s in chain:
        v = (s.get("skillOverrides") or {}).get(name)
        if v is not None:
            return v in ("off", "user-invocable-only")
    return False


def trigger_queries(entry) -> list[str]:
    if isinstance(entry, dict):
        return list(entry.get("queries") or [])
    return list(entry or [])


def trigger_source(path: Path, cfg: C.Config | None = None, *, real: Path | None = None) -> dict:
    real = path.resolve() if real is None else real
    host = cfg.host if cfg else "claude"
    if cfg and real.is_relative_to(cfg.data / "skills"):
        host = "shared"
    return {"host": host, "path": str(real)}


def trigger_key(source: dict) -> str:
    return "source:" + hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def trigger_entry(data: dict, name: str, source: dict, text: str) -> dict:
    """Accept only queries pinned to this source and its current generation inputs or complete file."""

    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    file_hashes = {digest(text), digest(text.removeprefix("\ufeff"))}
    key = trigger_key(source)
    entry = data.get(key)
    if entry is None:
        entry = data.get(name)
        if not isinstance(entry, dict) or "inputs" in entry or entry.get("sha") not in file_hashes:
            return {}
    elif not isinstance(entry, dict) or entry.get("source") != source or entry.get("name") != name:
        return {}
    inputs = entry.get("inputs")
    if inputs is None:
        return entry if entry.get("sha") in file_hashes else {}
    meta, body = frontmatter(text)
    description = meta.get("description") or next(
        (t for line in body.splitlines() if (t := line.strip().lstrip("#").strip())), name
    )
    if (
        not isinstance(inputs, dict)
        or inputs.get("name") != name
        or inputs.get("description") != description[: C.TRIGGER_DESCRIPTION_CHARS]
        or entry.get("sha") != digest(json.dumps(inputs, sort_keys=True))
    ):
        return {}
    return entry


class BM25:
    """Okapi BM25 over weighted skill fields. IDF assumes at least `prior_docs` documents, so a small library
    scores on the same scale as a large one instead of every shared word looking common."""

    def __init__(
        self,
        docs: list[Doc],
        usage: dict | None = None,
        k1: float = 1.2,
        b: float = 0.75,
        prior_docs: int | None = None,
    ):
        prior_docs = C.ROUTER_PRIOR_DOCS if prior_docs is None else prior_docs
        self.docs, self.usage, self.k1, self.b = docs, usage or {}, k1, b
        self.exclude: set[str] = set()
        self.terms = []
        for d in docs:
            t = []
            for field, w in WEIGHTS.items():
                t += toks(d.fields.get(field, "")) * w
            self.terms.append(t)
        n = max(len(self.terms), prior_docs)
        self.avgdl = sum(map(len, self.terms)) / max(1, len(self.terms))
        self.tf, df = [], {}
        for t in self.terms:
            tf: dict = {}
            for w in t:
                tf[w] = tf.get(w, 0) + 1
            self.tf.append(tf)
            for w in tf:
                df[w] = df.get(w, 0) + 1
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        if len(docs) >= C.ROUTER_COMPACT_MIN_DOCS:
            self._compact()

    def _compact(self) -> None:
        from array import array

        try:
            lengths = array("I", (len(t) for t in self.terms))
            postings = {}
            for i, frequencies in enumerate(self.tf):
                for word, frequency in frequencies.items():
                    postings.setdefault(word, array("I")).extend((i, frequency))
        except OverflowError:
            return
        self.lengths, self.postings = lengths, postings
        del self.terms, self.tf

    def scores(self, query: str) -> list[tuple[float, int]]:
        if hasattr(self, "postings"):
            return self._compact_scores(query)
        qt, out = set(toks(query)), []
        for i, t in enumerate(self.terms):
            if self.docs[i].name in self.exclude:
                continue
            dl, s = len(t), 0.0
            for w in qt:
                f = self.tf[i].get(w)
                if f:
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if s > 0:
                out.append((s * (1 + 0.1 * math.log1p(self.usage.get(self.docs[i].name, 0))), i))
        return sorted(out, reverse=True)

    def _compact_scores(self, query: str) -> list[tuple[float, int]]:
        totals = {}
        for word in set(toks(query)):
            postings = self.postings.get(word, ())
            for offset in range(0, len(postings), 2):
                i, f = postings[offset], postings[offset + 1]
                if self.docs[i].name in self.exclude:
                    continue
                dl = self.lengths[i]
                value = self.idf[word] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
                totals[i] = totals.get(i, 0.0) + value
        return sorted(
            ((s * (1 + 0.1 * math.log1p(self.usage.get(self.docs[i].name, 0))), i) for i, s in totals.items() if s > 0),
            reverse=True,
        )

    def route(
        self, query: str, k: int = C.ROUTER_K, min_score: float = C.ROUTER_THRESHOLD, rel: float = C.ROUTER_REL
    ) -> list[tuple[Doc, float]]:
        """Top-k docs above `min_score`; a doc without triggers scores lower, so it needs ROUTER_PLAIN_RATIO of it."""
        sc = self.scores(query)
        if not sc:
            return []
        top, out = sc[0][0], []
        for s, i in sc[:k]:
            doc = self.docs[i]
            if doc.kind == "skill" and doc.source in C.ROUTER_EXCLUDED_SOURCES:
                continue  # backfilling would promote weaker local matches previously below the top-k cutoff
            floor = min_score if doc.fields.get("triggers") else min_score * C.ROUTER_PLAIN_RATIO
            if s >= floor and s >= rel * top:
                out.append((doc, round(s, 2)))
        return out


def _generation(info: os.stat_result) -> tuple:
    return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino


def _mtime(p: Path) -> tuple | None:
    try:
        return _generation(p.stat())
    except OSError:
        return None


def load_triggers(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def skill_index(
    skill_dirs: list[Path],
    claude_home: Path,
    chain: list[dict],
    triggers_path: Path,
    cache: Path,
    usage: dict | None = None,
    *,
    sources: dict[str, str] | None = None,
    files: list[tuple[Path, str]] | None = None,
    cfg: C.Config | None = None,
    resolved: dict[Path, Path] | None = None,
    signatures: dict[Path, tuple] | None = None,
) -> BM25:
    """BM25 over every visible skill, cached until a SKILL.md, the triggers file, or a router tunable changes."""
    resolved = {} if resolved is None else resolved
    files = (
        skill_files([*skill_dirs, *([cfg.skills_library] if cfg else [])], claude_home, chain, resolved=resolved)
        if files is None
        else files
    )
    sources = sources or {}
    roots = skill_roots(
        claude_home, cfg.skills_home if cfg else claude_home / "skills", cfg.env if cfg else dict(os.environ)
    )
    sig = tuple(
        (str(p), str(real), prefix, skill_source(p, prefix, sources.get(str(p), ""), roots=roots, real=real), m)
        for p, prefix in files
        if (m := signatures[p] if signatures is not None and p in signatures else _mtime(p)) is not None
        for real in (resolved[p] if p in resolved else p.resolve(),)
    )
    tunables = tuple(sorted((k, v) for k, v in vars(C).items() if k.startswith("ROUTER_")))
    host = "cursor-masked" if cfg is not None and cfg.host == "cursor" else cfg.host if cfg else "claude"
    managed_stamp = _mtime(cfg.data / "skills_ledger.jsonl") if cfg and cfg.skills_library == cfg.skills_home else None
    key = (CACHE_VERSION, host, tunables, sig, _mtime(triggers_path), managed_stamp)
    bm = None
    try:
        cached_key, cached = pickle.loads(cache.read_bytes())
        if cached_key == key and isinstance(cached, BM25):
            bm = cached
    except Exception:  # unreadable or foreign cache: rebuild it
        bm = None
    if bm is None:
        triggers = load_triggers(triggers_path)
        docs: dict[str, Doc] = {}
        library = cfg.skills_library.resolve() if cfg else None
        home_roots = home_skill_roots(cfg) if cfg else ()
        managed = canonical_names(cfg, (Path(real) for _, real, prefix, _, _ in sig if not prefix))
        for path, real, prefix, source, _ in sig:
            identity = trigger_source(Path(path), cfg, real=Path(real))
            if d := _skill_doc(Path(path), prefix, triggers, source, source=identity, cfg=cfg):
                if (
                    not prefix
                    and d.name in managed
                    and Path(real).parent.parent != library
                    and home_skill(Path(path), home_roots)
                ):
                    continue
                previous = docs.get(d.name)
                if previous is None or (
                    d.name in managed
                    and not prefix
                    and Path(real).parent.parent != library
                    and not home_skill(Path(path), home_roots)
                    and Path(previous.path).resolve().parent.parent == library
                ):
                    docs[d.name] = d
        bm = BM25(list(docs.values()))
        atomic_write(cache, pickle.dumps((key, bm)))
    bm.usage = usage or {}
    bm.exclude = {d.name for d in bm.docs if blocked_by_overrides(d.name, chain)}
    return bm


def topic_index(store: TopicStore) -> BM25:
    docs = []
    for p in store.topics():
        t = parse_topic(p.read_text(encoding="utf-8", errors="replace"))
        name = t.get("name") or p.stem
        docs.append(
            Doc(
                name,
                t.get("description", ""),
                str(p),
                "memory",
                {
                    "name": name.replace("-", " "),
                    "description": t.get("description", ""),
                    "headings": t.get("body", "")[:400],
                },
            )
        )
    return BM25(docs)
