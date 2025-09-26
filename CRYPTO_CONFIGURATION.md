# How to Change the Cryptocurrency

This document explains how to change the cryptocurrency that the market maker trades. The project has been updated to make this process much simpler. Most hardcoded values are gone, and the bot now relies on an environment variable and fetches market details automatically.

The project is currently configured to trade `PAXG` by default.

To change the cryptocurrency, you primarily need to edit your `.env` and `docker-compose.yml` files.

### 1. `gather_lighter_data.py` (If needed)

The data collector service needs to know which markets to track. Ensure the cryptocurrency you want to trade is included in the `CRYPTO_TICKERS` list in this file.

*   **File:** `gather_lighter_data.py`
    ```python
    CRYPTO_TICKERS = ['ETH', 'BTC', 'PAXG', 'ASTER']  # Symbols to track
    ```
    If you want to trade a different cryptocurrency not on this list (e.g., `SOL`), you should add it.
    ```python
    CRYPTO_TICKERS = ['ETH', 'BTC', 'PAXG', 'ASTER', 'SOL']
    ```

### 2. `.env` File

This is the most important step. You must set the `MARKET_SYMBOL` environment variable to the ticker symbol of the cryptocurrency you want to trade. The `market_maker.py` script reads this variable to determine which market to trade on.

*   **File:** `.env`
    ```env
    MARKET_SYMBOL=BTC
    ```

### 3. `docker-compose.yml`

You need to update the ticker symbol in the `avellaneda-calculator` and `supertrend-calculator` services to match the one you set in your `.env` file.

*   **`avellaneda-calculator` service:**
    *   **`command`:** The command already uses the `MARKET_SYMBOL` environment variable, so you don't need to change it. It defaults to `PAXG` if the variable is not set.
        ```yaml
        # In the avellaneda-calculator service
        command: >
          sh -c "
            while true; do
              echo '📊 Calculating Avellaneda parameters...';
              python calculate_avellaneda_parameters.py ${MARKET_SYMBOL:-PAXG} --minutes 10;
              echo '⏰ Waiting 10 minutes before next calculation...';
              sleep 600;
            done
          "
        ```

*   **`supertrend-calculator` service:**
    *   **`command`:** The command already uses the `MARKET_SYMBOL` environment variable, so you don't need to change it. It defaults to `PAXG` if the variable is not set.
        ```yaml
        # In the supertrend-calculator service
        command: >
          sh -c "
            while true; do
              echo '📈 Finding trend...';
              python find_trend_lighter.py --symbol ${MARKET_SYMBOL:-PAXG} --interval 1m;
              echo '⏰ Waiting 2 minutes before next calculation...';
              sleep 120;
            done
          "
        ```

That's it! You no longer need to manually edit `market_maker.py`, `calculate_avellaneda_parameters.py`, or `find_trend_lighter.py`. The bot will automatically:
- Fetch the correct Market ID, tick sizes, and other details from the Lighter API.
- Load the correct `avellaneda_parameters_<TICKER>.json` and `supertrend_params_<TICKER>.json` files based on the `MARKET_SYMBOL`.

