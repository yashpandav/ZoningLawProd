"""
Toronto Zoning By-law 569-2013 — RAG Ingestion Pipeline
=========================================================
Voyage AI  |  voyage-2 (1024-dim)  |  Qdrant  |  SPLADE sparse

VERIFIED PDF STRUCTURE (read from actual PDFs, not assumed):

  Both Part 2 and Part 3 have IDENTICAL layout:
    Pages  1-21  → Table of Contents (auto-detected and skipped)
    Page  22+    → Real by-law content

  Part 2 (9067...pdf) → toronto_zoning_exceptions
    900.2.10  R   zone  exceptions  #2–994
    900.3.10  RD  zone  exceptions  #1–1463
    900.4.10  RS  zone  exceptions  #1–339
    900.5.10  RT  zone  exceptions  #1–363
    900.6.10  RM  zone  exceptions  #1–501
    900.7.10  RA  zone  exceptions  #1–779
    900.8.10  RAC zone  exceptions  #1–200

  Part 3 (90a5...pdf) → toronto_zoning_exceptions
    900.10.10 CL  zone  exceptions  #1–590
    900.11.10 CR  zone  exceptions  #1–2648
    900.12.10 CRE zone  exceptions  #1–89
    900.20.10 E   zone  exceptions  #1–318
    900.21.10 EL  zone  exceptions  #1–129
    900.22.10 EH  zone  exceptions  #1–45
    900.24.10 EO  zone  exceptions  #1–30
    900.30.10 I   zone  exceptions  #1–95
    900.31.10 IH  zone  exceptions  #1–23
    900.33.10 IS  zone  exceptions  #1–1
    900.34.10 IPW zone  exceptions  #1–95
    900.40.10 O   zone  exceptions  #1–213
    900.41.10 ON  zone  exceptions  #1–38
    900.42.10 OR  zone  exceptions  #1–85
    900.43.10 OC  zone  exceptions  #1–10
    900.44.10 OG  zone  exceptions  #1–3
    900.50.10 UT  zone  exceptions  #4–48

BUGS FIXED IN THIS VERSION (all verified against real PDF pages):

  Bug 1 — False section headings closed real sections too early
    The old SECTION_HEADING regex matched bare integers and decimal numbers
    inside exception body text:
      "29-43 Cardiff Rd." → matched as section "29 Cornish Rd."
      "0.7 parking spaces..." → matched as section "0.7 parking spaces"
      "4.5 metres;" → matched as section "4.5 metres"
    Each false match CLOSED the current real section (e.g. 900.2.10)
    after only 1 page — losing all remaining exception text.
    Fix: require section IDs to contain at least one dot (e.g. 900.2, 900.2.10)
    OR be an explicit "Chapter N" heading. Bare integers never match.

  Bug 2 — exception_number stored only the first exception per chunk
    Old code: EXCEPTION_NUMBER.search(text) → only first match
    A chunk covering exceptions 955-975 stored exception_number=955.
    Query for exception #961 → NOT FOUND.
    Fix: EXCEPTION_ENTRY.finditer(text) → ALL exceptions in chunk
    Store: exception_numbers=[955,...,975] (list for MatchAny queries)
           exception_number=955 (scalar, backward compat)
           exception_number_min/max for range queries

  Bug 3 — TOC pages polluted section text
    Pages 1-21 are table-of-contents pages. Their text would prepend
    noise to the first real section's carry-over buffer if not filtered.
    Fix: is_toc_page() detects pages where >40% of non-blank lines end
    with dot-sequences + page numbers ("......... 123") and skips them.

INSTALL:
  pip install pdfplumber qdrant-client tiktoken tqdm python-dotenv google-genai

.env required:
  GCP_PROJECT_ID=your-project
  GCP_LOCATION=us-central1
  QDRANT_URL=http://localhost:6333
  QDRANT_API_KEY=          # blank for local Qdrant
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import pdfplumber
import tiktoken
import voyageai
from dotenv import load_dotenv
from tqdm import tqdm

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, SparseVectorParams, SparseIndexParams,
    PointStruct, PayloadSchemaType, SparseVector,
)

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# VOYAGE AI CLIENT
# ─────────────────────────────────────────────────────────────────────────────
_voyage = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])

# ─────────────────────────────────────────────────────────────────────────────
# SPARSE (BM25) EMBEDDER — lazy-loaded on first use, not at import time
# ─────────────────────────────────────────────────────────────────────────────
from fastembed import SparseTextEmbedding as _SparseModel

_sparse_embedder = None  # initialized on first call to embed_sparse_text()

def _get_sparse_embedder() -> _SparseModel:
    global _sparse_embedder
    if _sparse_embedder is None:
        print("[SPARSE] Loading SPLADE model...")
        _sparse_embedder = _SparseModel(model_name="prithivida/Splade_PP_en_v1")
        print("[SPARSE] SPLADE model ready")
    return _sparse_embedder

def embed_sparse_text(text: str) -> dict:
    """Return {"indices": [...], "values": [...]} for Qdrant sparse vector."""
    result = list(_get_sparse_embedder().embed([text[:1_800]]))[0]
    return {
        "indices": result.indices.tolist(),
        "values":  result.values.tolist(),
    }

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

PDF_PARTS = [
    # ── TEST FILE ─────────────────────────────────────────────────────────────
    # Small 7-page PDF for validating the Voyage AI + BM25 pipeline end-to-end.
    # Swap back to the production parts below once the test passes.
    # {
    #     "path":        "../GardenSuits.pdf",
    #     "part_num":    1,
    #     "description": "Garden Suites — test ingestion",
    #     "collection":  "toronto_zoning_rules",
    #     "skip_toc":    False,   # 7-page doc has no TOC
    # },

    # ── PRODUCTION PARTS (re-enable after test succeeds) ──────────────────────
    # Part 1 — Chapters 1-800 (zone rules, parking, definitions)
    {
        "path":        "../97ec-City-Planning-Zoning-Zoning-By-law-Part-1.pdf",
        "part_num":    1,
        "description": "Chapters 1-800: zone rules, parking, definitions",
        "collection":  "toronto_zoning_rules",
        "skip_toc":    True,
    },

    # Part 2 — Chapter 900 Part A — Residential exceptions
    {
        "path":        "../9067-City-Planning-Zoning-Zoning-By-law-Part-2.pdf",
        "part_num":    2,
        "description": "Chapter 900-A: Residential exceptions (R RD RS RT RM RA RAC)",
        "collection":  "toronto_zoning_exceptions",
        "skip_toc":    True,
    },

    # Part 3 — Chapter 900 Part B — Commercial / Employment / Institutional
    {
        "path":        "../90a5-City-Planning-Zoning-Zoning-By-law-Part-3.pdf",
        "part_num":    3,
        "description": "Chapter 900-B: Commercial/Employment/Institutional exceptions",
        "collection":  "toronto_zoning_exceptions",
        "skip_toc":    True,
    },
]

BYLAW_NUMBER         = "569-2013"
EMBEDDING_MODEL      = "voyage-2"
EMBEDDING_DIM        = 1024
EMBED_BATCH          = 8          # voyage-2 supports up to 128 docs per batch
MAX_EMBED_CHARS      = 120_000    # voyage-2 supports ~16k tokens, ~120k chars is safe
MAX_TOKENS_PER_CHUNK = 600
MIN_TOKENS_PER_CHUNK = 30
UPSERT_BATCH         = 64
EMBED_SLEEP_SECS     = 0.1        # polite pause between batches (voyage-2 rate limit: 1000 RPM)

# ─────────────────────────────────────────────────────────────────────────────
# REGEX PATTERNS
# ─────────────────────────────────────────────────────────────────────────────

# ── Section heading — two-tier strict matching ───────────────────────────────
#
# Problem with the old single regex:
#   r'^(\d+(?:\.\d+){0,4})\s{1,6}(.+)$'
# It matched ANY line starting with a digit, including:
#   "29-43 Cardiff Rd." (address in exception body text)
#   "0.7 parking spaces per unit" (measurement)
#   "4.5 metres;" (setback value)
# Each false match incorrectly CLOSED the current open section.
#
# Solution: two patterns that together cover all real headings:
#
#   SECTION_DOTTED — IDs with at least one dot: 900.2, 900.2.10, 10.20.30
#     Eliminates bare integers (29, 4) and leading-zero decimals (0.7)
#     Title must start with a capital letter — eliminates "(9)(B)..." lines
#     Title must not end with ; or , — eliminates "4.5 metres;"
#
#   SECTION_CHAPTER — Explicit "Chapter N" prefix
#     Handles headings like "Chapter 900 Site Specific Exceptions"
#
SECTION_DOTTED = re.compile(
    r'^((?:Chapter\s+)?([1-9]\d*\.\d+(?:\.\d+){0,3}))'
    r'\s{1,6}'
    r'([A-Z][^\n;]{0,118}[A-Za-z0-9])$',
    re.MULTILINE,
)
SECTION_CHAPTER = re.compile(
    r'^(Chapter\s+([1-9]\d*))'
    r'\s{1,6}'
    r'([A-Z][^\n;]{0,118}[A-Za-z0-9])$',
    re.MULTILINE,
)

# ── Exception entry ──────────────────────────────────────────────────────────
# Matches: "(7) Exception R 7" or "(961) Exception RD 961"
EXCEPTION_ENTRY = re.compile(
    r'^\((\d+)\)\s+Exception\s+([A-Z]+)\s+(\d+)',
    re.MULTILINE,
)

# ── TOC line detector ────────────────────────────────────────────────────────
# TOC lines end with "............. 123" (dots + page number)
TOC_LINE = re.compile(r'\.{4,}\s*\d+\s*$')

# ── Page header (repeated boilerplate on every page) ────────────────────────
PAGE_HEADER = re.compile(
    r'By-law 569-2013 as amended\s*\n'
    r'Zoning By-law for the City of Toronto\s*\n'
    r'Office Consolidation[^\n]*\n',
    re.MULTILINE,
)

# ── Other metadata patterns ──────────────────────────────────────────────────
AMENDMENT_TAG = re.compile(r'\[\s*[Bb]y-?law[:\s]+([\d\-A-Z/\s]+?)\s*\]')
CROSS_REF = re.compile(
    r'(?:regulation|[Ss]ection|[Cc]lause|[Aa]rticle|[Cc]hapter)\s+'
    r'(\d+(?:\.\d+){1,4}(?:\(\d+\))?(?:\([A-Z]\))?(?:\([ivxl]+\))?)',
)
DESPITE_REF = re.compile(
    r'[Dd]espite\s+(?:regulation|[Ss]ection|[Cc]lause)\s+([\d.]+)',
)
ADDRESS_HINT = re.compile(
    r'\bOn\s+([\d,\s]+(?:and\s+\d+)?\s+[A-Z][a-zA-Z\s]+'
    r'(?:Avenue|Street|Road|Drive|Boulevard|Crescent|Court|Way|Lane'
    r'|Place|Trail|Circle|Gate|Path|Row|Terrace)\.?)',
)
UNDER_APPEAL_MARKERS = [
    "bright yellow", "under appeal",
    "not in full force and effect", "shaded dark yellow",
]

# ─────────────────────────────────────────────────────────────────────────────
# LOOKUP MAPS
# ─────────────────────────────────────────────────────────────────────────────

# Section ID prefix → zone symbol (Part 1 non-exception chapters)
ZONE_SYMBOL_MAP = {
    "10": None,   "10.10": "R",   "10.20": "RD",  "10.40": "RS",
    "10.60": "RT","10.80": "RM",  "15": "RA",     "15.10": "RA",
    "15.20": "RAC","20": "CL",   "25": "CR",     "30": "CL",
    "40": "CR",   "50": "CRE",   "60": "E",      "60.10": "EL",
    "60.20": "E", "70": "I",     "80": "I",      "90": "O",
    "95": "UT",   "100": "UT",
}

# 900.X prefix → zone symbol (derived by reading the actual PDFs)
EXCEPTION_ZONE_MAP = {
    "900.2":  "R",   "900.3":  "RD",  "900.4":  "RS",  "900.5":  "RT",
    "900.6":  "RM",  "900.7":  "RA",  "900.8":  "RAC",
    "900.10": "CL",  "900.11": "CR",  "900.12": "CRE",
    "900.20": "E",   "900.21": "EL",  "900.22": "EH",  "900.24": "EO",
    "900.30": "I",   "900.31": "IH",  "900.33": "IS",  "900.34": "IPW",
    "900.40": "O",   "900.41": "ON",  "900.42": "OR",  "900.43": "OC",
    "900.44": "OG",  "900.50": "UT",
}

# Valid first-level chapter numbers in By-law 569-2013.
# Used by find_section_headings() to filter false-positive section IDs like
# "187.83" (an elevation value in metres) or "1356.2015" (an amendment number).
VALID_CHAPTER_PREFIXES = {
    '1','2','3','4','5','6','7','8','9',
    '10','15','20','25','30','40','50','60','70','80','90','95',
    '100','150','200','210','220','230','600','800','900','990','995',
}

ZONE_CATEGORY_MAP = {
    "R":"Residential","RD":"Residential","RS":"Residential",
    "RT":"Residential","RM":"Residential",
    "RA":"Residential Apartment","RAC":"Residential Apartment",
    "CL":"Commercial","CR":"Commercial Residential",
    "CRE":"Commercial Residential Employment",
    "E":"Employment Industrial","EL":"Employment Industrial",
    "EH":"Employment Industrial","EO":"Employment Industrial",
    "I":"Institutional","IS":"Institutional","IH":"Institutional","IPW":"Institutional",
    "O":"Open Space","ON":"Open Space","OR":"Open Space",
    "OC":"Open Space","OG":"Open Space",
    "UT":"Utility Transportation","NA":"Natural Area",
}

CONTENT_TYPE_MAP = {
    "permitted use":"permitted_uses",    "permitted uses":"permitted_uses",
    "lot requirement":"lot_requirements","lot frontage":"lot_requirements",
    "lot area":"lot_requirements",       "setback":"setbacks",
    "yard":"setbacks",                   "front yard":"setbacks",
    "rear yard":"setbacks",              "side yard":"setbacks",
    "height":"height",                   "floor space index":"floor_space_index",
    "floor area":"floor_space_index",    "parking":"parking",
    "bicycle parking":"parking",         "loading":"loading",
    "access to lot":"access",            "driveway":"access",
    "landscaping":"landscaping",         "ancillary building":"ancillary_buildings",
    "ancillary structure":"ancillary_buildings",
    "definition":"definitions",
    "general":"general_regulation",      "interpretation":"general_regulation",
    "overlay":"overlay",                 "exception":"exception",
    "garden suite":"ancillary_buildings","secondary suite":"ancillary_buildings",
}

GENERAL_REG_CHAPTERS = {"1","2","5","150","200","210","220","230","800","990","995"}

CHAPTER_NAME_MAP = {
    "10":"Residential","15":"Residential Apartment",
    "20":"Commercial Local","25":"Commercial Residential",
    "40":"Commercial Residential","50":"Commercial Residential Employment",
    "60":"Employment Industrial","70":"Institutional",
    "90":"Open Space","150":"Specific Use Regulations",
    "200":"Parking","600":"Overlay Regulations",
    "800":"Definitions","900":"Site-Specific Exceptions",
}

# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ZoningChunk:
    chunk_id:             str           = ""
    section_id:           str           = ""
    section_title:        str           = ""
    parent_id:            str           = ""
    parent_title:         str           = ""
    chapter_id:           str           = ""
    part_num:             int           = 0
    zone_symbol:          str           = ""
    zone_category:        str           = ""
    is_general_reg:       bool          = False
    is_exception:         bool          = False
    content_type:         str           = "general_regulation"
    # Exception number fields — ALL numbers per chunk (not just the first)
    exception_number:     Optional[int] = None   # first found — backward compat
    exception_numbers:    list[int]     = field(default_factory=list)
    exception_number_min: Optional[int] = None
    exception_number_max: Optional[int] = None
    exception_zone:       str           = ""
    address_hints:        list[str]     = field(default_factory=list)
    pdf_filename:         str           = ""
    page_start:           int           = 0
    page_end:             int           = 0
    bylaw_number:         str           = BYLAW_NUMBER
    last_amended:         str           = "2024-04-01"
    references:           list[str]     = field(default_factory=list)
    despite_refs:         list[str]     = field(default_factory=list)
    amendment_refs:       list[str]     = field(default_factory=list)
    under_appeal:         bool          = False
    text:                 str           = ""
    embed_text:           str           = ""
    token_count:          int           = 0
    vector:               list[float]   = field(default_factory=list, repr=False)
    sparse_vector:        dict          = field(default_factory=dict, repr=False)

    def to_payload(self) -> dict:
        d = asdict(self)
        d.pop("embed_text", None)
        d.pop("vector", None)
        d.pop("sparse_vector", None)
        return d

# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

_enc = tiktoken.get_encoding("cl100k_base")

def count_tokens(text: str) -> int:
    return len(_enc.encode(text))

def stable_id(section_id: str, part_num: int, page: int) -> str:
    return hashlib.sha1(f"{section_id}::{part_num}::{page}".encode()).hexdigest()[:16]

def get_parent_id(section_id: str) -> str:
    parts = section_id.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else ""

def get_chapter_id(section_id: str) -> str:
    return section_id.split(".")[0]

def strip_page_header(text: str) -> str:
    """Remove the repeated by-law boilerplate header and lone page numbers."""
    text = PAGE_HEADER.sub("", text)
    text = re.sub(r'^\s*\d{1,4}\s*$', '', text, flags=re.MULTILINE)
    return text.strip()

def is_toc_page(text: str) -> bool:
    """
    Detect a Table of Contents page.

    TOC pages have most lines ending with ".......... 123" (dots + page number).
    We skip these — their section references are navigation aids, not by-law text.
    Threshold: >40% of non-blank lines look like TOC entries.

    Verified: pages 1-21 of both Part 2 and Part 3 are TOC pages.
    Page 22 is the first real content page in both PDFs.
    """
    lines = [l for l in text.split('\n') if l.strip()]
    if not lines:
        return True
    toc_count = sum(1 for l in lines if TOC_LINE.search(l))
    return toc_count / len(lines) > 0.40

def find_section_headings(text: str) -> list:
    """
    Find all real section headings on a page.

    Returns matches sorted by position, de-duplicated.
    Two-step filter:
      1. SECTION_DOTTED / SECTION_CHAPTER regex (structural)
      2. VALID_CHAPTER_PREFIXES check (semantic) — eliminates false positives
         like "187.83 AMSL." (elevation value) where the first segment (187)
         is not a valid by-law chapter number.
    """
    found: dict[int, object] = {}
    for m in SECTION_DOTTED.finditer(text):
        prefix = m.group(2).split('.')[0]
        if prefix in VALID_CHAPTER_PREFIXES:
            found[m.start()] = m
    for m in SECTION_CHAPTER.finditer(text):
        if m.start() not in found:
            found[m.start()] = m
    return sorted(found.values(), key=lambda m: m.start())

def classify_zone(section_id: str, section_title: str) -> tuple[str, str]:
    """Return (zone_symbol, zone_category) for a chunk."""
    chapter = section_id.split(".")[0]
    if chapter == "900":
        # Use verified map: 900.3 → "RD", 900.11 → "CR", etc.
        prefix = ".".join(section_id.split(".")[:2])  # e.g. "900.3" from "900.3.10__s12"
        if prefix in EXCEPTION_ZONE_MAP:
            sym = EXCEPTION_ZONE_MAP[prefix]
            return sym, ZONE_CATEGORY_MAP.get(sym, "Exception")
        # Fallback: parse from title e.g. "Exceptions for RD Zone"
        m = re.search(
            r'\b(R|RD|RS|RT|RM|RA|RAC|CL|CR|CRE|EL|EH|EO|E|'
            r'I|IH|IS|IPW|O|ON|OR|OC|OG|UT|NA)\b',
            section_title,
        )
        if m:
            sym = m.group(1)
            return sym, ZONE_CATEGORY_MAP.get(sym, "Exception")
        return "", "Exception"
    # Non-exception chapters: longest matching prefix wins
    for depth in range(4, 0, -1):
        prefix = ".".join(section_id.split(".")[:depth])
        if prefix in ZONE_SYMBOL_MAP:
            sym = ZONE_SYMBOL_MAP[prefix]
            # None means the chapter covers mixed/multiple zones — store as "" so
            # the chunk appears in general residential queries without being zone-filtered
            if sym is None:
                return "", "General"
            return sym, ZONE_CATEGORY_MAP.get(sym, "General")
    return "", "General"

def classify_content_type(title: str) -> str:
    tl = title.lower()
    for kw, ct in CONTENT_TYPE_MAP.items():
        if kw in tl:
            return ct
    return "general_regulation"

def extract_all_exception_numbers(text: str) -> list[int]:
    """
    Extract EVERY exception number in a chunk's text.

    The old code used .search() which returned only the first match.
    A chunk covering exceptions 955-975 would store exception_number=955,
    so querying for #961 returned NOT FOUND.

    This function uses .finditer() to get all matches, then deduplicates
    and sorts them. The query layer uses MatchAny([961]) on the
    exception_numbers list field, which finds the correct chunk.
    """
    return sorted(set(int(m.group(1)) for m in EXCEPTION_ENTRY.finditer(text)))

def build_embed_text(c: "ZoningChunk") -> str:
    """
    Build the text sent to the embedding model.
    Richer context = better cosine similarity at query time.
    """
    lines = ["Toronto Zoning By-law 569-2013"]
    if c.zone_symbol:
        lines.append(f"Zone: {c.zone_symbol} — {c.zone_category}")
    ch_name = CHAPTER_NAME_MAP.get(c.chapter_id, f"Chapter {c.chapter_id}")
    lines.append(f"Chapter {c.chapter_id} — {ch_name}")
    if c.parent_id and c.parent_title:
        lines.append(f"Parent: {c.parent_id} {c.parent_title}")
    lines.append(f"Section {c.section_id}: {c.section_title}")
    lines.append(f"Type: {c.content_type}")
    if c.exception_numbers:
        lines.append(f"Exceptions: {c.exception_numbers[:5]} ...")
    header = " | ".join(lines[:3]) + "\n" + "\n".join(lines[3:])
    return f"{header}\n\n{c.text}"

# ─────────────────────────────────────────────────────────────────────────────
# PDF EXTRACTION — streaming, one page at a time with carry-over buffer
# ─────────────────────────────────────────────────────────────────────────────

def _close_section(
    sid: str, title: str, text: str, pg_start: int, pg_end: int,
) -> Optional[dict]:
    """Finalize section text and return a raw section dict, or None if empty."""
    body = re.sub(
        r'^\s*' + re.escape(sid) + r'\s+' + re.escape(title) + r'\s*',
        '', text.strip(), count=1,
    ).strip()
    if not body:
        return None
    return {
        "section_id":    sid,
        "section_title": title,
        "text":          body,
        "page_start":    pg_start,
        "page_end":      pg_end,
    }

def extract_raw_sections(pdf_path: str, skip_toc: bool = True) -> list[dict]:
    """
    Stream through a PDF and extract sections using a carry-over buffer.

    WHY STREAMING:
    Sections like "900.3.10 Exceptions for RD Zone" span 1 078 pages.
    A batched extractor closes sections at batch boundaries, losing all
    text past the batch edge. Streaming keeps one open section in RAM
    and only closes it when a new heading is found — no content is lost.

    WHY SKIP TOC:
    Pages 1-21 of Parts 2 and 3 are table-of-contents pages. They contain
    section titles with trailing dot-leaders and page numbers — not actual
    by-law text. If included, this noise would be prepended to the first
    real section. is_toc_page() detects and skips these automatically.

    MEMORY:
    Only the current section's accumulated text is kept in RAM.
    For the largest section (900.3.10, 1 078 pages) that is ~8-12 MB.
    """
    print(f"\n[EXTRACT] {Path(pdf_path).name}")
    raw_sections: list[dict] = []
    toc_skipped = 0

    cur_sid:      str = ""
    cur_title:    str = ""
    cur_text:     str = ""
    cur_pg_start: int = 0
    cur_pg_end:   int = 0

    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        print(f"  Pages: {total}  (streaming  toc_skip={skip_toc})")

        for i in tqdm(range(total), desc="  Reading pages", unit="pg"):
            pg_num = i + 1
            try:
                page_text = strip_page_header(pdf.pages[i].extract_text() or "")
            except Exception:
                page_text = ""

            if skip_toc and is_toc_page(page_text):
                toc_skipped += 1
                continue

            headings = find_section_headings(page_text)

            if not headings:
                # No new section on this page — accumulate into current
                if cur_sid:
                    cur_text += "\n" + page_text
                    cur_pg_end = pg_num
            else:
                # Text before the first heading belongs to the current section
                pre = page_text[:headings[0].start()].strip()
                if cur_sid and pre:
                    cur_text += "\n" + pre
                    cur_pg_end = pg_num

                for j, h in enumerate(headings):
                    # Close the current open section
                    if cur_sid:
                        sec = _close_section(cur_sid, cur_title, cur_text,
                                             cur_pg_start, cur_pg_end)
                        if sec:
                            raw_sections.append(sec)

                    # Open new section
                    cur_sid      = h.group(2).strip()
                    cur_title    = h.group(3).strip()
                    cur_pg_start = pg_num
                    cur_pg_end   = pg_num
                    start        = h.end()
                    end          = headings[j + 1].start() if j + 1 < len(headings) else len(page_text)
                    cur_text     = page_text[start:end].strip()

        # Close the final section at EOF
        if cur_sid:
            sec = _close_section(cur_sid, cur_title, cur_text, cur_pg_start, cur_pg_end)
            if sec:
                raw_sections.append(sec)

    print(f"  TOC pages skipped:  {toc_skipped}")
    print(f"  Raw sections found: {len(raw_sections)}")
    return raw_sections

# ─────────────────────────────────────────────────────────────────────────────
# CHUNKING — exception-aware splitting
# ─────────────────────────────────────────────────────────────────────────────
#
# THE CRITICAL INSIGHT (confirmed by reading actual PDF pages):
# Exception section text uses ONLY single newlines — there are zero blank lines
# in 5 pages of exception content. The old split_long_section used re.split(r'\n\s*\n')
# which never fires, leaving 900.2.10 as ONE massive chunk with 554 exceptions.
# The ingest run confirmed this: 17 total chunks with avg 458.4 exceptions/chunk.
#
# Fix: for Chapter 900 exception sections, split on individual exception
# entry boundaries ("(N) Exception X N"). Each exception becomes its own chunk.
# For non-exception sections (Chapter 1-800), keep the blank-line splitter.
#
def split_exception_section(section: dict, max_tokens: int) -> list[dict]:
    """
    Split a Chapter 900 exception section by individual exception entries.

    Each "(N) Exception X N\n..." block becomes its own sub-chunk.
    If a single exception exceeds max_tokens (rare but possible for complex ones),
    it is split on single newlines as a last resort.

    The preamble (text before the first exception, e.g. "The regulations located
    in Article 900.2.10 apply only to...") is kept as a separate leading chunk
    if it meets the minimum token threshold.
    """
    text     = section["text"]
    base_sid = section["section_id"]
    matches  = list(EXCEPTION_ENTRY.finditer(text))

    if not matches:
        # No exception entries found — fall back to generic splitter
        return split_generic_section(section, max_tokens)

    sub_chunks: list[dict] = []
    idx = 0

    # Preamble text before the first exception entry
    preamble = text[:matches[0].start()].strip()
    if preamble and count_tokens(preamble) >= MIN_TOKENS_PER_CHUNK:
        sub = dict(section)
        sub["section_id"] = f"{base_sid}__s{idx}"
        sub["text"]       = preamble
        sub_chunks.append(sub)
        idx += 1

    # One chunk per exception entry (group small ones to stay above MIN_TOKENS)
    current = ""
    for i, m in enumerate(matches):
        end          = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entry_text   = text[m.start():end].strip()
        entry_tokens = count_tokens(entry_text)

        if not current:
            current = entry_text
        else:
            candidate = current + "\n\n" + entry_text
            if count_tokens(candidate) <= max_tokens:
                # Group with previous — keeps related small exceptions together
                current = candidate
            else:
                # Flush current group
                sub = dict(section)
                sub["section_id"] = f"{base_sid}__s{idx}"
                sub["text"]       = current
                sub_chunks.append(sub)
                idx += 1
                current = entry_text

        # If a single entry already exceeds max_tokens, flush it immediately
        if count_tokens(current) > max_tokens:
            sub = dict(section)
            sub["section_id"] = f"{base_sid}__s{idx}"
            sub["text"]       = current
            sub_chunks.append(sub)
            idx += 1
            current = ""

    # Flush remainder
    if current:
        sub = dict(section)
        sub["section_id"] = f"{base_sid}__s{idx}"
        sub["text"]       = current
        sub_chunks.append(sub)

    return sub_chunks


def split_generic_section(section: dict, max_tokens: int) -> list[dict]:
    """
    Fallback splitter for non-exception sections (Chapter 1-800).
    Splits on blank lines only — never on regulatory sub-markers.
    """
    paragraphs = re.split(r'\n\s*\n', section["text"])
    sub_chunks: list[dict] = []
    current = ""
    idx = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        candidate = (current + "\n\n" + para).strip() if current else para
        if count_tokens(candidate) <= max_tokens:
            current = candidate
        else:
            if current:
                sub = dict(section)
                sub["section_id"] = f"{section['section_id']}__s{idx}"
                sub["text"]       = current
                sub_chunks.append(sub)
                idx += 1
            current = para

    if current:
        sub = dict(section)
        if idx > 0:
            sub["section_id"] = f"{section['section_id']}__s{idx}"
        sub["text"] = current
        sub_chunks.append(sub)

    return sub_chunks


def split_section(section: dict, max_tokens: int) -> list[dict]:
    """Route to the correct splitter based on chapter."""
    if get_chapter_id(section["section_id"]) == "900":
        return split_exception_section(section, max_tokens)
    return split_generic_section(section, max_tokens)

# ─────────────────────────────────────────────────────────────────────────────
# ENRICHMENT — raw dict → ZoningChunk
# ─────────────────────────────────────────────────────────────────────────────

def enrich_chunk(
    raw: dict,
    pdf_filename: str,
    part_num: int,
    parent_title_map: dict[str, str],
) -> ZoningChunk:
    sid          = raw["section_id"]
    title        = raw["section_title"]
    text         = raw["text"]
    chapter      = get_chapter_id(sid)
    parent_id    = get_parent_id(sid)
    parent_title = parent_title_map.get(parent_id, "")

    zone_sym, zone_cat = classify_zone(sid, title)
    is_exc = chapter == "900"
    is_gen = chapter in GENERAL_REG_CHAPTERS and not is_exc

    exc_numbers: list[int] = []
    exc_zone = zone_sym if is_exc else ""

    if is_exc:
        exc_numbers = extract_all_exception_numbers(text)
        if not exc_zone:
            m = re.search(
                r'\b(R|RD|RS|RT|RM|RA|RAC|CL|CR|CRE|EL|EH|EO|E|'
                r'I|IH|IS|IPW|O|ON|OR|OC|OG|UT|NA)\b',
                title,
            )
            if m:
                exc_zone = m.group(1)
                zone_sym = exc_zone
                zone_cat = ZONE_CATEGORY_MAP.get(exc_zone, "Exception")

    c = ZoningChunk(
        chunk_id             = stable_id(sid, part_num, raw["page_start"]),
        section_id           = sid,
        section_title        = title,
        parent_id            = parent_id,
        parent_title         = parent_title,
        chapter_id           = chapter,
        part_num             = part_num,
        zone_symbol          = zone_sym,
        zone_category        = zone_cat,
        is_general_reg       = is_gen,
        is_exception         = is_exc,
        content_type         = "exception" if is_exc else classify_content_type(title),
        exception_number     = exc_numbers[0] if exc_numbers else None,
        exception_numbers    = exc_numbers,
        exception_number_min = min(exc_numbers) if exc_numbers else None,
        exception_number_max = max(exc_numbers) if exc_numbers else None,
        exception_zone       = exc_zone,
        address_hints        = list(set(
            m.group(1).strip() for m in ADDRESS_HINT.finditer(text)
        )) if is_exc else [],
        pdf_filename         = pdf_filename,
        page_start           = raw["page_start"],
        page_end             = raw["page_end"],
        references           = list(set(CROSS_REF.findall(text))),
        despite_refs         = list(set(DESPITE_REF.findall(text))),
        amendment_refs       = list(set(AMENDMENT_TAG.findall(text))),
        under_appeal         = any(marker in text.lower() for marker in UNDER_APPEAL_MARKERS),
        text                 = text,
        token_count          = count_tokens(text),
    )
    c.embed_text = build_embed_text(c)
    c.sparse_vector = embed_sparse_text(c.text)
    return c

def pdf_to_chunks(pdf_path: str, part_num: int, skip_toc: bool = True) -> list[ZoningChunk]:
    fname        = Path(pdf_path).name
    raw_sections = extract_raw_sections(pdf_path, skip_toc=skip_toc)
    parent_title_map = {s["section_id"]: s["section_title"] for s in raw_sections}

    chunks:        list[ZoningChunk] = []
    skipped_short  = 0
    sections_split = 0

    for raw in tqdm(raw_sections, desc="  Enriching", unit="sec"):
        tok = count_tokens(raw["text"])
        if tok < MIN_TOKENS_PER_CHUNK:
            skipped_short += 1
            continue
        if tok > MAX_TOKENS_PER_CHUNK:
            sub_raws = split_section(raw, MAX_TOKENS_PER_CHUNK)
            sections_split += 1
        else:
            sub_raws = [raw]
        for sub_raw in sub_raws:
            if count_tokens(sub_raw["text"]) < MIN_TOKENS_PER_CHUNK:
                continue
            chunks.append(enrich_chunk(sub_raw, fname, part_num, parent_title_map))

    print(f"  Chunks produced:     {len(chunks)}")
    print(f"  Skipped (too short): {skipped_short}")
    print(f"  Sections split:      {sections_split}")

    exc_chunks = [c for c in chunks if c.exception_numbers]
    if exc_chunks:
        all_nums = sorted(set(n for c in exc_chunks for n in c.exception_numbers))
        avg      = sum(len(c.exception_numbers) for c in exc_chunks) / len(exc_chunks)
        multi    = sum(1 for c in exc_chunks if len(c.exception_numbers) > 1)
        print(f"  Exception numbers:   {len(all_nums):,} unique  "
              f"#{all_nums[0]}–#{all_nums[-1]}  avg {avg:.1f}/chunk  multi={multi}")
        by_zone: dict[str, set] = defaultdict(set)
        for c in exc_chunks:
            for n in c.exception_numbers:
                by_zone[c.exception_zone].add(n)
        for zone, nums in sorted(by_zone.items()):
            s = sorted(nums)
            print(f"    {zone or '?':6}: {len(s):5}  #{s[0]}–#{s[-1]}")
    return chunks

# ─────────────────────────────────────────────────────────────────────────────
# EMBEDDING
# ─────────────────────────────────────────────────────────────────────────────

def embed_chunks(
    chunks: list[ZoningChunk],
    checkpoint_file: str = "embed_checkpoint.json",
) -> list[ZoningChunk]:
    """
    Embed chunks via Voyage AI voyage-2.
    Checkpoint saves after every batch — safe to Ctrl+C and resume.
    """
    print(f"\n[EMBED] {len(chunks)} chunks  model={EMBEDDING_MODEL}  batch={EMBED_BATCH}  cap={MAX_EMBED_CHARS} chars")

    done: dict[str, list[float]] = {}
    if Path(checkpoint_file).exists():
        with open(checkpoint_file) as f:
            done = json.load(f)
        print(f"  Resuming: {len(done)} already embedded")

    todo = [c for c in chunks if c.chunk_id not in done]
    print(f"  To embed: {len(todo)}")

    for bs in tqdm(range(0, len(todo), EMBED_BATCH), desc="  Embedding", unit="batch"):
        batch = todo[bs : bs + EMBED_BATCH]
        texts = [
            c.embed_text[:MAX_EMBED_CHARS] if len(c.embed_text) > MAX_EMBED_CHARS
            else c.embed_text
            for c in batch
        ]
        for attempt in range(6):
            try:
                result = _voyage.embed(
                    texts,
                    model=EMBEDDING_MODEL,
                    input_type="document",
                )
                for chunk, emb in zip(batch, result.embeddings):
                    done[chunk.chunk_id] = emb
                with open(checkpoint_file, "w") as f:
                    json.dump(done, f)
                break
            except Exception as exc:
                if attempt == 5:
                    raise RuntimeError(f"Embedding failed after 6 attempts: {exc}") from exc
                wait = 2 ** (attempt + 1)
                print(f"\n  [{attempt+1}/6] {exc}. Retry in {wait}s...")
                time.sleep(wait)
        time.sleep(EMBED_SLEEP_SECS)

    missing = 0
    for c in chunks:
        if c.chunk_id in done:
            c.vector = done[c.chunk_id]
        else:
            missing += 1
    if missing:
        print(f"  WARNING: {missing} chunks have no vector (skipped in upsert)")

    Path(checkpoint_file).unlink(missing_ok=True)
    print("  Checkpoint removed")
    return chunks

# ─────────────────────────────────────────────────────────────────────────────
# QDRANT SETUP
# ─────────────────────────────────────────────────────────────────────────────

KEYWORD_FIELDS = [
    "zone_symbol","zone_category","content_type","chapter_id",
    "is_exception","is_general_reg","exception_zone",
    "under_appeal","pdf_filename","section_id",
]
INTEGER_FIELDS = [
    "exception_number",       # first found, scalar, backward compat
    "exception_number_min",   # lowest in chunk
    "exception_number_max",   # highest in chunk
    "page_start","page_end","token_count","part_num",
]

def setup_collection(qdrant: QdrantClient, name: str) -> None:
    existing = {c.name for c in qdrant.get_collections().collections}
    if name not in existing:
        print(f"  Creating: {name}")
        qdrant.create_collection(
            collection_name=name,
            vectors_config={
                "dense": VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE, on_disk=True)
            },
            sparse_vectors_config={
                "sparse": SparseVectorParams(index=SparseIndexParams(on_disk=True))
            }
        )
    else:
        print(f"  Exists:   {name} (will upsert/overwrite)")

    for fname in KEYWORD_FIELDS:
        try:
            qdrant.create_payload_index(name, fname, PayloadSchemaType.KEYWORD)
        except Exception:
            pass
    for fname in INTEGER_FIELDS:
        try:
            qdrant.create_payload_index(name, fname, PayloadSchemaType.INTEGER)
        except Exception:
            pass
    # exception_numbers is list[int] — INTEGER index enables MatchAny queries
    try:
        qdrant.create_payload_index(name, "exception_numbers", PayloadSchemaType.INTEGER)
        print(f"    Index: exception_numbers (INTEGER list — MatchAny ready)")
    except Exception:
        pass

def upsert_chunks(chunks: list[ZoningChunk], collection: str, qdrant: QdrantClient) -> None:
    valid = [c for c in chunks if c.vector and c.sparse_vector]
    if len(valid) < len(chunks):
        print(f"  WARNING: {len(chunks)-len(valid)} chunks without vectors — skipped")
    print(f"  Upserting {len(valid)} → '{collection}'")

    for i in tqdm(range(0, len(valid), UPSERT_BATCH), desc="  Upserting", unit="batch"):
        batch  = valid[i : i + UPSERT_BATCH]
        points = [
            PointStruct(
                id=int(c.chunk_id, 16),
                vector={
                    "dense": c.vector,
                    "sparse": c.sparse_vector,
                },
                payload=c.to_payload()
            )
            for c in batch
            if c.vector and c.sparse_vector
        ]
        for attempt in range(5):
            try:
                qdrant.upsert(collection_name=collection, points=points)
                break
            except Exception as exc:
                wait = 2 ** attempt
                print(f"  Upsert error: {exc}. Retry in {wait}s...")
                time.sleep(wait)
        else:
            raise RuntimeError(f"Upsert failed at batch {i}")

# ─────────────────────────────────────────────────────────────────────────────
# BACKREF INDEX
# ─────────────────────────────────────────────────────────────────────────────

def build_backref_index(all_chunks: list[ZoningChunk]) -> dict[str, list[str]]:
    """Maps section_id → [chunk_ids that reference it via cross-refs or despite clauses]."""
    index: dict[str, list[str]] = defaultdict(list)
    for c in all_chunks:
        for ref in c.references + c.despite_refs:
            index[ref].append(c.chunk_id)
    return dict(index)

# ─────────────────────────────────────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────────────────────────────────────

# Expected exception counts from actual PDF analysis
_EXPECTED = {
    "R":(554,994),"RD":(1305,1463),"RS":(316,339),"RT":(351,363),
    "RM":(442,501),"RA":(509,779),"RAC":(190,200),
    "CL":(130,590),"CR":(1576,2648),"CRE":(74,89),
    "E":(160,318),"EL":(35,129),"EH":(40,45),"EO":(25,30),
    "I":(72,95),"IH":(14,23),"IS":(1,1),"IPW":(81,95),
    "O":(22,213),"ON":(24,38),"OR":(52,85),
    "OC":(8,10),"OG":(3,3),"UT":(13,48),
}

def print_stats(all_chunks: list[ZoningChunk]) -> None:
    from collections import Counter
    print("\n" + "=" * 70)
    print("INGESTION STATS")
    print("=" * 70)
    print(f"  Total chunks:         {len(all_chunks):,}")
    print(f"  Total tokens:         {sum(c.token_count for c in all_chunks):,}")
    print(f"  Exception chunks:     {sum(1 for c in all_chunks if c.is_exception):,}")
    print(f"  General-reg chunks:   {sum(1 for c in all_chunks if c.is_general_reg):,}")

    exc_with = [c for c in all_chunks if c.exception_numbers]
    if exc_with:
        all_nums = sorted(set(n for c in exc_with for n in c.exception_numbers))
        multi    = [c for c in exc_with if len(c.exception_numbers) > 1]
        avg      = sum(len(c.exception_numbers) for c in exc_with) / len(exc_with)
        print(f"\n  Exception number coverage:")
        print(f"    Unique exception numbers: {len(all_nums):,}")
        print(f"    Chunks with >1 exception: {len(multi):,}")
        print(f"    Avg exceptions per chunk: {avg:.1f}")
        by_zone: dict[str, set] = defaultdict(set)
        for c in exc_with:
            for n in c.exception_numbers:
                by_zone[c.exception_zone].add(n)
        print(f"\n  By zone (actual vs expected):")
        for zone, nums in sorted(by_zone.items()):
            s   = sorted(nums)
            exp = _EXPECTED.get(zone)
            if exp:
                ok  = "✅" if len(s) >= exp[0] * 0.80 else "⚠️ "
                es  = f"  expected ≥{exp[0]:,} unique  max #{exp[1]}"
            else:
                ok, es = "  ", ""
            print(f"  {ok} {zone:6}: {len(s):5} unique  #{s[0]}–#{s[-1]}{es}")

    ct = Counter(c.content_type for c in all_chunks)
    print(f"\n  Top content types:")
    for ctype, n in ct.most_common(10):
        print(f"    {ctype:<32} {n:>6,}")
    print("=" * 70)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    qdrant = QdrantClient(
        url    =os.getenv("QDRANT_URL", "http://localhost:6333"),
        api_key=os.getenv("QDRANT_API_KEY") or None,
    )

    # Set up all required collections first
    collection_names = {p["collection"] for p in PDF_PARTS}
    print("\n── Setting up Qdrant collections ──────────────────────────────")
    for name in collection_names:
        setup_collection(qdrant, name)

    all_chunks: list[ZoningChunk] = []
    for part_cfg in PDF_PARTS:
        pdf_path = Path(part_cfg["path"])
        if not pdf_path.exists():
            print(f"\nWARNING: {pdf_path} not found — skipping.")
            continue
        print(f"\n── Part {part_cfg['part_num']}: {part_cfg['description']} ──")
        chunks = pdf_to_chunks(
            str(pdf_path),
            part_num = part_cfg["part_num"],
            skip_toc = part_cfg.get("skip_toc", True),
        )
        chunks = embed_chunks(
            chunks,
            checkpoint_file=f"embed_checkpoint_part{part_cfg['part_num']}.json",
        )
        upsert_chunks(chunks, part_cfg["collection"], qdrant)
        all_chunks.extend(chunks)

    print("\n── Building cross-reference back-index ────────────────────────")
    backref = build_backref_index(all_chunks)
    out = Path("backref_index.json")
    with open(out, "w") as f:
        json.dump(backref, f, indent=2)
    print(f"  Saved: {out}  ({len(backref):,} entries)")

    print_stats(all_chunks)
    print("\nIngestion complete.")

if __name__ == "__main__":
    main()