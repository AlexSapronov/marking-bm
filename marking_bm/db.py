import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,role TEXT NOT NULL DEFAULT 'manager' CHECK(role IN ('manager','logistics','supervisor')));
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,username TEXT,admin INTEGER NOT NULL,csrf TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(ip TEXT PRIMARY KEY,failures INTEGER NOT NULL,until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS suppliers(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,merged_into TEXT REFERENCES suppliers(id),codes_xls INTEGER NOT NULL DEFAULT 0 CHECK(codes_xls IN (0,1)));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog(gtin TEXT PRIMARY KEY,tnved TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pn_map(gtin TEXT NOT NULL,pn TEXT NOT NULL,PRIMARY KEY(gtin,pn));
CREATE TABLE IF NOT EXISTS documents(
 id TEXT PRIMARY KEY,number TEXT NOT NULL,supplier_id TEXT NOT NULL REFERENCES suppliers(id),
 owner TEXT NOT NULL REFERENCES users(username),status TEXT NOT NULL DEFAULT 'draft',revision INTEGER NOT NULL DEFAULT 0,
 created TEXT NOT NULL,updated TEXT NOT NULL,completed TEXT,parent_id TEXT REFERENCES documents(id),
 production_date TEXT,kpp TEXT,fias_id TEXT,last_error TEXT,current_version INTEGER NOT NULL DEFAULT 0,transferred_version INTEGER);
CREATE TABLE IF NOT EXISTS document_stops(doc_id TEXT PRIMARY KEY REFERENCES documents(id),actor TEXT NOT NULL,created TEXT NOT NULL,previous_status TEXT NOT NULL,resumed_at TEXT,resumed_by TEXT);
CREATE TABLE IF NOT EXISTS operation_stops(op_id TEXT NOT NULL REFERENCES operations(id),doc_id TEXT NOT NULL REFERENCES document_stops(doc_id),created TEXT NOT NULL,PRIMARY KEY(op_id,doc_id));
CREATE INDEX IF NOT EXISTS operation_stops_document ON operation_stops(doc_id);
CREATE TABLE IF NOT EXISTS group_stops(group_id TEXT NOT NULL,doc_id TEXT NOT NULL REFERENCES document_stops(doc_id),created TEXT NOT NULL,PRIMARY KEY(group_id,doc_id));
CREATE TABLE IF NOT EXISTS lines(id INTEGER PRIMARY KEY,doc_id TEXT NOT NULL REFERENCES documents(id),pn TEXT NOT NULL,
 gtin TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 99999999),labels INTEGER NOT NULL CHECK(labels>0),tnved TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS codes(id TEXT PRIMARY KEY,raw TEXT NOT NULL,cis TEXT NOT NULL UNIQUE,gtin TEXT NOT NULL,serial TEXT NOT NULL,
 source_op TEXT NOT NULL,external_status TEXT NOT NULL,checked TEXT,local_state TEXT NOT NULL,
 doc_id TEXT REFERENCES documents(id),pn TEXT,quantity INTEGER,tnved TEXT,atk_id TEXT,reuse_allowed INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS pool ON codes(gtin,local_state,doc_id);
CREATE TABLE IF NOT EXISTS assignments(id INTEGER PRIMARY KEY,code_id TEXT NOT NULL REFERENCES codes(id),doc_id TEXT NOT NULL,
 pn TEXT NOT NULL,quantity INTEGER NOT NULL,tnved TEXT NOT NULL,active INTEGER NOT NULL,created TEXT NOT NULL,ended TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_assignment ON assignments(code_id) WHERE active=1;
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,created TEXT NOT NULL,actor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batch_docs(batch_id TEXT NOT NULL REFERENCES batches(id),doc_id TEXT NOT NULL UNIQUE REFERENCES documents(id),supplier_id TEXT NOT NULL,PRIMARY KEY(batch_id,doc_id));
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,kind TEXT NOT NULL,doc_id TEXT,group_id TEXT,
 op_key TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,sha256 TEXT NOT NULL,state TEXT NOT NULL,
 external_id TEXT,result TEXT,signature TEXT,lease TEXT,error TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS introduction_documents(doc_id TEXT PRIMARY KEY REFERENCES documents(id),op_id TEXT NOT NULL REFERENCES operations(id));
CREATE TABLE IF NOT EXISTS introduction_atks(atk_id TEXT PRIMARY KEY REFERENCES atks(id),op_id TEXT NOT NULL REFERENCES operations(id));
CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY,op_id TEXT NOT NULL REFERENCES operations(id),body TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operation_attention(op_id TEXT PRIMARY KEY REFERENCES operations(id),resolved_by TEXT NOT NULL,reason TEXT NOT NULL,resolved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,doc_id TEXT,group_id TEXT,version INTEGER NOT NULL,kind TEXT NOT NULL,
 name TEXT NOT NULL,content BLOB NOT NULL,sha256 TEXT NOT NULL,snapshot TEXT NOT NULL,created TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS doc_artifact ON artifacts(doc_id,version,kind) WHERE doc_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS atks(id TEXT PRIMARY KEY,code TEXT UNIQUE,supplier_id TEXT NOT NULL,tnved TEXT NOT NULL,batch_id TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,op_id TEXT NOT NULL,state TEXT NOT NULL,UNIQUE(batch_id,supplier_id,tnved));
CREATE TABLE IF NOT EXISTS atk_versions(atk_id TEXT NOT NULL,version INTEGER NOT NULL,composition TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(atk_id,version));
CREATE TABLE IF NOT EXISTS transformations(id TEXT PRIMARY KEY,atk_id TEXT NOT NULL,kind TEXT NOT NULL,code_ids TEXT NOT NULL,
 actor TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,reviewer TEXT,op_id TEXT,created TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_transform ON transformations(atk_id) WHERE state IN ('approval','approved','processing');
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,entity TEXT NOT NULL,detail TEXT NOT NULL,created TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS immutable_applied BEFORE UPDATE OF raw,cis,gtin,serial,source_op,doc_id,pn,quantity,tnved ON codes WHEN OLD.external_status='APPLIED' BEGIN SELECT RAISE(ABORT,'applied code immutable'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_codes BEFORE DELETE ON codes BEGIN SELECT RAISE(ABORT,'code history immutable'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_assignments BEFORE DELETE ON assignments BEGIN SELECT RAISE(ABORT,'assignment history immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER IF NOT EXISTS immutable_artifacts_update BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT,'immutable artifact'); END;
CREATE TRIGGER IF NOT EXISTS immutable_artifacts_delete BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT,'immutable artifact'); END;
CREATE TRIGGER IF NOT EXISTS immutable_payload BEFORE UPDATE OF payload,sha256 ON operations BEGIN SELECT RAISE(ABORT,'immutable payload'); END;
CREATE TRIGGER IF NOT EXISTS immutable_atk_versions_update BEFORE UPDATE ON atk_versions BEGIN SELECT RAISE(ABORT,'immutable version'); END;
CREATE TRIGGER IF NOT EXISTS immutable_atk_versions_delete BEFORE DELETE ON atk_versions BEGIN SELECT RAISE(ABORT,'immutable version'); END;
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            if "last_error" not in {
                r[1] for r in db.execute("PRAGMA table_info(documents)")
            }:
                db.execute("ALTER TABLE documents ADD COLUMN last_error TEXT")
            if "role" not in {r[1] for r in db.execute("PRAGMA table_info(users)")}:
                db.execute(
                    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'manager' CHECK(role IN ('manager','logistics','supervisor'))"
                )
            # Passwords are opt-in only through one-time admin-issued setup codes.
            # Existing passwordless sessions must NOT survive the migration.
            columns = {r[1] for r in db.execute("PRAGMA table_info(users)")}
            if "password_hash" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
                db.execute("DELETE FROM sessions WHERE admin=0")
            if "setup_token_hash" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN setup_token_hash TEXT")
            if "setup_expires" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN setup_expires REAL")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(2)")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(3)")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(4)")
            db.commit()

            # SQLite cannot widen a CHECK constraint with ALTER TABLE. Existing
            # installations may still restrict users.role to manager/logistics,
            # so rebuild only that table while preserving accounts and sessions.
            user_table = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
            if user_table and "'supervisor'" not in (user_table["sql"] or ""):
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute(
                        "CREATE TABLE users_new("
                        "username TEXT PRIMARY KEY,"
                        "enabled INTEGER NOT NULL DEFAULT 1,"
                        "role TEXT NOT NULL DEFAULT 'manager' "
                        "CHECK(role IN ('manager','logistics','supervisor')),"
                        "password_hash TEXT,"
                        "setup_token_hash TEXT,"
                        "setup_expires REAL)"
                    )
                    db.execute(
                        "INSERT INTO users_new(username,enabled,role,password_hash,"
                        "setup_token_hash,setup_expires) "
                        "SELECT username,enabled,"
                        "CASE WHEN role IN ('manager','logistics','supervisor') "
                        "THEN role ELSE 'manager' END,"
                        "password_hash,setup_token_hash,setup_expires FROM users"
                    )
                    db.execute("DROP TABLE users")
                    db.execute("ALTER TABLE users_new RENAME TO users")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.execute("PRAGMA foreign_keys=ON")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(5)")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(6)")
            if "codes_xls" not in {r[1] for r in db.execute("PRAGMA table_info(suppliers)")}:
                db.execute("ALTER TABLE suppliers ADD COLUMN codes_xls INTEGER NOT NULL DEFAULT 0 CHECK(codes_xls IN (0,1))")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(7)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def read(self):
        """Consistent read snapshot without locking competing writers in WAL mode."""
        db = self.connect()
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.rollback()
            db.close()

    def all(self, sql, args=()):
        db = self.connect()
        try:
            return [dict(r) for r in db.execute(sql, args)]
        finally:
            db.close()

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None


def audit(db, actor, action, entity, detail=None):
    db.execute(
        "INSERT INTO audit(actor,action,entity,detail,created) VALUES(?,?,?,?,?)",
        (actor, action, entity, dumps(detail or {}), now()),
    )
