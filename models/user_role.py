import bcrypt
from sqlalchemy import Table, Column, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import (
    TIMESTAMP,
    JSONB,
    BOOLEAN,
    INTEGER,
    TEXT,
    BYTEA,
    UUID,
)

from database import Base, utc_time_now

user_role = Table(
    "user_role",
    Base.metadata,
    Column("user_id", TEXT, ForeignKey("user_role.user.id")),
    Column("role_id", INTEGER, ForeignKey("user_role.role.id")),
    schema="user_role",
)


class User(Base):
    __tablename__ = "user"
    __table_args__ = {"schema": "user_role"}

    id = Column(TEXT, primary_key=True, unique=True)
    name = Column(TEXT, nullable=False, unique=True)
    email = Column(TEXT, nullable=False, unique=True)
    profile_creation_date = Column(
        TIMESTAMP(timezone=True), nullable=False, default=utc_time_now
    )
    active = Column(BOOLEAN, nullable=False, default=True)
    google_credentials = Column(JSONB)
    roles = relationship("Role", secondary=user_role, backref="users")
    last_token_refresh = Column(TIMESTAMP(timezone=True), default=utc_time_now)
    client_id = Column(UUID(as_uuid=True), ForeignKey("general.client.id"))
    client = relationship("Client", foreign_keys=[client_id])

    hashed_password = Column(BYTEA)
    hashed_pin = Column(BYTEA)

    def set_password(self, new_password: str):
        if not new_password.strip():
            raise Exception("Cannot set password to nothing")

        salt = bcrypt.gensalt(rounds=15)
        self.hashed_password = bcrypt.hashpw(new_password.encode(), salt)

    def check_password(self, password: str) -> bool:
        if self.hashed_password is None:
            raise Exception("User has not set password")

        return bcrypt.checkpw(password.encode(), self.hashed_password)

    def set_pin(self, new_pin: str):
        if not new_pin.strip():
            raise Exception("Cannot set password to nothing")

        if len(new_pin) != 4:
            raise Exception("Pin must be 4 digits")

        if not new_pin.isdecimal():
            raise Exception("Pin must only be numbers")

        salt = bcrypt.gensalt(rounds=15)
        self.hashed_pin = bcrypt.hashpw(new_pin.encode(), salt)

    def check_pin(self, pin: str) -> bool:
        if self.hashed_pin is None:
            raise Exception("User has not set pin")

        return bcrypt.checkpw(pin.encode(), self.hashed_pin)

    def get_id(self):
        return self.id

    @property
    def roles_list(self):
        roles_list = []
        for item in self.roles:
            roles_list.append(item.name)
        return roles_list

    @property
    def is_active(self):
        return self.active

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False


class Role(Base):
    __tablename__ = "role"
    __table_args__ = {"schema": "user_role"}

    id = Column(INTEGER, unique=True, primary_key=True, autoincrement=True)
    name = Column(TEXT, unique=True)
    info = Column(TEXT)
