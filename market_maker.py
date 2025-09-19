import asyncio
import logging
import lighter
import os
import time
import json
import math
from typing import Tuple, Optional
from datetime import datetime
from dotenv import load_dotenv
from lighter.exceptions import ApiException

load_dotenv()

# Setup comprehensive logging
# Remove any existing handlers to avoid conflicts
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

# Create logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# File handler for debug logs (includes everything) - reset on each run
# Try to write to logs directory (shared volume) first, fallback to current directory
log_paths = ['logs/market_maker_debug.txt', 'market_maker_debug.txt']
log_file = None
for path in log_paths:
    try:
        # Create logs directory if it doesn't exist
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else '.', exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
        # Test if we can create the file
        with open(path, 'w') as test_file:
            pass
        log_file = path
        break
    except (OSError, PermissionError):
        continue

if log_file is None:
    log_file = 'market_maker_debug.txt'  # Final fallback

file_handler = logging.FileHandler(log_file, mode='w')
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s')
file_handler.setFormatter(file_formatter)

# Console handler for info logs only
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(console_formatter)

# Add handlers to logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Prevent propagation to root logger
logger.propagate = False

# Suppress noisy third-party loggers from console (but allow in file)
websockets_logger = logging.getLogger('websockets')
websockets_logger.setLevel(logging.WARNING)
asyncio_logger = logging.getLogger('asyncio')
asyncio_logger.setLevel(logging.WARNING)
urllib3_logger = logging.getLogger('urllib3')
urllib3_logger.setLevel(logging.WARNING)
logging.root.setLevel(logging.WARNING)

# Configuration
BASE_URL = "https://mainnet.zklighter.elliot.ai"
API_KEY_PRIVATE_KEY = os.getenv("API_KEY_PRIVATE_KEY")
ACCOUNT_INDEX = int(os.getenv("ACCOUNT_INDEX"))
API_KEY_INDEX = int(os.getenv("API_KEY_INDEX"))
MARKET_SYMBOL = "PAXG"  # The symbol for the market
MARKET_ID = None  # Will be fetched dynamically at startup.
PRICE_TICK_SIZE = None  # Will be fetched dynamically at startup.
AMOUNT_TICK_SIZE = None  # Will be fetched dynamically at startup.
SPREAD = 0.035 / 100.0  # Fallback spread percent if Avellaneda parameters are not used.
BASE_AMOUNT = 0.047  # Fallback order size if dynamic sizing fails.
USE_DYNAMIC_SIZING = True
CAPITAL_USAGE_PERCENT = 0.99
SAFETY_MARGIN_PERCENT = 0.01
ORDER_TIMEOUT = 90  # seconds

# Global variables for websocket order book data
latest_order_book = None
order_book_received = asyncio.Event()
ws_connection_healthy = False
last_order_book_update = 0
current_mid_price_cached = None
ws_client = None
ws_task = None

# Global variables for tracking orders and strategy state
current_order_id = None
current_order_timestamp = None
last_mid_price = None
order_side = "buy"
order_counter = 1000
available_capital = None
last_capital_check = 0

# Inventory Management
current_position_size = 0  # Tracks the size of our current position.
last_order_base_amount = 0 # Stores the size of the last placed order.

# Avellaneda-Stoikov parameters
avellaneda_params = None
last_avellaneda_update = 0
AVELLANEDA_REFRESH_INTERVAL = 900 # seconds

# Flag to confirm startup position check has completed.
position_detected_at_startup = False


async def get_market_details(order_api, symbol: str) -> Optional[Tuple[int, float, float]]:
    """
    Retrieves the market index, price tick size, and amount tick size for a given symbol.
    """
    try:
        order_books_response = await order_api.order_books()
        for order_book in order_books_response.order_books:
            if order_book.symbol.upper() == symbol.upper():
                market_id = order_book.market_id
                price_tick_size = 10 ** -order_book.supported_price_decimals
                amount_tick_size = 10 ** -order_book.supported_size_decimals
                return market_id, price_tick_size, amount_tick_size
        return None  # Symbol not found
    except Exception as e:
        logger.error(f"An error occurred while fetching market details: {e}")
        return None


