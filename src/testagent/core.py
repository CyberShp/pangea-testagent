"""Transactional task state, device reservations and immutable skill versions."""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .skills import validate


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id():
    return uuid.uuid4().hex


class DomainError(ValueError):
    pass


class Core:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.redact = str
        self.sanitize = lambda value: value
        self.listeners = []
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA foreign_keys=ON;
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT UNIQUE NOT NULL);
          CREATE TABLE IF NOT EXISTS environments(id TEXT PRIMARY KEY, name TEXT, policy TEXT, roles TEXT);
          CREATE TABLE IF NOT EXISTS skills(id TEXT, version TEXT, name TEXT, mode TEXT, package TEXT, PRIMARY KEY(id,version));
          CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, title TEXT, status TEXT, scene TEXT, snapshot TEXT, created TEXT);
          CREATE TABLE IF NOT EXISTS reservations(device_id TEXT PRIMARY KEY REFERENCES devices(id), task_id TEXT REFERENCES tasks(id));
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(id), kind TEXT, payload TEXT, at TEXT);
          CREATE TABLE IF NOT EXISTS checks(task_id TEXT REFERENCES tasks(id), id TEXT, passed INTEGER, evidence TEXT, PRIMARY KEY(task_id,id));
          CREATE TABLE IF NOT EXISTS steps(task_id TEXT REFERENCES tasks(id), id TEXT, PRIMARY KEY(task_id,id));
          CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), operation TEXT, status TEXT);
        ''')

        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS device_settings(id TEXT PRIMARY KEY REFERENCES devices(id), port INTEGER, username TEXT, credential TEXT, fingerprint TEXT);
          CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, name TEXT, kind TEXT, config TEXT);
          CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(id), text TEXT, consumed INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), name TEXT, category TEXT, size INTEGER, created TEXT);
          CREATE TABLE IF NOT EXISTS recovery(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), plan TEXT, status TEXT, child_id TEXT, created TEXT);
          CREATE TABLE IF NOT EXISTS process_handles(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), device_id TEXT, details TEXT, active INTEGER);
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def emit(self, task, kind, payload):
        self.db.execute("INSERT INTO events(task_id,kind,payload,at) VALUES(?,?,?,?)",
                        (task, kind, json.dumps(self.sanitize(payload), ensure_ascii=False), now()))
        for listener in self.listeners:
            listener(task, kind)

    def device(self, name, address):
        if not name.strip() or not address.strip():
            raise DomainError("名称和地址不能为空")
        with self.tx():
            ident = new_id()
            try:
                self.db.execute("INSERT INTO devices VALUES(?,?,?)", (ident, name.strip(), address.strip().lower()))
            except sqlite3.IntegrityError as exc:
                raise DomainError("该设备地址已存在，请复用已有设备") from exc
            return ident

    def environment(self, name, roles, policy="confirm"):
        if not name.strip() or not isinstance(roles, dict) or not roles or policy not in ("confirm", "automatic"):
            raise DomainError("环境需要名称、设备角色和有效权限策略")
        with self.tx():
            for role, device in roles.items():
                if not role or not self.db.execute("SELECT id FROM devices WHERE id=?", (device,)).fetchone():
                    raise DomainError("设备角色引用无效")
            ident = new_id()
            self.db.execute("INSERT INTO environments VALUES(?,?,?,?)", (ident, name, policy, json.dumps(roles)))
            return ident

    def import_skill(self, package):
        result = validate(package)
        if result["mode"] == "invalid":
            raise DomainError("；".join(result["errors"]))
        with self.tx():
            try:
                self.db.execute("INSERT INTO skills VALUES(?,?,?,?,?)", (package["id"], package["version"],
                    package["name"], result["mode"], json.dumps(package, ensure_ascii=False)))
            except sqlite3.IntegrityError as exc:
                raise DomainError("版本已存在，更新请使用新版本号") from exc
        return result

    def create_task(self, title, environment_id, skill_id, version, backend="simulation", model="", parameters=None, roles=None):
        with self.tx():
            env = self.db.execute("SELECT * FROM environments WHERE id=?", (environment_id,)).fetchone()
            skill = self.db.execute("SELECT * FROM skills WHERE id=? AND version=?", (skill_id, version)).fetchone()
            if not env or not skill or not title.strip():
                raise DomainError("任务名称、环境或 Skill 版本无效")
            roles = roles if roles is not None else json.loads(env["roles"])
            if not isinstance(roles, dict) or not roles:
                raise DomainError("必须绑定设备角色")
            allowed = set(json.loads(env["roles"]).values())
            if not set(roles.values()).issubset(allowed):
                raise DomainError("角色绑定设备必须来自所选环境")
            package = json.loads(skill["package"])
            contract = json.loads(package["files"].get("contract.json", "{}"))
            if any(role["id"] not in roles for role in contract.get("roles", [])):
                raise DomainError("缺少 Skill 所需的设备角色")
            from .skills import validate_parameters
            parameters = validate_parameters(contract, parameters or {})
            profile = None
            if backend != 'simulation':
                row = self.db.execute('SELECT * FROM profiles WHERE id=?', (backend,)).fetchone()
                if not row:
                    raise DomainError('请选择有效执行后端')
                profile = dict(row)
                profile['config'] = json.loads(profile['config'])
                if profile['kind'] == 'openai' and not model:
                    raise DomainError('请选择或填写模型')
            devices = {}
            for device in roles.values():
                row = self.db.execute('SELECT d.*,s.port,s.username,s.credential,s.fingerprint FROM devices d LEFT JOIN device_settings s ON d.id=s.id WHERE d.id=?', (device,)).fetchone()
                if not row:
                    raise DomainError('绑定的设备不存在')
                devices[device] = dict(row)
                if backend != 'simulation' and row['address'].startswith('sim://'):
                    raise DomainError('真实后端不能操作模拟设备')
            ident = new_id()
            snapshot = {"environment_id": env["id"], "environment_name": env["name"], "roles": roles,
                        "policy": env["policy"], "skill": package, "mode": skill["mode"], "backend": backend,
                        "profile": profile, "model": model, "parameters": parameters, "devices": devices}
            self.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", (ident, title, "queued", "clean",
                json.dumps(snapshot, ensure_ascii=False), now()))
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('last_backend', json.dumps({'backend': backend, 'model': model})))
            self.emit(ident, "task.created", {"status": "queued", "backend": backend})
            return ident

    def task(self, ident):
        with self.lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
            if not row:
                raise DomainError("任务不存在")
            result = dict(row)
            result["snapshot"] = json.loads(result["snapshot"])
            return result

    def claim(self, ident):
        with self.tx():
            task = self.task(ident)
            if task["status"] != "queued":
                return False
            devices = set(task["snapshot"]["roles"].values())
            if any(self.db.execute("SELECT 1 FROM reservations WHERE device_id=? AND task_id<>?", (d,ident)).fetchone() for d in devices):
                return False
            for device in devices:
                self.db.execute("INSERT OR IGNORE INTO reservations VALUES(?,?)", (device, ident))
            self.db.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
            self.emit(ident, "task.state", {"status": "running"})
            return True

    def request_operation(self, ident, device_id, command, action_id=None, force_confirm=False):
        """Persist exact operation intent before any executor can run it."""
        with self.tx():
            task = self.task(ident)
            if task["status"] != "running":
                raise DomainError("任务当前不能发起操作")
            row = self.db.execute("SELECT task_id FROM reservations WHERE device_id=?", (device_id,)).fetchone()
            if device_id not in task["snapshot"]["roles"].values() or not row or row[0] != ident:
                raise DomainError("目标设备不属于本任务或未获得占用")
            contract = json.loads(task["snapshot"]["skill"]["files"].get("contract.json", "{}"))
            actions = {a["id"]: a for a in contract.get("critical_actions", [])}
            if action_id is not None and action_id not in actions:
                raise DomainError("关键操作未在 Skill 中声明")
            confirm = force_confirm or (task["snapshot"]["policy"] == "confirm" and (action_id is not None or task["snapshot"]["mode"] == "ordinary"))
            operation = {"device_id": device_id, "command": command, "action_id": action_id,
                         "description": actions.get(action_id, {}).get("description", "普通 Skill 操作确认")}
            operation_id = new_id()
            self.db.execute("INSERT INTO approvals VALUES(?,?,?,?)", (operation_id, ident, json.dumps(operation),
                            "pending" if confirm else "approved"))
            if confirm:
                self.db.execute("UPDATE tasks SET status='waiting_user' WHERE id=?", (ident,))
            self.emit(ident, "approval.requested" if confirm else "operation.ready", {"id": operation_id, **operation})
            return operation_id

    def approve(self, ident, approval_id):
        with self.tx():
            task = self.task(ident)
            row = self.db.execute("SELECT * FROM approvals WHERE id=? AND task_id=?", (approval_id, ident)).fetchone()
            if task["status"] != "waiting_user" or not row or row["status"] != "pending":
                raise DomainError("授权请求已失效或不属于该任务")
            self.db.execute("UPDATE approvals SET status='approved' WHERE id=?", (approval_id,))
            self.db.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
            self.emit(ident, "approval.granted", {"id": approval_id})

    def consume_operation(self, ident, operation_id):
        with self.tx():
            task = self.task(ident)
            row = self.db.execute("SELECT * FROM approvals WHERE id=? AND task_id=?", (operation_id, ident)).fetchone()
            if task["status"] != "running" or not row or row["status"] != "approved":
                raise DomainError("操作未授权、已执行或任务不在执行中")
            self.db.execute("UPDATE approvals SET status='consumed' WHERE id=?", (operation_id,))
            operation = json.loads(row["operation"])
            self.db.execute("UPDATE tasks SET scene='unknown' WHERE id=?", (ident,))
            self.emit(ident, "tool.started", {"id": operation_id, **operation})
            return operation

    def record_result(self, ident, operation_id, output):
        with self.tx():
            row = self.db.execute("SELECT status FROM approvals WHERE id=? AND task_id=?", (operation_id, ident)).fetchone()
            if not row or row[0] != "consumed":
                raise DomainError("没有对应的执行操作")
            self.db.execute("UPDATE approvals SET status='finished' WHERE id=?", (operation_id,))
            self.emit(ident, "tool.finished", {"id": operation_id, "output": output})

    def complete_step(self, ident, step_id):
        with self.tx():
            task = self.task(ident)
            contract = json.loads(task["snapshot"]["skill"]["files"].get("contract.json", "{}"))
            if task["status"] != "running" or step_id not in {s["id"] for s in contract.get("steps", [])}:
                raise DomainError("步骤不存在或任务不可推进")
            self.db.execute("INSERT OR IGNORE INTO steps VALUES(?,?)", (ident, step_id))
            self.emit(ident, "step.completed", {"id": step_id})

    def check(self, ident, check_id, passed, evidence_operation):
        with self.tx():
            task = self.task(ident)
            contract = json.loads(task["snapshot"]["skill"]["files"].get("contract.json", "{}"))
            row = self.db.execute("SELECT status FROM approvals WHERE id=? AND task_id=?", (evidence_operation, ident)).fetchone()
            if (task["status"] != "running" or check_id not in {c["id"] for c in contract.get("checks", [])}
                    or not row or row[0] != "finished" or type(passed) is not bool):
                raise DomainError("检查点、任务状态或工具证据无效")
            self.db.execute("INSERT OR REPLACE INTO checks VALUES(?,?,?,?)", (ident, check_id, int(passed), evidence_operation))
            self.emit(ident, "check.result", {"id": check_id, "passed": passed, "evidence": evidence_operation})

    def finish(self, ident):
        with self.tx():
            task = self.task(ident)
            if task["status"] != "running":
                raise DomainError("任务当前不能完成")
            contract = json.loads(task["snapshot"]["skill"]["files"].get("contract.json", "{}"))
            checks = {r[0]: r[1] for r in self.db.execute("SELECT id,passed FROM checks WHERE task_id=?", (ident,))}
            steps = {r[0] for r in self.db.execute("SELECT id FROM steps WHERE task_id=?", (ident,))}
            if any(c["required"] and checks.get(c["id"]) != 1 for c in contract.get("checks", [])):
                raise DomainError("必要检查点尚未通过")
            if any(s["required"] and s["id"] not in steps for s in contract.get("steps", [])):
                raise DomainError("必要步骤尚未完成")
            if self.db.execute("SELECT 1 FROM approvals WHERE task_id=? AND status != 'finished'", (ident,)).fetchone():
                raise DomainError("仍有未结束的操作")
            self.db.execute("UPDATE tasks SET status='succeeded',scene='clean' WHERE id=?", (ident,))
            self.db.execute("DELETE FROM reservations WHERE task_id=?", (ident,))
            self.emit(ident, "task.state", {"status": "succeeded"})

    def fail(self, ident, reason):
        with self.tx():
            if self.task(ident)["status"] in ("succeeded", "failed", "stopped"):
                return
            self.db.execute("UPDATE tasks SET status='failed',scene='unknown' WHERE id=?", (ident,))
            self.emit(ident, "task.failed", {"reason": reason})

    def stop_simulation(self, ident):
        """Only simulation has no external process; real executors must implement termination."""
        with self.tx():
            task = self.task(ident)
            if task["snapshot"]["backend"] != "simulation" or task["status"] in ("failed", "succeeded", "stopped"):
                raise DomainError("任务不能通过模拟停止入口终止")
            self.db.execute("UPDATE tasks SET status='stopped',scene='clean' WHERE id=?", (ident,))
            self.db.execute("DELETE FROM reservations WHERE task_id=?", (ident,))
            self.emit(ident, "task.stopped", {"evidence": "模拟器不创建远端进程；不代表真实脚本终止能力"})

    def recover_startup(self):
        with self.tx():
            rows = list(self.db.execute("SELECT id FROM tasks WHERE status IN ('queued','running','waiting_user','stopping')"))
            for row in rows:
                self.db.execute("UPDATE tasks SET status='failed',scene='unknown' WHERE id=?", (row[0],))
                self.emit(row[0], "task.failed", {"reason": "后台服务中断；不自动续跑，已有设备占用保留"})
            for recovery in list(self.db.execute("SELECT * FROM recovery WHERE status IN ('planning','running')")):
                self.db.execute("UPDATE recovery SET status='failed' WHERE id=?", (recovery['id'],))
                self.db.execute("UPDATE tasks SET scene='recovery_failed' WHERE id=?", (recovery['task_id'],))
                self.emit(recovery['task_id'], 'recovery.interrupted', {'id':recovery['id']})

    def events(self, ident, after=0):
        with self.lock:
            self.task(ident)
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in self.db.execute(
                "SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 1000", (ident, after))]

    def overview(self):
        with self.lock:
            return {table: [dict(r) for r in self.db.execute(query)] for table, query in {
                "devices": "SELECT * FROM devices", "environments": "SELECT * FROM environments",
                "skills": "SELECT id,version,name,mode FROM skills",
                "tasks": "SELECT id,title,status,scene,created FROM tasks ORDER BY created DESC",
                "reservations": "SELECT * FROM reservations"}.items()}
