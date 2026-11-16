"""Run a single real job scan without starting the web server."""
import json
import uuid
from core import Store, ROOT, now, run_scan

if __name__ == '__main__':
    store = Store(ROOT)
    store.initialize()
    token = store.acquire()
    if not token:
        raise SystemExit('A scan already holds the database lease')
    run_id = uuid.uuid4().hex
    with store.db() as db:
        db.execute('INSERT INTO runs(id,started_at,status) VALUES (?,?,?)', (run_id, now(), 'running'))
    run_scan(store, run_id, token)
    with store.db() as db:
        result = dict(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'completed' else 2)