def trim_exception(e: Exception) -> str:
    result = str(e).strip().split("\n")[-1]
    logger.debug(f"trim_exception input: {e}, output: {result}")
    return result


async def get_account_balances(account_api):
    """Get account balances."""
    logger.debug("get_account_balances called")
    logger.info("Retrieving account balances...")

    try:
        account = await account_api.account(by="index", value=str(ACCOUNT_INDEX))
        logger.info("Successfully retrieved account data")

        if hasattr(account, 'accounts') and account.accounts:
            logger.info(f"Found accounts array with {len(account.accounts)} entries")
            account_data = account.accounts[0]
            balance_fields = ['available_balance', 'collateral', 'total_asset_value', 'cross_asset_value']

            for field_name in balance_fields:
                if hasattr(account_data, field_name):
                    balance_value = getattr(account_data, field_name)
                    try:
                        balance_float = float(balance_value)
                        if balance_float > 0:
                            logger.info(f"✅ Using {field_name} as available capital: ${balance_float}")
                            return balance_float
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Could not convert {field_name} value '{balance_value}' to float: {e}")
            logger.warning(f"No positive balance found in any of the fields: {balance_fields}")
        else:
            logger.warning("No accounts array found in response")
        return 0
    except Exception as e:
        logger.error(f"Error getting account balances: {e}", exc_info=True)
        return 0


async def check_open_positions(account_api):
    """Check for open positions using the Lighter SDK."""
    logger.debug("check_open_positions called")
    logger.info("Detecting open positions using Lighter SDK AccountApi...")
    try:
        account = await account_api.account(by="index", value=str(ACCOUNT_INDEX))
        logger.info(f"Successfully retrieved account data")
        return account
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
        logger.info(f"Successfully placed position closing order: tx={tx_hash}")
        current_order_id = order_counter
        return True
    except Exception as e:
        logger.error(f"Exception in close_long_position: {e}", exc_info=True)
        return False


def on_order_book_update(market_id, order_book):
    """Callback function for websocket order book updates."""
    global latest_order_book, ws_connection_healthy, last_order_book_update, current_mid_price_cached
    try:
        if int(market_id) == MARKET_ID:
            bids = order_book.get('bids', [])
            asks = order_book.get('asks', [])
            if bids and asks:
                best_bid = float(bids[0]['price'])
                best_ask = float(asks[0]['price'])
                new_mid_price = (best_bid + best_ask) / 2
                current_mid_price_cached = new_mid_price
                if latest_order_book:
                    old_bids = latest_order_book.get('bids', [])
                    old_asks = latest_order_book.get('asks', [])
                    if old_bids and old_asks:
                        old_mid = (float(old_bids[0]['price']) + float(old_asks[0]['price'])) / 2
                        price_change_pct = abs(new_mid_price - old_mid) / old_mid * 100
                        if price_change_pct > 0.1:
                            logger.info(f"Price update: {old_mid:.2f} → {new_mid_price:.2f} ({price_change_pct:+.2f}%)")
            latest_order_book = order_book
            ws_connection_healthy = True
            last_order_book_update = time.time()
            order_book_received.set()
    except Exception as e:
        logger.error(f"Error in order book callback: {e}", exc_info=True)
        ws_connection_healthy = False

def on_account_update(account_id, account):
    """Callback for account updates (not used in this strategy)."""
    pass


