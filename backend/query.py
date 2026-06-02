"""
Toronto Zoning By-law 569-2013 — Query / Retrieval Pipeline
=============================================================================

ARCHITECTURE: Hybrid semantic + sparse retrieval, no LLM in hot path

  QUERY BUILD:  expand_query() adds domain synonyms; _enrich_for_embed() adds
                by-law-vocabulary phrases so the dense vector lands closer to
                real section text — without any LLM call (replaces HyDE).

  RETRIEVAL:    [sparse + dense] embed in parallel, then 4 Qdrant searches in
                parallel (hybrid RRF fusion).  Full mode fetches wider TOP_K so
                the Voyage reranker has more candidates.

  RANKING:      keyword boost → Voyage rerank-2.5 (full mode) → final sort.
                Exception chunks are always pinned above zone-rule chunks.

  SYNTHESIS:    OpenAI streaming (SSE) via AsyncOpenAI; retrieval runs in a
                thread executor before streaming starts.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _ConcurrentTimeout
from pathlib import Path
from typing import Optional

import yaml

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny, PayloadSchemaType

load_dotenv(Path(__file__).parent / ".env")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

EMBEDDING_MODEL   = "voyage-2"
CHAT_MODEL        = os.getenv("CHAT_MODEL", "gpt-4.1")

COLLECTION_RULES  = "toronto_zoning_rules"
COLLECTION_EXC    = "toronto_zoning_exceptions"
BACKREF_PATH      = Path(__file__).parent / "backref_index.json"

_RERANK_TIMEOUT_SECS     = 15.0  # was 5.0 — reranker was timing out before finishing; 8 chunks × ~400ms ≈ 3s well within 15s
_EMBED_TIMEOUT_SECS      = 3.5   # if Voyage dense embed takes longer, use sparse-only retrieval

SCORE_THRESHOLD_ZONE     = 0.35
SCORE_THRESHOLD_GENERAL  = 0.30
SCORE_THRESHOLD_EXC      = 0.35
SCORE_THRESHOLD_FALLBACK = 0.20

# Quick mode: smaller window, no reranker — speed over breadth
# Full mode: wider window feeds the Voyage reranker for better top-10 quality
TOP_K_ZONE       = 6   # quick
TOP_K_ZONE_FULL  = 9   # full  — reranker selects best 10 from this pool
TOP_K_GENERAL    = 3   # quick
TOP_K_GENERAL_FULL = 5 # full
TOP_K_EXC        = 2   # quick
TOP_K_EXC_FULL   = 3   # full

MAX_QUESTION_CHARS   = 10_000  # raised from 2000 — architects ask detailed multi-clause questions
MAX_CHUNK_TEXT_CHARS = 99999   # never truncate legal text sent to the LLM

BYLAW_BASE = "https://www.toronto.ca/zoning/bylaw_amendments/ZBL_NewProvision_Chapter"

logger = logging.getLogger("zoning.query")

# ─────────────────────────────────────────────────────────────────────────────
# PERF HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _ms(start: float) -> int:
    """Return elapsed milliseconds since `start = time.perf_counter()`."""
    return int((time.perf_counter() - start) * 1000)

def _fmt_tok(usage: dict | None) -> str:
    if not usage:
        return "—"
    return f"in={usage.get('prompt',0):,}  out={usage.get('completion',0):,}  total={usage.get('total',0):,}"

def _print_perf(rows: list[tuple[str, str, str]], title: str = "") -> None:
    """
    Print a tidy three-column perf table to stdout.
    rows = list of (step_label, latency_str, extra_str)
    """
    W = 72
    print()
    print("┌" + "─" * W + "┐")
    if title:
        pad = W - 2 - len(title)
        print(f"│  {title}" + " " * pad + "│")
        print("├" + "─" * 38 + "┬" + "─" * 14 + "┬" + "─" * (W - 54) + "┤")
    header = f"│  {'Step':<35}│  {'Latency':>9}  │  {'Info':<{W-56}}│"
    print(header)
    print("├" + "─" * 38 + "┼" + "─" * 14 + "┼" + "─" * (W - 54) + "┤")
    for label, lat, extra in rows:
        info_w = W - 56
        extra_t = extra[:info_w].ljust(info_w)
        print(f"│  {label:<35}│  {lat:>9}  │  {extra_t}│")
    print("└" + "─" * 38 + "┴" + "─" * 14 + "┴" + "─" * (W - 54) + "┘")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETONS
# ─────────────────────────────────────────────────────────────────────────────

_openai:          Optional[OpenAI]       = None
_openai_async:    Optional[AsyncOpenAI]  = None   # for SSE streaming synthesis
_qdrant:          Optional[QdrantClient] = None
_backref:         dict[str, list[str]]   = {}
_VOYAGE_CLIENT  = None   # initialized in init_vertex()
_SPARSE_EMBEDDER = None  # initialized in init_vertex()

# ── Dense embedding LRU cache ────────────────────────────────────────────────
# Saves a full Voyage API round-trip on repeated or similar queries. Thread-safe.
_EMBED_CACHE: "OrderedDict[str, list[float]]" = OrderedDict()
_EMBED_CACHE_MAX  = 256
_EMBED_CACHE_LOCK = threading.Lock()

# ── SPLADE serialization lock ─────────────────────────────────────────────────
# FastEmbed SPLADE (ONNX runtime) is NOT thread-safe. When _embed_sparse() and
# _embed() run in parallel threads, ONNX blocks internally, effectively
# serializing both calls and making total embed time = SPLADE + Voyage (~6s).
# Serializing SPLADE with this lock lets Voyage run truly in parallel → ~200ms.
_SPLADE_LOCK = threading.Lock()


def _ensure_qdrant_indexes() -> None:
    index_specs = {
        COLLECTION_RULES: {
            "zone_symbol":    PayloadSchemaType.KEYWORD,
            "chapter_id":     PayloadSchemaType.KEYWORD,
            "content_type":   PayloadSchemaType.KEYWORD,
            "is_general_reg": PayloadSchemaType.BOOL,
            "section_id":     PayloadSchemaType.KEYWORD,
        },
        COLLECTION_EXC: {
            "exception_number":     PayloadSchemaType.INTEGER,
            "exception_numbers":    PayloadSchemaType.INTEGER,  # list field
            "exception_number_min": PayloadSchemaType.INTEGER,
            "exception_number_max": PayloadSchemaType.INTEGER,
            "exception_zone":       PayloadSchemaType.KEYWORD,
            "section_id":           PayloadSchemaType.KEYWORD,
        },
    }
    for col, fields in index_specs.items():
        for fname, fschema in fields.items():
            try:
                _qdrant.create_payload_index(col, fname, fschema)
            except Exception:
                pass


def init_vertex() -> None:
    global _openai, _openai_async, _qdrant, _VOYAGE_CLIENT, _SPARSE_EMBEDDER
    api_key = os.environ["OPENAI_API_KEY"]
    # 120s is ample for synthesis; avoids inheriting the ambiguous library default
    _openai       = OpenAI(api_key=api_key, timeout=120.0)
    _openai_async = AsyncOpenAI(api_key=api_key, timeout=120.0)
    if os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true":
        try:
            from langsmith.wrappers import wrap_openai
            _openai       = wrap_openai(_openai)
            _openai_async = wrap_openai(_openai_async)
            logger.info("LangSmith tracing enabled — OpenAI clients wrapped")
        except Exception as _ls_err:
            logger.warning("LangSmith wrap failed (tracing disabled): %s", _ls_err)
    logger.info("OpenAI ready  chat=%s  async=yes  timeout=120s", CHAT_MODEL)
    _qdrant = QdrantClient(
        url    =os.getenv("QDRANT_URL", "http://localhost:6333"),
        api_key=os.getenv("QDRANT_API_KEY") or None,
    )
    logger.info("Qdrant ready")
    _ensure_qdrant_indexes()
    import voyageai
    from fastembed import SparseTextEmbedding
    _VOYAGE_CLIENT   = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
    _SPARSE_EMBEDDER = SparseTextEmbedding(model_name="prithivida/Splade_PP_en_v1")
    logger.info("Voyage AI ready  model=%s", EMBEDDING_MODEL)


AMENDMENTS_YAML_PATH = Path(__file__).parent / "amendments.yaml"


def load_backref() -> None:
    global _backref
    if Path(BACKREF_PATH).exists():
        with open(BACKREF_PATH) as f:
            _backref = json.load(f)
        if not _backref:
            logger.warning(
                "backref_index.json loaded but is empty — cross-ref backref expansion "
                "will be disabled. Re-run ingest.py main() to populate it."
            )
        else:
            logger.info("Backref loaded (%s entries)", len(_backref))
    else:
        logger.warning(
            "backref_index.json not found at %s — cross-ref backref expansion disabled. "
            "Run ingest.py main() to generate it.", BACKREF_PATH
        )


@functools.lru_cache(maxsize=1)
def load_amendment_details() -> dict:
    """Load amendments.yaml and return {id: {summary, details, in_force, affects, consolidated}}.

    Cached for the process lifetime — amendments.yaml changes only on deploy.
    """
    if not AMENDMENTS_YAML_PATH.exists():
        logger.warning("amendments.yaml not found at %s — amendment injection disabled", AMENDMENTS_YAML_PATH)
        return {}
    try:
        with open(AMENDMENTS_YAML_PATH) as f:
            data = yaml.safe_load(f)
        result: dict[str, dict] = {}
        for a in data.get("amendments", []):
            aid = a.get("id")
            if aid:
                result[aid] = {
                    "summary":      a.get("summary", ""),
                    "details":      (a.get("details") or "").strip(),
                    "in_force":     a.get("in_force"),
                    "affects":      a.get("affects", []),
                    "consolidated": bool(a.get("consolidated", False)),
                }
        logger.info("Amendment details loaded (%d entries)", len(result))
        return result
    except Exception as exc:
        logger.warning("Failed to load amendments.yaml: %s", exc)
        return {}


def get_system_status() -> dict:
    collections = {}
    qdrant_ok = _qdrant is not None
    if qdrant_ok:
        for coll in (COLLECTION_RULES, COLLECTION_EXC):
            try:
                info = _qdrant.get_collection(coll)
                collections[coll] = int(getattr(info, "points_count", 0) or 0)
            except Exception as exc:
                collections[coll] = f"error: {exc}"
    return {
        "vertex_ready":    _openai is not None,   # key kept for backward compat with health check
        "llm_ready":       _openai is not None,
        "qdrant_ready":    qdrant_ok,
        "backref_entries": len(_backref),
        "collections":     collections,
        "chat_model":      CHAT_MODEL,
    }


def _assert_ready() -> None:
    if _openai is None or _qdrant is None:
        raise RuntimeError("Call init_vertex() first.")


# ─────────────────────────────────────────────────────────────────────────────
# INPUT / OUTPUT SANITIZATION
# ─────────────────────────────────────────────────────────────────────────────

_INJECTION_PATTERNS = re.compile(
    r'(ignore (all |previous |above )?(instructions|rules|context)|'
    r'you are now|act as|pretend (you are|to be)|'
    r'disregard (your|all)|forget (everything|your instructions)|'
    r'new (instruction|directive|system prompt)|'
    r'</?(system|user|assistant|human|ai)>)',
    re.IGNORECASE,
)
_HTML_TAG   = re.compile(r'<[^>]{1,100}>')
_CTRL_CHARS = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


def sanitize_question(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("Question must be a string.")
    text = re.sub(r'\s+', ' ', raw.strip())
    text = _HTML_TAG.sub('', text)
    text = _CTRL_CHARS.sub('', text).strip()
    if not text:
        raise ValueError("Question cannot be empty.")
    if len(text) < 4:
        raise ValueError("Question is too short.")
    text = text[:MAX_QUESTION_CHARS]
    if _INJECTION_PATTERNS.search(text):
        logger.warning("[SANITIZE] Injection: %.80s", text)
        raise ValueError(
            "Your question contains patterns that cannot be processed. "
            "Please ask a plain question about this Toronto property."
        )
    return text


def sanitize_output(raw: str) -> str:
    """Remove injected HTML and collapse blank lines. No length cap."""
    if not raw:
        return ""
    text = _HTML_TAG.sub('', raw)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if len(text) < 80:
        logger.warning("[OUTPUT] Short response (%s chars)", len(text))
    return text


# ─────────────────────────────────────────────────────────────────────────────
# QUERY EXPANSION
# ─────────────────────────────────────────────────────────────────────────────

QUERY_EXPANSION_MAP: dict[str, list[str]] = {
    "coverage": [
        "lot coverage", "maximum lot coverage", "percentage of lot area",
        "percent of lot", "building coverage", "site coverage",
        "coverage requirement", "lot coverage overlay",
    ],
    "height": [
        "maximum height", "height of a building", "storeys", "storey",
        "building height", "height limit", "height overlay",
        "canadian geodetic datum", "angular plane",
    ],
    "tall":       ["height", "maximum height", "storeys", "building height"],
    "floor":      ["storeys", "height", "number of storeys"],
    "storey":     ["storeys", "height of a building", "maximum height"],
    "setback": [
        "front yard", "rear yard", "side yard", "interior side yard",
        "exterior side yard", "yard depth", "minimum yard",
        "distance from lot line", "separation distance",
    ],
    "yard": [
        "front yard", "rear yard", "side yard", "setback",
        "yard depth", "minimum yard requirement",
    ],
    "front yard":  ["minimum front yard", "setback from front lot line"],
    "rear yard":   ["minimum rear yard", "setback from rear lot line"],
    "side yard":   ["minimum side yard", "interior side yard", "exterior side yard"],
    "frontage":    ["lot frontage", "minimum lot frontage", "front lot line width"],
    "lot area":    ["minimum lot area", "lot size", "lot dimensions"],
    "lot size":    ["lot area", "lot frontage", "minimum lot"],
    "fsi": [
        "floor space index", "gross floor area", "gfa",
        "total gross floor area", "maximum gross floor area", "density",
    ],
    "density":     ["floor space index", "fsi", "gross floor area", "total floor area"],
    "gross floor": ["gross floor area", "floor space index", "gfa"],
    "parking": [
        "parking spaces", "parking requirement", "minimum parking",
        "visitor parking", "resident parking", "underground parking",
        "accessible parking", "drive aisle",
    ],
    "garage":      ["parking", "underground parking", "parking spaces"],
    "bicycle": [
        "bicycle parking", "long-term bicycle", "short-term bicycle",
        "bicycle space", "bicycle storage",
    ],
    "bike":        ["bicycle parking", "bicycle space", "bicycle storage"],
    "garden suite": [
        "garden suites", "ancillary building", "secondary suite",
        "additional residential unit", "section 150.15",
    ],
    "laneway":         ["laneway house", "laneway suite", "coach house", "ancillary building"],
    "secondary suite": ["garden suite", "additional residential unit", "ancillary"],
    "ancillary":       ["ancillary building", "ancillary structure", "garden suite"],
    "permitted":   ["permitted uses", "uses are permitted", "use of the lot", "use of land"],
    "allowed":     ["permitted uses", "what is permitted", "uses are permitted"],
    "build":       ["permitted uses", "uses are permitted", "what can i build"],
    "loading":     ["loading space", "loading spaces", "loading requirement"],
    "delivery":    ["loading space", "loading spaces", "truck"],
    "landscaping": ["landscape area", "soft landscaping", "minimum landscaping", "green space"],
    "amenity": [
        "amenity space", "indoor amenity space", "outdoor amenity space",
        "amenity area", "amenity requirement",
    ],
    "despite":     ["despite regulation", "despite section", "notwithstanding"],
    "override":    ["despite", "despite regulation", "notwithstanding", "takes precedence"],
    "exception": [
        "site-specific exception", "chapter 900", "prevailing by-law",
        "site specific provisions",
    ],
    "dwelling": [
        "dwelling unit", "dwelling units", "number of dwelling units",
        "maximum units", "apartment",
    ],
    "units":       ["dwelling units", "number of units", "maximum units"],
    "apartment":   ["dwelling units", "apartment building", "residential use"],
}


def expand_query(question: str, zone_symbol: str, bylaw_chapter: str) -> str:
    q_lower   = question.lower()
    synonyms: list[str] = []
    seen: set[str] = set()
    for signal, expansions in QUERY_EXPANSION_MAP.items():
        if signal in q_lower:
            for exp in expansions:
                if exp not in seen and exp.lower() not in q_lower:
                    synonyms.append(exp)
                    seen.add(exp)

    parts = [question]
    if synonyms:
        parts.append(" ".join(synonyms[:12]))

    ctx = ["Toronto Zoning By-law 569-2013"]
    if zone_symbol:
        ctx.append(f"{zone_symbol} zone")
    if bylaw_chapter:
        ctx.append(f"Chapter {bylaw_chapter}")
    parts.append(" ".join(ctx))

    expanded = " | ".join(parts)
    print(f"\n[QUERY_EXPAND] Original: {repr(question)}")
    print(f"[QUERY_EXPAND] Synonyms ({len(synonyms)}): {synonyms[:8]}")
    print(f"[QUERY_EXPAND] Expanded ({len(expanded)} chars): {expanded[:250]}")
    return expanded


# ─────────────────────────────────────────────────────────────────────────────
# QUERY ENRICHMENT  (replaces HyDE — zero LLM calls, zero latency)
# ─────────────────────────────────────────────────────────────────────────────
#
# HyDE worked by making the embed text "look like" a by-law section so that
# cosine-similarity to real sections was higher.  We achieve the same effect
# deterministically: map detected question intent → formal by-law vocabulary
# phrases that actually appear in the indexed document chunks.
#
# The phrases below are taken directly from indexed section titles/text so
# they are guaranteed to overlap with the stored vectors.

_INTENT_BYLAW_VOCAB: dict[str, str] = {
    "height":       "maximum building height storeys above established grade angular plane",
    "tall":         "maximum building height storeys floors above grade",
    "storey":       "maximum building height number of storeys above established grade",
    "floor":        "storeys number of storeys maximum height above grade",
    "high":         "maximum building height metres storeys",
    "coverage":     "maximum lot coverage percentage of lot area buildings structures",
    "cover":        "maximum lot coverage building footprint percentage lot area",
    "setback":      "minimum front yard rear yard interior side yard exterior side yard depth",
    "yard":         "minimum yard depth setback distance from lot line",
    "front yard":   "minimum front yard depth setback from front lot line",
    "rear yard":    "minimum rear yard depth setback from rear lot line",
    "side yard":    "minimum interior side yard exterior side yard depth",
    "fsi":          "floor space index maximum gross floor area total density",
    "density":      "floor space index gross floor area maximum density",
    "gross floor":  "gross floor area floor space index maximum",
    "parking":      "minimum required parking spaces visitor resident parking zone",
    "garage":       "parking spaces underground parking required",
    "bicycle":      "bicycle parking spaces long-term short-term section 220",
    "bike":         "bicycle parking spaces long-term short-term section 220",
    "permitted":    "permitted uses use of land lot dwelling residential commercial",
    "allowed":      "permitted uses what is permitted use of the lot",
    "use":          "permitted uses use of land residential dwelling commercial",
    "build":        "permitted uses what can be built constructed residential",
    "garden suite": "garden suite ancillary building additional residential unit section 150.15",
    "laneway":      "laneway suite ancillary building coach house section 150",
    "secondary":    "secondary suite additional residential unit garden suite ancillary",
    "loading":      "loading space required delivery truck access section 230",
    "delivery":     "loading space required delivery truck access",
    "amenity":      "amenity space indoor outdoor required minimum section 150",
    "landscap":     "minimum landscaping soft landscaping area",
    "exception":    "site-specific exception chapter 900 prevailing by-law site specific provisions",
    "override":     "despite regulation section notwithstanding site-specific exception",
    "despite":      "despite regulation notwithstanding site-specific provisions",
    "units":        "maximum dwelling units number of units residential",
    "apartment":    "dwelling units apartment building residential use maximum units",
    "frontage":     "minimum lot frontage front lot line width",
    "lot area":     "minimum lot area size dimensions",
    "lot size":     "minimum lot area frontage dimensions",
    "rooming":      "rooming house regulations dwelling units area section 150.25",
    "angular":      "angular plane height restriction degrees slope above grade",
    "accessory":    "accessory building structure ancillary residential uses",
    "basement":     "below established grade dwelling unit semi-basement",
    "detach":       "detached dwelling single-family residential zone RD RD zone",
    "semi-detach":  "semi-detached dwelling RS zone two-unit residential",
    "townhouse":    "townhouse row house attached dwelling RT zone multiple",
    "addition":     "building addition extension alteration existing structure setback",
    "fence":        "fence height maximum lot line boundary enclosure",
    "pool":         "swimming pool setback lot line accessory structure minimum",
    "driveway":     "driveway width vehicle access parking aisle minimum",
    "retail":       "retail store commercial use priority retail frontage ground floor",
    "commercial":   "commercial use permitted ground floor CR zone retail office",
    "repair":       "repair alteration existing building non-conforming use",
    "convert":      "conversion dwelling units existing building residential use",
}


def _enrich_for_embed(expanded: str, question: str, zone_symbol: str, bylaw_chapter: str) -> str:
    """
    Append by-law vocabulary phrases to the expanded query so the dense vector
    lands closer to real section embeddings — no LLM needed.
    """
    q = question.lower()
    vocab_parts: list[str] = []
    seen: set[str] = set()
    for signal, vocab in _INTENT_BYLAW_VOCAB.items():
        if signal in q:
            key = vocab[:40]
            if key not in seen:
                vocab_parts.append(vocab)
                seen.add(key)

    # Structural zone+chapter phrase (matches section headers in indexed text)
    if zone_symbol and bylaw_chapter:
        vocab_parts.append(f"In the {zone_symbol} zone Chapter {bylaw_chapter} provisions regulations")
    elif zone_symbol:
        vocab_parts.append(f"In the {zone_symbol} zone regulations provisions")

    if vocab_parts:
        return f"{expanded} | {' '.join(vocab_parts)}"
    return expanded


_MAX_EMBED_CHARS = 120_000  # voyage-2 supports ~16k tokens

def _embed(text: str) -> list[float]:
    """Dense embedding via Voyage AI voyage-2, with thread-safe LRU cache."""
    _assert_ready()
    if len(text) > _MAX_EMBED_CHARS:
        logger.warning("[EMBED] Truncating %d → %d chars", len(text), _MAX_EMBED_CHARS)
        text = text[:_MAX_EMBED_CHARS]

    # Lock only for dict read — compute outside the lock to avoid blocking other threads
    with _EMBED_CACHE_LOCK:
        if text in _EMBED_CACHE:
            _EMBED_CACHE.move_to_end(text)
            print(f"[EMBED] Cache HIT  ({len(text)} chars)  cache_size={len(_EMBED_CACHE)}")
            return _EMBED_CACHE[text]

    # Expensive network call — no lock held here
    result = _VOYAGE_CLIENT.embed([text], model=EMBEDDING_MODEL, input_type="query")
    vec = result.embeddings[0]
    print(f"[EMBED] {len(text)} chars → {len(vec)} dims  (cache miss, size={len(_EMBED_CACHE)})")

    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE[text] = vec
        _EMBED_CACHE.move_to_end(text)
        while len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
            _EMBED_CACHE.popitem(last=False)

    return vec


def _embed_sparse(text: str) -> dict:
    """Sparse SPLADE embedding via FastEmbed — serialized to avoid ONNX thread contention."""
    with _SPLADE_LOCK:
        result = list(_SPARSE_EMBEDDER.embed([text[:10_000]]))[0]
    return {
        "indices": result.indices.tolist(),
        "values":  result.values.tolist(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# KEYWORD BOOST
# ─────────────────────────────────────────────────────────────────────────────

def _keyword_boost(chunks: list[dict], question: str) -> list[dict]:
    q_words = {w for w in re.split(r'\W+', question.lower()) if len(w) >= 4}
    for chunk in chunks:
        combined = (
            (chunk.get("section_title") or "").lower() + " " +
            (chunk.get("text") or "").lower()[:500]
        )
        boost = min(sum(0.10 for w in q_words if w in combined), 0.25)
        chunk["_boosted_score"] = chunk.get("score", 0.0) + boost
    return chunks


def _rerank(chunks: list[dict], question: str, top_n: int = 6) -> list[dict]:
    """
    Rerank using Voyage AI rerank-2.5 with a hard timeout.
    - Caps candidates at 12 (each adds ~200-300ms; 21 chunks = 6s).
    - Falls back to keyword-boosted order if Voyage is slow or fails.
    - Always pins exception_direct chunks above reranked results.
    """
    if not chunks or _VOYAGE_CLIENT is None:
        return chunks

    direct_exc = [c for c in chunks if c.get("source") == "exception_direct"]
    to_rerank  = [c for c in chunks if c.get("source") != "exception_direct"]

    if not to_rerank:
        return direct_exc[:top_n]

    # Hard cap: 8 chunks × ~400ms = ~3.2s — well within the 15s timeout
    to_rerank = to_rerank[:8]
    docs = [(c.get("text") or "")[:2000] for c in to_rerank]

    def _call_rerank():
        return _VOYAGE_CLIENT.rerank(
            query=question,
            documents=docs,
            model="rerank-2.5",
            top_k=min(top_n, len(to_rerank)),
        )

    # Don't use `with ThreadPoolExecutor` here — its __exit__ calls shutdown(wait=True)
    # which blocks until the background thread completes, adding ~1-2s after a timeout.
    _ex = ThreadPoolExecutor(max_workers=1)
    future = _ex.submit(_call_rerank)
    try:
        result = future.result(timeout=_RERANK_TIMEOUT_SECS)
    except _ConcurrentTimeout:
        print(f"[RERANK] Timed out ({_RERANK_TIMEOUT_SECS}s) — using keyword-boosted order")
        _ex.shutdown(wait=False)   # return immediately; let background thread finish on its own
        return (direct_exc + to_rerank)[:top_n]
    except Exception as exc:
        logger.warning("[RERANK] Failed (%s) — using original order", exc)
        _ex.shutdown(wait=False)
        return chunks

    _ex.shutdown(wait=False)   # future already done, no blocking

    reranked = []
    for item in result.results:
        chunk = to_rerank[item.index]
        chunk["_rerank_score"] = item.relevance_score
        reranked.append(chunk)
    print(f"[RERANK] {len(to_rerank)} → {len(reranked)} chunks  top_score={reranked[0]['_rerank_score']:.3f}")
    return direct_exc + reranked


# ─────────────────────────────────────────────────────────────────────────────
# QDRANT SEARCHES
# ─────────────────────────────────────────────────────────────────────────────

def _search_zone_rules(
    query_text: str,
    query_vec: list[float],
    sparse_vec: dict,           # Change 2: pre-computed once in retrieve()
    zone_symbol: str,
    chapter_id: str,
    top_k: int = TOP_K_ZONE,
) -> list[dict]:
    from qdrant_client.models import Prefetch, FusionQuery, Fusion, SparseVector

    must = []
    if zone_symbol:
        must.append(FieldCondition(key="zone_symbol", match=MatchValue(value=zone_symbol)))
    if chapter_id:
        must.append(FieldCondition(key="chapter_id", match=MatchValue(value=chapter_id)))

    # Sparse-only fallback when Voyage dense embed timed out
    if query_vec is None:
        print(f"\n[QDRANT:ZONE_RULES] sparse-only  zone={zone_symbol!r}  top_k={top_k}")
        try:
            hits = _qdrant.query_points(
                collection_name=COLLECTION_RULES,
                query=SparseVector(indices=sparse_vec["indices"], values=sparse_vec["values"]),
                using="sparse",
                query_filter=Filter(must=must) if must else None,
                limit=top_k,
                with_payload=True,
            ).points
            return [{"score": h.score, "source": "zone_rule", **h.payload} for h in hits]
        except Exception as exc:
            logger.warning("[ZONE_RULES] Sparse-only failed: %s", exc)
            return []

    print(f"\n[QDRANT:ZONE_RULES] zone={zone_symbol!r} chapter={chapter_id!r} top_k={top_k}")
    try:
        hits = _qdrant.query_points(
            collection_name=COLLECTION_RULES,
            prefetch=[
                Prefetch(query=query_vec, using="dense", limit=top_k * 3),
                Prefetch(
                    query=SparseVector(
                        indices=sparse_vec["indices"],
                        values=sparse_vec["values"],
                    ),
                    using="sparse",
                    limit=top_k * 3,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=Filter(must=must) if must else None,
            limit=top_k,
            with_payload=True,
        ).points
        results = [{"score": h.score, "source": "zone_rule", **h.payload} for h in hits]
    except Exception as exc:
        logger.warning("[ZONE_RULES] Hybrid failed (%s), falling back to dense-only", exc)
        hits = _qdrant.query_points(
            collection_name=COLLECTION_RULES,
            query=query_vec,
            using="dense",
            query_filter=Filter(must=must) if must else None,
            limit=top_k,
            with_payload=True,
            score_threshold=SCORE_THRESHOLD_ZONE,
        ).points
        results = [{"score": h.score, "source": "zone_rule", **h.payload} for h in hits]

    print(f"[QDRANT:ZONE_RULES] Pass-1: {len(results)} results")
    for r in results:
        print(f"   {r['score']:.3f}  {r.get('section_id','?')}  type={r.get('content_type','?')}")

    if not results and chapter_id:
        must2 = [m for m in must if getattr(m, "key", None) != "chapter_id"]
        hits2 = _qdrant.query_points(
            collection_name=COLLECTION_RULES,
            query=query_vec,
            using="dense",
            query_filter=Filter(must=must2) if must2 else None,
            limit=top_k,
            with_payload=True,
            score_threshold=SCORE_THRESHOLD_ZONE,
        ).points
        results = [{"score": h.score, "source": "zone_rule", **h.payload} for h in hits2]
        print(f"[QDRANT:ZONE_RULES] Pass-2 (zone only): {len(results)} results")

    if not results:
        must3 = [m for m in must if getattr(m, "key", None) != "chapter_id"]
        hits3 = _qdrant.query_points(
            collection_name=COLLECTION_RULES,
            query=query_vec,
            using="dense",
            query_filter=Filter(must=must3) if must3 else None,
            limit=top_k,
            with_payload=True,
            score_threshold=SCORE_THRESHOLD_FALLBACK,
        ).points
        results = [{"score": h.score, "source": "zone_rule_fallback", **h.payload} for h in hits3]
        print(f"[QDRANT:ZONE_RULES] Pass-3 fallback: {len(results)} results")

    return results


def _search_general_regs(
    query_text: str,
    query_vec: list[float],
    sparse_vec: dict,           # Change 2: pre-computed once in retrieve()
    top_k: int = TOP_K_GENERAL,
) -> list[dict]:
    from qdrant_client.models import Prefetch, FusionQuery, Fusion, SparseVector

    # Sparse-only fallback when Voyage dense embed timed out
    if query_vec is None:
        print(f"\n[QDRANT:GENERAL_REGS] sparse-only  top_k={top_k}")
        try:
            hits = _qdrant.query_points(
                collection_name=COLLECTION_RULES,
                query=SparseVector(indices=sparse_vec["indices"], values=sparse_vec["values"]),
                using="sparse",
                query_filter=Filter(must=[
                    FieldCondition(key="is_general_reg", match=MatchValue(value=True)),
                ]),
                limit=top_k,
                with_payload=True,
            ).points
            return [{"score": h.score, "source": "general_reg", **h.payload} for h in hits]
        except Exception as exc:
            logger.warning("[GENERAL_REGS] Sparse-only failed: %s", exc)
            return []

    print(f"\n[QDRANT:GENERAL_REGS] top_k={top_k}")
    try:
        hits = _qdrant.query_points(
            collection_name=COLLECTION_RULES,
            prefetch=[
                Prefetch(query=query_vec, using="dense", limit=top_k * 3),
                Prefetch(
                    query=SparseVector(
                        indices=sparse_vec["indices"],
                        values=sparse_vec["values"],
                    ),
                    using="sparse",
                    limit=top_k * 3,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=Filter(must=[
                FieldCondition(key="is_general_reg", match=MatchValue(value=True)),
            ]),
            limit=top_k,
            with_payload=True,
        ).points
        results = [{"score": h.score, "source": "general_reg", **h.payload} for h in hits]
    except Exception as exc:
        logger.warning("[GENERAL_REGS] Hybrid failed (%s), falling back to dense-only", exc)
        try:
            hits = _qdrant.query_points(
                collection_name=COLLECTION_RULES,
                query=query_vec,
                using="dense",
                query_filter=Filter(must=[
                    FieldCondition(key="is_general_reg", match=MatchValue(value=True)),
                ]),
                limit=top_k,
                with_payload=True,
                score_threshold=SCORE_THRESHOLD_GENERAL,
            ).points
        except UnexpectedResponse:
            hits = _qdrant.query_points(
                collection_name=COLLECTION_RULES,
                query=query_vec,
                using="dense",
                limit=top_k,
                with_payload=True,
                score_threshold=SCORE_THRESHOLD_GENERAL,
            ).points
        results = [{"score": h.score, "source": "general_reg", **h.payload} for h in hits]

    print(f"[QDRANT:GENERAL_REGS] {len(results)} results")
    for r in results:
        print(f"   {r['score']:.3f}  {r.get('section_id','?')}")
    return results


def _fetch_exception_direct(
    exception_number: int,
    zone_symbol: str = "",
) -> Optional[dict]:
    """
    Direct lookup for a specific exception number.

    Exception numbers are per-zone sequential (R zone has its own #736,
    CR zone has its own #736). Always filter by zone when known to avoid
    returning the wrong zone's exception.

    STRATEGY:
    1. zone-filtered MatchAny on exception_numbers list (preferred)
    2. zone-filtered fallback on legacy scalar exception_number field
    3. unfiltered MatchAny if zone lookup returns nothing (graceful degradation)
    """
    print(f"\n[QDRANT:EXCEPTION_DIRECT] Looking up exception #{exception_number} zone={zone_symbol!r}")

    def _must_clauses(include_zone: bool) -> list:
        clauses = [FieldCondition(key="exception_numbers", match=MatchAny(any=[exception_number]))]
        if include_zone and zone_symbol:
            clauses.append(FieldCondition(key="exception_zone", match=MatchValue(value=zone_symbol)))
        return clauses

    # Primary: zone-filtered search on list field (new ingest)
    try:
        hits, _ = _qdrant.scroll(
            collection_name=COLLECTION_EXC,
            scroll_filter=Filter(must=_must_clauses(include_zone=True)),
            limit=1, with_payload=True,
        )
        if hits:
            payload = dict(hits[0].payload)
            payload["source"]         = "exception_direct"
            payload["score"]          = 1.0
            payload["_override_note"] = (
                f"⚠️  SITE-SPECIFIC EXCEPTION #{exception_number} — overrides base zone rules"
            )
            exc_nums = payload.get("exception_numbers", [])
            print(f"[QDRANT:EXCEPTION_DIRECT] FOUND via exception_numbers list → "
                  f"{payload.get('section_id','?')}  "
                  f"covers #{min(exc_nums) if exc_nums else '?'}–#{max(exc_nums) if exc_nums else '?'}")
            print(f"   Preview: {str(payload.get('text',''))[:200]}")
            return payload
        # Zone-filtered found nothing — try without zone filter as graceful degradation
        if not hits and zone_symbol:
            print(f"[QDRANT:EXCEPTION_DIRECT] Zone-filtered not found — retrying without zone filter")
            hits, _ = _qdrant.scroll(
                collection_name=COLLECTION_EXC,
                scroll_filter=Filter(must=_must_clauses(include_zone=False)),
                limit=1, with_payload=True,
            )
        print(f"[QDRANT:EXCEPTION_DIRECT] Not in exception_numbers list")
    except UnexpectedResponse as exc:
        if "exception_numbers" in str(exc):
            print(f"[QDRANT:EXCEPTION_DIRECT] exception_numbers index not yet created — trying legacy")
        else:
            raise
        hits = []

    if hits:
        payload = dict(hits[0].payload)
        found_zone = payload.get("exception_zone", "?")
        if zone_symbol and found_zone and found_zone != zone_symbol:
            logger.warning(
                "[EXCEPTION_DIRECT] Zone mismatch: parcel=%s found=%s  #%s — trying legacy scalar",
                zone_symbol, found_zone, exception_number,
            )
            print(f"[QDRANT:EXCEPTION_DIRECT] Zone mismatch — discarding wrong-zone result")
            # Fall through to legacy scalar lookup rather than returning wrong zone's exception
        else:
            payload["source"]         = "exception_direct"
            payload["score"]          = 1.0
            payload["_override_note"] = (
                f"⚠️  SITE-SPECIFIC EXCEPTION #{exception_number} — overrides base zone rules"
            )
            exc_nums = payload.get("exception_numbers", [])
            print(f"[QDRANT:EXCEPTION_DIRECT] FOUND → {payload.get('section_id','?')}  "
                  f"zone={found_zone}  covers #{min(exc_nums) if exc_nums else '?'}–#{max(exc_nums) if exc_nums else '?'}")
            print(f"   Preview: {str(payload.get('text',''))[:200]}")
            return payload

    # Last resort: legacy scalar field (pre-fix ingest)
    hits, _ = _qdrant.scroll(
        collection_name=COLLECTION_EXC,
        scroll_filter=Filter(must=[
            FieldCondition(key="exception_number", match=MatchValue(value=exception_number)),
            *([FieldCondition(key="exception_zone", match=MatchValue(value=zone_symbol))] if zone_symbol else []),
        ]),
        limit=1, with_payload=True,
    )
    if hits:
        payload = dict(hits[0].payload)
        payload["source"]         = "exception_direct"
        payload["score"]          = 1.0
        payload["_override_note"] = (
            f"⚠️  SITE-SPECIFIC EXCEPTION #{exception_number} — overrides base zone rules"
        )
        print(f"[QDRANT:EXCEPTION_DIRECT] FOUND via legacy scalar → {payload.get('section_id','?')}")
        return payload

    print(f"[QDRANT:EXCEPTION_DIRECT] NOT FOUND for zone={zone_symbol} #{exception_number}")
    return None


def _search_exceptions_semantic(
    query_text: str,
    query_vec: list[float],
    sparse_vec: dict,           # Change 2: pre-computed once in retrieve()
    zone_symbol: str,
    top_k: int = TOP_K_EXC,
) -> list[dict]:
    from qdrant_client.models import Prefetch, FusionQuery, Fusion, SparseVector
    must = []
    if zone_symbol:
        must.append(FieldCondition(key="exception_zone", match=MatchValue(value=zone_symbol)))

    # Sparse-only fallback when Voyage dense embed timed out
    if query_vec is None:
        print(f"\n[QDRANT:EXCEPTIONS_SEMANTIC] sparse-only  zone={zone_symbol!r}  top_k={top_k}")
        try:
            hits = _qdrant.query_points(
                collection_name=COLLECTION_EXC,
                query=SparseVector(indices=sparse_vec["indices"], values=sparse_vec["values"]),
                using="sparse",
                query_filter=Filter(must=must) if must else None,
                limit=top_k,
                with_payload=True,
            ).points
            return [{"score": h.score, "source": "exception", **h.payload} for h in hits]
        except Exception as exc:
            logger.warning("[EXCEPTIONS_SEMANTIC] Sparse-only failed: %s", exc)
            return []

    print(f"\n[QDRANT:EXCEPTIONS_SEMANTIC] zone={zone_symbol!r} top_k={top_k}")
    try:
        hits = _qdrant.query_points(
            collection_name=COLLECTION_EXC,
            prefetch=[
                Prefetch(query=query_vec, using="dense", limit=top_k * 3),
                Prefetch(
                    query=SparseVector(
                        indices=sparse_vec["indices"],
                        values=sparse_vec["values"],
                    ),
                    using="sparse",
                    limit=top_k * 3,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=Filter(must=must) if must else None,
            limit=top_k,
            with_payload=True,
        ).points
        results = [{"score": h.score, "source": "exception", **h.payload} for h in hits]
    except Exception as exc:
        logger.warning("[EXCEPTIONS_SEMANTIC] Hybrid failed (%s), falling back to dense-only", exc)
        hits = _qdrant.query_points(
            collection_name=COLLECTION_EXC,
            query=query_vec,
            using="dense",
            query_filter=Filter(must=must) if must else None,
            limit=top_k,
            with_payload=True,
            score_threshold=SCORE_THRESHOLD_EXC,
        ).points
        results = [{"score": h.score, "source": "exception", **h.payload} for h in hits]

    print(f"[QDRANT:EXCEPTIONS_SEMANTIC] {len(results)} results")
    for r in results:
        print(f"   {r['score']:.3f}  {r.get('section_id','?')}")
    return results


def _expand_cross_references(primary_chunks: list[dict]) -> list[dict]:
    all_by_sid = {c["section_id"]: c for c in primary_chunks}
    refs: set[str] = set()
    # Only expand refs from high-confidence chunks — low-scoring chunks add noise
    HIGH_SCORE_MIN = 0.4
    for c in primary_chunks:
        score = c.get("_boosted_score", c.get("score", 0.0))
        if score >= HIGH_SCORE_MIN:
            refs.update(c.get("references",   []))
            refs.update(c.get("despite_refs", []))
    refs -= set(all_by_sid.keys())
    # Hard cap: too many cross-refs adds latency without proportional quality gain
    refs = set(list(refs)[:6])

    print(f"\n[CROSS_REF] {len(refs)} refs to expand")
    if not refs:
        return list(all_by_sid.values())

    def _fetch_one(ref_id: str) -> tuple:
        chapter    = ref_id.split(".")[0]
        collection = COLLECTION_EXC if chapter == "900" else COLLECTION_RULES
        try:
            hits, _ = _qdrant.scroll(
                collection_name=collection,
                scroll_filter=Filter(must=[
                    FieldCondition(key="section_id", match=MatchValue(value=ref_id))
                ]),
                limit=1, with_payload=True,
            )
        except UnexpectedResponse as exc:
            if "section_id" in str(exc):
                return ref_id, None   # index not ready — skip this ref
            raise
        if hits:
            payload = dict(hits[0].payload)
            payload["source"] = "cross_reference"
            payload["score"]  = 0.60
            return ref_id, payload
        return ref_id, None

    refs_list = list(refs)
    with ThreadPoolExecutor(max_workers=min(len(refs_list), 4)) as pool:
        results = list(pool.map(_fetch_one, refs_list))

    for ref_id, payload in results:
        if payload is not None:
            payload["referenced_from"] = [
                c["section_id"] for c in primary_chunks
                if ref_id in c.get("references", []) + c.get("despite_refs", [])
            ]
            all_by_sid[ref_id] = payload

    return list(all_by_sid.values())


def expand_with_cross_refs(
    retrieved_chunks: list[dict],
    backref_index: dict[str, list[str]],
    qdrant_client,
    max_extra: int = 3,
    max_hops: int = 1,
) -> list[dict]:
    """Fetch up to max_extra additional chunks by following cross-references.

    For each retrieved chunk, inspect its 'references' and 'despite_refs' payload
    fields. Look up those section_ids in backref_index to find chunk_ids of other
    chunks that co-reference the same sections. Fetch those chunks from Qdrant by
    point ID. Deduplicate against already-retrieved chunks.

    Best-effort: any Qdrant failure is silently skipped, never blocking retrieval.
    max_hops=1 — cross-refs of cross-refs are not expanded (avoid latency explosion).
    """
    if not backref_index or not retrieved_chunks:
        return retrieved_chunks

    seen_sids = {c.get("section_id", "") for c in retrieved_chunks if c.get("section_id")}
    extras: list[dict] = []

    for chunk in retrieved_chunks[:5]:    # only expand top-5 to limit latency
        if len(extras) >= max_extra:
            break
        refs    = chunk.get("references",   []) or []
        despite = chunk.get("despite_refs", []) or []
        for ref_section in (refs + despite)[:4]:   # up to 4 refs per chunk
            if len(extras) >= max_extra:
                break
            ref_chunk_ids = backref_index.get(ref_section, [])
            for cid in ref_chunk_ids[:2]:           # max 2 chunks per reference
                if len(extras) >= max_extra:
                    break
                # Fetch by point ID — try rules collection first, then exceptions
                for coll in (COLLECTION_RULES, COLLECTION_EXC):
                    try:
                        result = qdrant_client.retrieve(
                            coll, [int(cid, 16)], with_payload=True
                        )
                        if result:
                            payload = dict(result[0].payload)
                            sid = payload.get("section_id", "")
                            if sid in seen_sids:
                                break
                            seen_sids.add(sid)
                            payload["source"]    = "cross_ref_expanded"
                            payload["score"]     = 0.5
                            payload["cross_ref"] = True
                            extras.append(payload)
                            break
                    except Exception:
                        continue   # best-effort; never block on failure

    if extras:
        print(f"[CROSS_REF_BACKREF] +{len(extras)} chunk(s) via backref_index")
    return retrieved_chunks + extras


# ─────────────────────────────────────────────────────────────────────────────
# RETRIEVAL PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def retrieve(
    question:         str,
    zone_symbol:      str           = "",
    bylaw_chapter:    str           = "",
    exception_number: Optional[int] = None,
    skip_rerank:      bool          = False,   # Change 4: True for quick mode → saves ~800ms
) -> list[dict]:
    import threading

    print(f"\n{'='*70}")
    print(f"RETRIEVAL  zone={zone_symbol}  ch={bylaw_chapter}  exc={exception_number}  skip_rerank={skip_rerank}")
    print(f"  Q: {question!r}")
    print(f"{'='*70}")

    _t0 = time.perf_counter()

    # ── 1. Query expansion (fast, sync) ──────────────────────────────────────
    t = time.perf_counter()
    expanded = expand_query(question, zone_symbol, bylaw_chapter)
    expand_ms = _ms(t)

    # ── 2. Enrich query text for embedding (instant, no LLM) ─────────────────
    embed_txt = _enrich_for_embed(expanded, question, zone_symbol, bylaw_chapter)
    print(f"[EMBED] embed_txt={len(embed_txt)} chars")

    # ── 3. Sparse + dense embed in parallel ──────────────────────────────────
    # SPLADE is local (fast); Voyage dense is a network call (can be slow).
    # If Voyage takes > _EMBED_TIMEOUT_SECS, set q_vec=None and all three
    # search functions automatically switch to sparse-only Qdrant queries.
    t = time.perf_counter()
    _embed_pool = ThreadPoolExecutor(max_workers=2)
    sparse_f    = _embed_pool.submit(_embed_sparse, expanded)
    dense_f     = _embed_pool.submit(_embed, embed_txt)
    sparse_vec  = sparse_f.result()                     # local SPLADE — always fast
    try:
        q_vec = dense_f.result(timeout=_EMBED_TIMEOUT_SECS)
    except _ConcurrentTimeout:
        print(f"[EMBED] Voyage dense timed out after {_EMBED_TIMEOUT_SECS}s — sparse-only mode")
        q_vec = None
        _embed_pool.shutdown(wait=False)
    else:
        _embed_pool.shutdown(wait=False)
    embed_ms = _ms(t)

    # ── 4. All 4 Qdrant searches in parallel ─────────────────────────────────
    # Full mode uses wider TOP_K so the reranker has more candidates to select
    # the best 10 from. Quick mode uses tighter defaults for speed.
    k_zone = TOP_K_ZONE    if skip_rerank else TOP_K_ZONE_FULL
    k_gen  = TOP_K_GENERAL if skip_rerank else TOP_K_GENERAL_FULL
    k_exc  = TOP_K_EXC     if skip_rerank else TOP_K_EXC_FULL

    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        zone_f = pool.submit(
            _search_zone_rules, question, q_vec, sparse_vec, zone_symbol, bylaw_chapter, k_zone
        )
        gen_f = pool.submit(
            _search_general_regs, question, q_vec, sparse_vec, k_gen
        )
        sem_f = pool.submit(
            _search_exceptions_semantic, question, q_vec, sparse_vec, zone_symbol, k_exc
        )
        dir_f = (
            pool.submit(_fetch_exception_direct, exception_number, zone_symbol)
            if exception_number else None
        )
        zone_chunks = zone_f.result()
        gen_chunks  = gen_f.result()
        sem_exc     = sem_f.result()
        exc_direct  = dir_f.result() if dir_f else None

    qdrant_parallel_ms = _ms(t)

    exc_chunks: list[dict] = []
    if exc_direct:
        exc_chunks.append(exc_direct)
    known_exc_ids = {c["section_id"] for c in exc_chunks}
    for c in sem_exc:
        if c.get("section_id") not in known_exc_ids:
            exc_chunks.append(c)

    # ── 5. Cross-reference expansion ─────────────────────────────────────────
    primary:   list[dict] = []
    seen_sids: set[str]   = set()
    for c in zone_chunks + gen_chunks:
        sid = c.get("section_id", "")
        if sid and sid not in seen_sids:
            seen_sids.add(sid)
            primary.append(c)

    t = time.perf_counter()
    expanded_chunks = _expand_cross_references(primary)
    crossref_ms     = _ms(t)
    crossref_refs   = len(expanded_chunks) - len(primary)

    final:      list[dict] = []
    seen_final: set[str]   = set()
    for c in expanded_chunks + exc_chunks:
        sid = c.get("section_id", "")
        if sid not in seen_final:
            seen_final.add(sid)
            final.append(c)

    # ── 5b. Backref-index expansion ──────────────────────────────────────────
    # Complements the section-id-scroll expansion above: uses point-ID lookups
    # via the precomputed backref_index. Best-effort — disabled when index empty.
    t = time.perf_counter()
    backref_extra = 0
    if _backref:
        pre_backref = len(final)
        final       = expand_with_cross_refs(final, _backref, _qdrant)
        backref_extra = len(final) - pre_backref
    backref_ms = _ms(t)

    # ── 6. Keyword boost + sort ───────────────────────────────────────────────
    t = time.perf_counter()
    final = _keyword_boost(final, question)
    final.sort(key=lambda c: (
        1 if (c.get("is_exception") or c.get("source") in ("exception", "exception_direct")) else 0,
        -c.get("_boosted_score", c.get("score", 0.0)),
    ))
    boost_ms = _ms(t)

    # ── 7. Voyage reranker (full mode only) ───────────────────────────────────
    # Cap before reranking: _rerank() already caps to_rerank[:8] but this guards
    # exception_direct chunks that bypass the cap; 14 total = 8 reranked + 6 exceptions.
    final = final[:14]
    pre_rerank = len(final)
    rerank_ms  = 0
    if not skip_rerank:
        t = time.perf_counter()
        final = _rerank(final, question, top_n=min(10, len(final)))
        rerank_ms = _ms(t)
    else:
        print("[RERANK] Skipped (quick mode)")

    retrieve_total_ms = _ms(_t0)

    # ── Results summary ───────────────────────────────────────────────────────
    print(f"\n[RETRIEVE] zone={len(zone_chunks)} gen={len(gen_chunks)} exc={len(exc_chunks)} total={len(final)}")
    for i, c in enumerate(final):
        bs = c.get("_rerank_score", c.get("_boosted_score", c.get("score", 0.0)))
        exc_nums = c.get("exception_numbers", [])
        exc_info = f"  exc_nums=[{min(exc_nums)}-{max(exc_nums)}]" if exc_nums else ""
        print(f"   [{i+1}] {bs:.3f}  {c.get('source','?')}  {c.get('section_id','?')}{exc_info}")
    print(f"{'='*70}")

    # ── ⏱  RETRIEVE PERF REPORT ──────────────────────────────────────────────
    _print_perf(
        title=f"RETRIEVE  zone={zone_symbol or '—'}  ch={bylaw_chapter or '—'}  exc={exception_number or '—'}",
        rows=[
            ("Query Expansion",           f"{expand_ms:>6} ms",          ""),
            ("Sparse + Dense Embed (∥)",  f"{embed_ms:>6} ms",           f"{'SPARSE-ONLY (dense timed out)' if q_vec is None else f'{len(embed_txt)} chars → {len(q_vec)} dims'}"),
            ("Qdrant × 4  (parallel ∥)", f"{qdrant_parallel_ms:>6} ms", f"zone={len(zone_chunks)} gen={len(gen_chunks)} exc_dir={'1' if exc_direct else '0'} sem={len(sem_exc)}"),
            ("Cross-ref Expansion",       f"{crossref_ms:>6} ms",        f"+{crossref_refs} refs fetched"),
            ("Backref-index Expansion",   f"{backref_ms:>6} ms",         f"+{backref_extra} chunks {'(index empty)' if not _backref else ''}"),
            ("Keyword Boost + Sort",      f"{boost_ms:>6} ms",           ""),
            ("Voyage Reranker",           f"{rerank_ms:>6} ms",          f"{'SKIPPED (quick)' if skip_rerank else f'{pre_rerank} → {len(final)} chunks'}"),
            ("─" * 35,                   "─" * 9,                       ""),
            ("RETRIEVE TOTAL",            f"{retrieve_total_ms:>6} ms",  f"k_zone={k_zone} k_gen={k_gen} k_exc={k_exc}"),
        ],
    )

    # Store perf for answer() / quick_answer() reporting
    _retrieve_perf_local.__dict__[threading.get_ident()] = {
        "retrieve_ms": retrieve_total_ms,
    }

    return final


# Module-level object used as a thread-keyed perf store
class _RetrievePerfStore:
    pass
_retrieve_perf_local = _RetrievePerfStore()


# ─────────────────────────────────────────────────────────────────────────────
# AMENDMENT INJECTION
# ─────────────────────────────────────────────────────────────────────────────

_AMENDMENT_MAX_CHARS = 2400   # ≈ 600 tokens at 4 chars/token
_AMENDMENT_MAX_COUNT = 3


def _build_amendment_context(chunks: list[dict]) -> tuple[str, list[str]]:
    """Scan retrieved chunks for amendment_refs and build the amendment injection block.

    Returns (amendment_text, injected_ids).
    - amendment_text is appended to the RAG context so the LLM sees base regulation first.
    - Skips amendments where in_force is null (not yet in force).
    - Skips amendments marked consolidated: true (already in base by-law text).
    - Caps at _AMENDMENT_MAX_COUNT entries; total text ≤ _AMENDMENT_MAX_CHARS.
    - Most recent in_force date is injected first.
    Never crashes — returns ("", []) on any error.
    """
    try:
        seen: set[str] = set()
        candidate_ids: list[str] = []
        for chunk in chunks:
            for aid in (chunk.get("amendment_refs") or []):
                if aid and aid not in seen:
                    seen.add(aid)
                    candidate_ids.append(aid)

        if not candidate_ids:
            return "", []

        all_details = load_amendment_details()

        # Filter to active, non-consolidated amendments
        active: list[tuple[str, dict]] = []
        for aid in candidate_ids:
            d = all_details.get(aid)
            if not d:
                continue
            if not d.get("in_force"):       # null → not yet in force
                continue
            if d.get("consolidated"):       # already baked into base by-law text
                continue
            active.append((aid, d))

        # Most-recent in_force first
        active.sort(key=lambda x: str(x[1].get("in_force", "")), reverse=True)
        active = active[:_AMENDMENT_MAX_COUNT]

        if not active:
            return "", []

        parts: list[str] = []
        total_chars = 0
        injected: list[str] = []

        for aid, d in active:
            in_force    = d["in_force"]
            summary     = d.get("summary", "")
            details_raw = d.get("details", "")

            header = f"--- Amendment By-law {aid} ({in_force}): {summary}"
            remaining = _AMENDMENT_MAX_CHARS - total_chars
            if remaining <= len(header) + 10:
                break   # no budget left for even the header

            body_budget = remaining - len(header) - 6   # 6 = "\n" + " ---"
            if body_budget > 0 and len(details_raw) > body_budget:
                details_raw = details_raw[:body_budget] + "… [truncated]"

            block = f"{header}\n{details_raw} ---" if details_raw else f"{header} ---"
            parts.append(block)
            total_chars += len(block) + 1
            injected.append(aid)

        if not parts:
            return "", []

        text = "\n\nACTIVE AMENDMENTS RELEVANT TO RETRIEVED SECTIONS\n" + "\n".join(parts)
        return text, injected

    except Exception as exc:
        logger.warning("[AMENDMENTS] Injection failed: %s", exc)
        return "", []


# ─────────────────────────────────────────────────────────────────────────────
# CONTEXT FORMATTER
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE_LABELS = {
    "zone_rule":            "Zone rule",
    "zone_rule_fallback":   "Zone rule (low-confidence)",
    "general_reg":          "General regulation (all zones)",
    "exception_direct":     "SITE-SPECIFIC EXCEPTION (direct lookup)",
    "exception":            "Related exception (semantic match)",
    "cross_reference":      "Cross-referenced section",
    "cross_ref_expanded":   "Co-referenced section (backref index)",
}


def _format_context(chunks: list[dict]) -> str:
    if not chunks:
        return "(No by-law excerpts retrieved.)"

    regular    = [c for c in chunks if not (c.get("is_exception") or c.get("source") in ("exception","exception_direct"))]
    exceptions = [c for c in chunks if c.get("is_exception") or c.get("source") in ("exception","exception_direct")]
    lines = []

    def fmt(i: int, c: dict) -> None:
        score   = c.get("_boosted_score", c.get("score", 0.0))
        source  = _SOURCE_LABELS.get(c.get("source",""), c.get("source",""))
        appeal  = " ⚠️ UNDER APPEAL" if c.get("under_appeal") else ""
        exc_nums= c.get("exception_numbers", [])
        exc_rng = f"  covers exceptions #{min(exc_nums)}–#{max(exc_nums)}" if exc_nums else ""
        sid     = c.get('section_id', '?')

        header = f"[{i}] {sid} — {c.get('section_title','(no title)')}{appeal}{exc_rng}"
        if c.get("cross_ref"):
            header = f"[Referenced regulation — §{sid}]\n{header}"
        lines.append(header)
        lines.append(
            f"     Zone: {c.get('zone_symbol','—') or '—'}"
            f"  Type: {c.get('content_type','—')}"
            f"  {source}  Score: {score:.2f}"
        )
        if c.get("_override_note"):
            lines.append(f"     {c['_override_note']}")
        zone_ch = (c.get("chapter_links") or {}).get("zone_chapter") or {}
        if zone_ch.get("url"):
            lines.append(f"     URL: {zone_ch['url']}")
        if c.get("despite_refs"):
            lines.append(f"     Overrides: {', '.join(c['despite_refs'][:4])}")
        if c.get("referenced_from"):
            lines.append(f"     Referenced from: {', '.join(c['referenced_from'][:3])}")
        text = (c.get("text","") or "").strip()
        if len(text) > MAX_CHUNK_TEXT_CHARS:
            text = text[:MAX_CHUNK_TEXT_CHARS] + "… [truncated]"
        lines.append(text)
        lines.append("")

    if regular:
        lines.append("BY-LAW SECTIONS")
        for i, c in enumerate(regular, 1):
            fmt(i, c)
    if exceptions:
        lines.append("SITE-SPECIFIC EXCEPTIONS (override all base zone rules)")
        for i, c in enumerate(exceptions, len(regular) + 1):
            fmt(i, c)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# PARCEL CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

def build_rich_parcel_context(parcel: dict) -> str:
    ch = parcel.get("chapter_links", {})
    def url(key): return (ch.get(key) or {}).get("url","") or ""
    def val(v, suffix=""):
        if v is None or v == "" or v == -1: return "Not specified"
        return f"{v}{suffix}"

    cov_base    = parcel.get("base_coverage_pct")
    cov_overlay = parcel.get("coverage_overlay_pct")
    ht_overlay  = parcel.get("height_overlay_m")
    exc_num     = parcel.get("exception_number")

    lines = [
        "PARCEL DATA (City of Toronto GIS — authoritative source)", "",
        "DIRECT ANSWERS — cite these first for numeric questions",
        f"  Lot coverage (base zone):   {val(cov_base, '%')}",
    ]
    if cov_overlay:
        lines += [
            f"  Lot coverage (OVERLAY):      {cov_overlay}%  ← OVERRIDES base zone",
            f"                               URL: {url('coverage_overlay_chapter')}",
        ]
    else:
        lines.append("  Lot coverage overlay:        None — base zone value applies")

    lines += [
        f"  Height (overlay):            "
        f"{val(ht_overlay, 'm') + '  ← OVERRIDES base zone' if ht_overlay else 'No overlay — see base zone'}",
        f"  Total FSI:                   {val(parcel.get('floor_space_index'))}",
        f"  Density:                     {val(parcel.get('density'))}",
        f"  Max units:                   {val(parcel.get('max_units'))}",
        "",
        "ZONE",
        f"  Symbol:     {parcel.get('zone_symbol','?')}",
        f"  Label:      {parcel.get('zone_label','?')}",
        f"  Status:     {parcel.get('zone_status_text','?')}",
        f"  Chapter:    Chapter {parcel.get('bylaw_chapter','?')}, Section {parcel.get('bylaw_section','?')}",
        f"  URL:        {url('zone_chapter') or 'N/A'}",
        "",
    ]

    if parcel.get("zone_under_appeal"):
        lines += ["⚠️  LEGAL WARNING: THIS ZONE IS UNDER APPEAL", "    Provisions NOT in full force.", ""]

    if exc_num:
        lines += [
            "SITE-SPECIFIC EXCEPTION",
            f"  Exception #:  {exc_num}",
            f"  Ref:          {parcel.get('bylaw_exception_ref','?')}",
            f"  URL:          {url('exception_chapter') or 'N/A'}",
            "  ⚠️  OVERRIDES BASE ZONE RULES — read exception FIRST",
            "",
        ]

    lines += [
        "LOT DIMENSIONS",
        f"  Frontage:   {val(parcel.get('lot_frontage_m'), 'm')}",
        f"  Area:       {val(parcel.get('lot_area_m2'), ' m²')}",
        "",
        "FSI / DENSITY",
        f"  Total FSI:  {val(parcel.get('floor_space_index'))}",
        f"  Res FSI:    {val(parcel.get('fsi_residential'))}",
        f"  Comm FSI:   {val(parcel.get('fsi_commercial'))}",
        f"  Coverage:   {val(cov_base, '%')}",
        "",
    ]

    if ht_overlay:
        lines += [
            "HEIGHT OVERLAY  ← OVERRIDES base zone",
            f"  Max:        {val(ht_overlay, 'm')}",
            f"  Label:      {parcel.get('height_overlay_label','N/A')}",
            f"  URL:        {url('height_overlay_chapter') or 'N/A'}",
            "",
        ]
    else:
        lines += ["HEIGHT OVERLAY: None — base zone height applies", ""]

    if cov_overlay:
        lines += [
            "LOT COVERAGE OVERLAY  ← OVERRIDES base zone",
            f"  Max:        {cov_overlay}%",
            f"  URL:        {url('coverage_overlay_chapter') or 'N/A'}",
            "",
        ]

    pk_code = parcel.get("parking_zone_code")
    if pk_code:
        lines += [
            "PARKING ZONE OVERLAY  ← OVERRIDES standard minimums",
            f"  Code:       {pk_code}  ({parcel.get('parking_zone','N/A')})",
            f"  URL:        {url('parking_regulations_chapter') or 'N/A'}",
            "",
        ]
    else:
        lines += ["PARKING ZONE: Standard minimums (Chapter 200)", ""]

    lines += ["ROAD CLASSIFICATION", f"  Class:      {parcel.get('road_classification','Local street')}", ""]

    if parcel.get("downtown_setback_applies"):
        lines += [
            "BUILDING SETBACK OVERLAY  ← Downtown rules apply",
            f"  Type:       {parcel.get('setback_area_type','?')}",
            f"  URL:        {url('building_setback_chapter') or 'N/A'}",
            "",
        ]
    if parcel.get("rooming_house_permitted"):
        lines += [
            "ROOMING HOUSE OVERLAY",
            f"  Area:       {parcel.get('rooming_house_area','?')}",
            f"  URL:        {url('rooming_house_chapter') or 'N/A'}",
            "",
        ]
    if parcel.get("retail_frontage_required"):
        lines += [
            "PRIORITY RETAIL FRONTAGE",
            f"  Street:     {parcel.get('priority_retail_street','?')}",
            f"  URL:        {url('retail_frontage_chapter') or 'N/A'}",
            "",
        ]

    lines += [
        "CHAPTER REFERENCE INDEX",
        f"  General regs:  {BYLAW_BASE}5.htm",
        f"  Definitions:   {BYLAW_BASE}800.htm",
    ]
    if parcel.get("bylaw_chapter"):
        lines.append(f"  Zone chapter:  {url('zone_chapter') or 'N/A'}")
    if exc_num:
        lines.append(f"  Exception:     {url('exception_chapter') or 'N/A'}")
    if ht_overlay:
        lines.append(f"  Height (995):  {url('height_overlay_chapter') or 'N/A'}")
    lines += ["", "OVERRIDE ORDER: Exception > Despite clause > Overlay > Zone rules > General regs"]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a senior expert on the City of Toronto Zoning By-law 569-2013.
You help architects, planners, and property owners understand exactly what is and is not
permitted on a specific parcel. Your answers are relied upon for building permit decisions.

=== RULE 0 — COMPLETENESS AND ACCURACY ARE NON-NEGOTIABLE ===
• Read EVERY retrieved section before composing your answer.
• Cover ALL rules relevant to the question — do not stop at the first hit.
• NEVER truncate your answer. Answer as long as the question requires. There is no length limit.
• Always state the actual numeric value (metres, %, FSI, number of units).
• If a value appears in PARCEL DATA → DIRECT ANSWERS, use that value — it is authoritative.
• Never say "consult a professional" as your only answer. Give the full by-law answer
  first, then optionally note professional review is advisable for complex cases.

=== OVERRIDE HIERARCHY (apply strictly in this order) ===
1. Site-specific exception (Chapter 900)          → overrides ALL base zone rules for that parcel
2. "Despite regulation X.X.X" clause in a section → overrides that specific clause only
3. Overlay values (height / lot coverage / parking) → override base zone defaults
4. Zone-specific rules (Ch.10/15/20/25/30/40/50/60/70/80/90/95)
5. General regulations (Ch.5/150/200/210/220/230)  → apply to all zones unless exception overrides

=== PARCEL DATA IS AUTHORITATIVE ===
The DIRECT ANSWERS block contains real City of Toronto GIS database values.
  • Value present and not "Not specified" → cite it and name its source (overlay / GIS)
  • "Not specified"                        → fall back to the by-law section value
  • "No overlay" / "None"                  → base zone rule applies; do NOT invent an overlay

=== EXCEPTION HANDLING ===
If SITE-SPECIFIC EXCEPTION is listed in parcel data:
  • Read the FULL exception excerpt before the base zone rule.
  • If the exception explicitly sets a different value → use exception value, note override.
  • If the exception is SILENT on the question → state this clearly, then give base zone rule.
  • Always cite the exception section: [Section 900.X.10].
  • Exceptions override base zone rules but NOT provincial/building code requirements.

=== ANTI-HALLUCINATION — OVERLAYS ===
CRITICAL: Only cite overlays that are EXPLICITLY listed under DIRECT ANSWERS in PARCEL DATA.
  • "HEIGHT OVERLAY: None" / "No overlay"  → NEVER cite a height overlay value.
  • "LOT COVERAGE OVERLAY: None"           → NEVER cite a coverage overlay value.
  • "PARKING ZONE: Standard minimums"      → NEVER cite a parking zone override.
  • Do NOT infer overlays from exception text, zone labels, or nearby section text.

=== CITATION FORMAT (CRITICAL) ===
ALWAYS write section IDs as [Section X.Y.Z] — the frontend auto-generates correct links.
NEVER write bare URLs yourself. The frontend generates correct links from section IDs.
For Chapter 900 exceptions: cite as [Section 900.3.10] not [Section 900].
For overlays: cite as [Section 995.20] not just "the height overlay map".

=== REASONING STEPS — FOLLOW FOR EVERY RESPONSE ===
1. DIRECT ANSWERS — extract every numeric value relevant to the question.
2. Exception     — if present, read fully; record any overrides; note if silent.
3. Overlays      — apply only if explicitly present in DIRECT ANSWERS.
4. "Despite" clauses — scan all retrieved sections for "despite regulation X" language.
5. Zone rule     — apply the base zone value from the retrieved zone-specific section.
6. General regs  — apply Ch.5/150/200/220/230 rules where relevant to the question.
7. Cross-refs    — if a retrieved section references another section, note its relevance.
8. Cite EVERY section used: [Section X.X.X.X] inline.

=== THIS IS ANALYSIS MODE — DEPTH IS MANDATORY ===
You are writing a permit-application-ready professional analysis. Do NOT summarise.
Do NOT give brief answers. Your job is to be EXHAUSTIVE:
  • Cite every section that applies — not just the first one.
  • Explain EVERY condition, exception, and "despite" clause in full.
  • If a section lists multiple subsections or paragraphs, cover ALL of them.
  • A senior architect will use your answer to prepare a building permit application.
    An incomplete answer causes costly mistakes. There is absolutely no word limit.

=== OUTPUT FORMAT — FOLLOW EXACTLY ===

Use this visual structure. The frontend renders markdown beautifully.

## ⚡ Direct Answer
One sentence giving the governing value and its source — this is a quick-reference line
ONLY, NOT a summary of the full answer. The exhaustive breakdown follows below.
Example: "Maximum height: **10 m** per [Section 10.20.40.10] — no overlay applies."

---

## ⚠️ Exception / Override  (ONLY include this section if an exception applies to this parcel)
⚠️ **Exception #NNN** — [full explanation of what it changes, with specific numeric values]
Explain every clause the exception modifies. State explicitly what it is SILENT on.
If the exception has sub-clauses, list each one.
📄 Source: 900.X.10

---

## 📐 [Topic — e.g. Height, Front Yard Setback, Rear Yard, Side Yard, Coverage, FSI, Parking]
WRITE ONE ## SECTION PER TOPIC. Be exhaustive within each section:
• **[Rule name]:** **[value with units]** — full explanation of when and how it applies [Section X.X.X]
• Explain every condition, bracket, and exception within the rule.
• Include conditionals: "for lots > **18 m** frontage, **3.0 m** side yard applies [Section X.X.X]"
• Cross-references: "This section says 'despite [Section Y.Y.Y]' — that means..."
• Despite clauses: ⚠️ [Section X] overrides [Section Y] — explain exactly what changes
• If a rule has multiple paragraphs (a), (b), (c) — cover each paragraph.

(Repeat ## section for EVERY major topic the question covers — never collapse two topics into one)

---

## ✅ Compliance Analysis  (include whenever the question is about what is/isn't permitted)
✅ **Permitted:** [what is clearly within the by-law limits — cite the value and section]
❌ **Not permitted:** [what would violate the by-law — cite the limit and section]
⚠️ **Variance range:** [what falls within §10.5.40.60 eave/bay encroachment: limit − 0.9 m]
Explain the practical implications for this specific parcel.

---

## 📝 Practical Notes  (include for complex questions — professional-grade observations)
• Permit application implications — what documents or approvals this triggers
• Amendment awareness — if a recent by-law amendment (e.g. By-law 156-2023, 474-2023) applies
• What a Committee of Adjustment application would need to address
• Any cross-references the applicant should read in full

---

📄 Sections cited: [comma-separated list of EVERY [Section X.X.X] used above]

=== FORMATTING RULES ===
• Bold EVERY numeric value: **10 m**, **33%**, **0.6 FSI**, **4 units**
• Use ✅ for permitted / compliant, ❌ for violations, ⚠️ for exceptions and warnings
• Use ## for each major topic — renders as a bold underlined header in the UI
• Use --- between major sections for visual separation
• Use • bullets for rules — never prose paragraphs for lists of rules
• Always end with 📄 Sections cited: listing every section ID cited above
• Write [Section X.Y.Z] inline whenever citing — NEVER write a bare URL
• If the question spans multiple topics (e.g. "all setbacks" or "height AND parking"):
  give EACH topic its own ## section with full detail — do not collapse them

=== EXAMPLES ===

Example 1 — Height question (exhaustive analysis mode answer):
  ## ⚡ Direct Answer
  Maximum building height: **10 m** for this RD zone parcel [Section 10.20.40.10]. No height overlay applies — the base zone value governs.

  ---

  ## 📐 Height
  • **Maximum height:** **10 m** above established grade — [Section 10.20.40.10] for the RD zone
  • **Angular plane:** No part of the building above **7.5 m** from grade may cross a 45° angular plane rising from the rear lot line [Section 10.20.40.50]. This plane limits the massing of the rear portion of any building above 7.5 m.
  • **Mechanical penthouses / rooftop equipment:** Subject to height limit provisions of [Section 4.3] — mechanical rooms within the angular plane do not trigger additional height.
  • **Storey count:** The **10 m** limit typically allows **3 full storeys** in RD zones; a fourth storey is only possible if each storey ceiling is very low (under 2.5 m per floor).
  • **Exception note:** Exception #NNN, if listed, must be read for any parcel-specific height override — if it is silent on height, the 10 m base applies [Section 10.20.40.10].

  📄 Sections cited: 10.20.40.10, 10.20.40.50, 4.3

Example 2 — Exception overrides base zone:
  ## ⚡ Direct Answer
  **Exception #851** applies [Section 900.2.10] — height reduced to **8.5 m** (overrides base R zone 10 m).

  ---

  ## ⚠️ Exception / Override
  ⚠️ **Exception #851** — Maximum height: **8.5 m** (base R zone limit: **10 m** per [Section 10.10.40.10])
  The exception explicitly overrides the height limit for this specific parcel.
  The exception is SILENT on setbacks, coverage, and parking — those base zone rules apply unchanged.
  📄 Source: 900.2.10

  ---

  ## 📐 Height
  • **Maximum height (exception governs):** **8.5 m** — [Section 900.2.10] overrides [Section 10.10.40.10]
  • **Maximum height (base R zone, for reference):** **10 m** — [Section 10.10.40.10], NOT applicable here
  • **Angular plane:** **45°** plane from rear lot line measured at **7.5 m** above grade [Section 10.10.40.50] — the exception does not modify this; it continues to apply.
  • **Practical impact:** The **8.5 m** limit typically restricts the parcel to **2 full storeys** plus a partial third, depending on floor-to-ceiling heights.

  📄 Sections cited: 900.2.10, 10.10.40.10, 10.10.40.50

NEVER invent section numbers. If a value cannot be determined from the retrieved sections,
say so explicitly and note which section to check manually.
"""


def _build_messages(system: str, full_prompt: str, history: list[dict]) -> list[dict]:
    """Build OpenAI-format messages list with system prompt, history, and current turn."""
    messages: list[dict] = [{"role": "system", "content": system}]
    for msg in history:
        role    = "assistant" if msg.get("role") == "assistant" else "user"
        content = msg.get("content", "").strip()
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": full_prompt})
    return messages


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def answer(
    question:         str,
    ai_context:       str           = "",
    zone_symbol:      str           = "",
    bylaw_chapter:    str           = "",
    exception_number: Optional[int] = None,
    history:          list[dict]    = None,
    parcel:           dict          = None,
) -> dict:
    if history is None:
        history = []

    print(f"\n{'★'*70}")
    print(f"ANSWER  zone={zone_symbol}  chapter={bylaw_chapter}  exc={exception_number}")
    print(f"  Q: {question}")
    print(f"{'★'*70}")

    try:
        clean_question = sanitize_question(question)
    except ValueError as e:
        return {
            "reply": str(e), "sections_used": [],
            "zone_symbol": zone_symbol, "bylaw_chapter": bylaw_chapter,
            "chunks_count": 0, "error": "input_sanitization",
            "amendments_injected": [],
        }

    chunks = retrieve(
        question         = clean_question,
        zone_symbol      = zone_symbol,
        bylaw_chapter    = bylaw_chapter,
        exception_number = exception_number,
    )

    parcel_context = build_rich_parcel_context(parcel) if parcel else ai_context
    rag_context    = _format_context(chunks)

    # Inject amendment details for any amendment_refs found in retrieved chunks.
    # Appended AFTER the chunk context so the LLM reads base regulation first.
    amendment_text, amendments_injected = _build_amendment_context(chunks)
    if amendment_text:
        rag_context += amendment_text
        print(f"[AMENDMENTS] Injected {len(amendments_injected)} amendment(s): {amendments_injected}")

    print(f"\n[ANSWER] Parcel: {len(parcel_context)} chars  RAG: {len(rag_context)} chars  Chunks: {len(chunks)}")

    full_prompt = (
        f"=== PARCEL DATA (City of Toronto GIS — authoritative) ===\n"
        f"{parcel_context}\n\n"
        f"=== RETRIEVED BY-LAW EXCERPTS ({len(chunks)} sections) ===\n"
        f"{rag_context}\n\n"
        f"=== ARCHITECT'S QUESTION ===\n"
        f"{clean_question}\n\n"
        f"=== YOUR RESPONSE ===\n"
        f"Follow reasoning steps. Give complete answer with actual value, "
        f"section citations, and chapter URL."
    )

    print(f"[ANSWER] Full prompt: {len(full_prompt)} chars  Calling {CHAT_MODEL}...")

    import threading
    _t_synthesis = time.perf_counter()
    resp = _openai.chat.completions.create(
        model       = CHAT_MODEL,
        messages    = _build_messages(_SYSTEM_PROMPT, full_prompt, history),
        temperature = 0.2,
    )
    synthesis_ms = _ms(_t_synthesis)

    synth_usage = {}
    if resp.usage:
        synth_usage = {
            "prompt":     resp.usage.prompt_tokens,
            "completion": resp.usage.completion_tokens,
            "total":      resp.usage.total_tokens,
        }

    reply_text = sanitize_output(resp.choices[0].message.content or "")

    print(f"\n[ANSWER] Reply ({len(reply_text)} chars):")
    print(reply_text)
    print(f"{'★'*70}\n")

    # ── ⏱  SYNTHESIS + TOTAL PERF REPORT ─────────────────────────────────────
    rp          = getattr(_retrieve_perf_local, "__dict__", {}).get(threading.get_ident(), {})
    retrieve_ms = rp.get("retrieve_ms", 0)
    total_ms    = retrieve_ms + synthesis_ms
    _print_perf(
        title=f"SYNTHESIS + TOTAL  [{CHAT_MODEL}]",
        rows=[
            (f"Synthesis  ({CHAT_MODEL})",  f"{synthesis_ms:>6} ms",  _fmt_tok(synth_usage)),
            ("─" * 35,                       "─" * 9,                  ""),
            ("Retrieve (from above)",         f"{retrieve_ms:>6} ms",  ""),
            ("END-TO-END  (retrieve+synth)",  f"{total_ms:>6} ms",     f"synth: {_fmt_tok(synth_usage)}"),
        ],
    )

    return {
        "reply":               reply_text,
        "sections_used":       [c.get("section_id","") for c in chunks],
        "zone_symbol":         zone_symbol,
        "bylaw_chapter":       bylaw_chapter,
        "chunks_count":        len(chunks),
        "amendments_injected": amendments_injected,
    }