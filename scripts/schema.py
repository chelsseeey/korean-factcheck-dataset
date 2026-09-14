"""
데이터 스키마 정의.

설계 원칙
- 기사 본문 전문은 최종 데이터셋에 저장하지 않는다. 캐시에만 두고,
  배포 대상에는 URL / 제목 / 언론사 / 일자 / 검증 대상 문장만 남긴다.
- claim_type 은 반드시 채운다. 논문 3.3(복합 claim subset 분석)에서 필요하며,
  나중에 붙이려면 전체를 다시 훑어야 한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

# --- 라벨 ------------------------------------------------------------------
# SUPPORTED : 해명자료가 해당 주장을 사실로 인정
# REFUTED   : 해명자료가 해당 주장을 반박
# NEI       : 근거로 판단 불가 (Not Enough Info)
Label = Literal["SUPPORTED", "REFUTED", "NEI"]

# --- claim 유형 ------------------------------------------------------------
# numeric  : 수치/금액/비율 주장        예) "예산이 3조원 삭감됐다"
# temporal : 시점/기간 주장             예) "내년 1월부터 시행된다"
# causal   : 인과/영향 주장             예) "규제 탓에 투자가 줄었다"
# simple   : 단일 사실 주장             예) "A부처가 B를 발표했다"
# compound : 위 요소가 둘 이상 결합     예) "작년 예산 3조를 깎아 투자가 줄었다"
ClaimType = Literal["numeric", "temporal", "causal", "simple", "compound"]

SCHEMA_VERSION = "1.1"


@dataclass
class Briefing:
    """정책브리핑 「사실은 이렇습니다」 해명자료 1건 (수집 단계 산출물)."""

    briefing_id: str          # 상세 페이지 newsId 등 고유 식별자
    url: str
    title: str
    published_at: str         # YYYY-MM-DD
    ministry: str = ""        # 해명 부처
    body: str = ""            # 해명 본문 (정부 저작물. 캐시 및 근거 추출에 사용)
    quoted_report: str = ""   # 본문에 인용된 '보도 내용' 블록 (축소안에서 claim 소스)
    source_hint: str = ""     # 본문에서 언급된 언론사/보도일자 원문 표현
    collected_at: str = ""
    schema_version: str = SCHEMA_VERSION
    # claim 추출 입력으로 쓸 수 있는 건인지. False 여도 삭제하지 않고 플래그만 둔다.
    usable: bool = True
    usable_reason: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class Article:
    """해명자료가 지목한 대상 기사. 본문은 저장하지 않는다."""

    article_id: str
    url: str
    title: str
    press: str = ""
    published_at: str = ""
    match_score: float = 0.0
    match_method: str = ""    # "explicit_link" | "search" | "manual"


@dataclass
class Claim:
    """검증 대상 주장 1건."""

    claim_id: str
    briefing_id: str
    article_id: Optional[str]     # 축소안(기사 매칭 생략)에서는 None
    claim_sentence: str
    claim_type: ClaimType
    label: Label
    evidence_sentence: str = ""   # 해명자료 내 근거 문장
    evidence_url: str = ""
    evidence_ministry: str = ""
    evidence_published_at: str = ""
    note: str = ""


@dataclass
class Record:
    """최종 배포 단위: 해명자료 1건 + 대상 기사 + claim 목록."""

    briefing_id: str
    briefing_url: str
    briefing_title: str
    briefing_published_at: str
    ministry: str
    article: Optional[Article]
    claims: list[Claim] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# --- 검증 ------------------------------------------------------------------

REQUIRED_CLAIM_FIELDS = ("claim_sentence", "claim_type", "label")
VALID_LABELS = {"SUPPORTED", "REFUTED", "NEI"}
VALID_TYPES = {"numeric", "temporal", "causal", "simple", "compound"}


def validate_record(rec: dict) -> list[str]:
    """레코드 1건의 문제점을 문자열 리스트로 반환. 비어 있으면 정상."""
    errs: list[str] = []

    if not rec.get("briefing_id"):
        errs.append("briefing_id 없음")
    if not rec.get("briefing_url", "").startswith("http"):
        errs.append("briefing_url 형식 이상")

    claims = rec.get("claims") or []
    if not claims:
        errs.append("claim 0개")

    for i, c in enumerate(claims):
        for f in REQUIRED_CLAIM_FIELDS:
            if not c.get(f):
                errs.append(f"claim[{i}] {f} 비어있음")
        if c.get("label") and c["label"] not in VALID_LABELS:
            errs.append(f"claim[{i}] 라벨 값 이상: {c['label']}")
        if c.get("claim_type") and c["claim_type"] not in VALID_TYPES:
            errs.append(f"claim[{i}] 유형 값 이상: {c['claim_type']}")
        if len(c.get("claim_sentence", "")) < 10:
            errs.append(f"claim[{i}] 문장이 너무 짧음")
        # 기사 본문 전문이 흘러들어간 경우를 잡는다
        if len(c.get("claim_sentence", "")) > 500:
            errs.append(f"claim[{i}] 문장이 500자 초과 (본문이 섞였을 가능성)")

    return errs


def label_distribution(records: list[dict]) -> dict:
    """라벨/유형 분포 요약. 파일럿 판단과 논문 표에 그대로 쓴다."""
    labels: dict[str, int] = {}
    types: dict[str, int] = {}
    n_claims = 0
    matched = 0

    for r in records:
        if r.get("article"):
            matched += 1
        for c in r.get("claims", []):
            n_claims += 1
            labels[c.get("label", "?")] = labels.get(c.get("label", "?"), 0) + 1
            types[c.get("claim_type", "?")] = types.get(c.get("claim_type", "?"), 0) + 1

    n = len(records)
    return {
        "briefings": n,
        "articles_matched": matched,
        "match_rate": round(matched / n, 3) if n else 0.0,
        "claims": n_claims,
        "claims_per_briefing": round(n_claims / n, 2) if n else 0.0,
        "labels": labels,
        "claim_types": types,
    }


def load_jsonl(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python schema.py <records.jsonl>")
        raise SystemExit(1)

    recs = load_jsonl(sys.argv[1])
    bad = 0
    for r in recs:
        errs = validate_record(r)
        if errs:
            bad += 1
            print(f"[{r.get('briefing_id')}] " + "; ".join(errs))

    print("\n--- 요약 ---")
    print(json.dumps(label_distribution(recs), ensure_ascii=False, indent=2))
    print(f"\n검증 실패 레코드: {bad}/{len(recs)}")
