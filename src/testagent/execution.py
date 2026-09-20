"""Confirmed operation plans and evidence-backed configuration comparisons."""
import difflib
import json
from jsonschema import validate

REMOTE_ACTIONS = ['exec', 'shell_open', 'shell_send', 'shell_close', 'remote_read',
                  'remote_write', 'upload', 'download', 'script', 'wait_connected']
OPERATION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'action': {'enum': REMOTE_ACTIONS}, 'role': {'type': 'string', 'minLength': 1},
        'action_id': {'type': 'string'}, 'command': {'type': 'string', 'minLength': 1},
        'timeout': {'type': 'number', 'minimum': 1, 'maximum': 86400},
        'session_id': {'type': 'string'}, 'text': {'type': 'string'},
        'expect': {'type': 'string', 'minLength': 1}, 'expect_disconnect': {'type': 'boolean'},
        'path': {'type': 'string'}, 'file_id': {'type': 'string'}, 'name': {'type': 'string'},
        'args': {'type': 'array', 'items': {'type': 'string'}},
        'capture_id': {'type': 'string'}, 'capture_phase': {'enum': ['before', 'after']},
    },
    'required': ['action', 'role'],
    'allOf': [
        {'if': {'properties': {'action': {'const': action}}}, 'then': {'required': fields}}
        for action, fields in {
            'exec': ['command'], 'shell_send': ['text'], 'remote_read': ['path'],
            'remote_write': ['path', 'text'], 'upload': ['path', 'file_id'], 'download': ['path'],
        }.items()
    ],
}


def canonical(operation):
    # SSH session IDs are allocated only when an approved shell_open runs. The
    # executor independently checks ownership of the actual session on every use.
    return {key: value for key, value in operation.items() if key != 'session_id'}


def validate_operation(operation, snapshot, *, simulation=False):
    from .core import DomainError
    if simulation and operation == simulation_operation():
        return
    validate(operation, OPERATION_SCHEMA)
    if operation['role'] not in snapshot['roles']:
        raise DomainError('预览引用了未绑定的设备角色')
    contract = json.loads(snapshot['skill']['files'].get('contract.json', '{}'))
    if operation.get('action_id') is not None and operation['action_id'] not in {a['id'] for a in contract.get('critical_actions', [])}:
        raise DomainError('关键操作未在 Skill 中声明')
    if operation['action'] == 'shell_send' and not (operation.get('expect') or operation.get('expect_disconnect')):
        raise DomainError('交互命令需要提示符或预期断连声明')
    if operation['action'] == 'script' and not (operation.get('path') or operation.get('file_id')):
        raise DomainError('脚本操作需要 path 或 file_id')
    if 'capture_id' in operation or 'capture_phase' in operation:
        definition = next((c for c in contract.get('comparisons', []) if c['id'] == operation.get('capture_id')), None)
        actual = {k: v for k, v in canonical(operation).items() if k not in ('capture_id', 'capture_phase')}
        if not definition or operation.get('capture_phase') not in ('before', 'after') or actual != {'role': definition['role'], **definition['operation']}:
            raise DomainError('配置采集必须与 Skill 声明的方法和设备角色一致')


def simulation_operation():
    return {'action': 'simulation', 'role': 'controller', 'command': 'diagnose → show test-config', 'action_id': 'enter-diagnostic'}


def comparisons(core, task):
    item = core.task(task)
    contract = json.loads(item['snapshot']['skill']['files'].get('contract.json', '{}'))
    with core.lock:
        rows = list(core.db.execute('SELECT * FROM config_captures WHERE task_id=?', (task,)))
    result = []
    for definition in contract.get('comparisons', []):
        captures = {r['phase']: dict(r) for r in rows if r['comparison_id'] == definition['id']}
        before, after = captures.get('before'), captures.get('after')
        available = before is not None and after is not None
        diff = '\n'.join(difflib.unified_diff(before['text'].splitlines(), after['text'].splitlines(), fromfile='before', tofile='after',lineterm='')) if available else ''
        result.append({'id': definition['id'], 'name': definition.get('name', definition['id']),
                       'role': definition['role'], 'available': available, 'changed': available and before['text'] != after['text'],
                       'before': before, 'after': after, 'diff': diff})
    return result
