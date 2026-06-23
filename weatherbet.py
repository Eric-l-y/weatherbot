#!/usr/bin/env python3
"""快捷入口 — 转发到 bot_v2.py"""
import sys

from bot_v2 import *

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="WeatherBet — Polymarket Weather Trading Bot")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "status", "report"],
                        help="run: start bot | status: show positions | report: full report")
    parser.add_argument("--real", action="store_true",
                        help="启用实盘交易（需要配置 CLOB 凭证）")
    args = parser.parse_args()

    if args.command == "run":
        run_loop(real_mode=args.real)
    elif args.command == "status":
        _cal = load_cal()
        print_status()
    elif args.command == "report":
        _cal = load_cal()
        print_report()
