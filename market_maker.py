# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file

import asyncio
import logging
import lighter
import os
import time
import json
import math
from typing import Tuple, Optional
from datetime import datetime
from lighter.exceptions import ApiException
import signal

# =========================
# Env & constants
# =========================
BASE_URL = "https://mainnet.zklighter.elliot.ai"
API_KEY_PRIVATE_KEY = os.getenv("API_KEY_PRIVATE_KEY")
ACCOUNT_INDEX = int(os.getenv("ACCOUNT_INDEX", "0"))
API_KEY_INDEX = int(os.getenv("API_KEY_INDEX", "0"))

MARKET_SYMBOL = os.getenv("MARKET_SYMBOL", "PAXG")
MARKET_ID = None
PRICE_TICK_SIZE = None
AMOUNT_TICK_SIZE = None

# Directories (mounted by docker-compose)
CLOSE_LONG_ON_STARTUP = os.getenv("CLOSE_LONG_ON_STARTUP", "false").lower() == "true"
PARAMS_DIR = os.getenv("PARAMS_DIR", "params")
LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Trading config
SPREAD = 0.035 / 100.0       # static fallback spread (if allowed)
BASE_AMOUNT = 0.047          # static fallback amount
USE_DYNAMIC_SIZING = True
CAPITAL_USAGE_PERCENT = 0.99
SAFETY_MARGIN_PERCENT = 0.01
ORDER_TIMEOUT = 90           # seconds

# Avellaneda
AVELLANEDA_REFRESH_INTERVAL = 900  # seconds
REQUIRE_PARAMS = os.getenv("REQUIRE_PARAMS", "false").lower() == "true"

# Global WS / state
latest_order_book = None
order_book_received = asyncio.Event()
ws_connection_healthy = False
last_order_book_update = 0
current_mid_price_cached = None
ws_client = None
ws_task = None

current_order_id = None
current_order_timestamp = None
last_mid_price = None
order_side = "buy"
order_counter = 1000
available_capital = None
last_capital_check = 0
current_position_size = 0
last_order_base_amount = 0

avellaneda_params = None
last_avellaneda_update = 0
position_detected_at_startup = False

# =========================
# Logging setup
# =========================
for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

log_file = os.path.join(LOG_DIR, "market_maker_debug.txt")
try:
    if os.path.exists(log_file):
        os.remove(log_file)
except Exception:
    pass

file_handler = logging.FileHandler(log_file, mode='w')
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s')
file_handler.setFormatter(file_formatter)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(console_formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)
logger.propagate = False

logging.getLogger('websockets').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.root.setLevel(logging.WARNING)

# =========================
# Helpers
# =========================
def trim_exception(e: Exception) -> str:
    return str(e).strip().split("\n")[-1]

async def get_market_details(order_api, symbol: str) -> Optional[Tuple[int, float, float]]:
    try:
        order_books_response = await order_api.order_books()
        for ob in order_books_response.order_books:
            if ob.symbol.upper() == symbol.upper():
                market_id = ob.market_id
                price_tick_size = 10 ** -ob.supported_price_decimals
                amount_tick_size = 10 ** -ob.supported_size_decimals
                return market_id, price_tick_size, amount_tick_size
        return None
    except Exception as e:
        logger.error(f"An error occurred while fetching market details: {e}")
        return None

async def get_account_balances(account_api):
    logger.info("Retrieving account balances...")
    try:
        account = await account_api.account(by="index", value=str(ACCOUNT_INDEX))
        if hasattr(account, 'accounts') and account.accounts:
            acc = account.accounts[0]
            for name in ['available_balance', 'collateral', 'total_asset_value', 'cross_asset_value']:
                if hasattr(acc, name):
                    try:
                        v = float(getattr(acc, name))
                        if v > 0:
                            logger.info(f"✅ Using {name} as available capital: ${v}")
                            return v
                    except Exception:
                        continue
        return 0.0
    except Exception as e:
        logger.error(f"Error getting account balances: {e}", exc_info=True)
        return 0.0

async def check_open_positions(account_api):
    try:
        return await account_api.account(by="index", value=str(ACCOUNT_INDEX))
    except Exception as e:
        logger.error(f"Error getting account information: {e}", exc_info=True)
        return None

