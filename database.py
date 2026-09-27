import logging
import os
import subprocess
import datetime
import pytz

import sqlalchemy as sa
from sqlalchemy import create_engine, MetaData
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
from sqlalchemy.dialects.postgresql import TIMESTAMP

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

DB_USERNAME = os.environ.get("POSTGRESQL_USERNAME", None)
DB_PASSWORD = os.environ.get("POSTGRESQL_PASSWORD", None)
DB_NAME = os.environ.get("POSTGRESQL_DB_NAME", None)

LOCAL_TIMEZONE = pytz.timezone("US/Central")

# Database connection settings (use env vars where available)
DB_HOST = os.environ.get("POSTGRESQL_HOST", "localhost")
DB_PORT = os.environ.get("POSTGRESQL_PORT", "5432")
DB_SSLMODE = os.environ.get("POSTGRESQL_SSLMODE", None)

# Production tuning
# Recycle pooled connections periodically to avoid stale SSL sockets.
DB_POOL_RECYCLE = os.environ.get("POSTGRESQL_POOL_RECYCLE", "1800")
DB_USE_NULLPOOL = os.environ.get("POSTGRESQL_USE_NULLPOOL", "false").lower() in ("1", "true", "yes")
# TCP keepalive settings passed to psycopg2 (optional)
DB_KEEPALIVES = {
    "keepalives": int(os.environ.get("POSTGRESQL_KEEPALIVES", 1)),
    "keepalives_idle": int(os.environ.get("POSTGRESQL_KEEPALIVES_IDLE", 30)),
    "keepalives_interval": int(os.environ.get("POSTGRESQL_KEEPALIVES_INTERVAL", 10)),
    "keepalives_count": int(os.environ.get("POSTGRESQL_KEEPALIVES_COUNT", 5)),
}

_db_url = f"postgresql://{DB_USERNAME}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Use pool_pre_ping to avoid errors from stale/closed connections and allow optional SSL/keepalives
connect_args = {}
if DB_SSLMODE:
    connect_args["sslmode"] = DB_SSLMODE

engine_kwargs = {
    "pool_pre_ping": True,
    "pool_size": int(os.environ.get("POSTGRESQL_POOL_SIZE", 5)),
    "max_overflow": int(os.environ.get("POSTGRESQL_MAX_OVERFLOW", 10)),
}

try:
    engine_kwargs["pool_recycle"] = int(DB_POOL_RECYCLE)
except Exception:
    # Fallback to 30 minutes if env var is invalid.
    engine_kwargs["pool_recycle"] = 1800

# include keepalives in connect_args
connect_args.update({k: v for k, v in DB_KEEPALIVES.items() if v is not None})
if connect_args:
    engine_kwargs["connect_args"] = connect_args

if DB_USE_NULLPOOL:
    from sqlalchemy.pool import NullPool

    engine_kwargs["poolclass"] = NullPool

engine = create_engine(_db_url, **engine_kwargs)

# Create MetaData
meta = MetaData()

# Retry ORM execute/commit on transient disconnects (e.g. stale SSL sockets).
from sqlalchemy.exc import OperationalError
import time


class RetrySession(sa.orm.Session):
    def execute(self, *args, **kwargs):
        return self._retry_db_operation(
            "execute",
            super().execute,
            "DB_EXECUTE_RETRIES",
            "DB_EXECUTE_BACKOFF",
            *args,
            **kwargs,
        )

    def commit(self, *args, **kwargs):
        return self._retry_db_operation(
            "commit",
            super().commit,
            "DB_COMMIT_RETRIES",
            "DB_COMMIT_BACKOFF",
            *args,
            **kwargs,
        )

    def _retry_db_operation(
        self, operation_name, operation, retries_env, backoff_env, *args, **kwargs
    ):
        retries = int(os.environ.get(retries_env, 2))
        backoff = float(os.environ.get(backoff_env, 0.5))
        for attempt in range(retries + 1):
            try:
                return operation(*args, **kwargs)
            except OperationalError:
                try:
                    super().rollback()
                except Exception:
                    pass
                if attempt < retries:
                    time.sleep(backoff * (2**attempt))
                    continue
                raise


# Create sessions
db_session = scoped_session(
    sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
        class_=RetrySession,
    )
)

# Create a base class for declarative models
Base = declarative_base(metadata=meta)


@sa.event.listens_for(sa.orm.Mapper, "init")
def receive_init(target, args, kwargs):
    check_if_all_aware(list(kwargs.values()))


