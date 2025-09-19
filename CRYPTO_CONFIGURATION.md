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

You need to update the ticker symbol in the `avellaneda-calculator` service to match the one you set in your `.env` file.

*   **`command`:** Change `PAXG` to your new ticker (e.g., `BTC`).
    ```yaml
    # In the avellaneda-calculator service
    command: >
      sh -c "
        while true; do
          echo '📊 Calculating Avellaneda parameters...';
          python calculate_avellaneda_parameters.py BTC --hours 4;
          echo '⏰ Waiting 2 hours before next calculation...';
          sleep 7200;
        done
      "
    ```

*   **`healthcheck`:** Update the filename in the healthcheck to match your new ticker.
    ```yaml
    # In the avellaneda-calculator service
    healthcheck:
      test: ["CMD", "test", "-f", "/app/params/avellaneda_parameters_BTC.json"]
    ```

That's it! You no longer need to manually edit `market_maker.py` or `calculate_avellaneda_parameters.py`. The bot will automatically:
- Fetch the correct Market ID, tick sizes, and other details from the Lighter API.
- Load the correct `avellaneda_parameters_<TICKER>.json` file based on the `MARKET_SYMBOL`.

