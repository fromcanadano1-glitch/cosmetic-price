#!/usr/bin/env python3
"""
화장품 최저가 — 배포용 서버 v1.0 (Render 등 클라우드용, 단일 파일)

로컬 데모(web_demo.py)와의 차이:
  - 0.0.0.0 바인딩 + PORT 환경변수 (클라우드 필수)
  - 검색 결과 10분 캐시 (네이버 API 하루 25,000회 한도 절약)
  - 브라우저 자동 열기 제거

로컬 테스트: python3 server.py --mock → http://localhost:8765
"""

import datetime
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 용량 파싱
# ---------------------------------------------------------------------------
_UNIT = r"(ml|mL|ML|㎖|g|G|그램)"
_NUM = r"(\d+(?:\.\d+)?)"
RE_MULT = re.compile(_NUM + r"\s*" + _UNIT + r"\s*[*xX×]\s*(\d+)")
RE_PLUS = re.compile(_NUM + r"\s*" + _UNIT + r"\s*\+\s*" + _NUM + r"\s*" + _UNIT)
RE_SINGLE = re.compile(_NUM + r"\s*" + _UNIT + r"\b")


def normalize_unit(u: str) -> str:
    u = u.lower()
    return "g" if u in ("g", "그램") else "ml"


def parse_volume(title: str):
    m = RE_MULT.search(title)
    if m:
        return float(m.group(1)) * int(m.group(3)), normalize_unit(m.group(2))
    m = RE_PLUS.search(title)
    if m and normalize_unit(m.group(2)) == normalize_unit(m.group(4)):
        return float(m.group(1)) + float(m.group(3)), normalize_unit(m.group(2))
    m = RE_SINGLE.search(title)
    if m:
        return float(m.group(1)), normalize_unit(m.group(2))
    return None, None


def strip_tags(s: str) -> str:
    return re.sub(r"</?b>", "", s)


MOCK_ITEMS = [
    {"title": "<b>이니스프리</b> 그린티 씨드 세럼 80ml", "lprice": "23900", "mallName": "네이버", "link": "https://example.com/1"},
    {"title": "이니스프리 그린티 씨드 히알루론산 세럼 160ml 대용량", "lprice": "41000", "mallName": "쿠팡", "link": "https://example.com/2"},
    {"title": "이니스프리 그린티 세럼 80ml*2 기획세트", "lprice": "43800", "mallName": "올리브영", "link": "https://example.com/3"},
    {"title": "이니스프리 그린티 씨드 세럼 30ml+30ml 증정 기획", "lprice": "19500", "mallName": "무신사", "link": "https://example.com/4"},
    {"title": "이니스프리 그린티 세럼 미니 3ml 샘플 체험분", "lprice": "1900", "mallName": "G마켓", "link": "https://example.com/5"},
    {"title": "이니스프리 그린티 클렌징폼 150g", "lprice": "6900", "mallName": "11번가", "link": "https://example.com/6"},
    {"title": "이니스프리 그린티 세럼 (용량 표기 없음)", "lprice": "25000", "mallName": "SSG", "link": "https://example.com/7"},
]

# ---------------------------------------------------------------------------
# 세일 캘린더 (2026, 공개된 일정/패턴 기반 — 확정 공지는 각 플랫폼에서 최종 확인)
# ---------------------------------------------------------------------------
D = datetime.date
SALE_DB = [
    ("올영세일", "올리브영", D(2026, 3, 1), D(2026, 3, 7), "분기 빅세일, 최대 70%"),
    ("올영세일", "올리브영", D(2026, 5, 31), D(2026, 6, 6), "분기 빅세일, 최대 70%"),
    ("올영세일", "올리브영", D(2026, 8, 30), D(2026, 9, 5), "분기 빅세일, 최대 70%"),
    ("올영세일", "올리브영", D(2026, 11, 30), D(2026, 12, 6), "분기 빅세일, 최대 70%"),
    ("무신사 명절 세일", "무신사", D(2026, 9, 26), D(2026, 10, 9), "뷰티 포함"),
    ("무신사 하반기 감사세일", "무신사", D(2026, 10, 21), D(2026, 10, 27), "뷰티 포함"),
    ("무진장 겨울 블프", "무신사", D(2026, 11, 16), D(2026, 11, 26), "무신사 최대 세일, 뷰티 포함"),
]
for _m in range(1, 13):
    SALE_DB.append(("올리브영데이", "올리브영", D(2026, _m, 25), D(2026, _m, 27),
                    "월간 정기세일, 멤버십 혜택 중심"))
