#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arbitrage_bot.py — Polymarket 自动套利机器人
================================================
基于 prediction-market-arbitrage 技能库的策略，
在 Polymarket 上自动寻找并执行套利交易。

使用方式:
    python arbitrage_bot.py                    # paper 模式
    python arbitrage_bot.py --real             # 实盘模式
    python arbitrage_bot.py --capital 200      # 指定资金
    python arbitrage_bot.py --interval 15      # 15秒扫描间隔
    python arbitrage_bot.py --once             # 只扫描一次
"""

import sys
import json
import time
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# =============================================================================
# 日志
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("arb")

# =============================================================================
# 配置加载
# =============================================================================

def load_config() -> dict:
    """加载配置文件，敏感字段从 .env 覆盖"""
    from config_helper import load_config as _load
    return _load()


def get_arb_config(cfg: dict) -> dict:
    """提取套利相关配置"""
    arb = cfg.get("arbitrage", {})
    return {
        "enabled": arb.get("enabled", True),
        "mode": arb.get("mode", cfg.get("trading_mode", "paper")),
        "capital": arb.get("capital", 200.0),
        "max_position_pct": arb.get("max_position_pct", 0.05),
        "min_profit_threshold": arb.get("min_profit_threshold", 0.03),
        "max_slippage": arb.get("max_slippage", 0.02),
        "daily_loss_limit_pct": arb.get("daily_loss_limit_pct", 0.02),
        "max_drawdown_pct": arb.get("max_drawdown_pct", 0.10),
        "min_liquidity": arb.get("min_liquidity", 50),
        "scan_interval": arb.get("scan_interval", 30),
        "max_events": arb.get("max_events", 50),
        "strategies": arb.get("strategies", {
            "complementary": True,
            "multi_outcome": True,
            "correlated": False,
        }),
    }


# =============================================================================
# 状态持久化
# =============================================================================

class ArbState:
    """套利系统状态"""

    def __init__(self, data_dir: str = "data/arbitrage"):
        self.path = Path(data_dir) / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "balance": 0.0,
            "peak_balance": 0.0,
            "daily_pnl": 0.0,
            "daily_pnl_date": "",
            "total_trades": 0,
            "total_pnl": 0.0,
            "last_scan": "",
        }

    def save(self):
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def update_pnl(self, pnl: float, capital: float):
        """更新盈亏"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 重置每日盈亏（新的一天）
        if self.data.get("daily_pnl_date") != today:
            self.data["daily_pnl"] = 0.0
            self.data["daily_pnl_date"] = today

        self.data["daily_pnl"] += pnl
        self.data["total_pnl"] += pnl
        self.data["total_trades"] += 1
        self.data["balance"] = capital + self.data["total_pnl"]
        self.data["peak_balance"] = max(
            self.data["peak_balance"], self.data["balance"]
        )
        self.save()

    def check_daily_loss_limit(self, capital: float, limit_pct: float) -> bool:
        """检查是否达到日亏损限制"""
        limit = capital * limit_pct
        if self.data["daily_pnl"] < -limit:
            log.warning("⚠️ 日亏损限制触发: $%.2f < -$%.2f",
                        self.data["daily_pnl"], limit)
            return True
        return False

    def check_drawdown(self, capital: float, max_pct: float) -> bool:
        """检查是否达到最大回撤"""
        peak = self.data.get("peak_balance", capital)
        if peak <= 0:
            return False
        drawdown = (peak - self.data["balance"]) / peak
        if drawdown > max_pct:
            log.warning("⚠️ 最大回撤触发: %.1f%% > %.1f%%",
                        drawdown * 100, max_pct * 100)
            return True
        return False


# =============================================================================
# 交易客户端初始化（复用 bot_v2.py 模式）
# =============================================================================

_trader = None

def get_trader(cfg: dict, force_paper: bool = False):
    """获取交易客户端（单例）"""
    global _trader
    if _trader is not None:
        return _trader

    is_real = (not force_paper and
               cfg.get("trading_mode") == "real" and
               bool(cfg.get("poly_private_key")))

    if is_real:
        try:
            from trader import PolymarketTrader
            _trader = PolymarketTrader(
                private_key=cfg["poly_private_key"],
                funder=cfg.get("poly_funder", ""),
                chain_id=cfg.get("poly_chain_id", 137),
                signature_type=cfg.get("poly_signature_type", 0),
                paper=False,
            )
            if not _trader.connect():
                log.warning("CLOB 连接失败，降级为 paper 模式")
                from trader import PolymarketTrader
                _trader = PolymarketTrader(paper=True)
        except ImportError:
            log.warning("py-clob-client 未安装，降级为 paper 模式")
            from trader import PolymarketTrader
            _trader = PolymarketTrader(paper=True)
    else:
        from trader import PolymarketTrader
        _trader = PolymarketTrader(paper=True)

    return _trader


# =============================================================================
# 主循环
# =============================================================================

