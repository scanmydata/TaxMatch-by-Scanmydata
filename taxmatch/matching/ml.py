"""Μηχανική μάθηση για ΓΡΗΓΟΡΟΤΕΡΗ αντιστοίχιση νέων ↔ πελατών, στο πρότυπο του paperless-ngx (ρητό αίτημα χρήστη,
2026-10-22).

Τι κρατήσαμε από το paperless-ngx (`documents/classifier.py`, matching algorithm «Auto»):
* μαθαίνει ΑΠΟ ΤΙΣ ΔΙΚΕΣ ΣΑΣ αποφάσεις — εκεί τα tags/correspondents που ανέθεσε ο χρήστης, εδώ ποια άρθρα έκρινε
  σχετικά το LLM και ποια matches επιβεβαιώθηκαν (👍) ή απορρίφθηκαν (👎)·
* ίδια προεπεξεργασία: πεζά, μόνο γράμματα/ψηφία, αφαίρεση stopwords και «stemming» (εδώ ελληνικό, χωρίς τόνους)·
* ίδια διανυσματοποίηση: λέξεις + ζεύγη λέξεων (n-grams 1–2), αγνοούνται όροι σε <`MIN_DF` άρθρα·
* επανεκπαίδευση ΜΟΝΟ όταν αλλάξουν τα δεδομένα εκπαίδευσης (hash, όπως το «classifier is up to date» του paperless).

Τι αλλάξαμε: ο ταξινομητής είναι Naive Bayes σε καθαρή Python αντί για το MLPClassifier του scikit-learn — το
scikit-learn φέρνει numpy+scipy (~100MB επιπλέον στον installer) για λίγες χιλιάδες σύντομα άρθρα, όπου το NB είναι
εξίσου καλό και εκπαιδεύεται σε κλάσματα δευτερολέπτου.

Χρήσεις (ποτέ «εφεύρεση» δεδομένων — CLAUDE.md §Κανόνες):
1. **Προτεραιότητα στο LLM**: τα άρθρα σε αναμονή αναλύονται με σειρά πιθανότητας να είναι σχετικά (`articles.ml_score`)
   — τα matches εμφανίζονται νωρίτερα.
2. **Παράκαμψη LLM** για άρθρα που ο ταξινομητής κρίνει άσχετα με πολύ μεγάλη βεβαιότητα, ΜΟΝΟ αν το μοντέλο έχει
   αρκετά δεδομένα και μετρημένη ακρίβεια ≥ `GATE_MIN_ACCURACY` σε δείγμα ελέγχου. Σημειώνονται «άσχετο (ML)» και
   ξαναπερνούν από LLM με «Επανέλεγχος».
3. **Πιθανοί πελάτες** για άρθρο που δεν έχει αναλυθεί ακόμη (προεπισκόπηση) — πάντα ως «υπό προϋποθέσεις».
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .. import config, db, settings_store
from ..textutil import strip_accents

log = logging.getLogger(__name__)

MIN_DF = 2                     # όρος σε λιγότερα άρθρα = θόρυβος (paperless: min_df)
MAX_FEATURES = 30000
MIN_TRAIN = 150                # κάτω από αυτό δεν εμπιστευόμαστε τον ταξινομητή σχετικότητας για παράκαμψη LLM
MIN_PER_CLASS = 30
GATE_MIN_ACCURACY = 0.92
GATE_THRESHOLD = 0.97          # P(άσχετο) για παράκαμψη του LLM
GATE_MIN_PRECISION = 0.98      # στο δείγμα ελέγχου: από όσα το μοντέλο θα παρέκαμπτε, πόσα ήταν πράγματι άσχετα
MIN_CLIENT_POSITIVES = 3       # πελάτης με λιγότερα επιβεβαιωμένα νέα δεν έχει δικό του μοντέλο
MODEL_VERSION = 1

_STOP = set(strip_accents(w).lower() for w in """
ο η το οι τα του της των τον την στο στη στην στον στα στις στους σε με για από και ή να θα δεν μη μην που πως ότι
οτι αν είναι ειναι έχει εχει έχουν εχουν ως κατά κατα μετά μετα προς έως εως όπως οπως αυτό αυτο αυτή αυτη αυτά αυτα
αυτές αυτους ένα ενα μια μία έναν εναν ενός ενος μιας επί επι υπό υπο τις τους πιο πολύ πολυ όλα ολα όλες ολες κάθε
καθε ακόμη ακομη ήδη ηδη νέα νεα νέο νεο νέες νεες
""".split())
_SUFFIXES = tuple(sorted((strip_accents(s) for s in (
    "ουμε", "ουνε", "ούσαν", "ησεις", "ήσεις", "ηκαν", "ήκαν", "ηση", "ήση", "σεις", "ματα", "ματος", "ματων", "ικες",
    "ικός", "ικη", "ική", "ικο", "ικό", "ικα", "ικά", "ους", "ων", "ες", "ας", "ης", "ος", "ου", "ει", "ουν", "αν", "α",
    "η", "ο", "ε", "ι", "υ", "ς")), key=len, reverse=True))


def _stem(word: str) -> str:
    """Απλό ελληνικό stemming: κόβει την κατάληξη, κρατά ≥ 4 χαρακτήρες ρίζα (ώστε «φορολογικές»/«φορολογικός» = ίδιο)."""
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)]
    return word


def tokens(text: str) -> list[str]:
    words = re.findall(r"[0-9a-zα-ω]+", strip_accents(text or "").lower())
    return [_stem(w) for w in words if w not in _STOP and (len(w) > 2 or w.isdigit())]


def features(text: str) -> set[str]:
    toks = tokens(text)
    return set(toks) | {f"{a} {b}" for a, b in zip(toks, toks[1:])}


def article_text(row: Any) -> str:
    keys = row.keys() if hasattr(row, "keys") else row
    get = (lambda k: row[k] if k in keys else "") if hasattr(row, "keys") else row.get
    return " ".join(str(get(k) or "") for k in ("title", "raw_summary", "full_text"))[:6000]


# ------------------------------------------------------------------ Bernoulli Naive Bayes (αραιό, καθαρή Python)
@dataclass
class _NB:
    """Δυαδικός Bernoulli NB: log-odds ανά όρο. `score()` = πιθανότητα θετικής κλάσης."""
    weights: dict[str, float] = field(default_factory=dict)
    bias: float = 0.0

    @classmethod
    def train(cls, docs: list[set[str]], labels: list[bool], vocab: set[str]) -> "_NB":
        pos = [d for d, y in zip(docs, labels) if y]
        neg = [d for d, y in zip(docs, labels) if not y]
        n_pos, n_neg = len(pos), len(neg)
        if not n_pos or not n_neg:
            return cls({}, 10.0 if n_pos else -10.0)
        cp, cn = Counter(f for d in pos for f in d if f in vocab), Counter(f for d in neg for f in d if f in vocab)
        weights: dict[str, float] = {}
        bias = math.log(n_pos / n_neg)
        for f in vocab:
            p1 = (cp[f] + 1) / (n_pos + 2)
            p0 = (cn[f] + 1) / (n_neg + 2)
            # παρών όρος: log(p1/p0)· απών: log((1-p1)/(1-p0)) — το απών ενσωματώνεται στο bias (αραιός υπολογισμός)
            w_present = math.log(p1 / p0)
            w_absent = math.log((1 - p1) / (1 - p0))
            bias += w_absent
            if abs(w_present - w_absent) > 0.05:
                weights[f] = w_present - w_absent
        return cls(weights, bias)

    def score(self, feats: set[str]) -> float:
        z = max(-50.0, min(50.0, self.bias + sum(self.weights.get(f, 0.0) for f in feats)))
        return 1 / (1 + math.exp(-z))


@dataclass
class Model:
    relevance: Optional[_NB] = None
    clients: dict[str, _NB] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    def p_relevant(self, text: str) -> Optional[float]:
        return self.relevance.score(features(text)) if self.relevance else None

    def suggest(self, text: str, threshold: float = 0.6, top: int = 15) -> list[tuple[str, float]]:
        feats = features(text)
        scored = sorted(((afm, nb.score(feats)) for afm, nb in self.clients.items()), key=lambda x: -x[1])
        return [(a, s) for a, s in scored[:top] if s >= threshold]

    @property
    def gate_ready(self) -> bool:
        return self.relevance is not None and gate_ok(self.stats)


def gate_ok(s: dict[str, Any]) -> bool:
    """Δικαιούται το μοντέλο να παρακάμπτει το LLM; (από τα στατιστικά της τελευταίας εκπαίδευσης)"""
    return (s.get("n_relevance", 0) >= MIN_TRAIN and min(s.get("n_pos", 0), s.get("n_neg", 0)) >= MIN_PER_CLASS
            and s.get("accuracy", 0.0) >= GATE_MIN_ACCURACY
            # Ο NB υπερεκτιμά τις πιθανότητες: δεν αρκεί η ακρίβεια στο 0,5 — μετράμε πόσο συχνά ΕΙΧΕ ΔΙΚΙΟ όταν έλεγε
            # «άσχετο ≥ 97%» στο δείγμα ελέγχου, και θέλουμε αρκετές τέτοιες περιπτώσεις.
            and s.get("gate_checked", 0) >= 10 and s.get("gate_precision", 0.0) >= GATE_MIN_PRECISION)


def _vocab(docs: Iterable[set[str]]) -> set[str]:
    df = Counter(f for d in docs for f in d)
    return {f for f, n in df.most_common(MAX_FEATURES) if n >= MIN_DF}


def _training_rows(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], dict[int, set[str]], dict[int, set[str]]]:
    arts = conn.execute("SELECT id, title, raw_summary, full_text, extraction_status, extracted_json FROM articles "
                        "WHERE extraction_status IN ('done','irrelevant') AND COALESCE(extraction_error,'') NOT LIKE 'ML:%' "
                        "ORDER BY id").fetchall()
    positives: dict[int, set[str]] = defaultdict(set)
    negatives: dict[int, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT article_id, afm, confidence, user_feedback FROM matches"):
        if r["user_feedback"] == -1:
            negatives[r["article_id"]].add(r["afm"])
        elif r["user_feedback"] == 1 or r["confidence"] >= 1:
            positives[r["article_id"]].add(r["afm"])
    return arts, positives, negatives


def _hash(arts: list[sqlite3.Row], positives: dict[int, set[str]], negatives: dict[int, set[str]]) -> str:
    h = hashlib.sha256(f"v{MODEL_VERSION}".encode())
    for a in arts:
        h.update(f"{a['id']}:{a['extraction_status']};".encode())
    for k in sorted(positives):
        h.update(f"+{k}:{','.join(sorted(positives[k]))};".encode())
    for k in sorted(negatives):
        h.update(f"-{k}:{','.join(sorted(negatives[k]))};".encode())
    return h.hexdigest()


def train(conn: sqlite3.Connection) -> Model:
    arts, positives, negatives = _training_rows(conn)
    docs = {a["id"]: features(article_text(a)) for a in arts}
    vocab = _vocab(docs.values())
    ids = [a["id"] for a in arts]
    labels = [a["extraction_status"] == "done" for a in arts]
    stats: dict[str, Any] = {"n_relevance": len(ids), "n_pos": sum(labels), "n_neg": len(labels) - sum(labels)}
    model = Model(stats=stats)
    if ids and 0 < sum(labels) < len(labels):
        # Ακρίβεια σε δείγμα ελέγχου (1 στα 5 άρθρα, σταθερά κατά id) — η «παράκαμψη LLM» ενεργοποιείται μόνο αν αξίζει.
        train_i = [i for i, x in enumerate(ids) if x % 5]
        test_i = [i for i, x in enumerate(ids) if not x % 5]
        if train_i and test_i:
            probe = _NB.train([docs[ids[i]] for i in train_i], [labels[i] for i in train_i], vocab)
            scores = {i: probe.score(docs[ids[i]]) for i in test_i}
            stats["accuracy"] = round(sum((scores[i] >= 0.5) == labels[i] for i in test_i) / len(test_i), 3)
            gated = [i for i in test_i if 1 - scores[i] >= GATE_THRESHOLD]
            stats["gate_checked"] = len(gated)
            stats["gate_precision"] = round(sum(not labels[i] for i in gated) / len(gated), 3) if gated else 0.0
        model.relevance = _NB.train([docs[i] for i in ids], labels, vocab)
    # Πελάτες: θετικά = επιβεβαιωμένα/βέβαια matches, αρνητικά = όλα τα υπόλοιπα σχετικά άρθρα (και τα 👎)
    relevant_ids = [a["id"] for a in arts if a["extraction_status"] == "done"]
    counts = Counter(afm for i in relevant_ids for afm in positives.get(i, ()))
    for afm, n in counts.items():
        if n < MIN_CLIENT_POSITIVES:
            continue
        ys = [afm in positives.get(i, ()) and afm not in negatives.get(i, ()) for i in relevant_ids]
        model.clients[afm] = _NB.train([docs[i] for i in relevant_ids], ys, vocab)
    stats["n_clients"] = len(model.clients)
    stats["vocab"] = len(vocab)
    return model


def _path() -> Path:
    return config.data_dir() / "ml" / "model.json"


def _dump(model: Model, data_hash: str) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": MODEL_VERSION, "hash": data_hash, "stats": model.stats,
               "relevance": None if model.relevance is None else [model.relevance.bias, model.relevance.weights],
               "clients": {a: [nb.bias, nb.weights] for a, nb in model.clients.items()}}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def load() -> tuple[Optional[Model], str]:
    try:
        payload = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ""
    if payload.get("version") != MODEL_VERSION:
        return None, ""
    rel = payload.get("relevance")
    model = Model(relevance=_NB(rel[1], rel[0]) if rel else None,
                  clients={a: _NB(w, b) for a, (b, w) in payload.get("clients", {}).items()},
                  stats=payload.get("stats", {}))
    return model, payload.get("hash", "")


def train_if_needed(conn: sqlite3.Connection) -> Model:
    """Όπως το paperless: ξαναεκπαιδεύει μόνο αν άλλαξαν τα δεδομένα (νέες αποφάσεις LLM, 👍/👎)."""
    arts, positives, negatives = _training_rows(conn)
    data_hash = _hash(arts, positives, negatives)
    model, old_hash = load()
    if model is not None and old_hash == data_hash:
        return model
    model = train(conn)
    _dump(model, data_hash)
    s = model.stats
    settings_store.set_value(conn, "ml_status", json.dumps({**s, "trained_at": db.utcnow()}, ensure_ascii=False))
    log.info("ML: εκπαίδευση σε %s άρθρα (%s σχετικά), %s πελάτες, ακρίβεια %s",
             s.get("n_relevance"), s.get("n_pos"), s.get("n_clients"), s.get("accuracy"))
    return model


def prioritize_and_gate(conn: sqlite3.Connection, model: Model) -> dict[str, int]:
    """Βαθμολογεί τα άρθρα σε αναμονή (σειρά ανάλυσης LLM) και — αν το μοντέλο το δικαιούται — σημειώνει «άσχετα (ML)»
    όσα είναι σχεδόν σίγουρα άσχετα, ώστε να μη χρειαστεί κλήση LLM."""
    if model.relevance is None:
        return {"scored": 0, "gated": 0}
    scored = gated = 0
    pending = conn.execute("SELECT id, title, raw_summary, full_text FROM articles "
                           "WHERE extraction_status IN ('pending','failed')").fetchall()
    conn.execute("BEGIN")
    try:
        for a in pending:
            p = model.relevance.score(features(article_text(a)))
            conn.execute("UPDATE articles SET ml_score=? WHERE id=?", (round(p, 4), a["id"]))
            scored += 1
            if model.gate_ready and (1 - p) >= GATE_THRESHOLD:
                conn.execute("UPDATE articles SET extraction_status='irrelevant', extraction_error=?, extracted_json=? "
                             "WHERE id=?", (f"ML: άσχετο με βεβαιότητα {1 - p:.0%} (χωρίς κλήση LLM)",
                                            json.dumps({"relevant": False, "summary": "", "ml": True}), a["id"]))
                gated += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"scored": scored, "gated": gated}


def suggest_for_article(conn: sqlite3.Connection, article: Any) -> list[dict[str, Any]]:
    """Πιθανοί πελάτες για άρθρο που ΔΕΝ έχει αναλυθεί ακόμη (για την προεπισκόπηση). Κενό αν δεν υπάρχει μοντέλο."""
    model, _h = load()
    if model is None or not model.clients:
        return []
    names = {r["afm"]: r["name"] for r in conn.execute("SELECT afm, name FROM businesses")}
    return [{"afm": a, "name": names.get(a, a), "status": "maybe", "reason": f"πρόταση ML ({s:.0%})"}
            for a, s in model.suggest(article_text(article)) if a in names]
