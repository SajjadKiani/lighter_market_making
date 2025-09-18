# How to Change the Cryptocurrency

This document explains how to change the cryptocurrency that the market maker trades. The project is currently configured to trade `PAXG`.

To change the cryptocurrency, you need to modify the following files:

### 1. `docker-compose.yml`

You need to replace all instances of `PAXG` with the new ticker symbol (e.g., `BTC`).

*   **Line 45:**
    ```yaml
    - ./avellaneda_parameters_PAXG.json:/app/avellaneda_parameters_PAXG.json
    ```
    **Change to (for BTC):**
    ```yaml
    - ./avellaneda_parameters_BTC.json:/app/avellaneda_parameters_BTC.json
    ```

*   **Line 52:**
    ```yaml
    python calculate_avellaneda_parameters.py PAXG --hours 4;
    ```
    **Change to (for BTC):**
    ```yaml
    python calculate_avellaneda_parameters.py BTC --hours 4;
    ```

*   **Line 58:**
    ```yaml
    test: ["CMD", "test", "-f", "/app/avellaneda_parameters_PAXG.json"]
    ```
    **Change to (for BTC):**
    ```yaml
    test: ["CMD", "test", "-f", "/app/avellaneda_parameters_BTC.json"]
    ```

*   **Line 70:**
    ```yaml
    - ./avellaneda_parameters_PAXG.json:/app/avellaneda_parameters_PAXG.json
    ```
    **Change to (for BTC):**
    ```yaml
    - ./avellaneda_parameters_BTC.json:/app/avellaneda_parameters_BTC.json
    ```

### 2. `calculate_avellaneda_parameters.py`

You need to change the default ticker and add a case for the new ticker in the `get_tick_size` function.

*   **Line 25:**
    ```python
    parser.add_argument('ticker', nargs='?', default='PAXG', help='Ticker symbol (default: BTC)')
    ```
    **Change to (for BTC):**
    ```python
    parser.add_argument('ticker', nargs='?', default='BTC', help='Ticker symbol (default: BTC)')
    ```

*   **Line 42:**
    ```python
    elif ticker == 'PAXG':
        return 0.01
    ```
    If you are using a new ticker, you will need to add a new `elif` statement with the appropriate tick size. For example, for `SOL`:
    ```python
    elif ticker == 'SOL':
        return 0.01
    ```

### 3. `market_maker.py`

You need to change the `MARKET_ID` and the filename of the parameters file.

*   **Line 77:**
    ```python
    MARKET_ID = 48  # PAXG market
    ```
    You need to change `48` to the `MARKET_ID` of the new cryptocurrency. You can find the `MARKET_ID` in the `markets_and_indexes.txt` file. For example, for `BTC`, the `MARKET_ID` is `1`.
    ```python
    MARKET_ID = 1  # BTC market
    ```

*   **Line 311:**
    ```python
    possible_paths = ['params/avellaneda_parameters_PAXG.json', 'avellaneda_parameters_PAXG.json', 'TRADER/avellaneda_parameters_PAXG.json']
    ```
    **Change to (for BTC):**
    ```python
    possible_paths = ['params/avellaneda_parameters_BTC.json', 'avellaneda_parameters_BTC.json', 'TRADER/avellaneda_parameters_BTC.json']
    ```

*   **Line 326:**
    ```python
    logger.warning("⚠️ avellaneda_parameters_PAXG.json not found in any expected location")
    ```
    **Change to (for BTC):**
    ```python
    logger.warning("⚠️ avellaneda_parameters_BTC.json not found in any expected location")
    ```

### 4. `gather_lighter_data.py`

You need to add or remove the desired ticker from the `CRYPTO_TICKERS` list.

*   **Line 71:**
    ```python
    CRYPTO_TICKERS = ['ETH', 'BTC', 'PAXG', 'ASTER']  # Symbols to track
    ```
    If you want to trade a different cryptocurrency, you should add it to this list. For example, to add `SOL`:
    ```python
    CRYPTO_TICKERS = ['ETH', 'BTC', 'PAXG', 'ASTER', 'SOL']  # Symbols to track
    ```
