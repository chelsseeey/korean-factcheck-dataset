"""
quoted_report(보도 내용)를 원자 claim으로 분해하고, 정부 입장과 대조해 라벨을 붙인다.

핵심 설계
- "이 보도가 사실인가"를 한 번에 묻지 않는다. 그렇게 물으면 전부 REFUTED 가 된다.
  보도 내용을 원자 단위로 쪼갠 뒤 각각을 정부 입장과 대조해야
  SUPPORTED / NEI 가 자연스럽게 나온다.
- 라벨 근거가 되는 정부 입장 문장을 반드시 원문 그대로 인용하게 한다.
  인용이 본문에 없으면 그 claim 은 폐기한다(환각 방지).

사용법
  export ANTHROPIC_API_KEY=...
  python extract_claims.py --in data/briefings.jsonl --out data/records.jsonl --limit 20
  python extract_claims.py --in data/briefings.jsonl --out /dev/stdout --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

# --- 본문 분할 --------------------------------------------------------------

# 정부 입장/설명 섹션의 시작 표지
EXPLAIN_MARKERS = [
    "정부 입장", "정부입장", "부처 입장", "설명 내용", "해명 내용",
    "사실관계", "이에 대해",
]
EXPLAIN_PATTERN = re.compile(r"<\s*[가-힣]{2,10}(?:부|처|청|위원회)\s*설명\s*>")

PRESS_RE = re.compile(
    r"([가-힣A-Za-z]{2,6}\s?(?:일보|신문|경제|방송|뉴스|타임스|투데이|TV|일보사))"
)
DATE_RE = re.compile(r"(\d{1,2})\s*[.\-월]\s*(\d{1,2})\s*일?")


def split_explanation(body: str, quoted_report: str) -> str:
    """정부 입장 부분만 잘라낸다. quoted_report 이후 구간에서 찾는다."""
    start = 0
    if quoted_report:
        idx = body.find(quoted_report[:60])
        if idx != -1:
            start = idx + len(quoted_report)

    rest = body[start:]
    m = EXPLAIN_PATTERN.search(rest)
    cands = [m.start()] if m else []
    cands += [rest.find(k) for k in EXPLAIN_MARKERS if rest.find(k) != -1]
    if cands:
        rest = rest[min(cands):]
    return rest[:6000].strip()


def parse_source(quoted_report: str, fallback: str = "") -> tuple[str, str]:
    """보도 내용 블록에서 언론사명과 보도일자를 뽑는다."""
    text = quoted_report or fallback
    press = ""
    m = PRESS_RE.search(text)
    if m:
        press = re.sub(r"\s+", "", m.group(1))
    date = ""
    d = DATE_RE.search(text[:200])
    if d:
        date = f"{int(d.group(1)):02d}-{int(d.group(2)):02d}"
    return press, date


# --- 프롬프트 ---------------------------------------------------------------

SYSTEM = """너는 한국어 사실 검증 데이터셋 구축을 돕는 주석자다.
정부 해명자료 한 건이 주어진다. 자료는 두 부분으로 되어 있다.

[보도 내용] 언론이 보도했다고 정부가 정리한 내용
[정부 입장] 그에 대한 정부의 설명

할 일은 두 단계다.

1단계 — 분해
[보도 내용]을 검증 가능한 원자 주장으로 쪼갠다.
- 원자 주장은 참/거짓을 따로 판단할 수 있는 최소 단위여야 한다.
- 한 문장에 수치와 인과가 함께 있으면 나눈다.
- 의견, 전망, 평가("우려된다", "비판이 나온다")는 제외한다.
- 원문 표현을 최대한 살리되, 주어가 생략됐으면 복원한다.
- 보통 1~4개가 나온다. 억지로 늘리지 않는다.

2단계 — 대조
각 원자 주장을 [정부 입장]과 대조해 라벨을 정한다.
- SUPPORTED : 정부 입장이 그 내용을 사실로 인정
- REFUTED   : 정부 입장이 그 내용을 부정하거나 사실과 다르다고 함
- NEI       : 정부 입장이 그 내용을 다루지 않음

중요
- evidence 는 [정부 입장]에 실제로 있는 문장을 그대로 옮긴다. 요약하거나 지어내지 않는다.
- NEI 인 경우 evidence 는 빈 문자열로 둔다.
- 정부가 부분적으로 인정하는 경우("~은 사실이나 ~는 다르다")를 놓치지 마라.
  이런 경우 인정된 부분은 SUPPORTED 가 된다.

claim_type 은 다음 중 하나다.
- numeric  : 수치, 금액, 비율
- temporal : 시점, 기간, 순서
- causal   : 인과, 영향
- simple   : 단일 사실
- compound : 위 요소가 둘 이상 결합되어 한 문장에 남은 경우

JSON 만 출력한다. 코드펜스나 설명을 붙이지 마라.
{"claims": [{"claim_sentence": "...", "claim_type": "...", "label": "...", "evidence_sentence": "..."}]}
"""

USER_TMPL = """[보도 내용]
{report}

