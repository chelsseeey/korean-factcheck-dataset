"""
정책브리핑 「사실은 이렇습니다」 수집기.

목록: https://www.korea.kr/briefing/actuallyList.do
상세: .../actuallyView.do?newsId=...

셀렉터는 사이트 구조에 따라 달라진다. 그래서 먼저 --probe 로 구조를 확인하고
SELECTORS 만 고친 뒤 본 수집을 돌리는 흐름으로 쓴다.

사용법
  1) 구조 확인   python collect_briefings.py --probe
  2) 파일럿 20건 python collect_briefings.py --limit 20 --out data/briefings.jsonl
  3) 확장        python collect_briefings.py --limit 300 --out data/briefings.jsonl --resume
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs

import io

import requests
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None
try:
    from pdfminer.high_level import extract_text as _pdfminer_text
except ImportError:  # pragma: no cover
    _pdfminer_text = None

from schema import Briefing

BASE = "https://www.korea.kr"
LIST_URL = f"{BASE}/briefing/actuallyList.do"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# --probe 결과를 보고 이 부분만 고치면 된다.
SELECTORS = {
    "detail_link": r"actuallyView\.do",          # 상세 링크 href 패턴 (정규식)
    "title": [".article_head h1", "h1"],
    # .article_head .info 의 첫 span 이 게시일 (예: "2026.09.12")
    "date": [".article_head .info span", ".info span"],
    # .gotosite 안에 <i class="tooltip">부처별 뉴스 이동</i> 이 섞여 나온다.
    # (.ministry 는 푸터의 '행정부처 바로가기' 라 쓰면 안 된다)
    "ministry": [".article_head .info .gotosite", ".gotosite"],
    # 주의: 아래 두 컨테이너는 상세 HTML 에 빈 껍데기로만 존재한다.
    # 실제 본문은 iframe#content_press (Synap 문서뷰어) 가 그려서 HTML 에 없다.
    # 그래서 본문은 첨부 PDF(/common/download.do?fileId=...) 에서 추출한다.
    "body": [".article_body", ".view_cont"],
    # 목록의 실제 기사 항목. 이 범위를 벗어난 링크는 '많이 본 뉴스' 사이드바다.
    "list_item": ".list_type li",
}

# 본문에서 '보도 내용' 블록을 찾을 때 쓰는 표지
# 실제 PDF 에서 관찰된 표기: "< 보도 매체 >", "< 보도내용 >", "< 보도 내용 >", "1. 관련 기사"
REPORT_MARKERS = [
    "보도 내용", "보도내용", "보도 매체", "보도매체",
    "보도 주요 내용", "보도 주요내용", "보도주요내용",
    "기사 주요 내용", "기사 주요내용",
    "관련 기사", "기사 내용", "언론 보도", "보도에 대한",
]
# 보도 블록의 끝(= 정부 해명 시작) 표지
EXPLAIN_MARKERS = [
    "정부 입장", "정부입장", "부처 입장", "부처입장",
    "설명 내용", "설명내용", "해명 내용", "해명내용",
]
# "< 농림축산식품부 설명 >" 처럼 부처명이 박힌 해명 머리말은 개별 문자열로
# 다 적을 수 없어서 패턴으로 잡는다.
EXPLAIN_PATTERN = re.compile(
    r"[<＜(\[]\s*[가-힣]{2,12}(?:부|처|청|위원회|위|원)\s*(?:설명|입장|해명)\s*[>＞)\]]"
    r"|\d+\.\s*(?:설명|해명)\s*(?:내용|드립니다)"
)

# body 끝의 담당자 실명·전화·이메일 블록. 저장 전에 잘라낸다.
# "담당 부서 …", "문의 : …", "담당자 …" 세 가지 머리말이 관찰됐다.
# 문서 끝뿐 아니라 중간(1쪽 말미)에도 박혀 있고, 그 뒤로 '참고' 본문이 이어지는
# 경우가 있다. 그래서 뒤를 통째로 자르지 않고 연락처 덩어리만 도려낸다.
CONTACT_BLOCK = re.compile(
    r"(?:담\s*당\s*부\s*서|문\s*의\s*(?:처)?\s*[::]?|담\s*당\s*자|책\s*임\s*자)"
    r"[^()]{0,60}\([^()]{0,40}(?:@[\w.-]+|\d{2,4}-\d{3,4}(?:-\d{4})?)[^()]{0,20}\)"
    r"(?:[^()]{0,80}\([^()]{0,40}(?:@[\w.-]+|\d{2,4}-\d{3,4}(?:-\d{4})?)[^()]{0,20}\))*"
)

# 사용 가능 판정용: 'ㅇ/○' 불릿 본문, 보도 서술어
BULLET_PAT = re.compile(r"[ㅇ○]\s*\S")
REPORT_PREDICATE = re.compile(
    r"(보도|주장|지적|비판|우려|제기|밝혔|평가)"
    r"(하였|했|되었|된|하고|이라|한 것|합니다|였습니다|였다|하며|한다|가 |를 |을 |도 )"
)
MIN_USABLE_LEN = 150
# 언론사/보도일자 힌트 추출용
PRESS_HINT = re.compile(
    r"(\d{1,2}월\s*\d{1,2}일)?\s*([가-힣A-Za-z]{2,10}(?:일보|신문|경제|방송|뉴스|타임스|투데이|TV|BS|BC))"
)


# --- HTTP ------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch(session: requests.Session, url: str, params: dict | None = None,
          delay: float = 1.0, retries: int = 3, data: dict | None = None,
          method: str = "GET") -> str | None:
    """예의 있는 GET/POST. 실패해도 죽지 않고 None 을 돌려준다.

    목록 페이지네이션은 GET 으로 동작하지 않는다. pageLink(n) 이 mainForm 을
    POST 서브밋하는 구조라, GET ?pageIndex=n 은 서버가 무시하고 1페이지를 준다."""
    for attempt in range(retries):
        try:
            if method == "POST":
                r = session.post(url, data=data, timeout=20)
            else:
                r = session.get(url, params=params, timeout=20)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                time.sleep(delay + random.uniform(0, 0.4))
                return r.text
            if r.status_code in (429, 503):
                wait = delay * (3 ** attempt)
                print(f"  [{r.status_code}] {wait:.1f}s 대기 후 재시도", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  [{r.status_code}] {url}", file=sys.stderr)
            return None
        except requests.RequestException as e:
            print(f"  요청 실패({attempt + 1}/{retries}): {e}", file=sys.stderr)
            time.sleep(delay * (2 ** attempt))
    return None


def fetch_bytes(session: requests.Session, url: str, delay: float = 1.0,
                retries: int = 3, referer: str = "") -> bytes | None:
    """첨부파일(PDF) 같은 바이너리용. 본문 추출에만 쓴다."""
    headers = {"Referer": referer} if referer else None
    for attempt in range(retries):
        try:
            r = session.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                time.sleep(delay + random.uniform(0, 0.4))
                return r.content
            if r.status_code in (429, 503):
                wait = delay * (3 ** attempt)
                print(f"  [{r.status_code}] {wait:.1f}s 대기 후 재시도", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  [{r.status_code}] {url}", file=sys.stderr)
            return None
        except requests.RequestException as e:
            print(f"  첨부 요청 실패({attempt + 1}/{retries}): {e}", file=sys.stderr)
            time.sleep(delay * (2 ** attempt))
    return None


# --- 파싱 ------------------------------------------------------------------

def pick_text(soup: BeautifulSoup, candidates: list[str]) -> str:
    for sel in candidates:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if txt:
                return txt
    return ""


def parse_list(html: str) -> list[dict]:
    """목록 페이지에서 상세 링크를 뽑는다."""
    soup = BeautifulSoup(html, "html.parser")
    pat = re.compile(SELECTORS["detail_link"])
    seen, out = set(), []

    # 목록 항목으로 범위를 좁힌다. 실패하면 예전처럼 페이지 전체를 훑는다.
    scope = soup.select(SELECTORS["list_item"])
    anchors = []
    for li in scope:
        anchors.extend(li.find_all("a", href=True))
    if not anchors:
        anchors = soup.find_all("a", href=True)

    for a in anchors:
        href = a["href"]
        if not pat.search(href):
            continue
        url = urljoin(BASE, href)
        qs = parse_qs(urlparse(url).query)
        news_id = (qs.get("newsId") or qs.get("newsid") or [None])[0]
        if not news_id:
            news_id = re.sub(r"\W+", "_", url)[-40:]
        if news_id in seen:
            continue
        seen.add(news_id)
        out.append({
            "briefing_id": news_id,
            "url": url,
            "list_title": a.get_text(" ", strip=True),
        })
    return out


def normalize_date(raw: str) -> str:
    m = re.search(r"(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", raw)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return raw.strip()


def _earliest(text: str, markers: list[str]) -> tuple[int, str]:
    """가장 앞에 나오는 표지의 (위치, 표지)를 준다. 없으면 (-1, "").
    리스트 순서가 아니라 실제 등장 위치 기준이어야 블록이 엉뚱하게 안 잘린다."""
    best, best_m = -1, ""
    for m in markers:
        i = text.find(m)
        if i != -1 and (best == -1 or i < best):
            best, best_m = i, m
    return best, best_m


def split_report_block(body: str) -> str:
    """본문에서 '보도 내용' 이후 정부 입장 이전까지를 잘라낸다.
    원문 기사 매칭이 안 풀릴 때 claim 소스로 쓰는 축소안의 재료."""
    idx, marker = _earliest(body, REPORT_MARKERS)
    if idx == -1:
        return ""
    rest = body[idx + len(marker):]
    e, _ = _earliest(rest, EXPLAIN_MARKERS)
    m = EXPLAIN_PATTERN.search(rest)
    if m and (e == -1 or m.start() < e):
        e = m.start()
    block = rest[:e] if e != -1 else rest[:1500]
    block = block.strip(" :：<>＜＞[]【】.\n")
    # "…보도하였습니다.2" 처럼 다음 절 번호가 붙어 나오는 꼬리를 뗀다
    return re.sub(r"[\s.,]*\d+[.\s]*$", "", block).strip()


def strip_contact_block(body: str) -> str:
    """'담당 부서 … 사무관 …(044-…)(…@korea.kr)' 구간 제거 (실명/전화/이메일)."""
    if not body:
        return body
    return re.sub(r"\s{2,}", " ", CONTACT_BLOCK.sub(" ", body)).strip()


def body_hash(body: str) -> str:
    """중복 문서 판정용 SHA-256. 담당부서 블록을 뗀 본문 기준."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def assess_usable(quoted_report: str) -> tuple[bool, str]:
    """claim 추출 입력으로 쓸 수 있는 건인지 판정. (usable, 사유)

    기사 제목만 인용하고 주장 내용이 없는 건을 걸러낸다. 제목 자체가 따옴표
    안에 들어오기 때문에 '따옴표 유무'로는 구분이 안 된다. 그래서 보도 서술어
    ('…보도하였습니다', '…주장하였다고') 유무를 본다."""
    q = quoted_report.strip()
    if not q:
        return False, "quoted_report 없음"
    if BULLET_PAT.search(q) or REPORT_PREDICATE.search(q):
        return True, ""
    if len(q) < MIN_USABLE_LEN:
        return False, "기사 제목만 있고 주장 내용 없음"
    return True, ""


