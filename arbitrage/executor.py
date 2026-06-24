"""
executor.py — 套利交易执行器
双腿/多腿同时下单，FOK 立即成交，单腿失败处理。
"""

import json
import time
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from .detector import Opportunity, Leg

log = logging.getLogger("arb.executor")


@dataclass
class ArbPosition:
    """已执行的套利仓位"""
    opportunity_id: str
    strategy: str
    event_title: str
    legs: List[Dict[str, Any]]   # [{market_id, token_id, side, price, shares, order_id, filled}]
    total_cost: float
    expected_profit: float
    status: str = "open"          # "open" | "resolved" | "failed" | "partial"
    opened_at: str = ""
    closed_at: str = ""
    pnl: float = 0.0


class ArbitrageExecutor:
    """执行套利交易"""

    def __init__(self, trader, max_position_pct: float = 0.05,
                 max_slippage: float = 0.02, execution_timeout: float = 5.0,
                 data_dir: str = "data/arbitrage"):
        self.trader = trader
        self.max_position_pct = max_position_pct
        self.max_slippage = max_slippage
        self.exec_timeout = execution_timeout
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.positions: List[ArbPosition] = []
        self._load_positions()

    # ------------------------------------------------------------------
    # 执行套利
    # ------------------------------------------------------------------

    def execute(self, opp: Opportunity, capital: float,
                paper: bool = True) -> Optional[ArbPosition]:
        """
        执行一个套利机会。

        Args:
            opp:     套利机会
            capital: 当前总资金
            paper:   是否为模拟模式

        Returns:
            成功的 ArbPosition，失败返回 None
        """
        # 计算仓位大小（不超过资金的 max_position_pct）
        max_spend = capital * self.max_position_pct
        # 每份成本 = opp.total_cost，最大份额
        shares = max_spend / opp.total_cost
        shares = max(1, int(shares))  # 至少 1 份

        log.info("执行 %s 套利: %s | %d 份 | 成本 $%.2f | 预期净利 %.1f%%",
                 opp.strategy, opp.event_title[:40], shares,
                 shares * opp.total_cost, opp.net_profit_pct * 100)

        opp_id = f"arb_{int(time.time())}_{opp.strategy}"
        position = ArbPosition(
            opportunity_id=opp_id,
            strategy=opp.strategy,
            event_title=opp.event_title,
            legs=[],
            total_cost=shares * opp.total_cost,
            expected_profit=shares * (1.0 - opp.total_cost),
            opened_at=datetime.now(timezone.utc).isoformat(),
        )

        # 逐腿下单
        failed_legs = []
        for i, leg in enumerate(opp.legs):
            # 重新获取实时价格（下单前）
            live_price = self._get_live_price(leg.market_id, leg.token_id)
            if live_price is None:
                live_price = leg.price

            # 检查滑点
            slippage = abs(live_price - leg.price) / leg.price if leg.price > 0 else 0
            if slippage > self.max_slippage:
                log.warning("腿 %d 滑点过大: %.1f%% > %.1f%%, 跳过",
                            i, slippage * 100, self.max_slippage * 100)
                failed_legs.append(i)
                continue

            # 下单
            actual_price = min(live_price, leg.price + self.max_slippage)
            order_id = self.trader.buy(
                token_id=leg.token_id,
                price=round(actual_price, 2),
                shares=shares,
                order_type="FOK" if not paper else "LIMIT",
            )

            leg_record = {
                "market_id": leg.market_id,
                "token_id": leg.token_id,
                "outcome_name": leg.outcome_name,
                "side": leg.side,
                "price": actual_price,
                "shares": shares,
                "order_id": order_id,
                "filled": shares if paper else 0,  # paper 模式假设全部成交
                "status": "filled" if order_id else "failed",
            }
            position.legs.append(leg_record)

            if not order_id:
                log.error("腿 %d 下单失败: %s", i, leg.outcome_name)
                failed_legs.append(i)

        # 处理部分成交
        if failed_legs:
            if len(failed_legs) == len(opp.legs):
                log.error("所有腿都失败，放弃套利")
                position.status = "failed"
            else:
                log.warning("%d/%d 腿失败，尝试平仓已成交的腿",
                            len(failed_legs), len(opp.legs))
                position.status = "partial"
                self._close_partial_position(position)
        else:
            position.status = "open"
            if not paper:
                # 更新实际总成本
                position.total_cost = sum(
                    l["price"] * l["filled"] for l in position.legs
                )

        self.positions.append(position)
        self._save_positions()

        log.info("套利执行完成: %s | 状态=%s | 成本=$%.2f",
                 opp_id, position.status, position.total_cost)
        return position

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _get_live_price(self, market_id: str, token_id: str) -> Optional[float]:
        """获取实时价格"""
        try:
            import requests
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets/{market_id}",
                timeout=(3, 5),
            )
            data = r.json()
            prices = json.loads(data.get("outcomePrices", "[]"))
            clob_ids = data.get("clobTokenIds", "")
            if clob_ids.startswith("["):
                token_list = json.loads(clob_ids)
            else:
                token_list = [t.strip() for t in clob_ids.split(",")] if clob_ids else []

            for i, tid in enumerate(token_list):
                if tid == token_id and i < len(prices):
                    return float(prices[i])
        except Exception as e:
            log.debug("获取实时价格失败: %s", e)
        return None

    def _close_partial_position(self, position: ArbPosition):
        """平仓部分成交的套利（卖出已成交的腿）"""
        for leg in position.legs:
            if leg["status"] == "filled" and leg["filled"] > 0:
                order_id = self.trader.sell(
                    token_id=leg["token_id"],
                    price=leg["price"] * 0.95,  # 略低于买入价确保成交
                    shares=leg["filled"],
                    order_type="IOC",
                )
                log.info("平仓腿: %s | order=%s", leg["outcome_name"], order_id)

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def _save_positions(self):
        """保存仓位到文件"""
        path = self.data_dir / "positions.json"
        data = [asdict(p) for p in self.positions]
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_positions(self):
        """从文件加载仓位"""
        path = self.data_dir / "positions.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.positions = [ArbPosition(**d) for d in data]
            except Exception as e:
                log.warning("加载仓位失败: %s", e)
                self.positions = []

    def get_open_positions(self) -> List[ArbPosition]:
        """获取所有未平仓的套利仓位"""
        return [p for p in self.positions if p.status == "open"]
