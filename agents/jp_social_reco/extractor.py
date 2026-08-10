from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .models import ContentItem, ExtractedRecommendation
from .resolver import resolve_mentions
from .settings import SIGNAL_DIR


CLI_CACHE_DIR = SIGNAL_DIR / "llm_cache"
CLI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CLI_CACHE_TTL_HOURS = 24

_SIDES = {"buy", "watch", "sell", "avoid", "neutral"}
_BUY_KW = [
    "買い", "買う", "買いたい", "買い増し", "押し目", "強気", "注目", "有望",
    "上がる", "上昇", "ブレイク", "テンバガー", "仕込み", "ロング",
    "buy", "long", "bullish", "accumulate", "recommend", "おすすめ",
    "买入", "看多", "推荐", "关注", "做多",
]
_SELL_KW = [
    "売り", "売る", "利確", "撤退", "弱気", "下落", "避ける", "ショート",
    "sell", "short", "bearish", "avoid", "exit",
    "卖出", "看空", "减仓", "回避",
]
_RISK_KW = ["広告", "PR", "案件", "提供", "アフィリエイト", "sponsored", "ad", "风险", "リスク"]

_PROMPT = """あなたは日本株のソーシャル投稿から投資アイデアを抽出する解析器です。

入力は X 投稿、YouTube 動画の文字起こし、または手動メモです。日本株の個別銘柄について、
買い推奨・注目・売り・回避のニュアンスがあるものだけを JSON で返してください。

必須 schema:
{{
  "items": [
    {{
      "item_id": "入力 item_id をそのまま返す",
      "code": "7203",
      "company_name": "トヨタ自動車",
      "side": "buy|watch|sell|avoid|neutral",
      "conviction": 0-10,
      "horizon": "intraday|swing|medium|long|unknown",
      "thesis": "短い理由",
      "risks": ["広告/PRの可能性", "..."],
      "evidence": "原文からの短い根拠"
    }}
  ]
}}

ルール:
- 日本株コードは 4 桁。社名だけのときも分かる範囲で code を入れる。
- item_id は入力配列の item_id をそのまま入れる。
- ただのニュース紹介、指数コメント、過去実績だけなら neutral か除外。
- 宣伝、PR、ポジショントーク、自信過剰な煽りは risks に入れる。
- 厳格 JSON のみ。markdown や説明は不要。

入力:
{payload}
"""