async def close_long_position(client, position_size, current_mid_price):
    """Close a long position with a reduce-only limit sell order."""
    global order_counter, current_order_id
    logger.info(f"Attempting to close long position of size {position_size}")
    sell_price = current_mid_price * (1.0 + SPREAD)
    order_counter += 1
    logger.info(f"Placing reduce-only sell order at {sell_price} to close long position")
    try:
        tx, tx_hash, err = await client.create_order(
            market_index=MARKET_ID,
            client_order_index=order_counter,
            base_amount=int(abs(position_size) / AMOUNT_TICK_SIZE),
            price=int(sell_price / PRICE_TICK_SIZE),
            is_ask=True,
            order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
            time_in_force=lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
            reduce_only=True
        )
        if err is not None:
            logger.error(f"Error placing position closing order: {trim_exception(err)}")
            return False
        logger.info(f"Successfully placed position closing order: tx={getattr(tx_hash,'tx_hash',tx_hash)}")
        current_order_id = order_counter
        return True
    except Exception as e:
        logger.error(f"Exception in close_long_position: {e}", exc_info=True)
        return False

def on_order_book_update(market_id, order_book):
    global latest_order_book, ws_connection_healthy, last_order_book_update, current_mid_price_cached
    try:
        if int(market_id) == MARKET_ID:
            bids = order_book.get('bids', [])
            asks = order_book.get('asks', [])
            if bids and asks:
                best_bid = float(bids[0]['price'])
                best_ask = float(asks[0]['price'])
                current_mid_price_cached = (best_bid + best_ask) / 2
            latest_order_book = order_book
            ws_connection_healthy = True
            last_order_book_update = time.time()
            order_book_received.set()
    except Exception as e:
        logger.error(f"Error in order book callback: {e}", exc_info=True)
        ws_connection_healthy = False

def on_account_update(account_id, account):
    pass

class RobustWsClient(lighter.WsClient):
    def handle_unhandled_message(self, message):
        try:
            if isinstance(message, dict):
                t = message.get('type', 'unknown')
                if t in ['ping', 'pong', 'heartbeat', 'keepalive', 'health']:
                    logger.debug(f"Received {t} message")
                    return
                else:
                    logger.warning(f"Unknown WS message: {message}")
        except Exception as e:
            logger.error(f"WS handle error: {e}", exc_info=True)

def get_current_mid_price():
    if current_mid_price_cached is not None and (time.time() - last_order_book_update) < 10:
        return current_mid_price_cached
    if latest_order_book is None:
        return None
    bids = latest_order_book.get('bids', [])
    asks = latest_order_book.get('asks', [])
    if not bids or not asks:
        return None
    return (float(bids[0]['price']) + float(asks[0]['price'])) / 2

async def calculate_dynamic_base_amount(account_api, current_mid_price):
    global available_capital, last_capital_check
    if not USE_DYNAMIC_SIZING:
        return BASE_AMOUNT
    now = time.time()
    if available_capital is None or (now - last_capital_check) > 60:
        available_capital = await get_account_balances(account_api)
        last_capital_check = now
        logger.info(f"Available capital: ${available_capital:.2f}")
    if available_capital <= 0:
        logger.warning(f"No available capital, using static BASE_AMOUNT: {BASE_AMOUNT}")
        return BASE_AMOUNT
    usable_capital = available_capital * (1 - SAFETY_MARGIN_PERCENT)
    order_capital = usable_capital * CAPITAL_USAGE_PERCENT
    if current_mid_price and current_mid_price > 0:
        dynamic = order_capital / current_mid_price
        dynamic = max(dynamic, 0.001)
        logger.info(f"Dynamic sizing: ${order_capital:.2f} / ${current_mid_price:.2f} = {dynamic:.6f} units")
        return dynamic
    else:
        logger.warning(f"Invalid mid price, using static BASE_AMOUNT: {BASE_AMOUNT}")
        return BASE_AMOUNT

