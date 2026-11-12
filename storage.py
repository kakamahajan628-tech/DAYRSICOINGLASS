"""Small portable durable store. PostgreSQL for deployment; SQLite for tests."""
import json
import time
from sqlalchemy import create_engine, MetaData, Table, Column, String, JSON, Float, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


class Store:
    def __init__(self, url):
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://"+url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://"+url[len("postgresql://"):]
        options = {"pool_pre_ping": True, "hide_parameters": True,
                   "json_serializer": lambda data: json.dumps(data, allow_nan=False)}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            options["pool_size"] = 3
            options["max_overflow"] = 2
            options["connect_args"] = {"sslmode": "require", "connect_timeout": 15}
        self.engine = create_engine(url, **options)
        metadata = MetaData()
        self.rows = Table("cg_watch_records_v1", metadata,
            Column("kind", String(40), primary_key=True), Column("key", String(240), primary_key=True),
            Column("payload", JSON, nullable=False), Column("updated", Float, nullable=False),
            Column("lease", Float, nullable=False, default=0))
        metadata.create_all(self.engine)

    def get(self, kind, key, default=None):
        with self.engine.connect() as connection:
            row = connection.execute(select(self.rows.c.payload).where(self.rows.c.kind==kind, self.rows.c.key==str(key))).first()
            return row[0] if row else default

    def put(self, kind, key, payload):
        constructor = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
        statement = constructor(self.rows).values(kind=kind, key=str(key), payload=payload, updated=time.time(), lease=0)
        statement = statement.on_conflict_do_update(index_elements=["kind", "key"], set_={"payload": payload, "updated": time.time()})
        with self.engine.begin() as connection:
            connection.execute(statement)

    def list(self, kind, after=0):
        with self.engine.connect() as connection:
            return [(r[0], r[1]) for r in connection.execute(select(self.rows.c.key, self.rows.c.payload).where(self.rows.c.kind==kind,self.rows.c.updated>=after))]

    def lease(self, name, seconds=1200):
        constructor = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
        now = time.time()
        with self.engine.begin() as connection:
            connection.execute(constructor(self.rows).values(kind="lease", key=name, payload={}, updated=now, lease=0)
                .on_conflict_do_nothing(index_elements=["kind", "key"]))
            result = connection.execute(update(self.rows).where(self.rows.c.kind=="lease", self.rows.c.key==name,
                self.rows.c.lease<now).values(lease=now+seconds, updated=now))
            return result.rowcount == 1

    def release(self, name):
        with self.engine.begin() as connection:
            connection.execute(update(self.rows).where(self.rows.c.kind=="lease",self.rows.c.key==name).values(lease=0))

    def close(self):
        self.engine.dispose()
