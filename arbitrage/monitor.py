"""
monitor.py — 套利仓位监控器
检查结算状态、未成交腿、计算盈亏。
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional
from .executor import ArbPosition

log = logging.getLogger("arb.monitor")


class ArbitrageMonitor:
    """监控套利仓位的结算和盈亏"""

    def __init__(self, executor):
        self.executor = executor

    # ------------------------------------------------------------------
    # 结算检查
    # ------------------------------------------------------------------

    def check_all_resolutions(self) -> List[dict]:
        """检查所有未平仓套利的结算状态"""
        results = []
        for pos in self.executor.get_open_positions():
            result = self._check_resolution(pos)
            if result:
                results.append(result)
        return results

    def _check_resolution(self, pos: ArbPosition) -> Optional[dict]:
        """检查单个套利仓位是否已结算"""
        import requests

        # 套利的所有腿都是 BUY，结算时有一条会赢，其他会输
        # 但因为我们买了所有结果，所以无论哪个赢，我们都会收到 $1/份
        # 只需要检查任意一条腿是否已结算

        if not pos.legs:
            return None

        first_leg = pos.legs[0]
        market_id = first_leg.get("market_id", "")
        if not market_id:
            return None

        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets/{market_id}",
                timeout=(3, 5),
            )
            data = r.json()
            prices = json.loads(data.get("outcomePrices", "[]"))
            is_closed = data.get("closed", False)

            if not is_closed:
                return None

            # 市场已结算
            # 计算盈亏：我们买了所有结果，赢的那条腿付 $1/份
            shares = first_leg.get("shares", 0)
            payout = shares * 1.0  # 无论哪个赢，每份都收回 $1
            pnl = payout - pos.total_cost

            pos.status = "resolved"
            pos.closed_at = datetime.now(timezone.utc).isoformat()
            pos.pnl = pnl
            self.executor._save_positions()

            result = {
                "opportunity_id": pos.opportunity_id,
                "event_title": pos.event_title,
                "shares": shares,
                "cost": pos.total_cost,
                "payout": payout,
                "pnl": pnl,
                "pnl_pct": (pnl / pos.total_cost * 100) if pos.total_cost > 0 else 0,
            }
            log.info("✅ 套利结算: %s | 盈亏=$%.2f (%.1f%%)",
                     pos.event_title[:40], pnl, result["pnl_pct"])
            return result

        except Exception as e:
            log.debug("检查结算失败 %s: %s", market_id, e)
            return None

    # ------------------------------------------------------------------
    # 未成交腿检查
    # ------------------------------------------------------------------

    def check_unfilled_legs(self) -> List[dict]:
        """检查是否有部分成交的仓位（单腿风险）"""
        unfilled = []
        for pos in self.executor.get_open_positions():
            for leg in pos.legs:
                if leg.get("status") != "filled":
                    unfilled.append({
                        "opportunity_id": pos.opportunity_id,
                        "event_title": pos.event_title,
                        "leg": leg,
                    })
                    log.warning("⚠️ 未成交腿: %s | %s",
                                pos.event_title[:30], leg.get("outcome_name"))
        return unfilled

    # ------------------------------------------------------------------
    # 盈亏统计
    # ------------------------------------------------------------------

    def calculate_pnl(self) -> dict:
        """计算总盈亏统计"""
        all_positions = self.executor.positions
        resolved = [p for p in all_positions if p.status == "resolved"]
        open_pos = [p for p in all_positions if p.status == "open"]
        failed = [p for p in all_positions if p.status in ("failed", "partial")]

        total_pnl = sum(p.pnl for p in resolved)
        total_invested = sum(p.total_cost for p in all_positions)
        wins = sum(1 for p in resolved if p.pnl > 0)
        losses = sum(1 for p in resolved if p.pnl <= 0)

        return {
            "total_trades": len(all_positions),
            "open": len(open_pos),
            "resolved": len(resolved),
            "failed": len(failed),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / max(1, len(resolved)),
            "total_pnl": total_pnl,
            "total_invested": total_invested,
            "roi": total_pnl / max(1, total_invested) * 100,
        }