class RobustWsClient(lighter.WsClient):
    """Enhanced WebSocket client with ping/pong handling."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ping_count = 0

    def handle_unhandled_message(self, message):
        try:
            if isinstance(message, dict):
                msg_type = message.get('type', 'unknown')
                if msg_type in ['ping', 'pong', 'heartbeat', 'keepalive', 'health']:
                    logger.debug(f"Received {msg_type} message - connection healthy")
                    return
                else:
                    logger.warning(f"Unknown message type '{msg_type}': {message}")
            else:
                logger.warning(f"Non-dict message received: {type(message)} - {message}")
        except Exception as e:
            logger.error(f"Error handling websocket message: {e}", exc_info=True)

def get_current_mid_price():
    """Get current mid price from cached websocket data."""
    global current_mid_price_cached
    if current_mid_price_cached is not None:
        price_age = time.time() - last_order_book_update
        if price_age < 10:
            return current_mid_price_cached
    if latest_order_book is None: return None
    bids = latest_order_book.get('bids', [])
    asks = latest_order_book.get('asks', [])
    if not bids or not asks: return None
    best_bid = float(bids[0]['price'])
    best_ask = float(asks[0]['price'])
    mid_price = (best_bid + best_ask) / 2
    current_mid_price_cached = mid_price
    return mid_price


async def calculate_dynamic_base_amount(account_api, current_mid_price):
    """Calculate base amount based on available capital."""
    global available_capital, last_capital_check
    if not USE_DYNAMIC_SIZING: return BASE_AMOUNT
    current_time = time.time()
    if available_capital is None or (current_time - last_capital_check) > 60:
        available_capital = await get_account_balances(account_api)
        last_capital_check = current_time
        logger.info(f"Available capital: ${available_capital:.2f}")
    if available_capital <= 0:
        logger.warning(f"No available capital, using static BASE_AMOUNT: {BASE_AMOUNT}")
        return BASE_AMOUNT
    usable_capital = available_capital * (1 - SAFETY_MARGIN_PERCENT)
    order_capital = usable_capital * CAPITAL_USAGE_PERCENT
    if current_mid_price and current_mid_price > 0:
        dynamic_base_amount = order_capital / current_mid_price
        min_order_size = 0.001
        if dynamic_base_amount < min_order_size:
            logger.warning(f"Calculated base amount {dynamic_base_amount:.6f} below minimum {min_order_size}, using minimum")
            dynamic_base_amount = min_order_size
        logger.info(f"Dynamic sizing: ${order_capital:.2f} / ${current_mid_price:.2f} = {dynamic_base_amount:.6f} units")
        return dynamic_base_amount
    else:
        logger.warning(f"Invalid mid price, using static BASE_AMOUNT: {BASE_AMOUNT}")
        return BASE_AMOUNT


def load_avellaneda_parameters():
    """Load and validate Avellaneda-Stoikov parameters from JSON file."""
    global avellaneda_params, last_avellaneda_update
    try:
        current_time = time.time()
        # Return True if parameters are fresh and have been validated previously.
        if avellaneda_params is not None and (current_time - last_avellaneda_update) < AVELLANEDA_REFRESH_INTERVAL:
            return True

        # Reset params to force re-validation.
        avellaneda_params = None

        possible_paths = [
            f'params/avellaneda_parameters_{MARKET_SYMBOL}.json',
            f'avellaneda_parameters_{MARKET_SYMBOL}.json',
            f'TRADER/avellaneda_parameters_{MARKET_SYMBOL}.json'
        ]

        params_file_content = None
        for path in possible_paths:
            try:
                with open(path, 'r') as f:
                    params_file_content = json.load(f)
                logger.debug(f"Loaded Avellaneda parameters from: {path}")
                break
            except FileNotFoundError:
                continue
            except json.JSONDecodeError as e:
                logger.warning(f"⚠️ Invalid JSON in {path}: {e}. Falling back to static spread.")
                return False

        if not params_file_content:
            logger.warning(f"⚠️ avellaneda_parameters_{MARKET_SYMBOL}.json not found. Falling back to static spread.")
            return False

        # Validate the structure and content of the parameters.
        limit_orders = params_file_content.get('limit_orders')
        if not isinstance(limit_orders, dict):
            logger.warning("⚠️ 'limit_orders' key is missing or invalid in JSON. Falling back to static spread.")
            return False

        delta_a = limit_orders.get('delta_a')
        delta_b = limit_orders.get('delta_b')

        if delta_a is None or delta_b is None:
            logger.warning("⚠️ 'delta_a' or 'delta_b' is missing in JSON. Falling back to static spread.")
            return False

        try:
            # Ensure the delta values are valid, finite, non-negative numbers.
            delta_a_float = float(delta_a)
            delta_b_float = float(delta_b)

            if not math.isfinite(delta_a_float) or not math.isfinite(delta_b_float):
                logger.warning("⚠️ 'delta_a' or 'delta_b' is not a finite number (NaN or Inf). Falling back to static spread.")
                return False

            if delta_a_float < 0 or delta_b_float < 0:
                logger.warning("⚠️ 'delta_a' and 'delta_b' must be non-negative. Falling back to static spread.")
                return False
        except (ValueError, TypeError):
            logger.warning("⚠️ 'delta_a' or 'delta_b' is not a valid number. Falling back to static spread.")
            return False

        # If all checks pass, set the global parameters.
        avellaneda_params = params_file_content
        last_avellaneda_update = current_time
        logger.info("📊 Successfully loaded and validated Avellaneda parameters.")
        logger.info(f"   Ask spread (delta_a): {delta_a}")
        logger.info(f"   Bid spread (delta_b): {delta_b}")
        return True

    except Exception as e:
        logger.error(f"❌ Unexpected error loading Avellaneda parameters: {e}")
        avellaneda_params = None  # Ensure fallback on unexpected error.
        return False

def calculate_order_price(mid_price, side):
    """Calculate order price using Avellaneda-Stoikov or static spread."""
    # load_avellaneda_parameters now handles all validation.
    # If it returns True, we can safely use the parameters.
    if load_avellaneda_parameters() and avellaneda_params:
        limit_orders = avellaneda_params['limit_orders']
        if side == "buy":
            return mid_price - float(limit_orders['delta_b'])
        else:  # side == "sell"
            return mid_price + float(limit_orders['delta_a'])

    # Fallback to static spread if parameters are invalid or unavailable.
    logger.debug("Falling back to static spread calculation.")
    if side == "buy":
        return mid_price * (1.0 - SPREAD)
    else:
        return mid_price * (1.0 + SPREAD)


async def place_order(client, side, price, order_id, base_amount):
    """Place a limit order."""
    global current_order_id
    is_ask = (side == "sell")
    base_amount_scaled = int(base_amount / AMOUNT_TICK_SIZE)
    price_scaled = int(price / PRICE_TICK_SIZE)
    logger.info(f"Placing {side} order: {base_amount:.6f} units at ${price:.2f} (ID: {order_id})")
    try:
        tx, tx_hash, err = await client.create_order(
            market_index=MARKET_ID,
            client_order_index=order_id,
            base_amount=base_amount_scaled,
            price=price_scaled,
            is_ask=is_ask,
            order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
            time_in_force=lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
            reduce_only=True if side == "sell" else False
        )
        if err is not None:
            logger.error(f"Error placing {side} order: {trim_exception(err)}")
            return False
        logger.info(f"Successfully placed {side} order: tx={tx_hash}")
        current_order_id = order_id
        return True
    except Exception as e:
        logger.error(f"Exception in place_order: {e}", exc_info=True)
        return False


async def cancel_order(client, order_id):
    """Cancel an existing order."""
    global current_order_id
    logger.info(f"Cancelling order {order_id}")
    try:
        tx, tx_hash, err = await client.cancel_order(
            market_index=MARKET_ID,
            order_index=order_id,
        )
        if err is not None:
            logger.error(f"Error cancelling order {order_id}: {trim_exception(err)}")
            return False
        logger.info(f"Successfully cancelled order {order_id}: tx={tx_hash}")
        current_order_id = None
        return True
    except Exception as e:
        logger.error(f"Exception in cancel_order: {e}", exc_info=True)
        return False


async def check_order_filled(order_api, client, order_id):
    """Check if an order has been filled."""
    try:
        auth_token, err = client.create_auth_token_with_expiry()
        if err:
            logger.error(f"Failed to create auth token for order check: {err}")
            return False
        active_orders_response = await order_api.account_active_orders(
            account_index=ACCOUNT_INDEX,
            market_id=MARKET_ID,
            auth=auth_token
        )
        if active_orders_response is None:
            logger.warning(f"No response from active orders API for order {order_id}")
            return False
        if not hasattr(active_orders_response, 'orders') or active_orders_response.orders is None:
            logger.warning(f"Invalid active orders response structure for order {order_id}")
            return False
        for order in active_orders_response.orders:
            if hasattr(order, 'client_order_index') and order.client_order_index == order_id:
                logger.debug(f"Order {order_id} is still active and unfilled.")
                return False
        logger.debug(f"Order {order_id} not found in active orders - likely filled or cancelled.")
        return True
    except ApiException as e:
        if e.status == 429:
            logger.warning("Too Many Requests when checking order status. Waiting 10s before retrying.")
            await asyncio.sleep(10)
        else:
            logger.warning(f"API Error checking order status for {order_id}: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.warning(f"Error checking order status for {order_id}: {e}", exc_info=True)
        return False


async def market_making_loop(client, account_api, order_api):
    """Main market making loop."""
    global last_mid_price, order_side, order_counter, current_order_id, position_detected_at_startup
    global current_position_size, last_order_base_amount

    logger.info("🚀 Starting Avellaneda-Stoikov market making loop...")
    if not position_detected_at_startup:
        logger.warning("Position detection may have failed. Please verify no open positions.")
        await asyncio.sleep(3)

    iteration = 0
    while True:
        iteration += 1
        try:
            if not await check_websocket_health():
                logger.warning("⚠ Websocket connection unhealthy, attempting restart...")
                if not await restart_websocket():
                    logger.error("Failed to restart websocket, retrying in 10 seconds")
                    await asyncio.sleep(10)
                    continue

            current_mid_price = get_current_mid_price()
            if current_mid_price is None:
                logger.warning("No order book data available, sleeping...")
                await asyncio.sleep(2)
                continue

            price_changed = (last_mid_price is None or abs(current_mid_price - last_mid_price) / last_mid_price > 0.001)
            order_price = calculate_order_price(current_mid_price, order_side)
            if current_mid_price > 0:
                percentage_diff = ((order_price - current_mid_price) / current_mid_price) * 100
            else:
                percentage_diff = 0.0
            logger.info(f"Mid: ${current_mid_price:.2f}, Target {order_side}: ${order_price:.2f} ({percentage_diff:+.4f}%), Price changed: {price_changed}")

            if current_order_id is not None and price_changed:
                await cancel_order(client, current_order_id)
            elif current_order_id is not None and not price_changed:
                logger.info(f"Order {current_order_id} still active - price unchanged")

            if current_order_id is None:
                order_counter += 1
                base_amount_to_use = 0
                
                # Inventory-aware order sizing
                if order_side == "buy":
                    # For a buy order, calculate the size based on available capital.
                    base_amount_to_use = await calculate_dynamic_base_amount(account_api, current_mid_price)
                elif order_side == "sell":
                    # For a sell order, the size is determined by the current position we hold.
                    if current_position_size > 0:
                        base_amount_to_use = current_position_size
                    else:
                        # This case should ideally not be reached if logic is correct.
                        logger.warning("Logic Error: Attempted to place a sell order but have no recorded position. Skipping.")
                        await asyncio.sleep(5)
                        continue # Skip this loop iteration and re-evaluate
                
                if base_amount_to_use > 0:
                    last_order_base_amount = base_amount_to_use
                    success = await place_order(client, order_side, order_price, order_counter, base_amount_to_use)
                    if not success:
                        await asyncio.sleep(5)
                        continue
                    last_mid_price = current_mid_price
                else:
                    logger.warning(f"Calculated order size is zero. Cannot place order.")
                    await asyncio.sleep(5)
                    continue

            start_time = time.time()
            filled = False
            while time.time() - start_time < ORDER_TIMEOUT:
                if current_order_id is not None:
                    filled = await check_order_filled(order_api, client, current_order_id)
                    if filled:
                        logger.info(f"✅ Order {current_order_id} was filled!")
                        current_order_id = None
                        break
                await asyncio.sleep(5)

            if filled:
                old_side = order_side

                # Update inventory after a successful order fill.
                if old_side == "buy":
                    # A buy order was filled, so we now have a position.
                    current_position_size = last_order_base_amount
                    logger.info(f"✅ Position opened. Current inventory size: {current_position_size}")
                elif old_side == "sell":
                    # A sell order was filled, closing our position.
                    current_position_size = 0
                    logger.info(f"✅ Position closed. Current inventory size is now 0.")

                
                order_side = "sell" if old_side == "buy" else "buy"
                logger.info(f"🔄 Order filled! Switching from {old_side} to {order_side} side")
                logger.info("⏳ Waiting 3 seconds before placing next order...")
                await asyncio.sleep(3)
            else:
                if current_order_id is not None:
                    logger.info(f"⏰ Order {current_order_id} not filled within {ORDER_TIMEOUT}s, cancelling.")
                    await cancel_order(client, current_order_id)
            
            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Error in market making loop iteration: {e}", exc_info=True)
            if "websocket" in str(e).lower():
                global ws_connection_healthy
                ws_connection_healthy = False
            await asyncio.sleep(5)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt in loop")
            if current_order_id is not None:
                await cancel_order(client, current_order_id)
            raise


async def track_balance(account_api):
    """Periodically tracks and logs the account balance."""
    log_path = "logs/balance_log.txt"
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
                logger.info(f"Skipping balance logging because a position is open (size: {current_position_size})")
        except Exception as e:
            logger.error(f"Error in track_balance: {e}", exc_info=True)
        await asyncio.sleep(300)  # 5 minutes


async def main():
    """Main function."""
    global latest_order_book, current_order_id, position_detected_at_startup, MARKET_ID, PRICE_TICK_SIZE, AMOUNT_TICK_SIZE
    logger.info("=== Market Maker Starting ===")

    api_client = lighter.ApiClient(configuration=lighter.Configuration(host=BASE_URL))
    account_api = lighter.AccountApi(api_client)
    order_api = lighter.OrderApi(api_client)

    # Fetch market details dynamically
    market_details = await get_market_details(order_api, MARKET_SYMBOL)
    if not market_details:
        logger.error(f"Could not retrieve market details for {MARKET_SYMBOL}. Exiting.")
        return
    MARKET_ID, PRICE_TICK_SIZE, AMOUNT_TICK_SIZE = market_details
    logger.info(f"Successfully fetched market details for {MARKET_SYMBOL}:")
    logger.info(f"  Market ID: {MARKET_ID}")
    logger.info(f"  Price Tick Size: {PRICE_TICK_SIZE}")
    logger.info(f"  Amount Tick Size: {AMOUNT_TICK_SIZE}")

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
    
    # Startup Safety Protocol: Cancel any existing orders to ensure a clean slate.
    logger.info("Ensuring a clean slate by cancelling any existing orders for this account...")

    # This cancels ALL orders for the account across ALL markets to ensure a clean state.
    try:
        tx, tx_hash, err = await client.cancel_all_orders(
            time_in_force=client.CANCEL_ALL_TIF_IMMEDIATE,
            time=0
        )
        if err is not None:
            # A failure here is critical; we cannot proceed without a clean state.
            logger.error(f"CRITICAL: Failed to cancel existing orders at startup: {trim_exception(err)}")
            logger.error("Cannot continue safely. Exiting.")
            await api_client.close()
            await client.close()
            return

        logger.info(f"Successfully sent cancel all orders transaction: {tx_hash.tx_hash if tx_hash else 'OK'}")
        # Wait a moment for the exchange to process the cancellation.
        await asyncio.sleep(3)

    except Exception as e:
        logger.error(f"CRITICAL: Exception during cancel all orders at startup: {e}", exc_info=True)
        logger.error("Cannot continue safely. Exiting.")
        await api_client.close()
        await client.close()
        return

    account_info_response = await check_open_positions(account_api)
    if account_info_response:
        try:
            position_found = False
            if hasattr(account_info_response, 'accounts') and account_info_response.accounts:
                account_info = account_info_response.accounts[0]
                if hasattr(account_info, 'positions') and account_info.positions:
                    for position in account_info.positions:
                        if hasattr(position, 'market_id') and position.market_id == MARKET_ID:
                            position_size = float(getattr(position, 'position', 0))
                            if abs(position_size) > 0:
                                logger.info(f"🔍 DETECTED POSITION in market {MARKET_ID}: {position_size}")
                                position_found = True
            if position_found: logger.info("✅ Position detection successful - found open position(s)")
            else: logger.info("✅ Position detection successful - no open positions found")
            position_detected_at_startup = True
        except Exception as e:
            logger.error(f"Error parsing account positions: {e}", exc_info=True)
    else:
        logger.warning("❌ Position detection failed.")

    global ws_client, ws_task, last_order_book_update
    last_order_book_update = time.time()
    ws_client = RobustWsClient(order_book_ids=[MARKET_ID], account_ids=[], on_order_book_update=on_order_book_update, on_account_update=on_account_update)
    ws_task = asyncio.create_task(ws_client.run_async())

    try:
        logger.info("Waiting for initial order book data...")
        await asyncio.wait_for(order_book_received.wait(), timeout=30.0)
        logger.info(f"✅ Websocket connected for market {MARKET_ID}")

        logger.info("Re-checking positions before trading...")
        position_info_response = await check_open_positions(account_api)
        if position_info_response:
            try:
                if hasattr(position_info_response, 'accounts') and position_info_response.accounts:
                    if hasattr(position_info_response.accounts[0], 'positions') and position_info_response.accounts[0].positions:
                        for position in position_info_response.accounts[0].positions:
                            if hasattr(position, 'market_id') and position.market_id == MARKET_ID:
                                position_size = float(getattr(position, 'position', 0))
                                if position_size > 0: # NOTE: This logic only handles closing existing LONG positions at startup.
                                    current_mid_price = get_current_mid_price()
                                    if current_mid_price:
                                        logger.info(f"🔄 Closing existing long position of size {position_size} at startup")
                                        if await close_long_position(client, position_size, current_mid_price):
                                            # Synchronize internal state with the closing action.
                                            global order_side, last_mid_price, current_position_size
                                            current_position_size = position_size
                                            order_side = "sell"
                                            last_mid_price = current_mid_price
                                            logger.info(f"✅ Position closing order placed. Inventory state updated to {current_position_size}.")
            except Exception as e:
                logger.warning(f"Error during startup position closing: {e}")

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
            try: await ws_task
            except asyncio.CancelledError: pass
        await client.close()
        await api_client.close()
        logger.info("Market maker stopped.")


async def check_websocket_health():
    """Check if websocket connection is healthy."""
    global ws_connection_healthy, last_order_book_update, ws_task
    if (time.time() - last_order_book_update) > 30:
        logger.warning(f"Websocket unhealthy - no updates for {time.time() - last_order_book_update:.1f}s")
        ws_connection_healthy = False
        return False
    if ws_task and ws_task.done():
        logger.warning("Websocket task has finished unexpectedly")
        try:
            ws_task.result()
        except Exception as e:
            logger.error(f"Websocket task exception: {e}", exc_info=True)
        ws_connection_healthy = False
        return False
    return ws_connection_healthy


async def restart_websocket():
    """Restart the websocket connection."""
    global ws_client, ws_task, order_book_received, ws_connection_healthy
    logger.info("🔄 Restarting websocket connection...")
    if ws_task and not ws_task.done():
        ws_task.cancel()
        try: await ws_task
        except asyncio.CancelledError: pass
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


if __name__ == "__main__":
    import signal

    async def main_with_signal_handling():
        """Wraps the main application to handle shutdown signals gracefully."""
        loop = asyncio.get_running_loop()
        main_task = asyncio.create_task(main())

        def shutdown_handler(sig):
            logger.info(f"Received exit signal {sig.name}, cancelling main task.")
            if not main_task.done():
                main_task.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown_handler, sig)

        try:
            await main_task
        except asyncio.CancelledError:
            logger.info("Main task cancelled. Cleanup is handled in main().")

    try:
        asyncio.run(main_with_signal_handling())
        logger.info("Application has finished gracefully.")
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application exiting.")
