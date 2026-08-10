import wordninja_enhanced as wordninja
import tldextract
import os
from bs4 import BeautifulSoup, Comment
from nltk.stem import PorterStemmer

# WORD_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'improved_crypto_words.txt.gz')
keyword_in_url = { # Table 11, Appendix D: "URL-filter" = Invest Words + Coin Words (paper 3696410.3714588)
    "crypto", "fx", "earn", "deposit", "trade", "capital", "invest", "global",
    "bit", "mining", "ltd", "finance", "miner", "trust", "profit", "asset",
    "cardano", "funding", "capitals", "fund", "limited", "chain", "digital",
    "btc", "assets", "wealth", "coin", "option", "prime", "bitcoin", "exchange",
    "money", "eth", "ethereum", "cryptocurrency", "ripple", "binance",
    "shiba inu", "dogecoin", "solana", "tether", "tron", "polkadot", "xrp",
    "ada", "bnb", "shib", "doge", "sol", "usdt", "trx", "dot", "algo",
    "litecoin", "chainlink", "uniswap", "pancakeswap", "avalanche", "neo",
    "iota", "aave", "luna", "synthetix", "theta", "grt", "1inch", "sushi",
    "matic", "btcusd", "usdbtc", "ethusd", "usdeth", "adausd", "usdada",
    "xrpusd", "usdxrp", "bnbusd", "usdbnb", "shibusd", "usdshib", "dogeusd",
    "usddoge", "solusd", "usdsol", "usdtusd", "usdusdt",
}

domain_whitelist = { # Update as needed!
    
}

# Paper §2.1.2: "we apply stemming prior to comparing word lists" -- e.g.
# "investors"/"investing" both reduce to "invest". Stem the keyword set once
# at import time so match_domain_name_with_keywords() doesn't redo it per call.
stemmer = PorterStemmer()
stemmed_keyword_in_url = {stemmer.stem(keyword) for keyword in keyword_in_url}

lm_ninja = None
if(lm_ninja is None):
    lm_ninja = wordninja.LanguageModel(
        language="en",
        add_words=["crypto", "fx", "ltd", "btc", "eth", "bitcoin", "ethereum", "cryptocurrency"],
    )

def match_domain_name_with_keywords(domain_name):
    for domain_kw in domain_whitelist:
        if(domain_name.endswith(domain_kw)):
            return False
    extracted = tldextract.extract(domain_name)
    domain_without_tld = extracted.domain
    if extracted.subdomain:
        domain_without_tld = extracted.subdomain + '.' + domain_without_tld
    domain_name_splits = {stemmer.stem(token) for token in lm_ninja.split(domain_without_tld)}
    for url_keyword in stemmed_keyword_in_url:
        if url_keyword in domain_name_splits: return True
    return False
