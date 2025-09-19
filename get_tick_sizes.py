import lighter
import asyncio
import argparse
from typing import Tuple, Optional

async def get_market_details(symbol: str) -> Optional[Tuple[int, float, float]]:
    """
    Retrieves the market index, price tick size, and amount tick size for a given symbol.

    Args:
        symbol: The symbol to look up (e.g., "PAXG").

    Returns:
        A tuple containing (market_index, price_tick_size, amount_tick_size),
        or None if the symbol is not found.
    """
    async with lighter.ApiClient() as api_client:
        order_api = lighter.OrderApi(api_client)

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
            print(f"An error occurred: {e}")
            return None

async def main():
    """Main function to run the script from the command line."""
    parser = argparse.ArgumentParser(description="Get market tick sizes for a given symbol from Lighter API.")
    parser.add_argument("--symbol", type=str, default="PAXG", help="The symbol to look up (e.g., PAXG).")
    args = parser.parse_args()

    details = await get_market_details(args.symbol)

    if details:
        market_id, price_tick_size, amount_tick_size = details
        print(f"Symbol: {args.symbol}")
        print(f"Market Index: {market_id}")
        print(f"Price Tick Size: {price_tick_size}")
        print(f"Amount Tick Size: {amount_tick_size}")
    else:
        print(f"Market for symbol '{args.symbol}' not found.")

if __name__ == "__main__":
    asyncio.run(main())
