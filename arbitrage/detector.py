"""
detector.py — 套利机会检测器
实现三种策略：互补套利、多结果套利、相关市场套利。
"""

import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from .scanner import BinaryMarket, MultiOutcomeEvent, Outcome

log = logging.getLogger("arb.detector")


# ======================================================================
# 数据结构
# ======================================================================

@dataclass
class Leg:
    """一笔交易的一条腿"""
    market_id: str
    token_id: str
    outcome_name: str
    side: str           # "BUY" or "SELL"
    price: float        # 执行价格
    shares: float = 0.0 # 分配的份额


@dataclass
class Opportunity:
    """一个套利机会"""
    strategy: str           # "complementary" | "multi_outcome" | "correlated"
    description: str
    gross_profit_pct: float # 毛利润率
    net_profit_pct: float   # 扣费后净利润率
    total_cost: float       # 总成本（每份）
    legs: List[Leg] = field(default_factory=list)
    event_title: str = ""
    event_slug: str = ""


# ======================================================================
# 费用模型
# ======================================================================

# Polymarket taker fee: ~2% on profit
TAKER_FEE_PCT = 0.02
# Gas fee on Polygon
GAS_FEE = 0.01


def estimate_fees(total_cost: float) -> float:
    """估算总费用（每 $1 仓位）"""
    profit = max(0, 1.0 - total_cost)
    taker_fee = profit * TAKER_FEE_PCT
    return taker_fee + GAS_FEE / 100


# ======================================================================
# 互斥性检测
# ======================================================================

# 非互斥问题模式：阈值型、包含型（"reach $X"、"before 2027"、"strike N"）
_NON_EXCLUSIVE_PATTERNS = [
    r'reach\s*\$?\d',
    r'dip\s+to\s*\$?\d',
    r'above\s*\$?\d',
    r'below\s*\$?\d',
    r'strike\s+\d+',
    r'before\s+\d{4}',
]

# 互斥型问题模式：赢家选举、比赛胜负
_EXCLUSIVE_KEYWORDS = [
    r'\bwin\b',
    r'\bend\s+in\s+a\s+draw\b',
    r'\belect\b',
]


def _question_to_template(question: str) -> str:
    """把问题文本转化为模板，只保留结构，替换变量为占位符"""
    t = re.sub(r'\$[\d,]+(\.\d+)?', '$X', question)
    t = re.sub(r'\b[A-Z][a-z]+ [A-Z][a-z]+(\s[A-Z][a-z]+)*\b', 'NAME', t)
    t = re.sub(r'\b\d{4}\b', 'YEAR', t)
    t = re.sub(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\b',
               'DATE', t)
    t = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', 'DATE', t)
    return t


def _has_non_exclusive_pattern(questions: list) -> bool:
    """检测问题列表是否包含非互斥模式（阈值/时间包含）"""
    all_q = ' '.join(questions).lower()
    for pat in _NON_EXCLUSIVE_PATTERNS:
        if re.search(pat, all_q):
            return True
    return False


def _is_winner_election_pattern(questions: list) -> bool:
    """检测是否为赢家选举模式（体育比赛/选举等）"""
    # 体育比赛特征：同时出现 win 和 draw（三种结果）
    has_win = any(re.search(r'\bwin\b', q.lower()) for q in questions)
    has_draw = any(re.search(r'\bdraw\b', q.lower()) for q in questions)

    if has_win and has_draw:
        return True

    # 选举/竞赛特征：所有问题都问 "Will X win Y?"
    if len(questions) >= 3:
        win_count = sum(1 for q in questions if re.search(r'\bwill\b.*\bwin\b', q.lower()))
        if win_count / len(questions) >= 0.8:
            return True

    return False


def is_mutually_exclusive(markets) -> bool:
    """
    判断一组子市场是否互斥。
    互斥 = 恰好只有一个结果会发生（选一个赢家、体育比赛等）。
    """
    if len(markets) < 2:
        return False

    questions = [m.question for m in markets if hasattr(m, 'question')]
    if len(questions) < 2:
        return False

    # 第一步：排除明显非互斥的
    if _has_non_exclusive_pattern(questions):
        log.debug("检测到非互斥模式，跳过")
        return False

    # 第二步：检测赢家选举模式（体育/选举/竞赛）
    if _is_winner_election_pattern(questions):
        return True

    # 第三步：模板一致性检查（结构完全相同的为互斥）
    templates = [_question_to_template(q) for q in questions]
    unique_templates = set(templates)
    if len(unique_templates) == 1:
        return True

    if len(unique_templates) <= 2:
        from collections import Counter
        template_counts = Counter(templates)
        most_common_count = template_counts.most_common(1)[0][1]
        if most_common_count / len(templates) >= 0.7:
            return True

    log.debug("问题模板不统一（%d 种模板），非互斥", len(unique_templates))
    return False


# ======================================================================
# 检测器
# ======================================================================

