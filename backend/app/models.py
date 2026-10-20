from sqlalchemy import Boolean, Date, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

class Draw(Base):
    __tablename__ = "draws"
    draw_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draw_date: Mapped[object] = mapped_column(Date, index=True)
    n1: Mapped[int] = mapped_column(Integer)
    n2: Mapped[int] = mapped_column(Integer)
    n3: Mapped[int] = mapped_column(Integer)
    n4: Mapped[int] = mapped_column(Integer)
    n5: Mapped[int] = mapped_column(Integer)
    n6: Mapped[int] = mapped_column(Integer)
    special: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
