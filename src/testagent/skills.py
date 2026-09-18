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
