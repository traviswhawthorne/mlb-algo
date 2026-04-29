"""
Double-click this file to print all bookmaker odds for today's first game.
Match the odds to your sportsbook to find its API key.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from odds_fetcher import identify_bookmakers

API_KEY = "23d6c9e831180dd8136d8116fa629d90"

identify_bookmakers(API_KEY)

input("\nPress Enter to close...")