# --- 본문(첨부 PDF) --------------------------------------------------------

def find_pdf_url(soup: BeautifulSoup) -> str:
    """상세 페이지 첨부파일 중 PDF 의 다운로드 URL. 없으면 빈 문자열."""
    for para in soup.select(".filedown p"):
        name_el = para.find("span")
        name = name_el.get_text(strip=True) if name_el else ""
        if not name.lower().endswith(".pdf"):
            continue
        a = para.find("a", class_="down") or para.find("a", href=True)
        if a and a.get("href"):
            return urljoin(BASE, a["href"])
    return ""


def pdf_to_text(blob: bytes) -> str:
    """첨부 PDF(정부 해명자료 원문)에서 텍스트를 뽑는다."""
    text = ""
    if PdfReader is not None:
        try:
            reader = PdfReader(io.BytesIO(blob))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception as e:
            print(f"  pypdf 실패: {e}", file=sys.stderr)
    if len(text.strip()) < 100 and _pdfminer_text is not None:
        try:
            text = _pdfminer_text(io.BytesIO(blob)) or text
        except Exception as e:
            print(f"  pdfminer 실패: {e}", file=sys.stderr)
    return re.sub(r"[ \t\u00a0]+", " ", re.sub(r"\s*\n\s*", " ", text)).strip()


