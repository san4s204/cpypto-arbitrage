from sqlalchemy.orm import Mapped, mapped_column, declarative_base
from sqlalchemy import BigInteger, Float, String

Base = declarative_base()

class OhlcvRaw(Base):
    __tablename__ = "ohlcv_raw"
    id:            Mapped[int]       = mapped_column(primary_key=True)
    exchange:      Mapped[str]       = mapped_column(String(8))
    symbol:        Mapped[str]       = mapped_column(String(20))
    ts:            Mapped[int]       = mapped_column(BigInteger)  # мс UTC
    open:          Mapped[float]     = mapped_column(Float)
    high:          Mapped[float]     = mapped_column(Float)
    low:           Mapped[float]     = mapped_column(Float)
    close:         Mapped[float]     = mapped_column(Float)
    volume:        Mapped[float]     = mapped_column(Float)
