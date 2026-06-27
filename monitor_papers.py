from __future__ import annotations

import argparse
import dataclasses
import email.utils
import hashlib
import html
import json
import logging
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dt_time, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import yaml

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - openai is optional at runtime.
    OpenAI = None


LOGGER = logging.getLogger("daily-d2nn-monitor")
USER_AGENT = "daily-d2nn-paper-monitor/1.0 (mailto:{mailto})"
ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


@dataclasses.dataclass
class Paper:
    title: str
    authors: list[str]
    venue: str
    published_date: date | None
    doi: str | None
    url: str
    abstract: str
    keywords: list[str]
    source: str
    is_preprint: bool = False
    arxiv_id: str | None = None
    external_ids: dict[str, str] = dataclasses.field(default_factory=dict)
    score: int = 0
    score_reasons: list[str] = dataclasses.field(default_factory=list)
    matched_keywords: list[str] = dataclasses.field(default_factory=list)
    reading_level: str = "可略读"
    chinese_summary: str = ""
    research_object: str = ""
    relation_to_d2nn: str = ""
    main_innovation: str = ""
    high_impact: bool = False

    @property
    def primary_key(self) -> str:
        if self.doi:
            return f"doi:{normalize_doi(self.doi)}"
        if self.arxiv_id:
            return f"arxiv:{normalize_arxiv_id(self.arxiv_id)}"
        title_hash = hashlib.sha1(normalize_title(self.title).encode("utf-8")).hexdigest()[:16]
        return f"title:{title_hash}"


