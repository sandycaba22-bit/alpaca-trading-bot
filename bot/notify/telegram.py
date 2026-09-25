"""Avisos de operaciones al chat de Telegram del administrador."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from bot.runtime_paths import data_file
from bot.security.secrets import mask_secret, register_secret

logger = logging.getLogger(__name__)

_TELEGRAM_HOST = "api.telegram.org"
_TIMEOUT_SECONDS = 8
_EVENTS_MAX = 800


def make_event_id(symbol: str, kind: str, signal_ts: str, operation: str) -> str:
    return f"{str(symbol).upper().strip()}|{str(kind).strip()}|{str(signal_ts).strip()}|{str(operation).strip()}"


class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = "", prefix: str = "") -> None:
        self.enabled = bool(token and chat_id)
        self._token = token
        self._chat_id = chat_id
        self._prefix = prefix.strip()
        self._sent_lock = threading.Lock()
        self._sent_ids: set[str] = set()
        self._sent_order: list[str] = []
        if token:
            register_secret(token)
        self._load_sent_ids()

    def _events_path(self):
        return data_file("telegram_events.json")

    def _load_sent_ids(self) -> None:
        try:
            path = self._events_path()
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            ids = raw.get("ids", []) if isinstance(raw, dict) else raw
            if isinstance(ids, list):
                ordered = [str(x) for x in ids[-_EVENTS_MAX:]]
                self._sent_order = ordered
                self._sent_ids = set(ordered)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._sent_ids = set()
            self._sent_order = []

    def _save_sent_ids(self) -> None:
        try:
            path = self._events_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"ids": self._sent_order[-_EVENTS_MAX:]}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _claim_event(self, event_id: str | None) -> bool:
        if not event_id:
            return True
        with self._sent_lock:
            if event_id in self._sent_ids:
                logger.info("Telegram duplicado descartado | event_id=%s", event_id)
                return False
            self._sent_ids.add(event_id)
            self._sent_order.append(event_id)
            overflow = len(self._sent_order) - _EVENTS_MAX
            if overflow > 0:
                for old in self._sent_order[:overflow]:
                    self._sent_ids.discard(old)
                self._sent_order = self._sent_order[overflow:]
            self._save_sent_ids()
        return True

    def _unclaim_event(self, event_id: str | None) -> None:
        if not event_id:
            return
        with self._sent_lock:
            self._sent_ids.discard(event_id)
            self._sent_order = [x for x in self._sent_order if x != event_id]
            self._save_sent_ids()

    def _send_event(self, event_id: str | None, text: str) -> bool:
        if not self._claim_event(event_id):
            return False
        ok = self._send(text)
        if not ok:
            self._unclaim_event(event_id)
        return ok

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
        event_id: str | None = None,
    ) -> bool:
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
        return self._send_event(event_id, "\n".join(lines))

    def notify_partial_close(
        self,
        symbol: str,
        sold_qty: float,
        remaining_qty: float,
        entry_price: float,
        exit_price: float,
        pnl_abs: float,
        pnl_pct: float,
        reason: str,
        order_status: str,
        order_qty: float,
        dry_run: bool,
        event_id: str | None = None,
    ) -> bool:
        icon = "🟢" if pnl_abs >= 0 else "🔴"
        sign = "+" if pnl_abs >= 0 else ""
        reason_label = {
            "stop_loss": "Stop Loss",
            "take_profit": "Take Profit",
            "signal_entry": "Señal de entrada",
            "sync_entry": "Entrada sync (calibrada)",
            "vol_2x": "Entrada vol_2x (multi-régimen + SMA50)",
            "signal_exit": "Señal contraria (venta)",
            "mode_switch": "Cambio de mercado",
            "dynamic_tp_limit": "TP límite alcanzado",
            "dynamic_tp_gap_market": "Gap — venta parcial a mercado",
            "dynamic_tp_gap_limit_ioc": "Gap — venta parcial límite IOC por spread ancho",
        }.get(reason, reason or "close")
        lines = [
            f"{icon} VENTA PARCIAL {symbol} — posición sigue abierta",
            f"Orden: {order_qty:g} solicitadas | {sold_qty:g} ejecutadas | estado: {order_status or 'canceled'}",
            f"Precio de entrada: ${entry_price:,.4f}",
            f"Precio de salida (fill): ${exit_price:,.4f}",
            f"P&L sobre lo vendido: {sign}${pnl_abs:,.2f} ({sign}{pnl_pct:.2f}%)",
            f"Cantidad vendida: {sold_qty:g}",
            f"Cantidad que sigue abierta: {remaining_qty:g}",
            f"Motivo original: {reason_label}",
        ]
        if dry_run:
            lines.append("Paper · dry-run (orden no enviada)")
        return self._send_event(event_id, "\n".join(lines))

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
        event_id: str | None = None,
        dust_qty: float = 0.0,
    ) -> bool:
        icon = "🟢" if pnl_abs >= 0 else "🔴"
        sign = "+" if pnl_abs >= 0 else ""
        reason_label = {
            "stop_loss": "Stop Loss",
            "take_profit": "Take Profit",
            "signal_entry": "Señal de entrada",
            "sync_entry": "Entrada sync (calibrada)",
            "vol_2x": "Entrada vol_2x (multi-régimen + SMA50)",
            "signal_exit": "Señal contraria (venta)",
            "mode_switch": "Cambio de mercado",
            "dynamic_tp_limit": "TP límite alcanzado",
            "dynamic_tp_gap_market": "Gap — venta parcial a mercado",
            "dynamic_tp_gap_limit_ioc": "Gap — venta parcial límite IOC por spread ancho",
        }.get(reason, reason or "close")
        lines = [
            f"{icon} VENTA {symbol} — operación cerrada",
            f"Precio de entrada: ${entry_price:,.4f}",
            f"Precio de salida: ${exit_price:,.4f}",
            f"P&L: {sign}${pnl_abs:,.2f} ({sign}{pnl_pct:.2f}%)",
            f"Cantidad: {qty:g}",
            f"Motivo: {reason_label}",
        ]
        if dust_qty > 0:
            lines.append(
                f"(residuo de {dust_qty:g} ignorado por ser menor al mínimo operable)"
            )
        if dry_run:
            lines.append("Paper · dry-run (orden no enviada)")
        return self._send_event(event_id, "\n".join(lines))

    def notify_breakeven_locked(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        stop_price: float,
        gain_pct: float,
        event_id: str | None = None,
    ) -> bool:
        sign = "+" if gain_pct >= 0 else ""
        lines = [
            f"🟢 BREAKEVEN ASEGURADO {symbol}",
            f"Precio de entrada: ${entry_price:,.4f}",
            f"High: ${current_price:,.4f} ({sign}{gain_pct:.2f}%)",
            f"SL piso: ${stop_price:,.4f} (por encima de la entrada)",
            "El TP fijo sigue vigente. Si el precio se revierte, no cierra en pérdida.",
        ]
        return self._send_event(event_id, "\n".join(lines))

    def notify_trailing_activated(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        gain_pct: float,
        event_id: str | None = None,
    ) -> bool:
        sign = "+" if gain_pct >= 0 else ""
        lines = [
            f"🟡 TRAILING STOP {symbol} — activado",
            f"Precio de entrada: ${entry_price:,.4f}",
            f"Precio actual (high vela): ${current_price:,.4f}",
            f"Ganancia: {sign}{gain_pct:.2f}%",
            "Trailing activado — el SL sube con el high y no baja.",
        ]
        return self._send_event(event_id, "\n".join(lines))

    def notify_stream_fallback(self, reason: str) -> bool:
        lines = [
            "⚠️ WebSocket Alpaca caído — modo fallback REST",
            reason or "sin ticks recientes",
            "El bot sigue viendo precios por API REST. No está ciego.",
        ]
        return self._send("\n".join(lines))

    def notify_stream_restored(self) -> bool:
        lines = [
            "✅ WebSocket Alpaca recuperado — modo streaming",
            "Trailing y salidas otra vez tick a tick.",
        ]
        return self._send("\n".join(lines))

    def notify_stream_unstable(self, drops: int, window_seconds: float, recovered: bool) -> bool:
        minutes = max(1, int(round(float(window_seconds) / 60.0)))
        lines = [
            f"⚠️ WebSocket Alpaca inestable — {int(drops)} caídas en los últimos {minutes} min",
        ]
        if recovered:
            lines.append("Ya volvió a streaming. Agrupado para no spamear cada corte breve.")
        else:
            lines.append("Sigue en fallback REST hasta que el socket se estabilice.")
        return self._send("\n".join(lines))

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

    def notify_position_mismatch(self, details: str, *, profile: str = "") -> bool:
        lines = [
            "⛔ Bot NO arrancó — discrepancia de posiciones",
            details[:3000],
        ]
        event_id = make_event_id(profile or "hybrid", "position_mismatch", "startup", "once")
        return self._send_event(event_id, "\n".join(lines))

    def notify_order_timeout(
        self,
        symbol: str,
        side: str,
        qty: float,
        last_reason: str,
        *,
        exposed: bool,
        attempts: int,
        timeout_seconds: float,
        event_id: str | None = None,
    ) -> bool:
        lines = [
            "⚠️ Orden no ejecutada — se agotaron reintentos",
            f"{side.upper()} {symbol} qty={qty:g}",
            f"Último rechazo: {last_reason or 'desconocido'}",
            f"Intentos: {attempts} · ventana {timeout_seconds:.0f}s",
        ]
        if exposed:
            lines.append("La posición sigue ABIERTA (expuesta). No se cerró.")
        else:
            lines.append("No se abrió la posición.")
        return self._send_event(event_id, "\n".join(lines))

    def notify_fractional_wide_spread(
        self,
        symbol: str,
        side: str,
        qty: float,
        spread_pct: float,
        event_id: str | None = None,
    ) -> bool:
        lines = [
            "⚠️ Orden fraccionada ejecutada a mercado con spread amplio "
            f"({spread_pct * 100:.2f}%)",
            f"{side.upper()} {symbol} qty={qty:g}",
            "Alpaca no admite límite en qty fraccionaria de acciones; se envió market.",
        ]
        return self._send_event(event_id, "\n".join(lines))

    def notify_order_unfilled(
        self,
        symbol: str,
        side: str,
        qty: float,
        status: str,
        *,
        remaining_open: bool,
        filled_qty: float = 0.0,
        event_id: str | None = None,
    ) -> bool:
        lines = [
            "⚠️ Orden cancelada/expirada sin completar",
            f"{side.upper()} {symbol} qty={qty:g}",
            f"Estado Alpaca: {status or 'expired'}",
        ]
        if filled_qty > 0:
            lines.append(f"Fill parcial: {filled_qty:g} — el resto no se ejecutó.")
        if remaining_open:
            lines.append("La posición sigue ABIERTA en el broker. El libro no se cerró.")
        else:
            lines.append("No quedó posición abierta.")
        return self._send_event(event_id, "\n".join(lines))

    def notify_signal_filtered(
        self,
        symbol: str,
        signal: str,
        reason: str,
        event_id: str | None = None,
    ) -> bool:
        lines = [
            f"ℹ️ Señal {str(signal).upper()} {symbol} ignorada",
            reason or "filtro adicional",
        ]
        return self._send_event(event_id, "\n".join(lines))

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
        body = text
        if self._prefix:
            body = f"{self._prefix} {body}"
        url = f"https://{_TELEGRAM_HOST}/bot{self._token}/sendMessage"
        if urlparse(url).hostname != _TELEGRAM_HOST:
            logger.warning("Destino Telegram rechazado")
            return False
        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": body[:3500],
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        last_error = ""
        for attempt in range(1, 4):
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
                        last_error = f"HTTP {response.status}"
                        logger.warning(
                            "Telegram respondio HTTP %s | %s",
                            response.status,
                            raw[:240],
                        )
                    else:
                        try:
                            data = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            last_error = "cuerpo no JSON"
                            logger.warning(
                                "Telegram respondio cuerpo no JSON | %s", raw[:240]
                            )
                            data = {}
                        if data.get("ok"):
                            logger.info("Telegram mensaje enviado")
                            return True
                        last_error = str(data.get("description", raw[:240]))
                        logger.warning(
                            "Telegram API error | description=%s", last_error
                        )
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:240]
                except OSError:
                    pass
                last_error = f"HTTP {exc.code}"
                logger.warning("Telegram HTTP %s | %s", exc.code, body or exc.reason)
                if exc.code in {400, 401, 403, 404}:
                    return False
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = type(exc).__name__
                logger.warning("Telegram send: %s (intento %s/3)", last_error, attempt)
            if attempt < 3:
                time.sleep(float(attempt))
        logger.warning("Telegram: mensaje perdido tras 3 intentos | %s", last_error)
        return False