def _cache_key(items: list[ContentItem]) -> str:
    compact = [(it.creator, it.item_id, it.text[:800]) for it in items]
    text = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _cache_read(key: str) -> dict | None:
    p = CLI_CACHE_DIR / f"parsed_{key}.json"
    if not p.exists():
        return None
    age_h = (datetime.now().timestamp() - p.stat().st_mtime) / 3600
    if age_h > CLI_CACHE_TTL_HOURS:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_write(key: str, data: dict) -> None:
    try:
        (CLI_CACHE_DIR / f"parsed_{key}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    try:
        from news_analyzer import _extract_json_from_cli_output
        parsed = _extract_json_from_cli_output(text)
        if parsed:
            return parsed
    except Exception:
        pass
    s = text.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            return None
    return None


def _normalize(raw: dict, src: ContentItem, method: str) -> ExtractedRecommendation | None:
    code = re.sub(r"\D", "", str(raw.get("code") or ""))[:4]
    if len(code) != 4:
        return None
    side = str(raw.get("side") or "watch").lower().strip()
    if side not in _SIDES:
        side = "watch"
    if side == "neutral":
        return None
    try:
        conviction = int(raw.get("conviction", 5))
    except Exception:
        conviction = 5
    conviction = max(0, min(10, conviction))
    risks = raw.get("risks") or []
    if not isinstance(risks, list):
        risks = [str(risks)]
    return ExtractedRecommendation(
        code=code,
        company_name=str(raw.get("company_name") or code).strip(),
        side=side,
        conviction=conviction,
        horizon=str(raw.get("horizon") or "unknown").strip(),
        thesis=str(raw.get("thesis") or "").strip()[:300],
        risks=[str(r).strip()[:120] for r in risks if str(r).strip()][:5],
        evidence=str(raw.get("evidence") or "").strip()[:240],
        creator=src.creator,
        source_type=src.source_type,
        source_url=src.url,
        published_at=src.published_at,
        item_id=src.item_id,
        extraction_method=method,
    )


def _window(text: str, token: str, width: int = 140) -> str:
    idx = text.lower().find(token.lower())
    if idx < 0:
        return text[:width]
    start = max(0, idx - width // 2)
    end = min(len(text), idx + len(token) + width // 2)
    return text[start:end].replace("\n", " ")


def _side_from_context(ctx: str) -> tuple[str, int]:
    low = ctx.lower()
    has_buy = any(k.lower() in low for k in _BUY_KW)
    has_sell = any(k.lower() in low for k in _SELL_KW)
    if has_buy and not has_sell:
        return "buy", 6
    if has_sell and not has_buy:
        return "sell", 6
    if has_buy and has_sell:
        return "watch", 4
    return "neutral", 0


def fallback_extract(items: list[ContentItem]) -> list[ExtractedRecommendation]:
    out: list[ExtractedRecommendation] = []
    for src in items:
        text = src.text or ""
        low = text.lower()
        risks = [k for k in _RISK_KW if k.lower() in low]
        for hit in resolve_mentions(text):
            code = hit["code"]
            name = hit.get("name") or code
            token = code if code in text else name
            if token not in text:
                for alias in hit.get("aliases") or []:
                    if alias and alias in text:
                        token = alias
                        break
            evidence = _window(text, token, width=220)
            side, conviction = _side_from_context(evidence)
            if side == "neutral":
                continue
            out.append(ExtractedRecommendation(
                code=code,
                company_name=name,
                side=side,
                conviction=conviction,
                horizon="unknown",
                thesis="ルール抽出: 銘柄名/コードと売買ニュアンスを検出",
                risks=risks[:5],
                evidence=evidence[:240],
                creator=src.creator,
                source_type=src.source_type,
                source_url=src.url,
                published_at=src.published_at,
                item_id=src.item_id,
                extraction_method="fallback_rules",
            ))
    return out


def llm_extract(items: list[ContentItem], *, timeout: int = 90) -> tuple[list[ExtractedRecommendation], str]:
    if not items:
        return [], "no_items"
    key = _cache_key(items)
    cached = _cache_read(key)
    if cached:
        recs = []
        src_by_id = {it.item_id: it for it in items}
        for raw in cached.get("recommendations", []):
            src = src_by_id.get(raw.get("item_id")) or items[0]
            rec = _normalize(raw, src, "llm_cached")
            if rec:
                recs.append(rec)
        return recs, "cached"

    compact = [
        {
            "item_id": it.item_id,
            "creator": it.creator,
            "source_type": it.source_type,
            "published_at": it.published_at,
            "title": it.title,
            "url": it.url,
            "text": it.text[:1800],
        }
        for it in items
    ]
    prompt = _PROMPT.format(payload=json.dumps(compact, ensure_ascii=False, indent=2))
    try:
        from ai_prompt import query_ai_cli
        out, status, provider, _ = query_ai_cli(
            prompt, timeout=timeout, fallback_on_unavailable=True
        )
    except Exception as exc:
        return [], f"llm_error: {exc}"
    if not out:
        return [], status

    parsed = _extract_json(out)
    if not parsed or not isinstance(parsed.get("items"), list):
        return [], f"parse_failed: {out[:160]}"

    recs: list[ExtractedRecommendation] = []
    for raw in parsed.get("items", []):
        if not isinstance(raw, dict):
            continue
        src_id = str(raw.get("item_id") or "")
        src = next((it for it in items if it.item_id == src_id), items[0])
        rec = _normalize(raw, src, f"llm_{provider or 'unknown'}")
        if rec:
            recs.append(rec)
    _cache_write(key, {"recommendations": [r.to_dict() for r in recs]})
    return recs, "ok"


def extract_recommendations(items: list[ContentItem], *, use_llm: bool = True,
                            timeout: int = 90) -> tuple[list[ExtractedRecommendation], str]:
    if not use_llm:
        return fallback_extract(items), "fallback_only"
    llm_recs, status = llm_extract(items, timeout=timeout)
    if llm_recs:
        return llm_recs, status
    fallback = fallback_extract(items)
    if fallback:
        return fallback, f"{status}; used_fallback_rules"
    return [], status
