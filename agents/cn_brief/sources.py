"""数据源 (只读, 免费公开接口; 2026-10-19 在本机实测可达).

  - 东方财富研报中心 reportapi.eastmoney.com: 个股研报 (qType=0) / 行业研报 (qType=1) / 宏观研报 (report/jg, qType=3)
  - 百度股市通财经日历 finance.pae.baidu.com: 全球经济数据 (时间为北京时间, star=重要性 1-5)
  (金十日历在本机网络 DNS 解析失败; 东方财富经济日历接口已下线 — 都不用)
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
BJT = timezone(timedelta(hours=8))
JST = timezone(timedelta(hours=9))
EM_REFERER = "https://data.eastmoney.com/"
REPORT_URL = "https://reportapi.eastmoney.com/report/list"
MACRO_URL = "https://reportapi.eastmoney.com/report/jg"
CAL_URL = "https://finance.pae.baidu.com/sapi/v1/financecalendar"
PDF_URL = "https://pdf.dfcfw.com/pdf/H3_{info}_1.pdf"
PAGE_URL = "https://data.eastmoney.com/report/info/{info}.html"


def _get(url: str, params: dict, referer: str, timeout: int = 25, retries: int = 2) -> dict:
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA, "Referer": referer}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url} 获取失败: {last}")


def _reports(q_type: int, begin: date, end: date, max_pages: int = 20, page_size: int = 100) -> list[dict]:
    out = []
    for page in range(1, max_pages + 1):
        url = MACRO_URL if q_type == 3 else REPORT_URL
        params = dict(pageSize=page_size, beginTime=begin.isoformat(), endTime=end.isoformat(), pageNo=page,
                      fields="", qType=q_type, orgCode="")
        if q_type == 3:
            params["author"] = ""
        else:
            params.update(industryCode="*", industry="*", rating="*", ratingChange="*", code="*", rcode="")
        j = _get(url, params, EM_REFERER)
        rows = j.get("data") or []
        out.extend(rows)
        total_pages = int(j.get("TotalPage") or 0)
        if not rows or (total_pages and page >= total_pages) or len(rows) < page_size:
            break
    return out


def stock_reports(begin: date, end: date) -> list[dict]:
    return _reports(0, begin, end)


def industry_reports(begin: date, end: date) -> list[dict]:
    return _reports(1, begin, end)


def macro_reports(begin: date, end: date) -> list[dict]:
    return _reports(3, begin, end, max_pages=3)


def report_link(row: dict) -> str:
    info = row.get("infoCode")
    return PAGE_URL.format(info=info) if info else ""


def calendar_day(day: date) -> list[dict]:
    """某一天 (北京时间日期) 的全部经济数据. 每条补 time_bjt / time_jst (datetime, 可能为 None)."""
    # 实测 (2026-10-19): 每页最多 30 条 (rn>30 也只给 30), pn 是页码; 预期值字段 indicateVal 只在有预期时出现
    raw, total = [], None
    for pn in range(0, 15):
        j = _get(CAL_URL, dict(start_date=day.isoformat(), end_date=day.isoformat(), pn=pn, rn=30,
                               cate="economic_data", finClientType="pc"), "https://gushitong.baidu.com/")
        info = (j.get("Result") or {}).get("calendarInfo") or []
        page = [x for block in info for x in (block.get("list") or [])]
        if info and total is None:
            total = int(_num_or(info[0].get("total"), 0))
        raw.extend(page)
        if len(page) < 30 or (total and len(raw) >= total):
            break
    rows, seen = [], set()
    for x in raw:
        key = (x.get("date"), x.get("time"), x.get("region"), x.get("title"))
        if key in seen:
            continue
        seen.add(key)
        x = dict(x)
        t = str(x.get("time") or "").strip()
        dt = None
        try:
            hh, mm = t.split(":")[:2]
            dt = datetime.fromisoformat(f"{x.get('date') or day.isoformat()}T{int(hh):02d}:{int(mm):02d}:00").replace(tzinfo=BJT)
        except Exception:
            pass
        x["time_bjt"] = dt
        x["time_jst"] = dt.astimezone(JST) if dt else None
        rows.append(x)
    return rows


def _num_or(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def calendar_range(start: date, days: int) -> list[dict]:
    out = []
    for i in range(days):
        try:
            out.extend(calendar_day(start + timedelta(days=i)))
        except Exception as e:        # noqa: BLE001
            out.append({"_error": f"{start + timedelta(days=i)}: {e}"})
    return out
