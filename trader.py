#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trader.py — Polymarket CLOB 实盘交易客户端

封装 py-clob-client，提供下单、撤单、查询持仓、查询余额等功能。
所有操作支持 paper / real 两种模式。

凭证是直接从钱包私钥派生的，不需要去网站申请。

使用方式:
    from trader import PolymarketTrader

    trader = PolymarketTrader(private_key="你的私钥", funder="你的钱包地址")

    # 买入
    order_id = trader.buy(token_id, price, shares)

    # 卖出
    order_id = trader.sell(token_id, price, shares)

    # 查询 USDC 余额
    balance = trader.get_balance()

    # 查询持仓
    orders = trader.get_orders()
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [trader] %(levelname)s %(message)s")
log = logging.getLogger("trader")

# ======================================================================
# py-clob-client 导入（仅在真实模式需要时加载）
# ======================================================================
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType, OpenOrderParams
    from py_clob_client.order_builder.constants import BUY, SELL
    HAS_CLOB = True
except ImportError:
    HAS_CLOB = False
    log.warning("py-clob-client 未安装。实盘交易需要: pip install py-clob-client")


class PolymarketTrader:
    """Polymarket CLOB 交易客户端"""

    def __init__(
        self,
        private_key: str = "",
        funder: str = "",
        chain_id: int = 137,
        signature_type: int = 0,
        paper: bool = True,
    ):
        """
        Args:
            private_key:    Ethereum 私钥（十六进制，可带/不带 0x 前缀）
            funder:         钱包地址（持有 USDC 和资金的实际地址）
            chain_id:       Polygon 链 ID，默认 137
            signature_type: 0=EOA/MetaMask, 1=Email/Magic wallet
            paper:          True 纸面交易（模拟），False 实盘
        """
        self.private_key = private_key
        self.funder = funder
        self.chain_id = chain_id
        self.signature_type = signature_type
        self.paper = paper
        self._client: Optional[ClobClient] = None
        self._connected = False

        if paper:
            log.info("PAPER（模拟）模式 — 不会发送真实订单")
        else:
            if not HAS_CLOB:
                raise ImportError("实盘模式需要安装 py-clob-client: pip install py-clob-client")
            if not private_key:
                raise ValueError("实盘模式需要提供 private_key")
            log.warning("⚠️ REAL（实盘）模式 — 将使用真实资金下单！")

    # ------------------------------------------------------------------
    # 连接（自动派生 API 凭证）
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """连接到 CLOB API，自动派生凭证"""
        if self.paper:
            self._connected = True
            return True

        try:
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=self.signature_type,
                funder=self.funder if self.funder else None,
            )
            # 从私钥自动派生 API Key / Passphrase / Secret
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            self._connected = True
            log.info("已连接到 CLOB API，凭证已从钱包派生")
            return True
        except Exception as e:
            log.error("连接失败: %s", e)
            return False

    def disconnect(self):
        self._client = None
        self._connected = False

    # ------------------------------------------------------------------
    # 余额查询
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        """查询 USDC 余额"""
        if self.paper:
            return 0.0

        if not self._ensure_connected():
            return 0.0

        try:
            balances = self._client.get_balances()
            for b in balances.get("balances", []):
                token_id = b.get("token_id", "")
                # USDC on Polygon
                if token_id.lower() == "0x2791bca1f2de4661ed88a30c99a7a9449aa84174":
                    return float(b.get("available", 0))
                if token_id.lower() == "0x0000000000000000000000000000000000000000":
                    return float(b.get("available", 0))
            return 0.0
        except Exception as e:
            log.error("查询余额失败: %s", e)
            return 0.0

    # ------------------------------------------------------------------
    # 订单查询
    # ------------------------------------------------------------------

    def get_orders(self, status: str = "open") -> List[Dict[str, Any]]:
        """查询订单列表"""
        if self.paper:
            return []

        if not self._ensure_connected():
            return []

        try:
            orders = self._client.get_orders(OpenOrderParams())
            results = []
            for o in orders if isinstance(orders, list) else orders.get("orders", []):
                results.append({
                    "order_id":  o.get("id"),
                    "token_id":  o.get("token_id"),
                    "side":      o.get("side"),
                    "price":     float(o.get("price", 0)),
                    "size":      float(o.get("size", 0)),
                    "filled":    float(o.get("filled_size", 0)),
                    "status":    o.get("status"),
                    "created":   o.get("created_at"),
                })
            return results
        except Exception as e:
            log.error("查询订单失败: %s", e)
            return []

    # ------------------------------------------------------------------
    # 买入
    # ------------------------------------------------------------------

    def buy(self, token_id: str, price: float, shares: float,
            order_type: str = "LIMIT") -> Optional[str]:
        """
        买入指定 outcome 的 token。

        Args:
            token_id:   Polymarket outcome 的 token_id（从 gamma API 获取）
            price:      出价（0.00 - 1.00）
            shares:     想要购买的份额数量
            order_type: "LIMIT" | "FOK" | "IOC"

        Returns:
            订单 ID，失败返回 None
        """
        if not self._ensure_connected():
            return None

        if self.paper:
            log.info("[PAPER] BUY | token=%s price=%.4f shares=%.2f", token_id, price, shares)
            return f"paper_buy_{token_id}_{time.time()}"

        try:
            ot = OrderType.GTC if order_type == "LIMIT" else OrderType.FOK
            order = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
            signed = self._client.create_order(order)
            resp = self._client.post_order(signed, ot)
            order_id = resp.get("order_id") or resp.get("id")
            if order_id:
                log.info("[REAL] BUY | order=%s token=%s price=%.4f shares=%.2f",
                         order_id, token_id, price, shares)
                return str(order_id)
            else:
                log.error("[REAL] 买入响应无 order_id: %s", json.dumps(resp, indent=2))
                return None
        except Exception as e:
            log.error("[REAL] 买入失败: %s", e)
            return None

    # ------------------------------------------------------------------
    # 卖出
    # ------------------------------------------------------------------

    def sell(self, token_id: str, price: float, shares: float,
             order_type: str = "IOC") -> Optional[str]:
        """
        卖出持仓。

        Args:
            token_id:   Polymarket outcome 的 token_id
            price:      卖出价
            shares:     卖出份额
            order_type: "LIMIT" | "IOC" | "FOK"（止损建议用 IOC）

        Returns:
            订单 ID，失败返回 None
        """
        if not self._ensure_connected():
            return None

        if self.paper:
            log.info("[PAPER] SELL | token=%s price=%.4f shares=%.2f", token_id, price, shares)
            return f"paper_sell_{token_id}_{time.time()}"

        try:
            if order_type == "IOC":
                ot = OrderType.IOC
            elif order_type == "FOK":
                ot = OrderType.FOK
            else:
                ot = OrderType.GTC

            order = OrderArgs(token_id=token_id, price=price, size=shares, side=SELL)
            signed = self._client.create_order(order)
            resp = self._client.post_order(signed, ot)
            order_id = resp.get("order_id") or resp.get("id")
            if order_id:
                log.info("[REAL] SELL | order=%s token=%s price=%.4f shares=%.2f",
                         order_id, token_id, price, shares)
                return str(order_id)
            else:
                log.error("[REAL] 卖出响应无 order_id: %s", json.dumps(resp, indent=2))
                return None
        except Exception as e:
            log.error("[REAL] 卖出失败: %s", e)
            return None

    # ------------------------------------------------------------------
    # 撤单
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: str) -> bool:
        if self.paper:
            log.info("[PAPER] CANCEL | order=%s", order_id)
            return True

        if not self._ensure_connected():
            return False

        try:
            self._client.cancel(order_id)
            log.info("[REAL] CANCEL | order=%s", order_id)
            return True
        except Exception as e:
            log.error("[REAL] 撤单失败: %s", e)
            return False

    def cancel_all(self) -> bool:
        if self.paper:
            log.info("[PAPER] CANCEL ALL")
            return True

        if not self._ensure_connected():
            return False

        try:
            self._client.cancel_all()
            log.info("[REAL] CANCEL ALL")
            return True
        except Exception as e:
            log.error("[REAL] 批量撤单失败: %s", e)
            return False

    # ------------------------------------------------------------------
    # 获取市场 token_ids（辅助方法）
    # ------------------------------------------------------------------

    def get_market_tokens(self, market_id: str) -> List[Dict[str, Any]]:
        """
        通过 gamma API 获取 market_id 对应的所有 outcome token_ids。

        返回格式:
        [
            {"outcome_index": 0, "token_id": "123...", "price": 0.12, "name": "52-53°F"},
            {"outcome_index": 1, "token_id": "456...", "price": 0.88, "name": "Other"},
        ]
        """
        import requests
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets/{market_id}",
                timeout=(5, 8),
            )
            data = r.json()
            outcomes_raw = data.get("outcomes", [])
            if isinstance(outcomes_raw, str) and outcomes_raw.startswith("["):
                outcomes = json.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw

            clob_ids_raw = data.get("clobTokenIds", "")
            if clob_ids_raw.startswith("["):
                token_ids = json.loads(clob_ids_raw)
            else:
                token_ids = [t.strip() for t in clob_ids_raw.split(",")] if clob_ids_raw else []

            result = []
            for i, outcome in enumerate(outcomes):
                result.append({
                    "outcome_index": i,
                    "token_id": token_ids[i] if i < len(token_ids) else "",
                    "name": outcome,
                    "price": float(json.loads(data.get("outcomePrices", "[0.5,0.5]"))[i]),
                })
            return result
        except Exception as e:
            log.error("获取市场 tokens 失败: %s", e)
            return []

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> bool:
        if self.paper:
            return True
        if not self._connected:
            return self.connect()
        return self._connected

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()