def fetch_body(session: requests.Session, soup: BeautifulSoup, page_url: str,
               delay: float) -> str:
    """해명자료 본문. 상세 HTML 에는 없고 첨부 PDF 에만 있다."""
    pdf_url = find_pdf_url(soup)
    if not pdf_url:
        return ""
    blob = fetch_bytes(session, pdf_url, delay=delay, referer=page_url)
    if not blob or not blob[:5].startswith(b"%PDF"):
        return ""
    return pdf_to_text(blob)


def parse_detail(html: str, url: str, briefing_id: str,
                 session: requests.Session | None = None,
                 delay: float = 1.2) -> Briefing:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    # .gotosite 안의 <i class="tooltip">부처별 뉴스 이동</i> 이 부처명에 섞인다
    for tip in soup.select("i.tooltip"):
        tip.decompose()

    title = pick_text(soup, SELECTORS["title"])
    date_raw = pick_text(soup, SELECTORS["date"])
    ministry = pick_text(soup, SELECTORS["ministry"])
    body = pick_text(soup, SELECTORS["body"])

    # 상세 HTML 의 본문 컨테이너는 빈 껍데기다. 실제 본문은 첨부 PDF 에 있다.
    if not body and session is not None:
        body = fetch_body(session, soup, url, delay)
    body = strip_contact_block(body)
    # 페이지 전체 텍스트를 본문으로 쓰면 내비게이션이 섞여 source_hint 가 오탐한다.

    hint = ""
    m = PRESS_HINT.search(body[:1200])
    if m:
        hint = m.group(0).strip()

    quoted = split_report_block(body)
    usable, reason = assess_usable(quoted)

    return Briefing(
        briefing_id=briefing_id,
        url=url,
        title=title,
        published_at=normalize_date(date_raw),
        ministry=ministry,
        body=body,
        quoted_report=quoted,
        source_hint=hint,
        collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        usable=usable,
        usable_reason=reason,
    )


