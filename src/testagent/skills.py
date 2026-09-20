"""Versioned, deterministic import checks. Never judge business completeness."""
import json
import re
from pathlib import PurePosixPath


def validate(package):
    errors = []
    if not isinstance(package, dict):
        return {"mode": "invalid", "errors": ["能力包必须是对象"]}
    files = package.get("files", {})
    if not isinstance(files, dict):
        files = {}
        errors.append("files 必须是文件名到 UTF-8 文本的映射")
    from .paths import safe_relative
    for name, content in files.items():
        try: safe_relative(name)
        except ValueError: errors.append(f'不合法的包内文件: {name}')
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
                or ":" in name or not isinstance(content, str)):
            errors.append(f"不合法的包内文件: {name}")
    if not isinstance(files.get("SKILL.md"),str) or not files["SKILL.md"].strip():
        errors.append("缺少非空 SKILL.md")
    for key in ("id", "version", "name"):
        if not isinstance(package.get(key), str) or not package[key].strip():
            errors.append(f"缺少 {key}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", str(package.get("id", ""))):
        errors.append("id 仅允许小写字母、数字、下划线和连字符")
    if "contract.json" not in files:
        return {"mode": "invalid" if errors else "ordinary", "errors": errors}
    try:
        contract = json.loads(files["contract.json"])
        if not isinstance(contract, dict):
            raise ValueError()
    except (ValueError, TypeError):
        return {"mode": "invalid", "errors": errors + ["contract.json 必须是 JSON 对象"]}
    if contract.get("schema_version") != 1:
        errors.append("不支持的 schema_version，当前为 1")
    ids = {}
    for field in ("roles", "steps", "checks", "critical_actions"):
        items = contract.get(field)
        if not isinstance(items, list):
            errors.append(f"{field} 必须是数组")
            items = []
        ids[field] = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                errors.append(f"{field} 的每项必须有 id")
                continue
            if item["id"] in ids[field]:
                errors.append(f"{field} 存在重复 id: {item['id']}")
            ids[field].add(item["id"])
            if field in ("checks", "critical_actions") and item.get("step_id") not in ids.get("steps", set()):
                errors.append(f"{field}/{item['id']} 引用了不存在的步骤")
            if field == "critical_actions" and not str(item.get("description", "")).strip():
                errors.append(f"关键操作 {item['id']} 缺少影响说明")
            if field in ("steps", "checks") and not isinstance(item.get("required"), bool):
                errors.append(f"{field}/{item['id']} 的 required 必须是布尔值")
    for name in contract.get("scripts", []) if isinstance(contract.get("scripts", []), list) else [None]:
        if not isinstance(name, str) or name not in files:
            errors.append(f"脚本引用不存在: {name}")
    try:
        from jsonschema import Draft202012Validator
        schema = contract.get('parameters', {'type': 'object'})
        Draft202012Validator.check_schema(schema)
        if schema.get('type') != 'object':
            errors.append('parameters 必须描述 object')
    except Exception as exc:
        errors.append('参数定义非法：' + str(exc).splitlines()[0])
    for check in contract.get('checks', []) if isinstance(contract.get('checks'), list) else []:
        if not isinstance(check,dict): continue
        rule=check.get('assertion')
        if rule is not None and (not isinstance(rule,dict) or not (('contains' in rule and isinstance(rule['contains'],str)) or 'equals' in rule)):
            errors.append('assertion 需要 contains 字符串或 equals 值')
    recovery = contract.get('recovery')
    if recovery is not None and not isinstance(recovery, str):
        errors.append('recovery 必须是恢复方法说明文本')
    from jsonschema import validate as validate_schema
    from .execution import OPERATION_SCHEMA
    try:
        topology = contract.get('topology', [])
        validate_schema(topology, {'type':'array','maxItems':200,'items':{'type':'object','additionalProperties':False,
            'properties':{'from':{'type':'string'},'to':{'type':'string'},'label':{'type':'string'}},'required':['from','to']}})
        for link in topology:
            if link['from'] not in ids['roles'] or link['to'] not in ids['roles'] or link['from']==link['to']:
                errors.append('拓扑连线必须引用两个不同的已声明角色')
        comparisons=contract.get('comparisons', [])
        validate_schema(comparisons, {'type':'array','maxItems':50,'items':{'type':'object','additionalProperties':False,
            'properties':{'id':{'type':'string','pattern':'^[a-z0-9][a-z0-9_-]{0,63}$'},'name':{'type':'string'},
                          'role':{'type':'string'},'operation':{'type':'object'}},'required':['id','role','operation']}})
        seen=set()
        for comparison in comparisons:
            if comparison['id'] in seen:errors.append('配置对比 id 不能重复')
            seen.add(comparison['id'])
            if comparison['role'] not in ids['roles']:errors.append('配置对比引用了未声明的角色')
            op=comparison['operation']
            validate_schema({'role':comparison['role'],**op},OPERATION_SCHEMA)
            if op['action'] not in ('exec','shell_send','remote_read') or any(k in op for k in ('role','session_id','capture_id','capture_phase','expect_disconnect')):
                errors.append('配置采集仅支持 exec、shell_send 或 remote_read，角色由 comparison.role 指定')
            if op['action']=='shell_send' and not op.get('expect'):errors.append('终端配置采集必须声明 expect')
    except Exception as exc:
        errors.append('拓扑或配置采集声明无效：'+str(exc).splitlines()[0])
    try:
        from .load_schema import WORKLOAD_DECLARATIONS
        validate_schema(contract.get('workloads',[]),WORKLOAD_DECLARATIONS)
        seen=set()
        for load in contract.get('workloads',[]):
            if load['id'] in seen:errors.append('负载声明 id 不能重复')
            seen.add(load['id'])
            if load['role'] not in ids['roles']:errors.append('负载声明引用了未定义角色')
            if load['driver']=='custom' and not all(load.get(k) for k in ('start','status','stop')):
                errors.append('自研工具需要声明 start/status/stop')
            if load.get('parser')=='jsonl' and not load.get('metrics'):errors.append('JSONL 采集需要声明指标字段与单位')
    except Exception as exc:errors.append('负载声明无效：'+str(exc).splitlines()[0])
    return {"mode": "invalid" if errors else "structured", "errors": errors}


def validate_parameters(contract, values):
    """Apply declared defaults and validate dynamic form values with JSON Schema."""
    from jsonschema import Draft202012Validator
    schema = contract.get('parameters', {'type': 'object'})
    result = dict(values)
    for name, definition in schema.get('properties', {}).items():
        if name not in result and 'default' in definition:
            result[name] = definition['default']
    errors = sorted(Draft202012Validator(schema).iter_errors(result), key=lambda e: str(e.path))
    if errors:
        raise ValueError('参数校验失败：' + '; '.join(e.message for e in errors))
    return result


def validate_workload_binding(snapshot, role, spec):
    contract=json.loads(snapshot['skill']['files'].get('contract.json','{}'))
    definitions=[w for w in contract.get('workloads',[]) if w['role']==role and w['driver']==spec['driver']]
    if not definitions:raise ValueError('Skill 未声明此角色的负载驱动')
    if spec['driver']=='custom':
        if not any(w.get('parser','none')==spec.get('parser','none') and w.get('metrics',[])==spec.get('metrics',[]) for w in definitions):
            raise ValueError('自研指标采集必须匹配 Skill 声明')