SALE_NOTE = "쿠팡은 정기 세일 없이 와우회원 상시할인·골드박스 일 특가 중심"


def upcoming_sales(today=None, horizon=90):
    today = today or datetime.date.today()
    out = []
    for name, platform, start, end, note in SALE_DB:
        if end < today or (start - today).days > horizon:
            continue
        ongoing = start <= today <= end
        out.append({
            "name": name, "platform": platform, "note": note,
            "start": start.isoformat(), "end": end.isoformat(),
            "ongoing": ongoing,
            "d_day": 0 if ongoing else (start - today).days,
        })
    out.sort(key=lambda s: (not s["ongoing"], s["d_day"]))
    return out


def buy_or_wait(best_mall: str, sales, wait_limit=45):
    if not best_mall:
        return None
    for s in sales:
        if s["platform"] in best_mall:
            if s["ongoing"]:
                return f"지금이 {s['name']} 기간({s['end']}까지) — 바로 사는 게 이득"
            if s["d_day"] <= wait_limit:
                return (f"{s['d_day']}일 후 {s['name']}({s['start']}~) 시작 — "
                        f"급한 게 아니면 기다리는 게 이득일 수 있음 ({s['note']})")
            break
    big = next((s for s in sales if s["name"] != "올리브영데이"), None)
    if big:
        when = "진행 중" if big["ongoing"] else f"{big['d_day']}일 후"
        return f"참고: {big['name']}이 {when}({big['start']}~{big['end']}) — 해당 플랫폼 가격도 그때 비교해볼 것"
    return None


# ---------------------------------------------------------------------------
# API 호출 + 캐시 + 분석
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", 8765))
MOCK_MODE = "--mock" in sys.argv
API_URL = "https://openapi.naver.com/v1/search/shop.json"

_CACHE = {}          # key → (timestamp, result)
_CACHE_TTL = 600     # 10분
_CACHE_MAX = 500
_CACHE_LOCK = threading.Lock()


