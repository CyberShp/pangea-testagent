"""Explicit in-process diagnostic simulator, not an AI or SSH implementation."""
import threading
import json
from .execution import simulation_operation

from .core import DomainError


class DiagnosticSession:
    def __init__(self):
        self.view = "device"

    def send(self, command):
        if command == "diagnose":
            self.view = "diagnose"
            return "diagnose>"
        if command == "show test-config" and self.view == "diagnose":
            return "flag=enabled\ndiagnose>"
        if command == "exit":
            self.view = "device"
            return "device>"
        raise DomainError("模拟命令不支持或不在诊断视图")


class Simulator:
    def __init__(self, core):
        self.core = core
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.closed.set()
        self.thread.join(timeout=2)

    def loop(self):
        while not self.closed.wait(.2):
            for task in self.core.overview()["tasks"]:
                try:
                    if task["status"] == "queued" and self.core.claim(task["id"]):
                        self.prepare(task["id"])
                    elif task["status"] == "running":
                        self.execute(task["id"])
                except DomainError as exc:
                    self.core.fail(task["id"], str(exc))

    def prepare(self, ident):
        task = self.core.task(ident)
        # The simulator is intentionally limited to this example contract.
        if task["snapshot"]["skill"]["id"] != "diagnostic-demo":
            self.core.fail(ident, "当前模拟器仅支持 diagnostic-demo；不会伪造其他 Skill 执行结果")
            return
        self.core.propose_preview(ident,'检查模拟控制器的测试标记','仅执行本机模拟诊断交互',
                                  '检查输出包含 flag=enabled',[simulation_operation()])

    def execute(self, ident):
        # Serialize stop vs execution. Real long-running executors must use cancel handles,
        # not hold the core transaction lock over a network operation.
        with self.core.lock:
            row = self.core.db.execute("SELECT id FROM approvals WHERE task_id=? AND status='approved'", (ident,)).fetchone()
            if not row:
                task=self.core.task(ident)
                if task['status']=='running':
                    plans=self.core.previews(ident)
                    if plans and plans[-1]['status']=='superseded':
                        with self.core.tx():self.core.db.execute('UPDATE messages SET consumed=1 WHERE task_id=?',(ident,))
                        self.prepare(ident)
                        return
                    if not plans or plans[-1]['status']!='approved':return
                    self.core.request_operation(ident,task['snapshot']['roles']['controller'],json.dumps(simulation_operation()),'enter-diagnostic')
                return
            self.core.consume_operation(ident, row[0])
            session = DiagnosticSession()
            output = session.send("diagnose") + "\n" + session.send("show test-config")
            self.core.record_result(ident, row[0], output)
            self.core.complete_step(ident, "inspect")
            self.core.check(ident, "flag-present", "flag=enabled" in output, row[0])
            self.core.finish(ident)
