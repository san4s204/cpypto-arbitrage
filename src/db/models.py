from sqlalchemy.orm import Mapped, mapped_column, declarative_base
from sqlalchemy import BigInteger, Float, String, UniqueConstraint

Base = declarative_base()

class OhlcvRaw(Base):
    __tablename__ = "ohlcv_raw"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", "ts", name="uix_exchange_symbol_ts"),
    )
    id:            Mapped[int]       = mapped_column(primary_key=True)
    exchange:      Mapped[str]       = mapped_column(String(8))
    symbol:        Mapped[str]       = mapped_column(String(20))
    ts:            Mapped[int]       = mapped_column(BigInteger)  # мс UTC
    open:          Mapped[float]     = mapped_column(Float)
    high:          Mapped[float]     = mapped_column(Float)
    low:           Mapped[float]     = mapped_column(Float)
    close:         Mapped[float]     = mapped_column(Float)
    volume:        Mapped[float]     = mapped_column(Float)


class OhlcvClean(Base):
    __tablename__  = "ohlcv_clean"
    __table_args__ = (UniqueConstraint("symbol", "ts"),)   # одна строка на пару/время

    id:         Mapped[int]   = mapped_column(primary_key=True)
    symbol:     Mapped[str]   = mapped_column(String(20))
    ts:         Mapped[int]   = mapped_column(BigInteger)      # мс UTC начала свечи

    # какая биржа была дешёвой / дорогой
    buy_ex:     Mapped[str]   = mapped_column(String(10))      # bybit / okx / binance / mexc
    sell_ex:    Mapped[str]   = mapped_column(String(10))

    # mid-цены на этих биржах
    buy_mid:    Mapped[float] = mapped_column(Float)           # USD
    sell_mid:   Mapped[float] = mapped_column(Float)

    # валовой и чистый спред
    spread:     Mapped[float] = mapped_column(Float)           # sell_mid − buy_mid
    net_spread: Mapped[float] = mapped_column(Float)           # spread − комиссия