# --- probe -----------------------------------------------------------------

def probe(session: requests.Session, delay: float) -> None:
    print("=== 목록 페이지 ===")
    html = fetch(session, LIST_URL, delay=delay)
    if not html:
        print("목록 페이지를 못 가져왔다. 네트워크/차단 여부 확인 필요.")
        return

    items = parse_list(html)
    print(f"상세 링크 후보: {len(items)}개")
    for it in items[:5]:
        print(f"  - {it['briefing_id']} | {it['list_title'][:40]} | {it['url']}")

    if not items:
        print("\n링크 패턴이 안 맞는다. 아래 href 샘플을 보고 SELECTORS['detail_link'] 수정:")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True)[:40]:
            print("   ", a["href"][:100])
        return

    print("\n=== 상세 페이지 ===")
    d = items[0]
    dhtml = fetch(session, d["url"], delay=delay)
    if not dhtml:
        print("상세 페이지 실패")
        return

    soup = BeautifulSoup(dhtml, "html.parser")
    for key in ("title", "date", "ministry", "body"):
        hit = None
        for sel in SELECTORS[key]:
            if soup.select_one(sel):
                hit = sel
                break
        print(f"  {key:9s}: {hit or '매칭 실패'}")

    print("\n본문 후보 컨테이너 (id/class, 텍스트 길이순):")
    cands = []
    for tag in soup.find_all(["div", "article", "section"]):
        t = tag.get_text(" ", strip=True)
        ident = tag.get("id") or " ".join(tag.get("class") or [])
        if ident and len(t) > 300:
            cands.append((len(t), ident))
    for ln, ident in sorted(set(cands), reverse=True)[:10]:
        print(f"   {ln:6d}자  {ident}")

    b = parse_detail(dhtml, d["url"], d["briefing_id"], session=session, delay=delay)
    print(f"\n본문 길이: {len(b.body)}자 (첨부 PDF 추출)")
    print(f"\n제목: {b.title[:60]}")
    print(f"일자: {b.published_at}   부처: {b.ministry}")
    print(f"언론사 힌트: {b.source_hint or '(없음)'}")
    print(f"보도내용 블록: {(b.quoted_report[:120] + '...') if b.quoted_report else '(추출 실패 — REPORT_MARKERS 확인)'}")


