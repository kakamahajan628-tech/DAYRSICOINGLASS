"""Small portable durable store. PostgreSQL for deployment; SQLite for tests."""
import json
import time
import uuid
from sqlalchemy import create_engine, MetaData, Table, Column, String, JSON, Float, select, update, Index, delete, func, text, cast
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
            options["pool_size"] = 2
            options["max_overflow"] = 1
            options["connect_args"] = {"sslmode": "require", "connect_timeout": 15,"prepare_threshold":None}
        self.engine = create_engine(url, **options)
        metadata = MetaData()
        self.rows = Table("cg_watch_records_v1", metadata,
            Column("kind", String(40), primary_key=True), Column("key", String(240), primary_key=True),
            Column("payload", JSON, nullable=False), Column("updated", Float, nullable=False),
            Column("lease", Float, nullable=False, default=0))
        Index("ix_cg_records_kind_updated", self.rows.c.kind, self.rows.c.updated)
        Index("ix_cg_records_coin", self.rows.c.kind, self.rows.c.payload["coin"].as_string())
        Index("ix_cg_records_prefix",self.rows.c.kind,self.rows.c.key,postgresql_ops={"key":"varchar_pattern_ops"})
        self.jobs = Table("cg_jobs_v2", metadata,
            Column("id", String(180), primary_key=True), Column("kind", String(30), nullable=False),
            Column("payload", JSON, nullable=False), Column("state", String(20), nullable=False),
            Column("owner", String(40), nullable=False), Column("lease", Float, nullable=False),
            Column("updated", Float, nullable=False), Column("result", String(160)))
        Index("ix_cg_jobs_claim",self.jobs.c.kind,self.jobs.c.state,self.jobs.c.lease)
        self.owners = {}
        if self.engine.dialect.name=="postgresql":
            # Serialize additive schema setup across the three startup processes.
            with self.engine.begin() as connection:
                connection.execute(text("SELECT pg_advisory_xact_lock(741902361)"))
                metadata.create_all(connection)
                for index in self.rows.indexes: index.create(connection,checkfirst=True)
                # Supabase exposes public-schema tables through PostgREST.
                # Keep this owner's private watchlist/jobs inaccessible to anon
                # and authenticated API roles. The table owner still operates
                # normally; do not FORCE RLS on the scanner's connection role.
                connection.execute(text("ALTER TABLE cg_watch_records_v1 ENABLE ROW LEVEL SECURITY"))
                connection.execute(text("ALTER TABLE cg_jobs_v2 ENABLE ROW LEVEL SECURITY"))
                roles=connection.execute(text("SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')")).scalars()
                for role in roles:
                    connection.execute(text("REVOKE ALL ON TABLE cg_watch_records_v1, cg_jobs_v2 FROM "+role))
        else: metadata.create_all(self.engine)

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
        return list(self.iter_records(kind,after=after))

    def put_watch(self,key,payload):
        constructor=sqlite_insert if self.engine.dialect.name=="sqlite" else pg_insert
        statement=constructor(self.rows).values(kind="watch",key=key,payload=payload,updated=time.time(),lease=0)
        statement=statement.on_conflict_do_update(index_elements=["kind","key"],
            set_={"payload":payload,"updated":time.time()},
            where=func.coalesce(self.rows.c.payload["suppressed"].as_boolean(),False)==False)
        with self.engine.begin() as connection: return connection.execute(statement).rowcount==1

    def put_latest(self,payload):
        constructor=sqlite_insert if self.engine.dialect.name=="sqlite" else pg_insert
        statement=constructor(self.rows).values(kind="meta",key="latest",payload=payload,updated=time.time(),lease=0)
        statement=statement.on_conflict_do_update(index_elements=["kind","key"],set_={"payload":payload,"updated":time.time()},
            where=func.coalesce(cast(self.rows.c.payload["key"].as_string(),Float),0)<=float(payload["key"]))
        with self.engine.begin() as connection: connection.execute(statement)

    def renew(self,name,seconds=60):
        token=self.owners.get(name)
        if not token: return False
        with self.engine.begin() as connection:
            return connection.execute(update(self.rows).where(self.rows.c.kind=="lease",self.rows.c.key==name,
                self.rows.c.payload["owner"].as_string()==token,self.rows.c.lease>=time.time())
                .values(lease=time.time()+seconds)).rowcount==1

    def iter_records(self,kind,after=0,key_suffix=None,coin=None,limit=None,key_prefix=None):
        statement=select(self.rows.c.key,self.rows.c.payload).where(self.rows.c.kind==kind,self.rows.c.updated>=after)
        if key_suffix is not None: statement=statement.where(self.rows.c.key.endswith(key_suffix,autoescape=True))
        if key_prefix is not None: statement=statement.where(self.rows.c.key.startswith(key_prefix,autoescape=True))
        if coin is not None: statement=statement.where(self.rows.c.payload["coin"].as_string()==coin)
        statement=statement.order_by(self.rows.c.key)
        if limit is not None: statement=statement.limit(limit)
        with self.engine.connect() as connection:
            result=connection.execution_options(yield_per=100,stream_results=True).execute(statement)
            try:
                for row in result: yield row[0],row[1]
            finally: result.close()

    def lease(self, name, seconds=1200):
        constructor = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
        now = time.time()
        token = uuid.uuid4().hex
        with self.engine.begin() as connection:
            connection.execute(constructor(self.rows).values(kind="lease", key=name, payload={}, updated=now, lease=0)
                .on_conflict_do_nothing(index_elements=["kind", "key"]))
            result = connection.execute(update(self.rows).where(self.rows.c.kind=="lease", self.rows.c.key==name,
                self.rows.c.lease<now).values(lease=now+seconds, updated=now,payload={"owner":token}))
            if result.rowcount == 1:
                self.owners[name]=token
                return True
            return False

    def release(self, name):
        token=self.owners.pop(name,None)
        if token is None: return
        with self.engine.begin() as connection:
            connection.execute(update(self.rows).where(self.rows.c.kind=="lease",self.rows.c.key==name,
                self.rows.c.payload["owner"].as_string()==token).values(lease=0))

    def enqueue(self,job_id,kind,payload):
        constructor=sqlite_insert if self.engine.dialect.name=="sqlite" else pg_insert
        statement=constructor(self.jobs).values(id=str(job_id),kind=kind,payload=payload,state="QUEUED",
            owner="",lease=0,updated=time.time()).on_conflict_do_nothing(index_elements=["id"])
        with self.engine.begin() as connection:
            return connection.execute(statement).rowcount==1

    def claim(self,kind,owner,seconds=1500):
        now=time.time()
        with self.engine.begin() as connection:
            candidate=select(self.jobs.c.id,self.jobs.c.state).where(self.jobs.c.kind==kind,
                (self.jobs.c.state=="QUEUED") | ((self.jobs.c.state=="RUNNING") & (self.jobs.c.lease<now)))
            candidate=candidate.order_by(self.jobs.c.updated).limit(1)
            if self.engine.dialect.name=="postgresql": candidate=candidate.with_for_update(skip_locked=True)
            previous=connection.execute(candidate).first()
            if previous is None: return None
            key=previous[0]
            claimed=connection.execute(update(self.jobs).where(self.jobs.c.id==key,
                (self.jobs.c.state=="QUEUED") | ((self.jobs.c.state=="RUNNING") & (self.jobs.c.lease<now)))
                .values(state="RUNNING",owner=owner,lease=now+seconds,updated=now))
            if claimed.rowcount!=1: return None
            row=connection.execute(select(self.jobs).where(self.jobs.c.id==key)).mappings().first()
            return dict(row,reclaimed=previous[1]=="RUNNING")

    def finish_job(self,key,owner,result,state="DONE"):
        with self.engine.begin() as connection:
            return connection.execute(update(self.jobs).where(self.jobs.c.id==key,self.jobs.c.owner==owner,
                self.jobs.c.state=="RUNNING").values(state=state,result=str(result)[:160],lease=0,updated=time.time())).rowcount==1

    def requeue_job(self,key,owner):
        with self.engine.begin() as connection:
            return connection.execute(update(self.jobs).where(self.jobs.c.id==key,self.jobs.c.owner==owner,
                self.jobs.c.state=="RUNNING").values(state="QUEUED",owner="",lease=0,updated=time.time())).rowcount==1

    def prune(self,kind,before):
        with self.engine.begin() as connection:
            return connection.execute(delete(self.rows).where(self.rows.c.kind==kind,self.rows.c.updated<before)).rowcount

    def prune_jobs(self,before):
        with self.engine.begin() as connection:
            connection.execute(delete(self.jobs).where(self.jobs.c.updated<before,self.jobs.c.state.in_(["DONE","FAILED"])))

    def inflight_runs(self,after):
        statement=select(self.rows.c.key,self.rows.c.payload).where(self.rows.c.kind=="run",self.rows.c.updated>=after,
            (self.rows.c.payload["state"].as_string()=="FETCHING") | (self.rows.c.payload["delivery"].as_string()=="SENDING"))
        with self.engine.connect() as connection: return [(r[0],r[1]) for r in connection.execute(statement)]

    def close(self):
        self.engine.dispose()
