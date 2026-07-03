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


def analyze_json(query: str, min_vol: float = 0, no_sample: bool = True):
    key = (query.strip(), min_vol, no_sample)
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
        if vol < min_vol or (no_sample and RE_SAMPLE.search(title)):
            filtered += 1
            continue
        rows.append({
            "title": title, "price": price, "vol": vol, "unit": unit,
            "unit_price": round(price / vol, 1),
            "mall": it.get("mallName", "?"), "link": it.get("link", ""),
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
<title>화장품 진짜 최저가</title>
<style>
  body{font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;max-width:860px;
       margin:40px auto;padding:0 16px;color:#222;background:#fafafa}
  h1{font-size:1.5rem} .sub{color:#777;font-size:.9rem;margin-bottom:24px}
  form{display:flex;gap:8px;margin-bottom:10px}
  input[type=text]{flex:1;padding:12px 16px;font-size:1rem;border:2px solid #ddd;border-radius:10px}
  button{padding:12px 24px;font-size:1rem;border:0;border-radius:10px;
         background:#1a7f5a;color:#fff;cursor:pointer}
  button:disabled{background:#aaa}
  .filters{display:flex;gap:16px;align-items:center;margin-bottom:24px;
           font-size:.85rem;color:#555}
  .filters input[type=number]{width:70px;padding:4px 8px;border:1px solid #ccc;border-radius:6px}
  .best{background:#e8f7f0;border:2px solid #1a7f5a;border-radius:12px;
        padding:16px 20px;margin-bottom:12px}
  .best b{font-size:1.15rem}
  .advice{background:#fff8e6;border:2px solid #e6a817;border-radius:12px;
          padding:12px 20px;margin-bottom:12px;font-size:.95rem}
  .sales{background:#fff;border:1px solid #ddd;border-radius:12px;
         padding:12px 20px;margin-bottom:20px;font-size:.85rem;color:#555}
  .sales b{color:#222} .sales .on{color:#1a7f5a;font-weight:700}
  table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden}
  th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #eee;font-size:.9rem}
  th{background:#f0f0f0} td.num{text-align:right;white-space:nowrap}
  tr.dim{color:#999} .note{color:#888;font-size:.8rem;margin-top:12px}
  .err{color:#c0392b;padding:12px;background:#fdecea;border-radius:8px}
  a{color:#1a7f5a}
</style></head><body>
<h1>화장품 진짜 최저가 🔍</h1>
<div class="sub">여러 플랫폼 판매가를 용량당 단가로 환산하고, 세일 타이밍까지 알려줍니다 —
브랜드+제품명으로 구체적으로 검색할수록 정확해요</div>
<form id="f"><input type="text" id="q" placeholder="예: 이니스프리 그린티 세럼" autofocus>
<button id="btn">검색</button></form>
<div class="filters">
  <label>최소 용량 <input type="number" id="minv" value="10"> ml/g</label>
  <label><input type="checkbox" id="nosample" checked> 샘플·체험분 제외</label>
</div>
<div id="out"></div>
<script>
const f=document.getElementById('f'),q=document.getElementById('q'),
      out=document.getElementById('out'),btn=document.getElementById('btn'),
      minv=document.getElementById('minv'),nosample=document.getElementById('nosample');
const won=n=>n.toLocaleString('ko-KR');
f.onsubmit=async e=>{
  e.preventDefault(); if(!q.value.trim())return;
  btn.disabled=true; out.innerHTML='검색 중... (최대 300개 수집)';
  try{
    const p=new URLSearchParams({q:q.value,min_vol:minv.value||'0',
                                 no_sample:nosample.checked?'1':'0'});
    const r=await fetch('/api/search?'+p);
    const d=await r.json();
    if(d.error){out.innerHTML='<div class="err">'+d.error+'</div>';return}
    if(!d.rows.length){out.innerHTML='<div class="err">조건에 맞는 상품이 없습니다. 필터를 낮춰보세요.</div>';return}
    let h='';
    if(d.best) h+='<div class="best">진짜 최저가: <b>'+d.best.mall+' '+won(d.best.price)+'원</b> ('
      +won(Math.round(d.best.unit_price))+'원/'+d.best.unit+', '+d.best.vol+d.best.unit+')<br>'
      +'<a href="'+d.best.link+'" target="_blank">'+d.best.title+'</a></div>';
    if(d.advice) h+='<div class="advice">⏰ '+d.advice+'</div>';
    if(d.sales&&d.sales.length){
      h+='<div class="sales"><b>다가오는 세일</b> — ';
      h+=d.sales.map(s=>(s.ongoing?'<span class="on">'+s.name+' 진행 중('+s.end+'까지)</span>'
        :s.name+' D-'+s.d_day+'('+s.start+'~)')).join(' · ');
      h+='<br>'+d.sale_note+'</div>';
    }
    h+='<table><tr><th>단가</th><th>판매가</th><th>용량</th><th>판매처</th><th>상품명</th></tr>';
    for(const r2 of d.rows){
      const dim=d.best&&r2.unit!==d.best.unit?' class="dim"':'';
      h+='<tr'+dim+'><td class="num"><b>'+won(Math.round(r2.unit_price))+'원/'+r2.unit+'</b></td>'
        +'<td class="num">'+won(r2.price)+'원</td><td class="num">'+r2.vol+r2.unit+'</td>'
        +'<td>'+r2.mall+'</td><td><a href="'+r2.link+'" target="_blank">'+r2.title+'</a></td></tr>';
    }
    h+='</table><div class="note">수집 '+d.fetched+'건 → 분석 '+d.total+'건 (파싱 실패 '
      +d.skipped+'건 · 필터 제외 '+d.filtered+'건)'
      +(d.mock?' · <b>목데이터 모드</b>':'')
      +' · 회색 행은 단위가 달라 별개 제품일 수 있음'
      +' · 세일 일정은 공개 패턴 기반 예상이며 확정 공지는 각 플랫폼 확인</div>';
    out.innerHTML=h;
  }catch(err){out.innerHTML='<div class="err">오류: '+err+'</div>'}
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
                self._send(200, json.dumps(
                    analyze_json(query, min_vol, no_sample), ensure_ascii=False),
                    "application/json")
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
