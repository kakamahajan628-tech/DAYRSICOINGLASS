"""Run on GitHub Actions or a backup host, not the 512 MB scanner.

pg_dump credentials use subprocess environment, never command arguments.
Encrypted archives are checked with pg_restore --list before upload. This is
archive validation; a separate restore drill is still required.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlsplit,unquote,parse_qs

def pg_env(url):
    parts=urlsplit(url.replace("postgresql+psycopg://","postgresql://",1))
    if parts.scheme not in ("postgres","postgresql") or not parts.hostname or not parts.path.strip("/"): raise ValueError("INVALID_BACKUP_DATABASE_URL")
    env=os.environ.copy()
    env.update(PGHOST=parts.hostname,PGPORT=str(parts.port or 5432),PGUSER=unquote(parts.username or "postgres"),
        PGPASSWORD=unquote(parts.password or ""),PGDATABASE=unquote(parts.path.lstrip("/")),PGSSLMODE="require",PGCONNECT_TIMEOUT="15")
    return env

def create(output,url,password):
    if len(password)<32: raise ValueError("BACKUP_ENCRYPTION_KEY_NEEDS_32_CHARACTERS")
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    env=pg_env(url)
    with tempfile.TemporaryDirectory() as folder:
        dump=Path(folder)/"database.dump"
        subprocess.run(["pg_dump","--format=custom","--no-owner","--no-acl","--file",str(dump)],env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(["pg_restore","--list",str(dump)],env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        salt=os.urandom(16); key=base64.urlsafe_b64encode(PBKDF2HMAC(algorithm=hashes.SHA256(),length=32,salt=salt,iterations=600000).derive(password.encode()))
        output=Path(output); output.parent.mkdir(parents=True,exist_ok=True)
        output.write_bytes(b"CG_BACKUP_V2\n"+salt+Fernet(key).encrypt(dump.read_bytes()))
        metadata={"archive_sha256":hashlib.sha256(output.read_bytes()).hexdigest(),"dump_bytes":dump.stat().st_size,"encrypted_bytes":output.stat().st_size,"archive_list_checked":True,"restore_tested":False,"epoch":time.time()}
        output.with_suffix(output.suffix+".json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")
        return metadata

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--output",default="backup/database.dump.enc"); parser.add_argument("--record-upload",default=None); parser.add_argument("--decrypt",default=None); args=parser.parse_args()
    if args.decrypt:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        data=Path(args.decrypt).read_bytes(); prefix=b"CG_BACKUP_V2\n"
        if not data.startswith(prefix): raise ValueError("UNKNOWN_BACKUP_FORMAT")
        salt=data[len(prefix):len(prefix)+16]
        key=base64.urlsafe_b64encode(PBKDF2HMAC(algorithm=hashes.SHA256(),length=32,salt=salt,iterations=600000).derive(os.environ["BACKUP_ENCRYPTION_KEY"].encode()))
        Path(args.output).write_bytes(Fernet(key).decrypt(data[len(prefix)+16:])); return
    url=os.environ.get("BACKUP_DATABASE_URL") or os.environ["DATABASE_URL"]
    if args.record_upload:
        from storage import Store
        metadata=json.loads(Path(args.output+".json").read_text(encoding="utf-8")); metadata["external_artifact"]=args.record_upload
        store=Store(url)
        try: store.put("meta","backup",metadata)
        finally: store.close()
    else: create(args.output,url,os.environ["BACKUP_ENCRYPTION_KEY"])

if __name__=="__main__":
    try: main()
    except Exception as error: raise SystemExit("Backup failed: "+type(error).__name__)
