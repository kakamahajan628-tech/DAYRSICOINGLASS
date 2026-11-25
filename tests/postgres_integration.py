"""CI-only integration against a disposable Postgres service, never user DB."""
import os
from pathlib import Path
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from storage import Store

def main():
    url=os.environ["TEST_DATABASE_URL"]
    # This disposable CI instance has no TLS. Production Store always requires
    # TLS; use a narrow test-only engine connection hook for this local test.
    from unittest.mock import patch
    from sqlalchemy import create_engine as real_create
    def create(url,**options):
        options["connect_args"]["sslmode"]="disable"
        return real_create(url,**options)
    key="integration:"+uuid.uuid4().hex
    with patch("storage.create_engine",side_effect=create):
        first=Store(url); second=Store(url)
    try:
        from sqlalchemy import text
        with first.engine.connect() as connection:
            enabled=connection.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname IN ('cg_watch_records_v1','cg_jobs_v2')")).scalars().all()
            assert len(enabled)==2 and all(enabled)
        assert first.enqueue(key,"command","/daily")
        assert not second.enqueue(key,"command","/daily")
        job=first.claim("command","owner-a",30)
        assert job["id"]==key and second.claim("command","owner-b",30) is None
        assert not second.finish_job(key,"owner-b","invalid")
        assert first.finish_job(key,"owner-a","done")
        assert first.lease(key,30) and not second.lease(key,30)
        first.release(key); assert second.lease(key,30); second.release(key)
        first.put("watch",key,{"active":False,"suppressed":True})
        assert not second.put_watch(key,{"active":True})
        print("PostgreSQL job claims, lease ownership and deletion protection PASS")
    finally: first.close(); second.close()

if __name__=="__main__": main()