class ApiError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def now_in_timezone(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def date_window(config: dict[str, Any], override_days: int | None = None) -> tuple[datetime, datetime]:
    tz_name = config.get("timezone", "Asia/Shanghai")
    end = now_in_timezone(tz_name)
    days = override_days or int(config.get("lookback_days", 7))
    start = end - timedelta(days=days)
    return start, end


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_markup(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return normalize_whitespace(value)


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value.strip()


def normalize_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    value = re.sub(r"^https?://arxiv\.org/(abs|pdf)/", "", value)
    value = re.sub(r"\.pdf$", "", value)
    return value


def normalize_venue(value: str) -> str:
    value = html.unescape(value or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return normalize_whitespace(value)


def normalize_title(value: str) -> str:
    value = html.unescape(value or "").lower()
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    stopwords = {"a", "an", "the", "in", "of", "for", "on", "to", "and", "with", "by", "et", "al"}
    tokens = [token for token in value.split() if token not in stopwords]
    return " ".join(tokens)


def contains_any(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def matched_terms(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    seen: list[str] = []
    for term in terms:
        if term.lower() in lowered and term not in seen:
            seen.append(term)
    return seen


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.date()
        except ValueError:
            continue
    parsed_dt = parse_iso_datetime(value)
    return parsed_dt.date() if parsed_dt else None


def parse_crossref_date(item: dict[str, Any]) -> date | None:
    for key in ("published-online", "published-print", "published", "issued", "created", "deposited"):
        date_info = item.get(key) or {}
        if key in {"created", "deposited"} and "date-time" in date_info:
            parsed = parse_iso_datetime(date_info.get("date-time"))
            if parsed:
                return parsed.date()
        parts = date_info.get("date-parts") or []
        if not parts or not parts[0]:
            continue
        year = parts[0][0]
        month = parts[0][1] if len(parts[0]) > 1 else 1
        day = parts[0][2] if len(parts[0]) > 2 else 1
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            continue
    return None


def is_in_window(published: date | None, start: datetime, end: datetime) -> bool:
    if not published:
        return False
    start_date = start.date()
    end_date = end.date()
    return start_date <= published <= end_date


def request_with_retry(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 25,
    retries: int = 3,
    backoff_seconds: float = 2.0,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise ApiError(f"HTTP {response.status_code}: {response.text[:300]}")
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            sleep_for = backoff_seconds * attempt
            LOGGER.warning("Request failed (%s/%s) for %s: %s; retrying in %.1fs", attempt, retries, url, exc, sleep_for)
            time.sleep(sleep_for)
    raise ApiError(f"Request failed after {retries} attempts: {url}: {last_error}")


def build_headers(config: dict[str, Any]) -> dict[str, str]:
    mailto = os.getenv("CROSSREF_MAILTO") or config.get("notification", {}).get("email", {}).get("to", "")
    return {"User-Agent": USER_AGENT.format(mailto=mailto or "unknown@example.com")}


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def search_arxiv(config: dict[str, Any], start: datetime, end: datetime) -> list[Paper]:
    search_cfg = config.get("search", {})
    keywords = config.get("keywords", [])
    timeout = int(search_cfg.get("request_timeout_seconds", 25))
    retries = int(search_cfg.get("retries", 3))
    backoff = float(search_cfg.get("retry_backoff_seconds", 2))
    max_results = int(search_cfg.get("max_results_per_source", 80))
    headers = build_headers(config)
    papers: list[Paper] = []

    for keyword_group in chunked(keywords, 8):
        query = " OR ".join([f'all:"{keyword}"' for keyword in keyword_group])
        params = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        response = request_with_retry(
            "https://export.arxiv.org/api/query",
            params=params,
            headers=headers,
            timeout=timeout,
            retries=retries,
            backoff_seconds=backoff,
        )
        root = ET.fromstring(response.text)
        for entry in root.findall("atom:entry", ARXIV_NS):
            title = normalize_whitespace(entry.findtext("atom:title", default="", namespaces=ARXIV_NS))
            abstract = normalize_whitespace(entry.findtext("atom:summary", default="", namespaces=ARXIV_NS))
            published_dt = parse_iso_datetime(entry.findtext("atom:published", default="", namespaces=ARXIV_NS))
            published = published_dt.date() if published_dt else None
            if not is_in_window(published, start, end):
                continue
            id_url = entry.findtext("atom:id", default="", namespaces=ARXIV_NS)
            arxiv_id = normalize_arxiv_id(id_url)
            doi_node = entry.find("arxiv:doi", ARXIV_NS)
            doi = normalize_doi(doi_node.text if doi_node is not None else None)
            authors = [
                normalize_whitespace(author.findtext("atom:name", default="", namespaces=ARXIV_NS))
                for author in entry.findall("atom:author", ARXIV_NS)
            ]
            authors = [author for author in authors if author]
            categories = [
                category.attrib.get("term", "")
                for category in entry.findall("atom:category", ARXIV_NS)
                if category.attrib.get("term")
            ]
            papers.append(
                Paper(
                    title=title,
                    authors=authors,
                    venue="arXiv (preprint)",
                    published_date=published,
                    doi=doi,
                    url=id_url or f"https://arxiv.org/abs/{arxiv_id}",
                    abstract=abstract,
                    keywords=categories,
                    source="arXiv",
                    is_preprint=True,
                    arxiv_id=arxiv_id,
                )
            )
        time.sleep(1.0)
    return papers


def search_crossref(config: dict[str, Any], start: datetime, end: datetime) -> list[Paper]:
    search_cfg = config.get("search", {})
    keywords = config.get("keywords", [])
    timeout = int(search_cfg.get("request_timeout_seconds", 25))
    retries = int(search_cfg.get("retries", 3))
    backoff = float(search_cfg.get("retry_backoff_seconds", 2))
    rows = min(int(search_cfg.get("max_results_per_source", 80)), 100)
    headers = build_headers(config)
    mailto = os.getenv("CROSSREF_MAILTO") or config.get("notification", {}).get("email", {}).get("to")
    papers: list[Paper] = []

    for keyword in keywords:
        params: dict[str, Any] = {
            "query.bibliographic": keyword,
            "filter": f"from-pub-date:{start.date().isoformat()},until-pub-date:{end.date().isoformat()},type:journal-article",
            "rows": rows,
            "sort": "published",
            "order": "desc",
        }
        if mailto:
            params["mailto"] = mailto
        response = request_with_retry(
            "https://api.crossref.org/works",
            params=params,
            headers=headers,
            timeout=timeout,
            retries=retries,
            backoff_seconds=backoff,
        )
        data = response.json()
        for item in data.get("message", {}).get("items", []):
            title = normalize_whitespace(" ".join(item.get("title") or []))
            if not title:
                continue
            published = parse_crossref_date(item)
            if not is_in_window(published, start, end):
                continue
            authors = []
            for author in item.get("author") or []:
                given = author.get("given", "")
                family = author.get("family", "")
                name = normalize_whitespace(f"{given} {family}")
                if name:
                    authors.append(name)
            container = item.get("container-title") or []
            venue = normalize_whitespace(container[0]) if container else "Unknown journal"
            doi = normalize_doi(item.get("DOI"))
            subjects = [normalize_whitespace(subject) for subject in item.get("subject") or []]
            url = item.get("URL") or (f"https://doi.org/{doi}" if doi else "")
            papers.append(
                Paper(
                    title=title,
                    authors=authors,
                    venue=venue,
                    published_date=published,
                    doi=doi,
                    url=url,
                    abstract=strip_markup(item.get("abstract", "")),
                    keywords=[subject for subject in subjects if subject],
                    source="Crossref",
                    is_preprint=False,
                )
            )
        time.sleep(0.2)
    return papers


def search_semantic_scholar(config: dict[str, Any], start: datetime, end: datetime) -> list[Paper]:
    search_cfg = config.get("search", {})
    keywords = config.get("keywords", [])
    timeout = int(search_cfg.get("request_timeout_seconds", 25))
    retries = int(search_cfg.get("retries", 3))
    backoff = float(search_cfg.get("retry_backoff_seconds", 2))
    limit = min(int(search_cfg.get("max_results_per_source", 80)), 100)
    headers = build_headers(config)
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key

    papers: list[Paper] = []
    fields = ",".join(
        [
            "title",
            "abstract",
            "authors",
            "venue",
            "publicationVenue",
            "publicationDate",
            "year",
            "externalIds",
            "url",
            "fieldsOfStudy",
            "publicationTypes",
            "journal",
        ]
    )
    for keyword in keywords:
        params = {"query": keyword, "limit": limit, "fields": fields}
        response = request_with_retry(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params=params,
            headers=headers,
            timeout=timeout,
            retries=retries,
            backoff_seconds=backoff,
        )
        data = response.json()
        for item in data.get("data") or []:
            title = normalize_whitespace(item.get("title") or "")
            if not title:
                continue
            published = parse_date(item.get("publicationDate"))
            if not is_in_window(published, start, end):
                continue
            authors = [normalize_whitespace(author.get("name", "")) for author in item.get("authors") or []]
            publication_venue = item.get("publicationVenue") or {}
            journal = item.get("journal") or {}
            venue = (
                publication_venue.get("name")
                or item.get("venue")
                or journal.get("name")
                or "Semantic Scholar"
            )
            external_ids = {str(key): str(value) for key, value in (item.get("externalIds") or {}).items() if value}
            doi = normalize_doi(external_ids.get("DOI"))
            arxiv_id = normalize_arxiv_id(external_ids.get("ArXiv"))
            publication_types = item.get("publicationTypes") or []
            is_preprint = bool(arxiv_id and not doi) or any("preprint" in value.lower() for value in publication_types)
            keywords_field = []
            keywords_field.extend(item.get("fieldsOfStudy") or [])
            keywords_field.extend(publication_types)
            papers.append(
                Paper(
                    title=title,
                    authors=[author for author in authors if author],
                    venue=f"{venue} (preprint)" if is_preprint and "preprint" not in venue.lower() else venue,
                    published_date=published,
                    doi=doi,
                    url=item.get("url") or (f"https://doi.org/{doi}" if doi else ""),
                    abstract=strip_markup(item.get("abstract", "")),
                    keywords=[normalize_whitespace(value) for value in keywords_field if value],
                    source="Semantic Scholar",
                    is_preprint=is_preprint,
                    arxiv_id=arxiv_id,
                    external_ids=external_ids,
                )
            )
        time.sleep(1.0 if not api_key else 0.1)
    return papers


def deduplicate_papers(papers: list[Paper]) -> list[Paper]:
    by_key: dict[str, Paper] = {}
    title_author_keys: dict[str, str] = {}

    for paper in papers:
        key = paper.primary_key
        fallback_key = title_author_key(paper)
        existing_key = key if key in by_key else title_author_keys.get(fallback_key)
        if existing_key and existing_key in by_key:
            by_key[existing_key] = merge_paper_records(by_key[existing_key], paper)
            continue
        by_key[key] = paper
        if fallback_key:
            title_author_keys[fallback_key] = key
    return list(by_key.values())


def title_author_key(paper: Paper) -> str:
    title = normalize_title(paper.title)
    first_author = ""
    if paper.authors:
        first_author = paper.authors[0].split(",")[0].split()[-1].lower()
    if not title:
        return ""
    return f"{title}|{first_author}"


def metadata_completeness(paper: Paper) -> int:
    fields = [paper.doi, paper.url, paper.abstract, paper.venue, paper.published_date]
    return sum(1 for field in fields if field)


def merge_paper_records(left: Paper, right: Paper) -> Paper:
    # Prefer journal metadata over preprint metadata, then prefer the fuller record.
    if left.is_preprint != right.is_preprint:
        primary, secondary = (right, left) if left.is_preprint else (left, right)
    elif metadata_completeness(right) > metadata_completeness(left):
        primary, secondary = right, left
    else:
        primary, secondary = left, right

    primary.doi = primary.doi or secondary.doi
    primary.arxiv_id = primary.arxiv_id or secondary.arxiv_id
    primary.url = primary.url or secondary.url
    primary.abstract = primary.abstract or secondary.abstract
    primary.authors = primary.authors or secondary.authors
    primary.keywords = sorted(set(primary.keywords + secondary.keywords))
    primary.external_ids.update(secondary.external_ids)
    return primary


def venue_is_high_impact(venue: str, whitelist: list[str]) -> bool:
    normalized = normalize_venue(venue)
    for candidate in whitelist:
        candidate_norm = normalize_venue(candidate)
        if normalized == candidate_norm or candidate_norm in normalized:
            return True
    return False


def paper_search_text(paper: Paper) -> str:
    return " ".join([paper.title, paper.abstract, " ".join(paper.keywords)])


def score_paper(paper: Paper, config: dict[str, Any]) -> Paper:
    weights = config.get("scoring", {})
    keywords = config.get("keywords", [])
    strict_keywords = config.get("strict_keywords", [])
    optical_terms = config.get("optical_anchor_terms", [])
    ml_terms = config.get("ml_anchor_terms", [])
    bonus_terms = config.get("bonus_terms", [])
    negative_terms = config.get("negative_terms", [])
    high_impact_venues = config.get("high_impact_venues", [])

    title_text = paper.title
    abstract_text = paper.abstract
    keyword_text = " ".join(paper.keywords)
    combined_text = paper_search_text(paper)

    title_strict = matched_terms(title_text, strict_keywords)
    title_keywords = matched_terms(title_text, keywords)
    abstract_keywords = matched_terms(abstract_text, keywords)
    keyword_matches = matched_terms(keyword_text, keywords + strict_keywords + bonus_terms)
    optical_matches = matched_terms(combined_text, optical_terms)
    ml_matches = matched_terms(combined_text, ml_terms)
    strict_matches = matched_terms(combined_text, strict_keywords)
    negative_matches = matched_terms(combined_text, negative_terms)
    bonus_matches = matched_terms(combined_text, bonus_terms)

    score = 0
    reasons: list[str] = []
    high_impact = venue_is_high_impact(paper.venue, high_impact_venues)
    if high_impact:
        score += int(weights.get("high_impact_venue", 35))
        reasons.append("高水平期刊")
    if title_strict:
        score += int(weights.get("title_strict_keyword", 35))
        reasons.append(f"title 精确命中: {', '.join(title_strict)}")
    if title_keywords:
        score += min(36, len(title_keywords) * int(weights.get("title_keyword", 18)))
        reasons.append(f"title 关键词: {', '.join(title_keywords)}")
    if abstract_keywords:
        score += min(40, len(abstract_keywords) * int(weights.get("abstract_keyword", 8)))
        reasons.append(f"abstract 关键词: {', '.join(abstract_keywords[:5])}")
    if keyword_matches:
        score += min(20, len(keyword_matches) * int(weights.get("keyword_field_match", 10)))
        reasons.append(f"keywords 命中: {', '.join(keyword_matches[:5])}")
    if bonus_matches:
        score += min(40, len(bonus_matches) * int(weights.get("optical_context_bonus", 8)))
        reasons.append(f"光学上下文: {', '.join(bonus_matches[:6])}")
    if paper.is_preprint:
        score += int(weights.get("arxiv_preprint", 3))
        reasons.append("arXiv/preprint")

    has_strict = bool(strict_matches)
    has_optical_ml_context = bool(optical_matches and ml_matches)
    if not has_strict and not has_optical_ml_context:
        score += int(weights.get("no_optical_anchor_penalty", -45))
        reasons.append("缺少光学衍射 + 神经网络共同上下文")
    if negative_matches and not has_strict:
        score += int(weights.get("generic_ml_penalty", -35))
        reasons.append(f"可能为普通机器学习误报: {', '.join(negative_matches[:4])}")

    paper.score = score
    paper.score_reasons = reasons
    paper.matched_keywords = sorted(set(title_keywords + abstract_keywords + keyword_matches + strict_matches + bonus_matches))
    paper.high_impact = high_impact
    paper.reading_level = reading_level_for_score(paper)
    return paper


def reading_level_for_score(paper: Paper) -> str:
    if paper.score >= 90 or (paper.high_impact and paper.score >= 75):
        return "强烈推荐"
    if paper.score >= 65:
        return "推荐"
    return "可略读"


class SeenStore:
    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = path
        self.data = data
        self.data.setdefault("seen", {})

    @classmethod
    def load(cls, path: Path) -> "SeenStore":
        if not path.exists():
            return cls(path, {"seen": {}})
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            data = {"seen": {key: {"legacy": True} for key in data}}
        return cls(path, data)

    def has(self, paper: Paper) -> bool:
        return paper.primary_key in self.data["seen"]

    def add(self, paper: Paper, sent_at: datetime) -> None:
        self.data["seen"][paper.primary_key] = {
            "title": paper.title,
            "venue": paper.venue,
            "date": paper.published_date.isoformat() if paper.published_date else None,
            "url": paper.url,
            "sent_at": sent_at.isoformat(),
        }

    def save(self) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(self.path)


def fallback_summary(paper: Paper) -> None:
    abstract = paper.abstract or "摘要暂缺"
    first_sentence = re.split(r"(?<=[.!?。！？])\s+", abstract)[0]
    first_sentence = first_sentence[:260]
    paper.research_object = infer_research_object(paper)
    paper.relation_to_d2nn = infer_relation_to_d2nn(paper)
    paper.main_innovation = infer_main_innovation(paper)
    paper.chinese_summary = (
        f"本文关注{paper.research_object}。根据题名和摘要，工作{paper.relation_to_d2nn}。"
        f"摘要要点：{first_sentence}"
    )


def infer_research_object(paper: Paper) -> str:
    text = paper_search_text(paper).lower()
    if "metasurface" in text:
        return "基于超表面的光学神经网络或光计算器件"
    if "phase mask" in text or "spatial light modulator" in text or "slm" in text:
        return "相位掩膜 / SLM 驱动的自由空间光学计算系统"
    if "diffractive" in text or "diffraction" in text:
        return "衍射光学神经网络或衍射光计算系统"
    if "photonic" in text:
        return "光子神经网络或光子计算系统"
    return "光学机器学习与神经网络相关系统"


def infer_relation_to_d2nn(paper: Paper) -> str:
    text = paper_search_text(paper).lower()
    if "d2nn" in text or "diffractive neural network" in text or "diffractive deep neural network" in text:
        return "直接研究 D2NN / 衍射神经网络"
    if "diffractive" in text and ("neural" in text or "machine learning" in text):
        return "与衍射光学和神经网络建模密切相关"
    if "optical computing" in text or "all-optical" in text:
        return "属于全光推理或光计算方向，可作为 D2NN 相关背景重点关注"
    return "与 D2NN 的关系可能偏间接，建议先快速浏览摘要确认"


def infer_main_innovation(paper: Paper) -> str:
    abstract = paper.abstract or ""
    sentences = re.split(r"(?<=[.!?])\s+", abstract)
    innovation_words = ("propose", "demonstrate", "novel", "first", "achieve", "experimental", "metasurface", "all-optical")
    for sentence in sentences:
        if any(word in sentence.lower() for word in innovation_words):
            return strip_markup(sentence)[:320]
    if abstract:
        return strip_markup(abstract)[:320]
    return "摘要暂缺，需打开论文页面进一步确认创新点。"


def enrich_summaries(papers: list[Paper], config: dict[str, Any], disable_openai: bool = False) -> None:
    summary_cfg = config.get("summary", {})
    use_openai = bool(summary_cfg.get("use_openai", True)) and not disable_openai
    api_key = os.getenv("OPENAI_API_KEY")
    if not use_openai or not api_key or OpenAI is None:
        for paper in papers:
            fallback_summary(paper)
        return

    model_env = summary_cfg.get("model_env", "OPENAI_MODEL")
    model = os.getenv(model_env) or summary_cfg.get("default_model", "gpt-4.1-mini")
    max_abstract_chars = int(summary_cfg.get("max_abstract_chars", 1800))
    client = OpenAI(api_key=api_key)

    for paper in papers:
        prompt = {
            "title": paper.title,
            "authors": paper.authors[:10],
            "venue": paper.venue,
            "date": paper.published_date.isoformat() if paper.published_date else None,
            "doi_or_url": paper.doi or paper.url,
            "abstract": paper.abstract[:max_abstract_chars],
            "keywords": paper.keywords,
            "score": paper.score,
            "score_reasons": paper.score_reasons,
            "is_preprint": paper.is_preprint,
        }
        try:
            completion = client.chat.completions.create(
                model=model,
                temperature=0.2,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是光学计算和衍射神经网络方向的论文速读助手。"
                            "请只输出 JSON，不要输出 Markdown。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "请为下面论文生成简短中文解读，JSON 字段必须包括："
                            "chinese_summary, research_object, relation_to_d2nn, main_innovation。"
                            "重点判断它和 D2NN / 衍射光学神经网络 / 全光推理的关系。\n\n"
                            f"{json.dumps(prompt, ensure_ascii=False)}"
                        ),
                    },
                ],
            )
            content = completion.choices[0].message.content or "{}"
            parsed = json.loads(content)
            paper.chinese_summary = normalize_whitespace(parsed.get("chinese_summary", ""))
            paper.research_object = normalize_whitespace(parsed.get("research_object", ""))
            paper.relation_to_d2nn = normalize_whitespace(parsed.get("relation_to_d2nn", ""))
            paper.main_innovation = normalize_whitespace(parsed.get("main_innovation", ""))
            if not paper.chinese_summary:
                fallback_summary(paper)
        except Exception as exc:
            LOGGER.warning("OpenAI summary failed for %s: %s", paper.title, exc)
            fallback_summary(paper)


def fetch_all_papers(config: dict[str, Any], start: datetime, end: datetime) -> tuple[list[Paper], list[str]]:
    source_config = config.get("search", {}).get("sources", {})
    fetchers = [
        ("arXiv", source_config.get("arxiv", True), search_arxiv),
        ("Crossref", source_config.get("crossref", True), search_crossref),
        ("Semantic Scholar", source_config.get("semantic_scholar", True), search_semantic_scholar),
    ]
    papers: list[Paper] = []
    errors: list[str] = []
    for name, enabled, fetcher in fetchers:
        if not enabled:
            continue
        try:
            LOGGER.info("Searching %s", name)
            source_papers = fetcher(config, start, end)
            LOGGER.info("%s returned %s candidate papers", name, len(source_papers))
            papers.extend(source_papers)
        except Exception as exc:
            LOGGER.exception("%s search failed", name)
            errors.append(f"{name}: {exc}")
    return papers, errors


def filter_and_rank(papers: list[Paper], config: dict[str, Any]) -> list[Paper]:
    threshold = int(config.get("score_threshold", 55))
    scored = [score_paper(paper, config) for paper in deduplicate_papers(papers)]
    accepted = [paper for paper in scored if paper.score >= threshold]
    accepted.sort(
        key=lambda paper: (
            paper.reading_level == "强烈推荐",
            paper.high_impact,
            paper.score,
            paper.published_date or date.min,
        ),
        reverse=True,
    )
    return accepted


def format_authors(authors: list[str], limit: int = 8) -> str:
    if not authors:
        return "Unknown authors"
    if len(authors) <= limit:
        return ", ".join(authors)
    return ", ".join(authors[:limit]) + f", et al. ({len(authors)} authors)"


def build_email(papers: list[Paper], config: dict[str, Any], errors: list[str], run_date: date) -> tuple[str, str]:
    max_papers = int(config.get("max_papers_in_email", 20))
    papers = papers[:max_papers]
    subject = f"[Daily D2NN Papers] {run_date.isoformat()} 最新衍射神经网络论文"
    high_impact_count = sum(1 for paper in papers if paper.high_impact)
    preprint_count = sum(1 for paper in papers if paper.is_preprint)
    top_papers = papers[:3]

    lines: list[str] = []
    lines.append("今日总结：")
    lines.append(f"- 今日检索到 {len(papers)} 篇相关论文")
    lines.append(f"- 其中顶刊 / 高水平期刊 {high_impact_count} 篇")
    lines.append(f"- arXiv / preprint {preprint_count} 篇")
    if top_papers:
        lines.append("- 最值得关注的 1-3 篇：")
        for index, paper in enumerate(top_papers, 1):
            lines.append(f"  {index}. {paper.title}（{paper.venue}，{paper.reading_level}，score={paper.score}）")
    else:
        lines.append("- 今日暂无新的顶刊论文")
        lines.append("")
        lines.append("今日暂无新的顶刊论文。最近窗口内没有发现超过阈值且未推送过的 D2NN / 衍射神经网络相关论文。")

    if errors:
        lines.append("")
        lines.append("检索警告：")
        for error in errors:
            lines.append(f"- {error}")

    if papers:
        lines.append("")
        lines.append("论文列表：")

    for index, paper in enumerate(papers, 1):
        venue = paper.venue
        if paper.is_preprint and "preprint" not in venue.lower():
            venue = f"{venue} (preprint)"
        doi_or_url = f"https://doi.org/{paper.doi}" if paper.doi else paper.url
        if paper.arxiv_id and not paper.doi:
            doi_or_url = paper.url or f"https://arxiv.org/abs/{paper.arxiv_id}"
        lines.append("")
        lines.append(f"{index}. Title: {paper.title}")
        lines.append(f"   Authors: {format_authors(paper.authors)}")
        lines.append(f"   Venue: {venue}")
        lines.append(f"   Date: {paper.published_date.isoformat() if paper.published_date else 'Unknown'}")
        lines.append(f"   DOI / URL: {doi_or_url or 'Unknown'}")
        lines.append(f"   中文摘要：{paper.chinese_summary}")
        lines.append(f"   研究对象：{paper.research_object}")
        lines.append(f"   和衍射神经网络的关系：{paper.relation_to_d2nn}")
        lines.append(f"   主要创新：{paper.main_innovation}")
        lines.append(f"   推荐阅读等级：{paper.reading_level}")
        lines.append(f"   关键词命中：{', '.join(paper.matched_keywords) if paper.matched_keywords else '无'}")
        lines.append(f"   评分：{paper.score}；依据：{'; '.join(paper.score_reasons)}")

    return subject, "\n".join(lines).strip() + "\n"


class EmailNotifier:
    def __init__(self, config: dict[str, Any]):
        email_cfg = config.get("notification", {}).get("email", {})
        self.to_addr = os.getenv(email_cfg.get("to_env", "MAIL_TO")) or email_cfg.get("to")
        self.smtp_host = os.getenv(email_cfg.get("smtp_host_env", "SMTP_HOST")) or email_cfg.get("smtp_host")
        smtp_port_value = os.getenv(email_cfg.get("smtp_port_env", "SMTP_PORT")) or email_cfg.get("smtp_port") or 465
        self.smtp_port = int(smtp_port_value)
        self.smtp_user = os.getenv(email_cfg.get("smtp_user_env", "SMTP_USER"))
        self.smtp_password = os.getenv(email_cfg.get("smtp_password_env", "SMTP_PASSWORD"))
        self.from_addr = os.getenv(email_cfg.get("from_env", "MAIL_FROM")) or self.smtp_user
        self.use_ssl = bool(email_cfg.get("use_ssl", True))
        self.starttls = bool(email_cfg.get("starttls", False))

    def validate(self) -> None:
        missing = []
        for name, value in [
            ("SMTP_HOST", self.smtp_host),
            ("SMTP_PORT", self.smtp_port),
            ("SMTP_USER", self.smtp_user),
            ("SMTP_PASSWORD", self.smtp_password),
            ("MAIL_TO/config notification.email.to", self.to_addr),
        ]:
            if not value:
                missing.append(name)
        if missing:
            raise RuntimeError(f"Missing email settings: {', '.join(missing)}")

    def send(self, subject: str, body: str) -> None:
        self.validate()
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = subject
        message["From"] = self.from_addr
        message["To"] = self.to_addr
        message["Date"] = email.utils.formatdate(localtime=True)

        if self.use_ssl:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30) as smtp:
                smtp.login(self.smtp_user, self.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
                if self.starttls:
                    smtp.starttls()
                smtp.login(self.smtp_user, self.smtp_password)
                smtp.send_message(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily D2NN / diffractive neural network paper monitor")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Path to config.yaml")
    parser.add_argument("--seen", type=Path, default=Path("seen_papers.json"), help="Path to seen_papers.json")
    parser.add_argument("--dry-run", action="store_true", help="Print email but do not send or update seen_papers.json")
    parser.add_argument("--days", type=int, default=None, help="Override lookback_days")
    parser.add_argument("--no-openai", action="store_true", help="Disable OpenAI summaries for this run")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    config = load_config(args.config)
    start, end = date_window(config, args.days)
    run_date = now_in_timezone(config.get("timezone", "Asia/Shanghai")).date()
    LOGGER.info("Searching papers from %s to %s", start.isoformat(), end.isoformat())

    seen = SeenStore.load(args.seen)
    raw_papers, errors = fetch_all_papers(config, start, end)
    accepted = filter_and_rank(raw_papers, config)
    new_papers = [paper for paper in accepted if not seen.has(paper)]
    LOGGER.info("Accepted %s papers, %s are new after seen-state filtering", len(accepted), len(new_papers))

    enrich_summaries(new_papers, config, disable_openai=args.no_openai)
    subject, body = build_email(new_papers, config, errors, run_date)

    if args.dry_run:
        print("\n" + "=" * 80)
        print(subject)
        print("=" * 80)
        print(body)
        return 0

    notifier = EmailNotifier(config)
    notifier.send(subject, body)
    LOGGER.info("Email sent to configured recipient")

    sent_at = now_in_timezone(config.get("timezone", "Asia/Shanghai"))
    for paper in new_papers:
        seen.add(paper, sent_at)
    if new_papers:
        seen.save()
        LOGGER.info("Updated %s with %s new papers", args.seen, len(new_papers))
    else:
        LOGGER.info("No new papers; seen state unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
