"""
scanner.py — Polymarket 市场扫描器
扫描 Gamma API 获取所有活跃市场的价格数据。
"""

import json
import logging
import time
import requests
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any

log = logging.getLogger("arb.scanner")

GAMMA_API = "https://gamma-api.polymarket.com"


# ======================================================================
# 数据结构
# ======================================================================

@dataclass
class Outcome:
    """单个结果（一个 market 的一侧）"""
    token_id: str
    name: str
    best_ask: float  # 最低卖出价（买入成本）
    best_bid: float  # 最高买入价（卖出收入）
    volume: float


@dataclass
class BinaryMarket:
    """二元市场（YES/NO）"""
    market_id: str
    question: str
    slug: str
    yes: Outcome
    no: Outcome
    closed: bool = False
    end_date: str = ""


@dataclass
class MultiOutcomeEvent:
    """多结果事件（包含多个子市场）"""
    event_id: str
    title: str
    slug: str
    markets: List[BinaryMarket] = field(default_factory=list)


# ======================================================================
# 扫描器
# ======================================================================

class MarketScanner:
    """扫描 Polymarket 上所有活跃市场"""

    def __init__(self, min_volume: float = 100, timeout: tuple = (5, 10)):
        self.min_volume = min_volume
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "WeatherBot-Arb/1.0",
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------
    # 主扫描方法
    # ------------------------------------------------------------------

    def scan_active_events(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """获取所有活跃事件列表（带重试）"""
        for attempt in range(3):
            try:
                r = self._session.get(
                    f"{GAMMA_API}/events",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": limit,
                        "offset": offset,
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                    timeout=self.timeout,
                )
                r.raise_for_status()
                return r.json() if isinstance(r.json(), list) else []
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                else:
                    log.warning("扫描活跃事件失败（3次重试）: %s", e)
        return []

    def fetch_market_detail(self, market_id: str) -> Optional[Dict]:
        """获取单个市场详情"""
        try:
            r = self._session.get(
                f"{GAMMA_API}/markets/{market_id}",
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("获取市场 %s 失败: %s", market_id, e)
            return None

    # ------------------------------------------------------------------
    # 二元市场扫描
    # ------------------------------------------------------------------

    def scan_binary_markets(self, max_events: int = 50) -> List[BinaryMarket]:
        """
        扫描所有活跃事件，提取二元市场（YES/NO）。
        对每个事件的每个子市场，解析 YES/NO 的 bestAsk。
        """
        results = []
        events = self.scan_active_events(limit=max_events)

        for event in events:
            markets_raw = event.get("markets", [])
            if not markets_raw:
                continue

            for m in markets_raw:
                if m.get("closed", False):
                    continue

                binary = self._parse_binary_market(m)
                if binary and (binary.yes.volume >= self.min_volume or
                               binary.no.volume >= self.min_volume):
                    results.append(binary)

        log.info("扫描到 %d 个活跃二元市场", len(results))
        return results

    def _parse_binary_market(self, m: Dict) -> Optional[BinaryMarket]:
        """解析 Gamma API 返回的市场数据为 BinaryMarket"""
        try:
            outcomes = m.get("outcomes", [])
            clob_ids_raw = m.get("clobTokenIds", "")
            prices_str = m.get("outcomePrices", "[]")

            # clobTokenIds 是 JSON 数组字符串: '["id1", "id2"]'
            if clob_ids_raw.startswith("["):
                token_ids = json.loads(clob_ids_raw)
            else:
                token_ids = [t.strip() for t in clob_ids_raw.split(",")] if clob_ids_raw else []

            # outcomes 也可能是 JSON 数组字符串
            if isinstance(outcomes, str) and outcomes.startswith("["):
                outcomes = json.loads(outcomes)

            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str

            if len(outcomes) < 2 or len(token_ids) < 2 or len(prices) < 2:
                return None

            yes_price = float(prices[0])
            no_price = float(prices[1])

            # bestAsk 是 YES 侧的真实最低卖价，比 outcomePrices 更准确
            real_best_ask = float(m.get("bestAsk", 0)) if m.get("bestAsk") else yes_price
            real_best_bid = float(m.get("bestBid", 0)) if m.get("bestBid") else yes_price

            # 如果 bestAsk 为 0 或无效，使用 outcomePrices
            if real_best_ask <= 0:
                real_best_ask = yes_price
            if real_best_bid <= 0:
                real_best_bid = yes_price

            # YES 侧
            yes = Outcome(
                token_id=token_ids[0],
                name=outcomes[0],
                best_ask=real_best_ask,
                best_bid=real_best_bid,
                volume=float(m.get("volume", 0)),
            )

            # NO 侧: bestAsk_no = 1 - bestBid_yes, bestBid_no = 1 - bestAsk_yes
            best_ask_no = 1.0 - real_best_bid
            best_bid_no = 1.0 - real_best_ask
            no = Outcome(
                token_id=token_ids[1],
                name=outcomes[1],
                best_ask=max(0.01, best_ask_no),  # 至少 0.01
                best_bid=max(0.01, best_bid_no),
                volume=float(m.get("volume", 0)),
            )

            return BinaryMarket(
                market_id=str(m.get("id", "")),
                question=m.get("question", ""),
                slug=m.get("slug", m.get("market_slug", "")),
                yes=yes,
                no=no,
                closed=m.get("closed", False),
                end_date=m.get("endDate", ""),
            )
        except Exception as e:
            log.debug("解析市场失败: %s", e)
            return None

    # ------------------------------------------------------------------
    # 多结果事件扫描
    # ------------------------------------------------------------------

    def scan_multi_outcome_events(self, max_events: int = 50) -> List[MultiOutcomeEvent]:
        """
        扫描包含 3+ 子市场的事件（多结果市场）。
        这类市场是套利的富矿。
        """
        results = []
        events = self.scan_active_events(limit=max_events)

        for event in events:
            markets_raw = event.get("markets", [])
            if len(markets_raw) < 3:
                continue  # 只关注 3+ 结果的事件

            moe = MultiOutcomeEvent(
                event_id=str(event.get("id", "")),
                title=event.get("title", ""),
                slug=event.get("slug", ""),
            )

            for m in markets_raw:
                if m.get("closed", False):
                    continue
                binary = self._parse_binary_market(m)
                if binary:
                    moe.markets.append(binary)

            if len(moe.markets) >= 3:
                results.append(moe)

        log.info("扫描到 %d 个多结果事件（3+ 子市场）", len(results))
        return results

    # ------------------------------------------------------------------
    # 获取实时价格（下单前精确查询）
    # ------------------------------------------------------------------

    def get_live_ask(self, market_id: str, outcome_index: int = 0) -> Optional[float]:
        """获取指定市场的实时 bestAsk（下单前调用）"""
        detail = self.fetch_market_detail(market_id)
        if not detail:
            return None

        try:
            prices = json.loads(detail.get("outcomePrices", "[]"))
            if outcome_index < len(prices):
                return float(prices[outcome_index])
        except Exception:
            pass
        return None

    def get_token_ids(self, market_id: str) -> List[Dict[str, Any]]:
        """获取市场的所有 token_id（复用 trader.py 的模式）"""
        detail = self.fetch_market_detail(market_id)
        if not detail:
            return []

        outcomes = detail.get("outcomes", [])
        if isinstance(outcomes, str) and outcomes.startswith("["):
            outcomes = json.loads(outcomes)

        clob_ids_raw = detail.get("clobTokenIds", "")
        if clob_ids_raw.startswith("["):
            token_ids = json.loads(clob_ids_raw)
        else:
            token_ids = [t.strip() for t in clob_ids_raw.split(",")] if clob_ids_raw else []

        prices = json.loads(detail.get("outcomePrices", "[]"))

        result = []
        for i, name in enumerate(outcomes):
            result.append({
                "outcome_index": i,
                "token_id": token_ids[i] if i < len(token_ids) else "",
                "name": name,
                "price": float(prices[i]) if i < len(prices) else 0.0,
            })
        return result