def load_avellaneda_parameters() -> bool:
    """
    Charge et valide les paramètres Avellaneda depuis PARAMS_DIR en priorité.
    """
    global avellaneda_params, last_avellaneda_update
    try:
        now = time.time()
        if avellaneda_params is not None and (now - last_avellaneda_update) < AVELLANEDA_REFRESH_INTERVAL:
            return True

        avellaneda_params = None

        candidates = [
            os.path.join(PARAMS_DIR, f'avellaneda_parameters_{MARKET_SYMBOL}.json'),
            f'params/avellaneda_parameters_{MARKET_SYMBOL}.json',
            f'avellaneda_parameters_{MARKET_SYMBOL}.json',
            f'TRADER/avellaneda_parameters_{MARKET_SYMBOL}.json',
        ]
        data = None
        for p in candidates:
            try:
                with open(p, 'r') as f:
                    data = json.load(f)
                logger.info(f"📁 Loaded Avellaneda params from: {p}")
                break
            except FileNotFoundError:
                continue
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON in {p}: {e}.")
                return False

        if not data:
            logger.warning(f"Params file not found for {MARKET_SYMBOL}.")
            return False

        lo = data.get('limit_orders')
        if not isinstance(lo, dict):
            logger.warning("'limit_orders' missing/invalid in params.")
            return False

        da = lo.get('delta_a')
        db = lo.get('delta_b')
        try:
            da = float(da); db = float(db)
            if not (math.isfinite(da) and math.isfinite(db)) or da < 0 or db < 0:
                logger.warning("delta_a/delta_b invalid (NaN/Inf/negative).")
                return False
        except Exception:
            logger.warning("delta_a/delta_b not numeric.")
            return False

        avellaneda_params = data
        last_avellaneda_update = now
        logger.info(f"📊 Avellaneda OK. delta_a={da} delta_b={db}")
        return True

    except Exception as e:
        logger.error(f"Unexpected error loading params: {e}", exc_info=True)
        avellaneda_params = None
        return False

def calculate_order_price(mid_price, side) -> Optional[float]:
    ok = load_avellaneda_parameters()
    if ok and avellaneda_params:
        lo = avellaneda_params['limit_orders']
        return mid_price - float(lo['delta_b']) if side == "buy" else mid_price + float(lo['delta_a'])

    if REQUIRE_PARAMS:
        logger.info("REQUIRE_PARAMS enabled and no valid params → skipping quoting.")
        return None

    # fallback static
    return mid_price * (1.0 - SPREAD) if side == "buy" else mid_price * (1.0 + SPREAD)

async def place_order(client, side, price, order_id, base_amount):
    global current_order_id
    is_ask = (side == "sell")
    base_amount_scaled = int(base_amount / AMOUNT_TICK_SIZE)
    price_scaled = int(price / PRICE_TICK_SIZE)
    logger.info(f"Placing {side} order: {base_amount:.6f} units at ${price:.6f} (ID: {order_id})")
    try:
        tx, tx_hash, err = await client.create_order(
            market_index=MARKET_ID,
            client_order_index=order_id,
            base_amount=base_amount_scaled,
            price=price_scaled,
            is_ask=is_ask,
            order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
            time_in_force=lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
            reduce_only=(side == "sell")
        )
        if err is not None:
            logger.error(f"Error placing {side} order: {trim_exception(err)}")
            return False
        logger.info(f"Successfully placed {side} order: tx={getattr(tx_hash,'tx_hash',tx_hash)}")
        current_order_id = order_id
        return True
    except Exception as e:
        logger.error(f"Exception in place_order: {e}", exc_info=True)
        return False

async def cancel_order(client, order_id):
    """
    Remplacé par un cancel_all_orders car chaque bot tourne sur un sous-compte dédié.
    """
    global current_order_id
    logger.info(f"Cancelling all orders (was targeting order {order_id})")
    try:
        tx, tx_hash, err = await client.cancel_all_orders(
            time_in_force=client.CANCEL_ALL_TIF_IMMEDIATE,
            time=0
        )
        if err is not None:
            logger.error(f"Error cancelling all orders: {trim_exception(err)}")
            return False
        logger.info(f"Successfully cancelled all orders: tx={getattr(tx_hash,'tx_hash',tx_hash) if tx_hash else 'OK'}")
        current_order_id = None
        return True
    except Exception as e:
        logger.error(f"Exception in cancel_order: {e}", exc_info=True)
        return False