@sa.event.listens_for(Base, "attribute_instrument")
def receive_attribute_instrument(cls, key, inst):
    # check of property of model is a column and not a foreignkey
    if not hasattr(inst.property, "columns"):
        return

    # Check if property is type TIMESTAMP
    if isinstance(inst.property.columns[0].type, TIMESTAMP):
        if str(inst.property).endswith(".commit_timestamp"):

            @sa.event.listens_for(inst, "set")
            def no_change_commit_ts(target, value, oldvalue, initiator):
                raise Exception("Changing commit_timestamp is not allowed")

        else:

            @sa.event.listens_for(inst, "set")
            def receive_set(target, value, oldvalue, initiator):
                check_if_all_aware([value])


def init_db():
    pass

    # get all the tables defined in your models
    tables = Base.metadata.tables.values()

    # group the tables by schema
    schemas = {}
    for table in tables:
        schema_name = table.schema
        if schema_name not in schemas:
            schemas[schema_name] = []
        schemas[schema_name].append(table)

    if "alembic" not in schemas:
        schemas["alembic"] = []

    # create the schemas
    with engine.connect() as conn:
        for schema_name, tables in schemas.items():
            if not conn.dialect.has_schema(conn, schema_name):
                conn.execute(sa.schema.CreateSchema(schema_name))

        conn.commit()

    Base.metadata.create_all(bind=engine)

    create_roles()


def create_roles():
    from models.user_role import Role

    roles = [
        "admin",
        "document_control",
        "inventory",
        "inventory_aliquot",
        "inventory_general",
        "inventory_pcr",
        "item_request",
        "item_request_approve_over200",
        "item_request_approve_under200",
        "item_request_purchase",
        "results_report",
        "sample_control",
        "time_sheet",
    ]

    for role in roles:
        if db_session.query(Role).filter_by(name=role).first() is None:
            db_session.add(Role(name=role))
    db_session.commit()


def assign_admin():
    from models.user_role import User, Role

    selected_user = None

    while selected_user is None:
        for user in db_session.query(User).all():
            print(user.id, user.name)

        selection = input("ID to assign admin:")

        selected_user = db_session.get(User, selection)

    selected_user.roles.append(db_session.query(Role).filter_by(name="admin").first())
    db_session.commit()


def to_local(dt, format=None):
    if dt is None or not dt:
        return dt

    # if datetime is native the give UTC
    if dt.tzinfo is None or (
        (dt.tzinfo is not None) and (dt.tzinfo.utcoffset(dt) is None)
    ):
        dt = pytz.timezone("UTC").localize(dt)

    dt = dt.astimezone(LOCAL_TIMEZONE)

    if format == None:
        return dt
    else:
        return dt.strftime(format)


def to_utc(dt, *args):
    if len(args) > 1:
        raise TypeError(f"to_local expected 1 or 2 arguments, got {len(args)}")

    if dt is None or not dt:
        return dt

    if isinstance(dt, str):
        if len(args) != 1:
            raise TypeError(f"to_local expected 2 arguments, got {len(args)}")

        dt = LOCAL_TIMEZONE.localize(datetime.datetime.strptime(dt, args[0]))

    if isinstance(dt, datetime.datetime) and (
        (dt.tzinfo is None)
        or ((dt.tzinfo is not None) and (dt.tzinfo.utcoffset(dt) is None))
    ):
        dt = LOCAL_TIMEZONE.localize(dt)

    return dt.astimezone(datetime.UTC)


utc_types = (
    datetime.timezone(datetime.timedelta(0)),
    datetime.timezone.utc,
    pytz.utc,
)


def check_if_all_aware(list: list):
    for i in list:
        if isinstance(i, datetime.datetime):
            if (i.tzinfo is None) or (
                (i.tzinfo is not None) and (i.tzinfo.utcoffset(i) is None)
            ):
                raise AttributeError(
                    "Commit datetime is native, datetime should be aware"
                )

            if i.tzinfo not in utc_types:
                raise AttributeError("Commit datetime must be UTC")


def utc_time_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


"0 0 * * *"


