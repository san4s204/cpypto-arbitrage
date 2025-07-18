"""
Универсальный «вход» к базе:
  • engine  — единый объект подключения
  • Session — фабрика sync-сессий (scoped_session можно добавить позже)
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


from src.config import DB_URL   # берётся из .env → docker-compose

# echo=True печатает SQL-запросы (полезно при отладке, отключите в проде)
engine = create_engine(DB_URL, echo=False, pool_pre_ping=True)

# expire_on_commit=False — объекты не инвалидируются после commit
Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
