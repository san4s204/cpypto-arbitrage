# Крипто‑арбитраж‑бот

> **Спотовый арбитраж на Bybit · OKX · Bitget · MEXC** — полный пайплайн от поиска возможностей до живых сигналов в Telegram.

---

## 🚀 TL;DR

```bash
# клон и сборка
make compose-up     # postgres + redis + app
make etl            # загрузка рынков → ликвидность → OHLCV → чистая таблица
make notebook       # запуск Jupyter c шаблоном EDA
```

*14‑минутная* полная выгрузка (80 дней · 30‑мин TF · 4 биржи · 20 пар) → `ohlcv_clean` готова к анализу.

---

## 🏗️ Архитектура

```
┌────────┐   markets   ┌────────────┐   enrich   ┌────────┐
│ex/REST │────────────▶│ all_pairs  │───────────▶│ pairs  │
│  API   │             │  _raw.xlsx │            │  _top  │
└────────┘             └────────────┘            └────────┘
                               │                     │
                               ▼  OHLCV (async)      │
                        ┌─────────────┐              │
                        │ ohlcv_raw   │◀─────────────┘
                        └─────────────┘
                               │ ETL  (min↔max 4ex)
                               ▼
                        ┌─────────────┐
                        │ ohlcv_clean │
                        └─────────────┘
                               │ Notebook / Backtest
                               ▼
                        ┌─────────────┐  signals  ┌────────────┐
                        │ ws_listener │──────────▶│ Telegram   │
                        └─────────────┘   Redis   └────────────┘
```

- **Слой данных:** Postgres (SQLAlchemy ORM).
- **Загрузка:** CCXT (async) с учётом rate‑limit каждой биржи.
- **Обработка:** Pandas ETL → `min buy / max sell` на свечу, учёт комиссий maker‑taker.
- **Реальное время:** ccxt.pro websockets → Redis pub/sub → Telegram‑бот.

---

## 📑 Пошаговый пайплайн

| #  | Скрипт / Модуль           | Статус | Заметки                                                        |
| -- | ------------------------- | ------ | -------------------------------------------------------------- |
| 0  | **docker‑compose**        | ✔      | Postgres 15 · Redis 7 + сервис `app`                           |
| 1  | `fetch_markets.py`        | ✔      | Пересечение спотовых пар USDT (4 биржи) → `all_pairs_raw.xlsx` |
| 2A | `decorate_pairs.py`       | ✔      | 24 ч объём & depth‑bid‑1 на биржу → `all_pairs_enriched.xlsx`  |
| 2B | `filter_pairs.py`         | ✔      | фильтр ликвидности по правилам → `pairs_top.xlsx`              |
| 2C | `bulk_ohlcv.py`           | ✔      | 80 дней · 30 м свечи → `ohlcv_raw` (async)                     |
| 3  | `processing/etl.py`       | ✔      | расчёт `net_spread`, сохранение `buy_ex/sell_ex`               |
| 4  | `notebooks/eda.ipynb`     | ✔      | μ bp, σ, hit %, P95, статистика по парам                       |
| 5  | `realtime/ws_listener.py` | ⏳      | живой спред, публикация в Redis (> порога)                     |
| 6  | `bot/telegram.py`         | ⏳      | отправка алертов в TG‑чат                                      |
| 7  | Движок исполнения         | 🚧     | ордера maker/market, лог PnL                                   |

---

## 📈 Текущие результаты (30 м TF, 80 д)

| Пара      | μ bp     | P95 bp  | hit % | Комментарий  |
| --------- | -------- | ------- | ----- | ------------ |
| MOVE/USDT | **28.8** | **118** | 88    | топ‑кандидат |
| SCR/USDT  | 18.3     | 40      | 94    |              |
| UMA/USDT  | 17.2     | 32      | 93    |              |
| RVN/USDT  | 15.8     | 30      | 90    | legacy top   |
| *…*       | …        | …       | …     |              |

*Порог 30 bp → ≈10 сигналов/день по 4 парам.*

---

## 🛣️ Roadmap

### ✅ Сделано

- Докеризированы Postgres + Redis, env‑config, Poetry/venv
- Полный оффлайн ETL по 4 биржам
- Шаблон EDA‑ноутбука и ранжирование пар

### 🔜 Следующий спринт

- …

### 🗓️ Будущее

- …

---

## ▶️ Быстрый старт

```bash
# 1) клон и переменные окружения
cp .env.example .env   # задайте DB URL, API‑ключи

# 2) поднимаем стек
make compose-up        # postgres + redis

# 3) начальная выгрузка (80 д, 30 м)
make etl-full          # обёртка fetch→enrich→bulk→etl

# 4) исследуем
make jupyter           # открывает notebooks/eda.ipynb
```

> **Примечание для пользователей из РФ:** API OKX / Bitget / MEXC могут быть заблокированы. Настройте `GLOBAL_PROXY` в `.env` или работайте через VPN / Cloudflare WARP.

---

## 🧰 Технологический стек

- **Python 3.12**, **ccxt / ccxt.pro**, **SQLAlchemy 2**, **Pandas**
- **PostgreSQL 15**, **Redis 7**
- **Docker + docker‑compose**, **Makefile**
- **Telegram Bot API** для сигналов

---

## 🤝 Контрибьютинг

Пулл‑реквесты и ишью приветствуются!\
См. `CONTRIBUTING.md` для стиля кода и git‑флоу.

---

## 📜 Лицензия

MIT © 2025 Команда «Крипто‑бот»

