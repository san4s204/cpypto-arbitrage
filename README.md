# Crypto Arbitrage Trading Bot

## TL;DR
A fully autonomous crypto arbitrage trading bot that monitors price differences across multiple exchanges (CEX/DEX), identifies profitable opportunities (≥0.3% after fees), and executes trades automatically. Built with Python 3.12, asyncio, Redis, PostgreSQL, and Docker. Includes Telegram integration for trade confirmations and a monitoring dashboard.

## Table of Contents
- [Overview](#overview)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [API Key Setup](#api-key-setup)
- [Usage](#usage)
- [Deployment](#deployment)
- [Risk Management](#risk-management)
- [Monitoring](#monitoring)
- [FAQ](#faq)
- [Scaling Recommendations](#scaling-recommendations)

## Overview

This crypto arbitrage trading bot is designed to automatically identify and execute profitable arbitrage opportunities across multiple cryptocurrency exchanges. The system continuously monitors price differences between exchanges and executes trades when the potential profit exceeds a configurable threshold (default 0.3%) after accounting for all fees and potential slippage.

### Key Features

- **Multi-Exchange Support**: Monitors OKX, Bybit, HTX, and optionally Uniswap v3 and 1inch
- **Real-time Market Data**: Connects to exchange WebSockets for minimal latency
- **Graph-Based Arbitrage Detection**: Uses Dijkstra's algorithm to find profitable paths
- **Risk Management**: Implements volatility filters, liquidity checks, and capital limits
- **Telegram Integration**: Provides trade confirmations and real-time notifications
- **Cross-Exchange Transfers**: Automatically manages fund transfers between exchanges
- **Monitoring Dashboard**: Visualizes PnL, active trades, and system metrics
- **Containerized Architecture**: Runs in Docker for easy deployment and scaling

## Architecture

The system is built using a microservices architecture with the following components:

1. **MarketData**: Subscribes to WebSocket feeds from exchanges and normalizes data
2. **ArbEngine**: Identifies arbitrage opportunities using graph-based algorithms
3. **Execution**: Handles order routing, status monitoring, and retry logic
4. **FundsRouter**: Manages cross-exchange transfers and fee calculations
5. **TGManager**: Provides Telegram bot integration for notifications and confirmations
6. **Dashboard**: Offers a web interface for monitoring and configuration

### Data Flow

1. MarketData service collects real-time price data from exchanges via WebSockets
2. Data is normalized and stored in Redis cache with 100ms update frequency
3. ArbEngine constructs a price graph and runs Dijkstra's algorithm to find arbitrage paths
4. When an opportunity is found, it's validated against risk parameters
5. For opportunities requiring additional funds, TGManager requests confirmation via Telegram
6. Execution service places orders on respective exchanges
7. FundsRouter handles any necessary cross-exchange transfers
8. Results are stored in PostgreSQL and displayed on the dashboard

## Installation

### Prerequisites

- Docker and Docker Compose
- Git
- 2GB+ RAM, 2+ CPU cores
- Internet connection with low latency to major exchanges

### Clone Repository

```bash
git clone https://github.com/yourusername/crypto-arbitrage-bot.git
cd crypto-arbitrage-bot
```

### Environment Setup

Create a `.env` file in the project root with your configuration:

```bash
cp .env.sample .env
# Edit .env with your API keys and configuration
```

### Build and Start Services

```bash
docker-compose up -d
```

This will start all services in detached mode. To view logs:

```bash
docker-compose logs -f
```

## Configuration

The system is configured through environment variables in the `.env` file:

### General Settings

```
# Trading Parameters
MIN_PROFIT_MARGIN=0.003
MAX_CAPITAL_PER_TRADE=0.1
MIN_24H_VOLUME=400000
MAX_BID_ASK_SPREAD=0.004
VOLATILITY_THRESHOLD=0.03
VOLATILITY_WINDOW=300

# Database Configuration
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=crypto_arb_bot
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_secure_password

# Redis Configuration
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=your_secure_password

# Telegram Configuration
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_ADMIN_CHAT_ID=your_telegram_chat_id
```

### Exchange API Keys

```
# OKX
OKX_API_KEY=your_okx_api_key
OKX_API_SECRET=your_okx_api_secret
OKX_PASSWORD=your_okx_password
OKX_TAKER_FEE=0.001
OKX_MAKER_FEE=0.0008

# Bybit
BYBIT_API_KEY=your_bybit_api_key
BYBIT_API_SECRET=your_bybit_api_secret
BYBIT_TAKER_FEE=0.001
BYBIT_MAKER_FEE=0.0008

# HTX
HTX_API_KEY=your_htx_api_key
HTX_API_SECRET=your_htx_api_secret
HTX_TAKER_FEE=0.001
HTX_MAKER_FEE=0.0008

# Optional DEX Configuration
UNISWAP_WALLET_ADDRESS=your_wallet_address
UNISWAP_PRIVATE_KEY=your_private_key
UNISWAP_RPC_URL=https://mainnet.infura.io/v3/your_project_id
UNISWAP_FEE=0.003

ONEINCH_WALLET_ADDRESS=your_wallet_address
ONEINCH_PRIVATE_KEY=your_private_key
ONEINCH_RPC_URL=https://mainnet.infura.io/v3/your_project_id
ONEINCH_FEE=0.003
```

## API Key Setup

### OKX

1. Log in to your OKX account
2. Navigate to Account > API Management
3. Click "Create API"
4. Set permissions to "Trade" only (not "Withdrawal")
5. Set IP restrictions to your server's IP address
6. Complete 2FA verification
7. Save the API Key, Secret Key, and Passphrase

### Bybit

1. Log in to your Bybit account
2. Go to Account & Security > API Management
3. Click "Create New Key"
4. Select "Contract" permissions
5. Enable IP restriction and add your server's IP
6. Complete 2FA verification
7. Save the API Key and Secret Key

### HTX

1. Log in to your HTX account
2. Go to Account Center > API Management
3. Click "Create API Key"
4. Select "Trade" permission only
5. Add IP restriction for your server
6. Complete verification
7. Save the API Key and Secret Key

### Security Best Practices

- **Never** share your API keys or secrets
- Always use IP restrictions
- Limit permissions to trading only when possible
- Use separate API keys for each environment (dev, prod)
- Rotate keys regularly
- Monitor for unauthorized access

## Usage

### Dashboard Access

Once the system is running, access the dashboard at:

```
http://your_server_ip:8000
```

### Telegram Bot Commands

- `/start` - Start the bot
- `/help` - Show help message
- `/status` - Check bot status
- `/balance` - Check exchange balances
- `/pnl` - Check daily PnL

### Trade Confirmation Flow

1. When an arbitrage opportunity requires additional funds, you'll receive a Telegram message
2. The message will show the pair, exchanges, and expected profit margin
3. Click ✅ to confirm or ❌ to reject
4. If confirmed, the system will re-verify the opportunity and execute if still profitable
5. You'll receive a notification with the results after execution

## Deployment

### Local Deployment

For local development and testing:

```bash
docker-compose up -d
```

### Production Deployment

For production deployment on a VPS:

1. Set up a Ubuntu 22.04 VPS with at least 2GB RAM
2. Install Docker and Docker Compose
3. Clone the repository
4. Create and configure the `.env` file
5. Start the services:

```bash
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### VPS Requirements

- Ubuntu 22.04 LTS
- 2GB+ RAM
- 2+ CPU cores
- 40GB+ SSD storage
- Low latency connection to major exchanges
- Firewall configured to allow necessary outbound connections

## Risk Management

The system implements several risk management features:

### Capital Limits

- Maximum 10% of total capital per arbitrage opportunity
- Configurable via `MAX_CAPITAL_PER_TRADE` environment variable

### Profit Margin

- Minimum 0.3% profit after all fees
- Configurable via `MIN_PROFIT_MARGIN` environment variable

### Liquidity Filters

- Ignores pairs with 24h volume < $400,000
- Ignores pairs with bid/ask spread > 0.4%
- Configurable via environment variables

### Volatility Protection

- Blocks entry if price deviation > 3% in 5 minutes
- Configurable via `VOLATILITY_THRESHOLD` and `VOLATILITY_WINDOW`

### Network Transfer Limits

- Only uses networks with transfer time < 60 seconds
- Configurable via `MAX_TRANSFER_TIME` environment variable

## Monitoring

### Daily Monitoring Checklist

- [ ] Check daily PnL via dashboard or Telegram `/pnl` command
- [ ] Verify all exchange connections are active
- [ ] Check system error logs for any warnings
- [ ] Monitor balance distribution across exchanges
- [ ] Verify Telegram notifications are working

### Key Metrics to Track

1. **PnL Metrics**
   - Daily/weekly/monthly profit
   - Profit per pair
   - Profit per exchange
   - ROI percentage

2. **Performance Metrics**
   - Latency (market data to execution)
   - Opportunity detection rate
   - Execution success rate
   - Order fill rate

3. **Error Metrics**
   - WebSocket disconnections
   - Failed orders
   - Failed transfers
   - API rate limit hits

## FAQ

### General Questions

**Q: How much capital is recommended to start?**  
A: While the system can work with any amount, we recommend at least $10,000 across exchanges to effectively capture opportunities after fees.

**Q: Which pairs are most profitable for arbitrage?**  
A: Typically, major pairs like BTC/USDT and ETH/USDT have the most liquidity but smaller spreads. Mid-cap altcoins often have larger spreads but higher execution risk.

**Q: How often should I expect to find arbitrage opportunities?**  
A: This varies greatly with market conditions. During volatile periods, you might find several opportunities per hour. In stable markets, it might be just a few per day.

### Technical Questions

**Q: Can I run this on a home computer?**  
A: Yes, but a VPS with low latency to exchanges will perform significantly better. Latency is critical for arbitrage.

**Q: How do I add support for a new exchange?**  
A: Add the exchange configuration to `common/config.py`, implement the connection in `market_data/main.py`, and ensure the exchange is supported by CCXT.

**Q: What happens if an order fails to execute?**  
A: The system will attempt to cancel any related orders and log the failure. No further action in the arbitrage chain will be taken.

**Q: How are funds transferred between exchanges?**  
A: The FundsRouter service handles transfers using the withdrawal/deposit APIs of each exchange, preferring fast networks like TRC-20 for USDT.

## Scaling Recommendations

### Horizontal Scaling

To scale the system to handle more pairs and exchanges:

1. **Shard by Exchange**: Run separate MarketData instances for each exchange
2. **Shard by Currency**: Divide the trading pairs among multiple ArbEngine instances
3. **Load Balance**: Use multiple Execution instances with a load balancer

### Performance Optimization

1. **Reduce Latency**: Place servers geographically close to exchange data centers
2. **Optimize Redis**: Use Redis Cluster for larger datasets
3. **Database Partitioning**: Partition PostgreSQL tables by date for faster queries
4. **Memory Optimization**: Tune JVM settings and use memory-efficient data structures

### Adding More Exchanges

When adding more exchanges beyond the initial set:

1. Ensure the exchange has reliable WebSocket feeds
2. Verify the exchange has sufficient liquidity
3. Test withdrawal/deposit times between the new exchange and existing ones
4. Start with a small capital allocation to test performance
5. Gradually increase capital as performance is verified

### Capital Efficiency

To improve capital efficiency:

1. Implement dynamic capital allocation based on opportunity frequency
2. Use a central "hot wallet" exchange with fast transfers to others
3. Maintain balanced reserves on exchanges with frequent opportunities
4. Consider using stablecoins with fastest transfer networks (e.g., USDT on TRC-20)
