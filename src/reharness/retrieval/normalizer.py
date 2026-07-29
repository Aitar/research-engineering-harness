from __future__ import annotations

import re
import unicodedata

TOKEN_RE = re.compile(r"[a-z0-9_.:/+-]+|[\u3400-\u9fff]+", re.IGNORECASE)
ENTITY_ID_RE = re.compile(
    r"\b(?:PRJ|TASK|EVT|CON|REQ|PLAN|CHG|BUILD|TEST|TRUN|EVD|SNP|ART|REL|AUD)-[A-F0-9]{8,32}\b",
    re.IGNORECASE,
)
SHA_RE = re.compile(r"\b[a-f0-9]{40,64}\b", re.IGNORECASE)


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def lexical_terms(value: str) -> list[str]:
    normalized = normalize_text(value)
    terms: list[str] = []
    for token in TOKEN_RE.findall(normalized):
        if token not in terms:
            terms.append(token)
        if any("\u3400" <= char <= "\u9fff" for char in token):
            for size in (2, 3):
                if len(token) >= size:
                    for index in range(len(token) - size + 1):
                        gram = token[index : index + size]
                        if gram not in terms:
                            terms.append(gram)
    return terms


def index_text(title: str, body: str) -> str:
    variants = lexical_terms(f"{title}\n{body}")
    if not variants:
        return body
    return f"{body}\n\n__lexical_tokens__ {' '.join(variants)}"


def fts_query(value: str) -> str:
    terms = lexical_terms(value)
    escaped = [f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:24]]
    return " OR ".join(escaped)


def extract_entity_ids(value: str) -> list[str]:
    return [match.upper() for match in ENTITY_ID_RE.findall(value)]


def extract_hashes(value: str) -> list[str]:
    return [match.lower() for match in SHA_RE.findall(value)]
