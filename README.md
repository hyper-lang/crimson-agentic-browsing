# Crimson with Agentic Browsing

This repo takes project [Crimson](https://github.com/pragseclab/Crimson) from the paper The Poorest Man in Babylon: A Longitudinal Study of Cryptocurrency Investment Scams" and substitutes it's crawling script with agentic extraction in an attempt to increase wallet extraction rates.

The main contribution of this repo is utilizing the [browser-use](https://github.com/browser-use/browser-use) library . While the same pipeline as depicted in the paper is utilized, the following operational changes have been made:
- Dockerize python scripts, use of RabbitMQ and Certstream server Docker containers
- Replace the [elixir certstream server](https://github.com/CaliDog/certstream-server) with [certstream_server_rust](https://github.com/reloading01/certstream-server-rust)
- Replaced [Wordninja](https://github.com/keredson/wordninja) with [Wordninja Enhanced](https://github.com/timminator/wordninja-enhanced)
- Saves HTML scraped to disk
- Moved hardcoded IP addresses to a .env file
- Added URL and Content Filter Keywords listed in the paper into the scripts

While these changes were designed and architected by Zane Wong, free access to Claude's Sonnet 5 was used to expedite the development process.

## How to Use

In order to use this repository, first clone the repo:
```bash
git clone https://github.com/hyper-lang/crimson-agentic-browsing
```

Then, build the containers that require a python env:
```bash
docker compose build
```

Then, bring the containers up:
```bash
docker compose up -d
```

Data collected will be stored in the `data` folder, and findings will be in the `results` folder

Future Improvements:
- Altering browsing prompts and models to improve extraction rate
- Use [LOKI](https://arxiv.org/abs/2509.12181) to initially enumerate scam domains
- Use [LightGBM](https://github.com/lightgbm-org/LightGBM) on pretrained data as the model that determines if a given site is a scam
- Scale workers on demand?