# --- 수집 ------------------------------------------------------------------

def list_form_params(session: requests.Session, delay: float) -> dict:
    """목록 mainForm 의 hidden 값들. 페이지네이션 POST 에 그대로 실어 보낸다."""
    defaults = {
        "pageIndex": 1, "repCodeType": "", "repCode": "", "srchWord": "",
        "cateId": "", "period": "", "nRepCode": "", "cardnewsSe": "",
    }
    html = fetch(session, LIST_URL, delay=delay)
    if not html:
        return defaults
    form = BeautifulSoup(html, "html.parser").select_one("#mainForm")
    if not form:
        return defaults
    params = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            params[name] = inp.get("value") or ""
    defaults.update(params)
    return defaults


def load_done(path: str) -> tuple[set[str], set[str]]:
    """(이미 수집한 briefing_id, 이미 저장된 body 해시)"""
    done, hashes = set(), set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    done.add(rec["briefing_id"])
                    if rec.get("body"):
                        hashes.add(body_hash(rec["body"]))
                except Exception:
                    pass
    return done, hashes


def collect(limit: int, out: str, delay: float, resume: bool,
            page_param: str, start_page: int) -> None:
    session = make_session()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    done, seen_hashes = load_done(out) if resume else (set(), set())
    if done:
        print(f"이미 수집된 {len(done)}건은 건너뛴다.")

    form = list_form_params(session, delay)

    n, page, empty_pages, dupes = 0, start_page, 0, 0
    mode = "a" if resume else "w"

    with open(out, mode, encoding="utf-8") as fout:
        while n < limit and empty_pages < 3:
            payload = dict(form)
            payload[page_param] = page
            html = fetch(session, LIST_URL, data=payload, method="POST", delay=delay)
            if not html:
                empty_pages += 1
                page += 1
                continue

            items = [i for i in parse_list(html) if i["briefing_id"] not in done]
            if not items:
                empty_pages += 1
                page += 1
                continue
            empty_pages = 0
            print(f"[page {page}] 신규 {len(items)}건")

            for it in items:
                if n >= limit:
                    break
                dhtml = fetch(session, it["url"], delay=delay)
                if not dhtml:
                    continue
                b = parse_detail(dhtml, it["url"], it["briefing_id"],
                                 session=session, delay=delay)
                if not b.title:
                    b.title = it["list_title"]
                # 같은 해명자료가 다른 newsId 로 중복 게시되는 경우가 있다.
                # 먼저 수집된 쪽을 남긴다.
                if b.body:
                    h = body_hash(b.body)
                    if h in seen_hashes:
                        dupes += 1
                        print(f"  [중복] {b.briefing_id} {b.title[:32]} — 건너뜀")
                        done.add(b.briefing_id)
                        continue
                    seen_hashes.add(h)
                fout.write(b.to_json() + "\n")
                fout.flush()
                done.add(b.briefing_id)
                n += 1
                print(f"  ({n}/{limit}) {b.published_at} {b.title[:40]}")

            page += 1

    print(f"\n완료: {n}건 → {out}")
    if dupes:
        print(f"본문 중복으로 제외: {dupes}건")
    if n:
        print("다음: python schema.py 로 검증하기 전에, 무작위 5건을 눈으로 확인할 것.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="셀렉터 확인만 하고 종료")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default="data/briefings.jsonl")
    ap.add_argument("--delay", type=float, default=1.2, help="요청 간 대기(초)")
    ap.add_argument("--resume", action="store_true", help="기존 파일에 이어붙이기")
    ap.add_argument("--page-param", default="pageIndex")
    ap.add_argument("--start-page", type=int, default=1)
    args = ap.parse_args()

    if args.delay < 1.2:
        print(f"--delay {args.delay} 는 너무 짧다. 1.2 로 올린다.", file=sys.stderr)
        args.delay = 1.2

    if args.probe:
        probe(make_session(), args.delay)
        return

    collect(args.limit, args.out, args.delay, args.resume,
            args.page_param, args.start_page)


if __name__ == "__main__":
    main()