class ArbitrageDetector:
    """检测三种套利策略"""

    def __init__(self, min_profit_threshold: float = 0.03,
                 min_liquidity: float = 50):
        self.min_profit = min_profit_threshold
        self.min_liquidity = min_liquidity

    # ------------------------------------------------------------------
    # 策略 1: 互补套利（二元市场 YES + NO < $1）
    # ------------------------------------------------------------------

    def detect_complementary(self, market: BinaryMarket) -> Optional[Opportunity]:
        """
        检测二元市场互补套利。
        当 YES_ask + NO_ask < 1.0 时，同时买入两边可获利。
        """
        total_cost = market.yes.best_ask + market.no.best_ask

        if total_cost >= 1.0:
            return None

        gross_profit = 1.0 - total_cost
        gross_pct = gross_profit / total_cost

        fees = estimate_fees(total_cost)
        net_pct = (gross_profit - fees) / total_cost

        if net_pct < self.min_profit:
            return None

        # 检查流动性
        if market.yes.volume < self.min_liquidity or market.no.volume < self.min_liquidity:
            log.debug("跳过 %s: 流动性不足 (YES=%d, NO=%d)",
                      market.question[:40], market.yes.volume, market.no.volume)
            return None

        return Opportunity(
            strategy="complementary",
            description=f"YES({market.yes.best_ask:.3f}) + NO({market.no.best_ask:.3f}) = {total_cost:.3f} < 1.0",
            gross_profit_pct=gross_pct,
            net_profit_pct=net_pct,
            total_cost=total_cost,
            legs=[
                Leg(market_id=market.market_id, token_id=market.yes.token_id,
                    outcome_name=market.yes.name, side="BUY", price=market.yes.best_ask),
                Leg(market_id=market.market_id, token_id=market.no.token_id,
                    outcome_name=market.no.name, side="BUY", price=market.no.best_ask),
            ],
            event_title=market.question,
            event_slug=market.slug,
        )

    # ------------------------------------------------------------------
    # 策略 2: 多结果套利（所有 YES 之和 < $1）
    # ------------------------------------------------------------------

    def detect_multi_outcome(self, event: MultiOutcomeEvent) -> Optional[Opportunity]:
        """
        检测多结果事件套利。
        当所有结果的 YES_ask 之和 < 1.0 且结果互斥时，买入全部可获利。
        """
        if len(event.markets) < 3:
            return None

        # 关键：只处理真正互斥的事件
        if not is_mutually_exclusive(event.markets):
            log.debug("跳过非互斥事件: %s（子市场结果可同时发生）",
                      event.title[:50])
            return None

        total_cost = 0.0
        legs = []

        for m in event.markets:
            yes_ask = m.yes.best_ask
            total_cost += yes_ask
            legs.append(Leg(
                market_id=m.market_id,
                token_id=m.yes.token_id,
                outcome_name=m.yes.name,
                side="BUY",
                price=yes_ask,
            ))

        if total_cost >= 1.0:
            return None

        gross_profit = 1.0 - total_cost
        gross_pct = gross_profit / total_cost

        fees = estimate_fees(total_cost)
        net_pct = (gross_profit - fees) / total_cost

        if net_pct < self.min_profit:
            return None

        # 检查所有腿的流动性（取最低）
        min_vol = min(m.yes.volume for m in event.markets)
        if min_vol < self.min_liquidity:
            log.debug("跳过 %s: 最低流动性 %d < %d",
                      event.title[:40], min_vol, self.min_liquidity)
            return None

        return Opportunity(
            strategy="multi_outcome",
            description=f"{len(event.markets)} 个结果, 总价={total_cost:.3f}, 毛利={gross_pct:.1%}",
            gross_profit_pct=gross_pct,
            net_profit_pct=net_pct,
            total_cost=total_cost,
            legs=legs,
            event_title=event.title,
            event_slug=event.slug,
        )

    # ------------------------------------------------------------------
    # 策略 3: 相关市场不一致套利
    # ------------------------------------------------------------------

    def detect_correlated(self, group: List[BinaryMarket],
                          group_name: str = "") -> Optional[Opportunity]:
        """
        检测相关市场组中的概率不一致。
        例如温度市场的 "above X" 和各个精确桶之间应该概率一致。
        这是一个更复杂的策略，Phase 2 实现。
        """
        # TODO: Phase 2 — 实现相关市场不一致检测
        # 基本逻辑:
        # 1. 解析 "above X" / "below X" 类型的市场
        # 2. 用精确桶市场的 YES 价格推算 "above X" 的理论价格
        # 3. 如果偏差超过阈值，构建合成头寸套利
        return None

    # ------------------------------------------------------------------
    # 批量检测
    # ------------------------------------------------------------------

    def scan_all_opportunities(
        self,
        binary_markets: List[BinaryMarket],
        multi_events: List[MultiOutcomeEvent],
    ) -> List[Opportunity]:
        """扫描所有市场，返回所有套利机会（按利润率排序）"""
        opps: List[Opportunity] = []

        # 互补套利
        for m in binary_markets:
            opp = self.detect_complementary(m)
            if opp:
                opps.append(opp)
                log.info("🎯 互补套利: %s → 净利 %.1f%%",
                         opp.event_title[:50], opp.net_profit_pct * 100)

        # 多结果套利
        for e in multi_events:
            opp = self.detect_multi_outcome(e)
            if opp:
                opps.append(opp)
                log.info("🎯 多结果套利: %s → 净利 %.1f%% (%d 条腿)",
                         e.title[:50], opp.net_profit_pct * 100, len(opp.legs))

        # 按净利润率排序（降序）
        opps.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opps
