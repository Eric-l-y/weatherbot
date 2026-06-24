"""
notifier.py — 套利系统通知器
复用 bot_v2.py 的邮件通知模式。
"""

import json
import smtplib
import ssl
import logging
from email.mime.text import MIMEText
from typing import Optional

log = logging.getLogger("arb.notifier")


class ArbNotifier:
    """套利系统通知器（邮件）"""

    def __init__(self, config: dict):
        self.smtp_host = config.get("smtp_host", "")
        self.smtp_port = config.get("smtp_port", 587)
        self.smtp_user = config.get("smtp_user", "")
        self.smtp_pass = config.get("smtp_pass", "")
        self.smtp_tls = config.get("smtp_tls", True)
        self.notify_email = config.get("notify_email", "")

    @property
    def enabled(self) -> bool:
        return all([self.smtp_host, self.smtp_user, self.smtp_pass, self.notify_email])

    def send(self, subject: str, body: str):
        """发送邮件通知"""
        if not self.enabled:
            log.debug("邮件通知未配置，跳过")
            return

        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = f"[Arb] {subject}"
            msg["From"] = self.smtp_user
            msg["To"] = self.notify_email

            if self.smtp_tls:
                ctx = ssl.create_default_context()
                with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                    server.starttls(context=ctx)
                    server.login(self.smtp_user, self.smtp_pass)
                    server.send_message(msg)
            else:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port) as server:
                    server.login(self.smtp_user, self.smtp_pass)
                    server.send_message(msg)

            log.info("📧 通知已发送: %s", subject)
        except Exception as e:
            log.warning("发送通知失败: %s", e)

    # ------------------------------------------------------------------
    # 预设通知
    # ------------------------------------------------------------------

    def notify_opportunity(self, strategy: str, title: str,
                           net_pct: float, legs: int):
        """套利机会发现"""
        self.send(
            f"🎯 套利机会: {title[:40]}",
            f"策略: {strategy}\n"
            f"事件: {title}\n"
            f"预期净利: {net_pct:.1%}\n"
            f"交易腿数: {legs}",
        )

    def notify_executed(self, opp_id: str, title: str,
                        cost: float, expected_profit: float):
        """套利已执行"""
        self.send(
            f"✅ 套利执行: {title[:40]}",
            f"ID: {opp_id}\n"
            f"事件: {title}\n"
            f"成本: ${cost:.2f}\n"
            f"预期利润: ${expected_profit:.2f}",
        )

    def notify_resolved(self, title: str, pnl: float, pnl_pct: float):
        """套利已结算"""
        emoji = "💰" if pnl > 0 else "📉"
        self.send(
            f"{emoji} 套利结算: {title[:40]}",
            f"事件: {title}\n"
            f"盈亏: ${pnl:.2f} ({pnl_pct:.1f}%)",
        )

    def notify_risk_alert(self, alert_type: str, detail: str):
        """风控告警"""
        self.send(
            f"⚠️ 风控告警: {alert_type}",
            detail,
        )
