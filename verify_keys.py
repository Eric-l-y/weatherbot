#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_keys.py — 验证 Polymarket 交易凭证是否有效

检查项目：
  1. .env 中私钥和地址是否已填写
  2. 私钥格式是否正确（64位十六进制）
  3. 地址格式是否正确（以 0x 开头的 42 位十六进制）
  4. 能否成功连接 CLOB API 并派生凭证
  5. 能否查询 USDC 余额
  6. 能否查询挂单

用法：
    python verify_keys.py
"""

import sys
import os
from pathlib import Path

# 确保能 import 项目模块
sys.path.insert(0, str(Path(__file__).resolve().parent))


def check_env_vars():
    """检查 .env 中的私钥和地址"""
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

    key = os.environ.get("POLY_PRIVATE_KEY", "").strip()
    funder = os.environ.get("POLY_FUNDER", "").strip()

    print("=" * 55)
    print("  步骤 1/4: 检查 .env 配置")
    print("=" * 55)

    if not key:
        print("  ❌ POLY_PRIVATE_KEY 未填写")
        return None, None
    print(f"  ✅ POLY_PRIVATE_KEY 已填写 (长度 {len(key)})")

    if not funder:
        print("  ⚠️  POLY_FUNDER 未填写（可选，但建议填写）")
    else:
        print(f"  ✅ POLY_FUNDER 已填写: {funder[:6]}...{funder[-4:]}")

    return key, funder


def check_format(key, funder):
    """验证私钥和地址格式"""
    print()
    print("=" * 55)
    print("  步骤 2/4: 验证格式")
    print("=" * 55)

    ok = True

    # 私钥格式：去掉 0x 前缀后应为 64 位十六进制
    raw_key = key.replace("0x", "") if key else ""
    if len(raw_key) != 64 or not all(c in "0123456789abcdefABCDEF" for c in raw_key):
        print(f"  ❌ 私钥格式错误：应为 64 位十六进制字符串，实际长度 {len(raw_key)}")
        ok = False
    else:
        print("  ✅ 私钥格式正确 (64位十六进制)")

    # 地址格式：以 0x 开头，总长 42 位
    if funder:
        if not funder.startswith("0x") or len(funder) != 42:
            print(f"  ❌ 地址格式错误：应为 0x + 40位十六进制，实际长度 {len(funder)}")
            ok = False
        elif not all(c in "0123456789abcdefABCDEF" for c in funder[2:]):
            print("  ❌ 地址包含非法字符")
            ok = False
        else:
            print("  ✅ 地址格式正确 (0x + 40位十六进制)")
    else:
        print("  ⏭️  跳过地址格式检查（未填写）")

    return ok


def check_clob_installed():
    """检查 py-clob-client 是否安装"""
    print()
    print("=" * 55)
    print("  步骤 3/4: 检查依赖")
    print("=" * 55)

    try:
        import py_clob_client  # noqa: F401
        print("  ✅ py-clob-client 已安装")
        return True
    except ImportError:
        print("  ❌ py-clob-client 未安装")
        print("     请运行: pip install py-clob-client")
        return False


def check_connection(key, funder):
    """尝试连接 CLOB API，派生凭证，查余额"""
    print()
    print("=" * 55)
    print("  步骤 4/4: 连接 Polymarket CLOB API")
    print("=" * 55)

    from trader import PolymarketTrader

    trader = PolymarketTrader(
        private_key=key,
        funder=funder,
        paper=False,  # 真实连接，但不发任何订单
    )

    print("  ⏳ 正在连接...")
    connected = trader.connect()

    if not connected:
        print("  ❌ 连接失败 — 私钥可能无效或网络不通")
        print("     常见原因：")
        print("     - 私钥错误或不是 Polygon 钱包的私钥")
        print("     - 网络无法访问 clob.polymarket.com")
        print("     - 钱包未在 Polymarket 注册")
        return False

    print("  ✅ 连接成功！API 凭证已从私钥派生")

    # 查询交易历史（比余额更能说明账户是否活跃）
    print("  ⏳ 查询交易历史...")
    trades = trader._client.get_trades() if trader._client else []
    if trades:
        print(f"  ✅ 有 {len(trades)} 条交易记录，账户活跃")
        # 提取 maker_address 确认代理钱包
        maker = trades[0].get("maker_address", "")
        if maker:
            print(f"     链上交易地址: {maker[:6]}...{maker[-4:]}")
    else:
        print("  ⚠️  无交易记录（新账户或从未交易过）")

    # 查询链上授权余额（仅作参考）
    print("  ⏳ 查询链上授权余额（仅参考）...")
    balance = trader.get_balance()
    print(f"  {'✅' if balance > 0 else 'ℹ️ '} 链上授权余额: {balance:.2f}")
    if balance == 0:
        print("     说明: Polymarket 充值的 USDC 在交易所合约内部，")
        print("     不会显示在链上授权余额中，不影响实际交易。")

    # 查询挂单
    print("  ⏳ 查询挂单...")
    orders = trader.get_orders()
    if orders:
        print(f"  ✅ 当前有 {len(orders)} 个挂单")
    else:
        print("  ✅ 当前无挂单（正常）")

    # 查询支持的市场（只读，不会下单）
    print("  ⏳ 测试市场数据读取...")
    try:
        import requests
        resp = requests.get("https://gamma-api.polymarket.com/markets?limit=1", timeout=10)
        if resp.ok:
            print("  ✅ Gamma API 正常（市场数据可读取）")
        else:
            print(f"  ⚠️  Gamma API 返回 {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️  Gamma API 访问异常: {e}")

    trader.disconnect()
    return True


def main():
    print()
    print("  🔍 Polymarket 交易凭证验证工具")
    print()

    # 1. 检查环境变量
    key, funder = check_env_vars()
    if not key:
        print()
        print("  💡 请在 .env 文件中填写:")
        print("     POLY_PRIVATE_KEY=你的私钥(不带0x)")
        print("     POLY_FUNDER=你的钱包地址(带0x)")
        sys.exit(1)

    # 2. 格式检查
    if not check_format(key, funder):
        sys.exit(1)

    # 3. 依赖检查
    if not check_clob_installed():
        sys.exit(1)

    # 4. 连接测试
    success = check_connection(key, funder)

    print()
    print("=" * 55)
    if success:
        print("  ✅ 全部验证通过！API 凭证有效，可以进行自动交易。")
        print()
        print("  下一步:")
        print("  - 将 config.json 中 trading_mode 改为 \"real\"")
        print("  - 将 arbitrage.mode 也改为 \"real\"")
        print("  - 建议先运行 python arbitrage_bot.py --once 扫描一次")
        print("  - 确认无误后再运行全自动模式")
    else:
        print("  ❌ 验证未通过，请检查上方错误信息。")
    print("=" * 55)
    print()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