def run_arbitrage(cfg: dict, arb_cfg: dict, once: bool = False):
    """主套利循环"""

    from arbitrage import MarketScanner, ArbitrageDetector, ArbitrageExecutor
    from arbitrage import ArbitrageMonitor, ArbNotifier

    paper = arb_cfg["mode"] == "paper"
    capital = arb_cfg["capital"]

    log.info("=" * 60)
    log.info("Polymarket 自动套利系统启动")
    log.info("模式: %s | 资金: $%.0f | 间隔: %ds",
             "PAPER" if paper else "REAL", capital, arb_cfg["scan_interval"])
    log.info("策略: %s",
             ", ".join(k for k, v in arb_cfg["strategies"].items() if v))
    log.info("=" * 60)

    # 初始化组件
    trader = get_trader(cfg, force_paper=paper)
    scanner = MarketScanner(
        min_volume=arb_cfg["min_liquidity"],
    )
    detector = ArbitrageDetector(
        min_profit_threshold=arb_cfg["min_profit_threshold"],
        min_liquidity=arb_cfg["min_liquidity"],
    )
    executor = ArbitrageExecutor(
        trader=trader,
        max_position_pct=arb_cfg["max_position_pct"],
        max_slippage=arb_cfg["max_slippage"],
    )
    monitor = ArbitrageMonitor(executor)
    notifier = ArbNotifier(cfg)
    state = ArbState()

    # 尝试加载之前的状态
    capital = max(capital, state.data.get("balance", capital))

    scan_count = 0

    while True:
        scan_count += 1
        scan_start = time.time()

        log.info("--- 扫描 #%d 开始 ---", scan_count)

        # 风控检查
        if state.check_daily_loss_limit(capital, arb_cfg["daily_loss_limit_pct"]):
            notifier.notify_risk_alert(
                "日亏损限制",
                f"今日亏损: ${state.data['daily_pnl']:.2f}，暂停交易",
            )
            if not once:
                log.info("等待明天重置...")
                time.sleep(3600)
                continue
            else:
                break

        if state.check_drawdown(capital, arb_cfg["max_drawdown_pct"]):
            notifier.notify_risk_alert(
                "最大回撤",
                f"当前余额: ${state.data['balance']:.2f}，暂停交易",
            )
            if not once:
                time.sleep(3600)
                continue
            else:
                break

        try:
            # 1. 扫描市场
            binary_markets = scanner.scan_binary_markets(
                max_events=arb_cfg["max_events"]
            )
            multi_events = scanner.scan_multi_outcome_events(
                max_events=arb_cfg["max_events"]
            )

            # 2. 检测套利机会
            opps = detector.scan_all_opportunities(binary_markets, multi_events)

            if opps:
                log.info("发现 %d 个套利机会", len(opps))
                for opp in opps:
                    log.info("  📊 %s: %s | 净利 %.1f%%",
                             opp.strategy, opp.description, opp.net_profit_pct * 100)
            else:
                log.info("本轮未发现套利机会 (%d 二元市场, %d 多结果事件)",
                         len(binary_markets), len(multi_events))

            # 3. 执行最优机会
            executed = 0
            for opp in opps:
                pos = executor.execute(opp, capital, paper=paper)
                if pos and pos.status in ("open", "partial"):
                    executed += 1
                    pnl = -pos.total_cost  # 先记为成本，结算时再加收入
                    state.update_pnl(pnl, capital)
                    notifier.notify_executed(
                        pos.opportunity_id, pos.event_title,
                        pos.total_cost, pos.expected_profit,
                    )

                    # 每次只执行一个机会（小资金模式）
                    if capital < 500:
                        break

            # 4. 检查已有仓位结算
            resolutions = monitor.check_all_resolutions()
            for res in resolutions:
                state.update_pnl(res["pnl"], capital)
                notifier.notify_resolved(
                    res["event_title"], res["pnl"], res["pnl_pct"]
                )

            # 5. 检查未成交腿
            unfilled = monitor.check_unfilled_legs()
            if unfilled:
                log.warning("⚠️ 有 %d 个未成交腿", len(unfilled))

        except KeyboardInterrupt:
            log.info("用户中断，保存状态...")
            state.save()
            break
        except Exception as e:
            log.error("扫描出错: %s", e, exc_info=True)
            time.sleep(10)
            continue

        # 统计
        pnl_stats = monitor.calculate_pnl()
        scan_duration = time.time() - scan_start
        log.info("--- 扫描 #%d 完成 (%.1fs) | 总交易=%d 盈亏=$%.2f ROI=%.1f%% ---",
                 scan_count, scan_duration,
                 pnl_stats["total_trades"], pnl_stats["total_pnl"],
                 pnl_stats["roi"])

        state.data["last_scan"] = datetime.now(timezone.utc).isoformat()
        state.save()

        if once:
            log.info("单次扫描模式，退出")
            break

        # 等待下一轮
        time.sleep(arb_cfg["scan_interval"])


# =============================================================================
# CLI 入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket 自动套利机器人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--real", action="store_true",
                        help="使用真实交易模式（默认 paper）")
    parser.add_argument("--capital", type=float, default=None,
                        help="初始资金（默认从配置读取）")
    parser.add_argument("--interval", type=int, default=None,
                        help="扫描间隔秒数（默认从配置读取）")
    parser.add_argument("--once", action="store_true",
                        help="只扫描一次，不循环")
    parser.add_argument("--status", action="store_true",
                        help="显示当前状态和盈亏统计")

    args = parser.parse_args()

    cfg = load_config()
    arb_cfg = get_arb_config(cfg)

    # CLI 参数覆盖
    if args.real:
        arb_cfg["mode"] = "real"
    if args.capital is not None:
        arb_cfg["capital"] = args.capital
    if args.interval is not None:
        arb_cfg["scan_interval"] = args.interval

    # 状态查看
    if args.status:
        state = ArbState()
        print(json.dumps(state.data, indent=2))
        from arbitrage.executor import ArbitrageExecutor
        from arbitrage.monitor import ArbitrageMonitor
        trader = get_trader(cfg, force_paper=True)
        executor = ArbitrageExecutor(trader=trader, data_dir="data/arbitrage")
        monitor = ArbitrageMonitor(executor)
        stats = monitor.calculate_pnl()
        print("\n盈亏统计:")
        print(json.dumps(stats, indent=2))
        return

    if not arb_cfg["enabled"]:
        log.info("套利系统已禁用（config.json → arbitrage.enabled = false）")
        return

    run_arbitrage(cfg, arb_cfg, once=args.once)


if __name__ == "__main__":
    main()
