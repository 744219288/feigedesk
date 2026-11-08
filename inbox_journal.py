"""Durable receipt before delivery to the application's message database."""
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

class InboxJournal:
    def __init__(self, folder):
        Path(folder).mkdir(parents=True,exist_ok=True)
        self.path=Path(folder)/'inbox-journal.sqlite'
        self.lock=threading.RLock()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS pending(k TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS verified_identities(talk TEXT PRIMARY KEY,buyer TEXT,scope TEXT,conflict INTEGER DEFAULT 0)')
            db.commit()

    def deliver(self, item, callback):
        key=json.dumps([item.get('shop_scope',''),item['conversation_id'],item['id'],bool(item.get('transcript'))])
        with self.lock, closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT OR REPLACE INTO pending VALUES (?,?)',(key,json.dumps(item,ensure_ascii=False)))
            db.commit()
            callback(item)
            db.execute('DELETE FROM pending WHERE k=?',(key,))
            db.commit()

    def replay(self, callback):
        with self.lock, closing(sqlite3.connect(self.path)) as db:
            for key,payload in db.execute('SELECT k,payload FROM pending LIMIT 100').fetchall():
                item=json.loads(payload)
                item['historical']=True
                callback(item)
                db.execute('DELETE FROM pending WHERE k=?',(key,))
                db.commit()

    def close(self):
        pass

    def remember_identity(self,identity):
        with self.lock,closing(sqlite3.connect(self.path)) as db:
            old=db.execute('SELECT buyer,scope,conflict FROM verified_identities WHERE talk=?',(identity['talk'],)).fetchone()
            if old and (old[2] or old[:2]!=(identity['buyer'],identity['scope'])):
                db.execute('UPDATE verified_identities SET conflict=1 WHERE talk=?',(identity['talk'],))
            else:db.execute('INSERT OR IGNORE INTO verified_identities(talk,buyer,scope) VALUES (?,?,?)',(identity['talk'],identity['buyer'],identity['scope']))
            db.commit()

    def identities(self):
        with self.lock,closing(sqlite3.connect(self.path)) as db:
            return {talk:({'talk':talk,'buyer':buyer,'scope':scope} if not conflict else None)
                    for talk,buyer,scope,conflict in db.execute('SELECT talk,buyer,scope,conflict FROM verified_identities')}