[정부 입장]
{explanation}
"""


def call_api(report: str, explanation: str, model: str, retries: int = 3) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    for attempt in range(retries):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1500,
                temperature=0,
                system=SYSTEM,
                messages=[{
                    "role": "user",
                    "content": USER_TMPL.format(report=report, explanation=explanation),
                }],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"  JSON 파싱 실패({attempt + 1}): {e}", file=sys.stderr)
        except Exception as e:
            print(f"  API 실패({attempt + 1}): {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
    return {"claims": []}


# --- 검증 -------------------------------------------------------------------

VALID_LABELS = {"SUPPORTED", "REFUTED", "NEI"}
VALID_TYPES = {"numeric", "temporal", "causal", "simple", "compound"}


def verify_claims(claims: list[dict], explanation: str) -> tuple[list[dict], list[str]]:
    """스키마와 근거 인용을 검사한다. 인용이 본문에 없으면 버린다."""
    kept, dropped = [], []
    norm_exp = re.sub(r"\s+", "", explanation)

    for c in claims:
        label = c.get("label", "")
        ev = (c.get("evidence_sentence") or "").strip()

        if label not in VALID_LABELS:
            dropped.append(f"라벨 이상: {label}")
            continue
        if c.get("claim_type") not in VALID_TYPES:
            dropped.append(f"유형 이상: {c.get('claim_type')}")
            continue
        if len(c.get("claim_sentence", "")) < 10:
            dropped.append("claim 너무 짧음")
            continue

        if label == "NEI":
            c["evidence_sentence"] = ""
        else:
            if not ev:
                dropped.append(f"{label} 인데 근거 없음")
                continue
            # 근거 문장이 실제 본문에 있는지 (공백 무시 부분일치)
            probe = re.sub(r"\s+", "", ev)[:40]
            if probe not in norm_exp:
                dropped.append(f"근거가 본문에 없음: {ev[:40]}")
                continue
        kept.append(c)

    return kept, dropped


# --- 메인 -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/briefings.jsonl")
    ap.add_argument("--out", default="data/records.jsonl")
    ap.add_argument("--limit", type=int, default=0, help="0이면 전체")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--dry-run", action="store_true",
                    help="API 호출 없이 본문 분할 결과만 확인")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.inp, encoding="utf-8") if l.strip()]
    rows = [r for r in rows if r.get("usable", True)]
    if args.limit:
        rows = rows[:args.limit]

    stats = {"in": len(rows), "claims": 0, "dropped": 0, "no_report": 0,
             "labels": {}, "types": {}, "press_found": 0}
    out_rows = []

    for i, b in enumerate(rows, 1):
        report = b.get("quoted_report", "")
        if not report:
            stats["no_report"] += 1
            print(f"[{i}/{len(rows)}] {b['briefing_id']} 보도내용 없음 — 건너뜀")
            continue

        explanation = split_explanation(b.get("body", ""), report)
        press, rdate = parse_source(report, b.get("source_hint", ""))
        if press:
            stats["press_found"] += 1

        if args.dry_run:
            print(f"\n=== [{i}] {b['briefing_id']} | {press or '언론사?'} {rdate}")
            print(f"보도내용 : {report[:150]}")
            print(f"정부입장 : {explanation[:150]}")
            continue

        print(f"[{i}/{len(rows)}] {b['briefing_id']} {press} …", end="", flush=True)
        result = call_api(report, explanation, args.model)
        kept, dropped = verify_claims(result.get("claims", []), explanation)
        stats["dropped"] += len(dropped)
        print(f" claim {len(kept)}개 (폐기 {len(dropped)})")
        for d in dropped:
            print(f"    · {d}")

        claims = []
        for j, c in enumerate(kept):
            claims.append({
                "claim_id": f"{b['briefing_id']}-{j}",
                "briefing_id": b["briefing_id"],
                "article_id": None,
                "claim_sentence": c["claim_sentence"],
                "claim_type": c["claim_type"],
                "label": c["label"],
                "evidence_sentence": c.get("evidence_sentence", ""),
                "evidence_url": b["url"],
                "evidence_ministry": b.get("ministry", ""),
                "evidence_published_at": b.get("published_at") or "unknown",
                "note": "",
            })
            stats["claims"] += 1
            stats["labels"][c["label"]] = stats["labels"].get(c["label"], 0) + 1
            stats["types"][c["claim_type"]] = stats["types"].get(c["claim_type"], 0) + 1

        out_rows.append({
            "briefing_id": b["briefing_id"],
            "briefing_url": b["url"],
            "briefing_title": b.get("title", ""),
            "briefing_published_at": b.get("published_at", ""),
            "ministry": b.get("ministry", ""),
            "reported_press": press,
            "reported_date": rdate,
            "article": None,
            "claims": claims,
            "schema_version": "1.0",
        })

    if args.dry_run:
        print(f"\n보도내용 없음 {stats['no_report']}건 / 언론사 식별 {stats['press_found']}건")
        return

    with open(args.out, "w", encoding="utf-8") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n--- 요약 ---")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    n = stats["claims"]
    if n:
        ref = stats["labels"].get("REFUTED", 0)
        print(f"\nREFUTED 비율: {ref / n:.1%}  (85% 넘으면 라벨 불균형 대응 필요)")


if __name__ == "__main__":
    main()
