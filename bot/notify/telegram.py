"""Avisos de operaciones al chat de Telegram del administrador."""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

from bot.security.secrets import mask_secret, register_secret

logger = logging.getLogger(__name__)

_TELEGRAM_HOST = "api.telegram.org"
_TIMEOUT_SECONDS = 8


class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = "") -> None:
        self.enabled = bool(token and chat_id)
        self._token = token
        self._chat_id = chat_id
        if token:
            register_secret(token)

    def notify_opened(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        reason: str,
        dry_run: bool,
        *,
        stop_price: float | None = None,
        take_profit_price: float | None = None,
        stop_pct: float | None = None,
        take_profit_pct: float | None = None,
    ) -> None:
        side_label = "COMPRA" if side.lower() == "buy" else "VENTA"
        icon = "🟢" if side.lower() == "buy" else "🔴"
        lines = [
            f"{icon} {side_label} {symbol}",
            f"Precio de entrada: ${price:,.4f}",
            f"Cantidad: {qty:g}",
            f"Motivo: {reason or 'signal'}",
        ]
        if stop_price is not None and take_profit_price is not None:
            sl_txt = f"{stop_pct * 100:.2f}%" if stop_pct is not None else "—"
            tp_txt = f"{take_profit_pct * 100:.2f}%" if take_profit_pct is not None else "—"
            lines.append(f"Stop Loss: ${stop_price:,.4f} (−{sl_txt})")
            lines.append(f"Take Profit: ${take_profit_price:,.4f} (+{tp_txt})")
        if dry_run:
            lines.append("Paper · dry-run (orden no enviada)")
        self._send("\n".join(lines))

    def notify_closed(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        exit_price: float,
        pnl_abs: float,
        pnl_pct: float,
        reason: str,
        dry_run: bool,
    ) -> None:
        icon = "🟢" if pnl_abs >= 0 else "🔴"
        sign = "+" if pnl_abs >= 0 else ""
        reason_label = {
            "stop_loss": "Stop Loss",
            "take_profit": "Take Profit",
            "signal_6m": "Señal contraria (venta)",
            "mode_switch": "Cambio de mercado",
        }.get(reason, reason or "close")
        lines = [
            f"{icon} VENTA {symbol} — operación cerrada",
            f"Precio de entrada: ${entry_price:,.4f}",
            f"Precio de salida: ${exit_price:,.4f}",
            f"P&L: {sign}${pnl_abs:,.2f} ({sign}{pnl_pct:.2f}%)",
            f"Cantidad: {qty:g}",
            f"Motivo: {reason_label}",
        ]
        if dry_run:
            lines.append("Paper · dry-run (orden no enviada)")
        self._send("\n".join(lines))

    def notify_bot_state(self, running: bool, cancelled_orders: int = 0) -> bool:
        if running:
            text = "▶️ Bot activado — planificador Tesla 3-6-9 en marcha."
        else:
            text = (
                f"⏸️ Bot detenido — loop en pausa. "
                f"Órdenes pendientes canceladas: {cancelled_orders}."
            )
        return self._send(text)

    def notify_mode_switch(self, mode_label: str, symbols: list[str]) -> bool:
        text = (
            f"🔄 Cambio de mercado automático\n"
            f"{mode_label}\n"
            f"Activos: {', '.join(symbols) if symbols else '—'}"
        )
        return self._send(text)

    def notify_position_mismatch(self, details: str) -> bool:
        lines = [
            "⛔ Bot NO arrancó — discrepancia de posiciones",
            details[:3000],
        ]
        return self._send("\n".join(lines))

    def notify_live_order_blocked(self, symbol: str, side: str, qty: float, reason: str) -> bool:
        lines = [
            "⛔ Orden LIVE bloqueada — no se envió a Alpaca",
            f"{side.upper()} {symbol} qty={qty:g}",
            reason,
            "Para TTY: escribe CONFIRMO en consola.",
            "Para PM2: data/live_confirm.txt con línea 1 CONFIRMO y línea 2 = account id live.",
        ]
        return self._send("\n".join(lines))

    def notify_startup_smoke_test(self, *, symbols: str = "", tick_seconds: int = 60) -> bool:
        """Alerta obligatoria al arrancar — confirma token, chat_id y red."""
        lines = [
            "✅ Prueba de conexión — bot Tesla 3-6-9 en marcha.",
            f"Símbolos: {symbols or '—'} | tick: {tick_seconds}s",
            "Si ves este mensaje, Telegram está operativo.",
        ]
        return self._send("\n".join(lines))

    def start_background_startup(
        self,
        *,
        symbols: str = "",
        tick_seconds: int = 60,
    ) -> None:
        """Verifica y envía prueba de humo sin bloquear el arranque del bot."""
        if not self.enabled:
            return

        def _worker() -> None:
            try:
                if self.verify():
                    logger.info("Telegram verificado (async)")
                else:
                    logger.warning("Telegram: verificacion async fallo — revisa token/chat_id")
                if self.notify_startup_smoke_test(symbols=symbols, tick_seconds=tick_seconds):
                    logger.info("Telegram: prueba de humo enviada (async)")
                else:
                    logger.warning("Telegram: prueba de humo async no entregada")
            except Exception as exc:
                logger.warning("Telegram async: %s", type(exc).__name__)

        threading.Thread(
            target=_worker,
            name="telegram-startup",
            daemon=True,
        ).start()

    def verify(self) -> bool:
        """Comprueba token y chat sin enviar mensajes de prueba."""
        if not self.enabled:
            return False
        url = f"https://{_TELEGRAM_HOST}/bot{self._token}/getMe"
        if urlparse(url).hostname != _TELEGRAM_HOST:
            return False
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                if response.status >= 300:
                    return False
                data = json.loads(response.read().decode("utf-8"))
                return bool(data.get("ok"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            logger.warning("Telegram verify: %s", type(exc).__name__)
            return False

    def _send(self, text: str) -> bool:
        if not self.enabled:
            logger.debug("Telegram deshabilitado — mensaje omitido")
            return False
        url = f"https://{_TELEGRAM_HOST}/bot{self._token}/sendMessage"
        if urlparse(url).hostname != _TELEGRAM_HOST:
            logger.warning("Destino Telegram rechazado")
            return False
        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": text[:3500],
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if response.status >= 300:
                    logger.warning("Telegram respondio HTTP %s | %s", response.status, raw[:240])
                    return False
                try:
                    data = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    logger.warning("Telegram respondio cuerpo no JSON | %s", raw[:240])
                    return False
                if not data.get("ok"):
                    logger.warning(
                        "Telegram API error | description=%s",
                        data.get("description", raw[:240]),
                    )
                    return False
                logger.info("Telegram mensaje enviado")
                return True
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:240]
            except OSError:
                pass
            logger.warning(
                "Telegram HTTP %s | %s",
                exc.code,
                body or exc.reason,
            )
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("Telegram send: %s", type(exc).__name__)
            return False