def fetch_items(query: str):
    if MOCK_MODE:
        return MOCK_ITEMS
    cid = os.environ.get("NAVER_CLIENT_ID")
    secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not secret:
        raise RuntimeError("환경변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 설정 필요")
    items = []
    for start in (1, 101, 201):
        url = (f"{API_URL}?query={urllib.parse.quote(query)}"
               f"&display=100&start={start}&sort=sim")
        req = urllib.request.Request(url, headers={
            "X-Naver-Client-Id": cid,
            "X-Naver-Client-Secret": secret,
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            batch = json.load(r)["items"]
        items.extend(batch)
        if len(batch) < 100:
            break
    return items


RE_SAMPLE = re.compile(r"샘플|체험|테스터|파우치|미니어처|증정품|1회용")
RE_FREESHIP = re.compile(r"무료\s*배송|무배|무료직배송")


def analyze_json(query: str, min_vol: float = 0, no_sample: bool = True,
                 free_ship_only: bool = False):
    key = (query.strip(), min_vol, no_sample, free_ship_only)
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]

    items = fetch_items(query)
    rows, skipped, filtered = [], 0, 0
    for it in items:
        title = strip_tags(it["title"])
        price = int(it["lprice"])
        vol, unit = parse_volume(title)
        if vol is None or price == 0:
            skipped += 1
            continue
        freeship = bool(RE_FREESHIP.search(title))
        if (vol < min_vol or (no_sample and RE_SAMPLE.search(title))
                or (free_ship_only and not freeship)):
            filtered += 1
            continue
        rows.append({
            "title": title, "price": price, "vol": vol, "unit": unit,
            "unit_price": round(price / vol, 1),
            "mall": it.get("mallName", "?"), "link": it.get("link", ""),
            "freeship": freeship,
        })
    rows.sort(key=lambda r: r["unit_price"])

    best = None
    if rows:
        units = [r["unit"] for r in rows]
        main_unit = max(set(units), key=units.count)
        best = next(r for r in rows if r["unit"] == main_unit)

    sales = upcoming_sales()
    result = {"query": query, "rows": rows[:30], "skipped": skipped,
              "filtered": filtered, "fetched": len(items),
              "total": len(rows), "best": best, "mock": MOCK_MODE,
              "sales": sales[:4], "sale_note": SALE_NOTE,
              "advice": buy_or_wait(best["mall"] if best else "", sales)}

    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = (now, result)
    return result


# ---------------------------------------------------------------------------
# 웹 페이지
# ---------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>실속 — 화장품 진짜 최저가</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>
  :root{--bg:#f6f4ef;--ink:#191713;--muted:#8b857a;--line:#e9e4db;--card:#ffffff;
        --brand:#0d5c46;--brand-deep:#0a4534;--brand-soft:#eaf3ef;
        --gold:#b9862f;--gold-soft:#fbf3e2}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Pretendard Variable',Pretendard,-apple-system,'Apple SD Gothic Neo',sans-serif;
       background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
  .wrap{max-width:920px;margin:0 auto;padding:0 20px 80px}
  header{padding:64px 0 36px;text-align:center}
  .logo{display:inline-flex;align-items:center;gap:10px;font-weight:800;
        font-size:.95rem;letter-spacing:.14em;color:var(--brand);text-transform:uppercase}
  .logo::before{content:'';width:26px;height:2px;background:var(--brand)}
  .logo::after{content:'';width:26px;height:2px;background:var(--brand)}
  h1{font-size:clamp(1.7rem,4.5vw,2.6rem);font-weight:800;letter-spacing:-.04em;
     margin:14px 0 12px;line-height:1.2}
  h1 em{font-style:normal;color:var(--brand)}
  .sub{color:var(--muted);font-size:.95rem;line-height:1.6;max-width:560px;margin:0 auto}
  .searchbox{position:relative;max-width:640px;margin:32px auto 14px}
  .searchbox input{width:100%;padding:18px 130px 18px 26px;font-size:1.05rem;
        font-family:inherit;border:1.5px solid var(--line);border-radius:999px;
        background:var(--card);outline:none;
        box-shadow:0 10px 34px rgba(25,23,19,.07);transition:border-color .15s,box-shadow .15s}
  .searchbox input:focus{border-color:var(--brand);box-shadow:0 10px 34px rgba(13,92,70,.14)}
  .searchbox button{position:absolute;right:7px;top:7px;bottom:7px;padding:0 30px;
        font-size:.98rem;font-weight:700;font-family:inherit;border:0;border-radius:999px;
        background:var(--brand);color:#fff;cursor:pointer;transition:background .15s}
  .searchbox button:hover{background:var(--brand-deep)}
  .searchbox button:disabled{background:#c2beb5;cursor:default}
  .filters{display:flex;gap:22px;justify-content:center;align-items:center;
           font-size:.85rem;color:var(--muted)}
  .filters label{display:flex;align-items:center;gap:7px;cursor:pointer}
  .filters input[type=number]{width:64px;padding:5px 9px;border:1px solid var(--line);
        border-radius:8px;font-family:inherit;background:var(--card)}
  .filters input[type=checkbox]{accent-color:var(--brand);width:15px;height:15px}
  #out{margin-top:34px}
  .spinner{display:flex;flex-direction:column;align-items:center;gap:14px;
           padding:46px 0;color:var(--muted);font-size:.9rem}
  .spinner .ring{width:34px;height:34px;border-radius:50%;
        border:3px solid var(--line);border-top-color:var(--brand);
        animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .best{background:linear-gradient(135deg,var(--brand) 0%,var(--brand-deep) 100%);
        color:#fff;border-radius:20px;padding:26px 30px;margin-bottom:14px;
        box-shadow:0 16px 44px rgba(13,92,70,.25)}
  .best .label{font-size:.78rem;font-weight:700;letter-spacing:.12em;opacity:.75;
        text-transform:uppercase;margin-bottom:8px}
  .best .price{font-size:1.9rem;font-weight:800;letter-spacing:-.02em;line-height:1.15}
  .best .price small{font-size:1rem;font-weight:600;opacity:.85;margin-left:8px}
  .best a{color:#fff;opacity:.88;font-size:.92rem;text-decoration:underline;
        text-underline-offset:3px;display:inline-block;margin-top:9px}
  .advice{display:flex;gap:12px;align-items:flex-start;background:var(--gold-soft);
        border:1px solid #ecd9ac;border-radius:16px;padding:16px 20px;
        margin-bottom:14px;font-size:.93rem;line-height:1.55;color:#5d4a1e}
  .advice::before{content:'⏰';font-size:1.1rem;line-height:1.4}
  .sales{background:var(--card);border:1px solid var(--line);border-radius:16px;
        padding:16px 20px;margin-bottom:26px}
  .sales .t{font-size:.78rem;font-weight:700;letter-spacing:.1em;color:var(--muted);
        text-transform:uppercase;margin-bottom:10px}
  .chips{display:flex;flex-wrap:wrap;gap:8px}
  .chip{display:inline-flex;align-items:center;gap:7px;font-size:.82rem;
        padding:6px 13px;border-radius:999px;background:var(--bg);
        border:1px solid var(--line);color:#55503f}
  .chip b{color:var(--brand);font-weight:800}
  .chip.on{background:var(--brand-soft);border-color:var(--brand);color:var(--brand-deep)}
  .sales .foot{font-size:.78rem;color:var(--muted);margin-top:10px}
  .tablecard{background:var(--card);border:1px solid var(--line);border-radius:18px;
        overflow:hidden;box-shadow:0 8px 30px rgba(25,23,19,.05)}
  .tscroll{overflow-x:auto}
  table{width:100%;border-collapse:collapse;min-width:640px}
  th{font-size:.72rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
     color:var(--muted);text-align:left;padding:14px 16px;background:#fbfaf7;
     border-bottom:1px solid var(--line)}
  td{padding:13px 16px;border-bottom:1px solid #f1eee7;font-size:.9rem;vertical-align:middle}
  tr:last-child td{border-bottom:0}
  tbody tr{transition:background .12s} tbody tr:hover{background:#faf8f3}
  td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
  .unitp{font-weight:800;color:var(--brand)}
  .mall{display:inline-block;font-size:.78rem;font-weight:700;padding:4px 11px;
        border-radius:999px;background:var(--brand-soft);color:var(--brand-deep);
        white-space:nowrap}
  .fs{display:inline-block;font-size:.7rem;font-weight:700;padding:3px 8px;
      border-radius:999px;background:#e7f0fb;color:#2b5d9e;white-space:nowrap;
      margin-left:6px}
  .shipnote{font-size:.8rem;opacity:.75;margin-top:7px}
  tr.dim{opacity:.45}
  td a{color:var(--ink);text-decoration:none}
  td a:hover{color:var(--brand);text-decoration:underline;text-underline-offset:3px}
  .note{color:var(--muted);font-size:.78rem;line-height:1.7;margin-top:16px;text-align:center}
  .err{color:#a13326;padding:18px 22px;background:#fbeae6;border:1px solid #f0cfc7;
       border-radius:14px;font-size:.92rem}
  footer{margin-top:60px;text-align:center;color:#b3ada1;font-size:.78rem}
  @media(max-width:560px){
    header{padding:44px 0 26px}
    .searchbox input{padding:15px 108px 15px 20px;font-size:.95rem}
    .searchbox button{padding:0 20px}
    .filters{flex-wrap:wrap;gap:12px}
    .best .price{font-size:1.5rem}
  }
</style></head><body>
<div class="wrap">
<header>
  <div class="logo">실속</div>
  <h1>같은 화장품, <em>진짜 최저가</em>로 사세요</h1>
  <div class="sub">여러 플랫폼 판매가를 용량당 단가(원/ml)로 환산해 비교하고,
  세일 타이밍까지 알려드립니다. 브랜드+제품명으로 검색할수록 정확해요.</div>
  <form class="searchbox" id="f">
    <input type="text" id="q" placeholder="예: 이니스프리 그린티 세럼" autofocus>
    <button id="btn">검색</button>
  </form>
  <div class="filters">
    <label>최소 용량 <input type="number" id="minv" value="10"> ml/g</label>
    <label><input type="checkbox" id="nosample" checked> 샘플·체험분 제외</label>
    <label><input type="checkbox" id="freeship"> 무료배송 표기만</label>
  </div>
</header>
<div id="out"></div>
<footer>가격 정보: 네이버쇼핑 · 세일 일정은 공개 패턴 기반 예상치입니다</footer>
</div>
<script>
const f=document.getElementById('f'),q=document.getElementById('q'),
      out=document.getElementById('out'),btn=document.getElementById('btn'),
      minv=document.getElementById('minv'),nosample=document.getElementById('nosample'),
      freeship=document.getElementById('freeship');
const won=n=>n.toLocaleString('ko-KR');
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
f.onsubmit=async e=>{
  e.preventDefault(); if(!q.value.trim())return;
  btn.disabled=true;
  out.innerHTML='<div class="spinner"><div class="ring"></div>최대 300개 상품을 비교하는 중...</div>';
  try{
    const p=new URLSearchParams({q:q.value,min_vol:minv.value||'0',
                                 no_sample:nosample.checked?'1':'0',
                                 free_ship:freeship.checked?'1':'0'});
    const r=await fetch('/api/search?'+p);
    const d=await r.json();
    if(d.error){out.innerHTML='<div class="err">'+esc(d.error)+'</div>';return}
    if(!d.rows.length){out.innerHTML='<div class="err">조건에 맞는 상품이 없습니다. 필터를 낮춰보세요.</div>';return}
    let h='';
    if(d.best) h+='<div class="best"><div class="label">진짜 최저가</div>'
      +'<div class="price">'+esc(d.best.mall)+' '+won(d.best.price)+'원'
      +'<small>'+won(Math.round(d.best.unit_price))+'원/'+d.best.unit+' · '+d.best.vol+d.best.unit+'</small></div>'
      +'<a href="'+esc(d.best.link)+'" target="_blank" rel="noopener">'+esc(d.best.title)+' ↗</a>'
      +'<div class="shipnote">'+(d.best.freeship?'무료배송 표기 상품'
        :'⚠ 배송비 제외 가격 — 최종가는 링크에서 배송비 포함으로 확인하세요')+'</div></div>';
    if(d.advice) h+='<div class="advice">'+esc(d.advice)+'</div>';
    if(d.sales&&d.sales.length){
      h+='<div class="sales"><div class="t">다가오는 세일</div><div class="chips">';
      h+=d.sales.map(s=>(s.ongoing
        ?'<span class="chip on"><b>'+esc(s.name)+'</b> 진행 중 · '+s.end+'까지</span>'
        :'<span class="chip"><b>D-'+s.d_day+'</b>'+esc(s.name)+' · '+s.start+'~</span>')).join('');
      h+='</div><div class="foot">'+esc(d.sale_note)+'</div></div>';
    }
    h+='<div class="tablecard"><div class="tscroll"><table><thead><tr>'
      +'<th style="text-align:right">단가</th><th style="text-align:right">판매가</th>'
      +'<th style="text-align:right">용량</th><th>판매처</th><th>상품명</th></tr></thead><tbody>';
    for(const r2 of d.rows){
      const dim=d.best&&r2.unit!==d.best.unit?' class="dim"':'';
      h+='<tr'+dim+'><td class="num"><span class="unitp">'+won(Math.round(r2.unit_price))+'원/'+r2.unit+'</span></td>'
        +'<td class="num">'+won(r2.price)+'원</td><td class="num">'+r2.vol+r2.unit+'</td>'
        +'<td><span class="mall">'+esc(r2.mall)+'</span>'
        +(r2.freeship?'<span class="fs">무료배송</span>':'')+'</td>'
        +'<td><a href="'+esc(r2.link)+'" target="_blank" rel="noopener">'+esc(r2.title)+'</a></td></tr>';
    }
    h+='</tbody></table></div></div><div class="note">수집 '+d.fetched+'건 → 분석 '+d.total
      +'건 (파싱 실패 '+d.skipped+'건 · 필터 제외 '+d.filtered+'건)'
      +(d.mock?' · <b>목데이터 모드</b>':'')
      +'<br>모든 가격은 배송비 제외 (네이버쇼핑 데이터에 배송비 미포함) · 무료배송 배지는 상품명 표기 기준'
      +'<br>흐린 행은 단위(ml/g)가 달라 별개 제품일 수 있음 · 세일 일정은 확정 공지 기준으로 재확인 필요</div>';
    out.innerHTML=h;
  }catch(err){out.innerHTML='<div class="err">오류: '+esc(String(err))+'</div>'}
  finally{btn.disabled=false}
};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE, "text/html")
        elif parsed.path == "/health":
            self._send(200, "ok", "text/plain")
        elif parsed.path == "/api/search":
            qs = urllib.parse.parse_qs(parsed.query)
            query = qs.get("q", [""])[0][:100]  # 길이 제한
            try:
                min_vol = float(qs.get("min_vol", ["0"])[0] or 0)
                no_sample = qs.get("no_sample", ["1"])[0] == "1"
                free_ship = qs.get("free_ship", ["0"])[0] == "1"
                self._send(200, json.dumps(
                    analyze_json(query, min_vol, no_sample, free_ship),
                    ensure_ascii=False), "application/json")
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False),
                           "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    mode = " (목데이터 모드)" if MOCK_MODE else ""
    print(f"서버 시작{mode}: 포트 {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료됨")