async def check_order_filled(order_api, client, order_id):
    try:
        auth_token, err = client.create_auth_token_with_expiry()
        if err:
            logger.error(f"Failed to create auth token: {err}")
            return False
        resp = await order_api.account_active_orders(
            account_index=ACCOUNT_INDEX,
            market_id=MARKET_ID,
            auth=auth_token
        )
        if resp is None or not hasattr(resp, 'orders') or resp.orders is None:
            return False
        for o in resp.orders:
            if getattr(o, 'client_order_index', None) == order_id:
                return False
        return True
    except ApiException as e:
        if getattr(e, "status", None) == 429:
            logger.warning("Too Many Requests when checking order status. Waiting 10s before retrying.")
            await asyncio.sleep(10)
        else:
            logger.warning(f"API Error checking order status for {order_id}: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.warning(f"Error checking order status for {order_id}: {e}", exc_info=True)
        return False

async def check_websocket_health():
    global ws_connection_healthy, last_order_book_update, ws_task
    if (time.time() - last_order_book_update) > 30:
        logger.warning(f"Websocket unhealthy - no updates for {time.time() - last_order_book_update:.1f}s")
        ws_connection_healthy = False
        return False
    if ws_task and ws_task.done():
        logger.warning("Websocket task finished unexpectedly")
        try:
            ws_task.result()
        except Exception as e:
            logger.error(f"WS task exception: {e}", exc_info=True)
        ws_connection_healthy = False
        return False
    return ws_connection_healthy

async def restart_websocket():
    global ws_client, ws_task, order_book_received, ws_connection_healthy
    logger.info("🔄 Restarting websocket connection...")
    if ws_task and not ws_task.done():
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
    ws_connection_healthy = False
    order_book_received.clear()
    ws_client = RobustWsClient(order_book_ids=[MARKET_ID], account_ids=[], on_order_book_update=on_order_book_update, on_account_update=on_account_update)
    ws_task = asyncio.create_task(ws_client.run_async())
    try:
        logger.info("Waiting for websocket reconnection...")
        await asyncio.wait_for(order_book_received.wait(), timeout=15.0)
        logger.info("✅ Websocket reconnected successfully")
        return True
    except asyncio.TimeoutError:
        logger.error("❌ Websocket reconnection failed - timeout.")
        return False

async def market_making_loop(client, account_api, order_api):
    global last_mid_price, order_side, order_counter, current_order_id
    global current_position_size, last_order_base_amount, position_detected_at_startup

    logger.info("🚀 Starting Avellaneda-Stoikov market making loop...")
    if not position_detected_at_startup:
        logger.warning("Position detection may have failed. Please verify no open positions.")
        await asyncio.sleep(3)

    while True:
        try:
            if not await check_websocket_health():
                logger.warning("⚠ Websocket connection unhealthy, attempting restart...")
                if not await restart_websocket():
                    logger.error("Failed to restart websocket, retrying in 10 seconds")
                    await asyncio.sleep(10)
                    continue

            current_mid_price = get_current_mid_price()
            if current_mid_price is None:
                logger.info("No order book data yet, sleeping...")
                await asyncio.sleep(2)
                continue

            price_changed = (last_mid_price is None or abs(current_mid_price - last_mid_price) / last_mid_price > 0.001)

            order_price = calculate_order_price(current_mid_price, order_side)
            if order_price is None:
                # REQUIRE_PARAMS true et pas de params → on attend
                await asyncio.sleep(3)
                continue

            # Log de l'écart en %
            if current_mid_price > 0:
                pct = ((order_price - current_mid_price) / current_mid_price) * 100.0
            else:
                pct = 0.0
            logger.info(f"Mid: ${current_mid_price:.6f}, Target {order_side}: ${order_price:.6f} ({pct:+.4f}%), Price changed: {price_changed}")

            if current_order_id is not None and price_changed:
                await cancel_order(client, current_order_id)
            elif current_order_id is not None and not price_changed:
                logger.info(f"Order {current_order_id} still active - price unchanged")

            if current_order_id is None:
                order_counter += 1
                if order_side == "buy":
                    base_amt = await calculate_dynamic_base_amount(account_api, current_mid_price)
                else:
                    if current_position_size > 0:
                        base_amt = current_position_size
                    else:
                        logger.info("Sell side but no inventory → skip.")
                        await asyncio.sleep(3)
                        continue

                if base_amt > 0:
                    last_order_base_amount = base_amt
                    ok = await place_order(client, order_side, order_price, order_counter, base_amt)
                    if not ok:
                        await asyncio.sleep(5)
                        continue
                    last_mid_price = current_mid_price
                else:
                    logger.warning("Calculated order size is zero, skip.")
                    await asyncio.sleep(3)
                    continue

            start = time.time()
            filled = False
            while time.time() - start < ORDER_TIMEOUT:
                if current_order_id is not None:
                    filled = await check_order_filled(order_api, client, current_order_id)
                    if filled:
                        logger.info(f"✅ Order {current_order_id} was filled!")
                        current_order_id = None
                        break
                await asyncio.sleep(5)

            if filled:
                prev_side = order_side
                if prev_side == "buy":
                    current_position_size = last_order_base_amount
                    logger.info(f"✅ Position opened. Inventory size: {current_position_size}")
                else:
                    current_position_size = 0
                    logger.info("✅ Position closed. Inventory size: 0")
                order_side = "sell" if prev_side == "buy" else "buy"
                logger.info(f"🔄 Switching from {prev_side} to {order_side} side")
                await asyncio.sleep(3)
            else:
                if current_order_id is not None:
                    logger.info(f"⏰ Order {current_order_id} not filled in {ORDER_TIMEOUT}s, cancelling.")
                    await cancel_order(client, current_order_id)

            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Loop error: {e}", exc_info=True)
            if "websocket" in str(e).lower():
                global ws_connection_healthy
                ws_connection_healthy = False
            await asyncio.sleep(5)

async def track_balance(account_api):
    log_path = os.path.join(LOG_DIR, "balance_log.txt")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    while True:
        try:
            if current_position_size == 0:
                balance = await get_account_balances(account_api)
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(log_path, "a") as f:
                    f.write(f"[{timestamp}] Available Capital: ${balance:,.2f}\n")
                logger.info(f"Balance of ${balance:,.2f} logged to {log_path}")
            else:
                logger.info(f"Skipping balance logging (open position: {current_position_size})")
        except Exception as e:
            logger.error(f"Error in track_balance: {e}", exc_info=True)
        await asyncio.sleep(300)

async def main():
    global MARKET_ID, PRICE_TICK_SIZE, AMOUNT_TICK_SIZE
    global ws_client, ws_task, last_order_book_update, position_detected_at_startup
    global last_mid_price, current_order_id, current_position_size, order_side

    logger.info("=== Market Maker Starting ===")

    api_client = lighter.ApiClient(configuration=lighter.Configuration(host=BASE_URL))
    account_api = lighter.AccountApi(api_client)
    order_api = lighter.OrderApi(api_client)

    details = await get_market_details(order_api, MARKET_SYMBOL)
    if not details:
        logger.error(f"Could not retrieve market details for {MARKET_SYMBOL}. Exiting.")
        return
    MARKET_ID, PRICE_TICK_SIZE, AMOUNT_TICK_SIZE = details
    logger.info(f"Market {MARKET_SYMBOL}: id={MARKET_ID}, tick(price)={PRICE_TICK_SIZE}, tick(amount)={AMOUNT_TICK_SIZE}")

    client = lighter.SignerClient(
        url=BASE_URL,
        private_key=API_KEY_PRIVATE_KEY,
        account_index=ACCOUNT_INDEX,
        api_key_index=API_KEY_INDEX,
    )
    err = client.check_client()
    if err is not None:
        logger.error(f"CheckClient error: {trim_exception(err)}")
        await api_client.close()
        await client.close()
        return
    logger.info("Client connected successfully")

    # Clean slate: cancel all
    try:
        tx, tx_hash, err = await client.cancel_all_orders(
            time_in_force=client.CANCEL_ALL_TIF_IMMEDIATE,
            time=0
        )
        if err is not None:
            logger.error(f"Failed to cancel existing orders at startup: {trim_exception(err)}")
            await api_client.close()
            await client.close()
            return
        await asyncio.sleep(3)
    except Exception as e:
        logger.error(f"Exception during cancel-all: {e}", exc_info=True)
        await api_client.close()
        await client.close()
        return

    # Detect positions
    info = await check_open_positions(account_api)
    if info:
        try:
            pos_found = False
            if hasattr(info, 'accounts') and info.accounts:
                acc = info.accounts[0]
                if hasattr(acc, 'positions') and acc.positions:
                    for p in acc.positions:
                        if getattr(p, 'market_id', None) == MARKET_ID:
                            sz = float(getattr(p, 'position', 0))
                            if abs(sz) > 0:
                                logger.info(f"🔍 DETECTED POSITION in market {MARKET_ID}: {sz}")
                                pos_found = True
            logger.info("✅ Position detection successful" + (" - found open position(s)" if pos_found else " - no open positions found"))
            position_detected_at_startup = True
        except Exception as e:
            logger.error(f"Error parsing account positions: {e}", exc_info=True)
    else:
        logger.warning("❌ Position detection failed.")

    last_order_book_update = time.time()
    ws_client = RobustWsClient(order_book_ids=[MARKET_ID], account_ids=[], on_order_book_update=on_order_book_update, on_account_update=on_account_update)
    ws_task = asyncio.create_task(ws_client.run_async())

    try:
        logger.info("Waiting for initial order book data...")
        await asyncio.wait_for(order_book_received.wait(), timeout=30.0)
        logger.info(f"✅ Websocket connected for market {MARKET_ID}")

        # Fermer automatiquement un long existant au démarrage si demandé
        if CLOSE_LONG_ON_STARTUP:
            info2 = await check_open_positions(account_api)
            if info2 and hasattr(info2, 'accounts') and info2.accounts:
                acc = info2.accounts[0]
                if hasattr(acc, 'positions') and acc.positions:
                    for p in acc.positions:
                        if getattr(p, 'market_id', None) == MARKET_ID:
                            sz = float(getattr(p, 'position', 0))
                            if sz > 0:
                                mid = get_current_mid_price()
                                if mid:
                                    logger.info(f"🔄 Closing existing long position of size {sz} at startup (flag ON)")
                                    ok = await close_long_position(client, sz, mid)
                                    if ok:
                                        global order_side, current_position_size, last_mid_price
                                        current_position_size = sz
                                        order_side = "sell"
                                        last_mid_price = mid
                                        logger.info(f"✅ Reduce-only sell placed. Inventory set to {current_position_size}.")
                                else:
                                    logger.warning("No fresh mid price yet; skip auto-close this boot.")

        balance_task = asyncio.create_task(track_balance(account_api))
        await market_making_loop(client, account_api, order_api)

    except asyncio.TimeoutError:
        logger.error("❌ Timeout waiting for initial order book data.")
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("=== Shutdown signal received - Stopping... ===")
    finally:
        logger.info("=== Market Maker Cleanup Starting ===")
        if 'balance_task' in locals() and not balance_task.done():
            balance_task.cancel()
            try:
                await balance_task
            except asyncio.CancelledError:
                pass
        if current_order_id is not None:
            logger.info(f"Cancelling open order {current_order_id} before exit...")
            await cancel_order(client, current_order_id)
        if ws_task and not ws_task.done():
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
        await client.close()
        await api_client.close()
        logger.info("Market maker stopped.")

# ============ Entrypoint with signal handling ============
if __name__ == "__main__":
    async def main_with_signal_handling():
        loop = asyncio.get_running_loop()
        main_task = asyncio.create_task(main())

        def shutdown_handler(sig):
            logger.info(f"Received exit signal {sig.name}, cancelling main task.")
            if not main_task.done():
                main_task.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, shutdown_handler, sig)
            except NotImplementedError:
                # add_signal_handler not available on some platforms (e.g., Windows event loop)
                pass

        try:
            await main_task
        except asyncio.CancelledError:
            logger.info("Main task cancelled. Cleanup is handled in main().")

    try:
        asyncio.run(main_with_signal_handling())
        logger.info("Application has finished gracefully.")
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application exiting.")
