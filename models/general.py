"""Minimal general models needed for User.client relationship."""

import uuid

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import BOOLEAN, TEXT, UUID

from database import Base


class Client(Base):
    __tablename__ = "client"
    __table_args__ = {"schema": "general"}

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(TEXT, unique=True, nullable=False)
    active = Column(BOOLEAN, nullable=False, default=True)