def backup():
    """
    Backup the PostgreSQL database. Supports both Linux (with encryption) and Windows (unencrypted).
    
    Linux: Creates encrypted backup with bzip2 compression and uploads to Google Drive on Sundays
    Windows/localhost: Creates simple SQL dump backup
    
    # Generate a public private key pair: OPENSSL
    'req -x509 -nodes -days 1000000 -newkey rsa:4096 -keyout {BACKUP_KEY_NAME}.pem -out {BACKUP_KEY_NAME}.pem.pub'

    # Backup and Encrypt with public key: COMMAND LINE
    'pg_dumpall | bzip2 | openssl smime -encrypt -aes256 -binary -outform DEM -out {DATABASE}.sql.bz2.ssl {BACKUP KEY NAME}.pem.pub'

    # Decrypt to a file using private key: OPENSSL
    'smime -decrypt -in {DATABASE}.sql.bz2.ssl -binary -inform DEM -inkey {BACKUP_KEY_NAME}.pem -out {DATABASE}.sql.bz2'

    # Restore Database: COMMAND LINE
    #Linux
    'psql -f "dir/to/file/{DATABASE}.sql" postgres'
    #Windows
    './psql.exe -U postgres -f "dir/to/file/{DATABASE}.sql" postgres'
    """

    from pytz import timezone
    import platform

    try:
        DB_USERNAME = os.environ.get('POSTGRESQL_USERNAME', None)
        DB_PASSWORD = os.environ.get('POSTGRESQL_PASSWORD', None)
        DB_NAME = os.environ.get('POSTGRESQL_DB_NAME', None)
        DB_HOST = os.environ.get('POSTGRESQL_HOST', 'localhost')
        DB_PORT = os.environ.get('POSTGRESQL_PORT', '5432')

        if not all([DB_USERNAME, DB_PASSWORD, DB_NAME]):
            logger.error("Database backup failed: Missing database credentials in environment variables")
            return False

        days_of_week = (
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        )

        now = datetime.datetime.now(timezone("America/Chicago"))
        day_of_week = now.weekday()
        timestamp = now.strftime("%Y%m%d%H%M%S")
        
        # Determine backup directory based on OS
        is_windows = platform.system() == "Windows"
        if is_windows:
            # Windows localhost backup to temp or project directory
            backup_dir = os.path.join(os.path.dirname(__file__), "backups")
            if not os.path.exists(backup_dir):
                os.makedirs(backup_dir)
            backup_filename = f"pg_xyzlapp_backup_{days_of_week[day_of_week]}_{timestamp}.sql"
            backup_path = os.path.join(backup_dir, backup_filename)
            
            # Create backup command for Windows
            cmd = f'pg_dump.exe -h {DB_HOST} -p {DB_PORT} -U {DB_USERNAME} -d {DB_NAME} -f "{backup_path}"'
            env = os.environ.copy()
            env['PGPASSWORD'] = DB_PASSWORD
            
        else:
            # Linux production backup with encryption
            environment_variables = {
                "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games:/snap/bin"
            }
            rsa_public_key_dir = "/mnt/disks/xyzdisk2/keys/pg_xyzlapp_backup_key.pem.pub"
            backup_dir = "/mnt/disks/xyzdisk2/DB_backup"
            
            if not os.path.exists(backup_dir):
                logger.error(f"Backup directory does not exist: {backup_dir}")
                return False
                
            backup_filename = f"pg_xyzlapp_backup_{days_of_week[day_of_week]}.sql.bz2.ssl"
            backup_path = os.path.join(backup_dir, backup_filename)
            
            # Create backup command for Linux with encryption
            cmd = f"pg_dumpall | bzip2 | openssl smime -encrypt -aes256 -binary -outform DEM -out {backup_path} {rsa_public_key_dir}"
            env = environment_variables

        # Execute backup command
        logger.info(f"Starting database backup to {backup_path}")
        result = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"Database backup failed with return code {result.returncode}")
            logger.error(f"stderr: {result.stderr}")
            return False
            
        logger.info(f"Database backup completed successfully: {backup_path}")
        
        # Upload to Google Drive on Sundays (Linux only)
        if day_of_week == 6 and not is_windows:
            try:
                from apps.Google_API import upload_gdrive

                upload_gdrive(
                    backup_path,
                    f'{timestamp}-pg_xyzlapp_backup.sql.bz2.ssl',
                    "0APwUhpl11ndCUk9PVA",
                    "1Bg1TY0AGaYCAkbKmmav_kdEWm34XkJwj",
                    spoof=True,
                )
                logger.info("Backup uploaded to Google Drive")
            except Exception as e:
                logger.error(f"Failed to upload backup to Google Drive: {str(e)}")
                return False
        
        return True
        
    except Exception as e:
        logger.error(f"Unexpected error during database backup: {str(e)}")
        return False
