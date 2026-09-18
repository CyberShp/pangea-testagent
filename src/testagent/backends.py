"""Backend boundary; capability status must not imply an installed executable works."""
from dataclasses import dataclass
import os
import shutil
from typing import Protocol, Callable


@dataclass(frozen=True)
class LaunchSpec:
    command: str
    args: tuple[str, ...]
    env: dict[str, str]


def launch_spec(provider, *, platform=os.name, env=None):
    env = os.environ if env is None else env
    if provider not in ("nga", "opencode", "codeagent"):
        raise ValueError("不支持的 ACP Agent")
    args = ("acp",)
    overrides = {}
    if provider == "opencode":
        args += ("--print-logs", "--log-level", "ERROR")
    if provider == "codeagent" and platform == "nt":
        value = next((v for k, v in env.items() if k.upper() == "CODEAGENT3_WINDOWS_SHELL_TYPE"), "powershell")
        overrides["CODEAGENT3_WINDOWS_SHELL_TYPE"] = value or "powershell"
    return LaunchSpec(shutil.which(provider) or provider, args, overrides)


class Backend(Protocol):
    def run(self, task: dict, tools: Callable, emit: Callable, cancelled: Callable) -> None: ...
    def cancel(self, task_id: str) -> None: ...


def capabilities():
    return [{"id": "simulation", "name": "本地模拟器", "implemented": True}] + [
        {"id": p, "name": p, "implemented": True, "requires_configuration": True}
        for p in ("openai", "nga", "opencode", "codeagent")]


def resolve_command(command):
    """PATH/absolute resolution plus the reference project's PowerShell fallback."""
    import json
    import subprocess
    from pathlib import Path
    value=os.path.expandvars(str(command))
    found=shutil.which(value)
    if found:return found
    if Path(value).is_file():return str(Path(value).resolve())
    if os.name=='nt':
        env=os.environ.copy();env['TESTAGENT_AGENT_COMMAND']=value
        script=("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                "$c=Get-Command -CommandType Application -Name $env:TESTAGENT_AGENT_COMMAND -ErrorAction SilentlyContinue | Select-Object -First 1; "
                "if ($null -eq $c) { @{found=$false}|ConvertTo-Json -Compress } else { @{found=$true;command=$c.Source}|ConvertTo-Json -Compress }")
        result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-Command',script],
                              env=env,capture_output=True,text=True,encoding='utf-8',timeout=8,creationflags=subprocess.CREATE_NO_WINDOW)
        parsed=json.loads(result.stdout.strip())
        if parsed.get('found') and parsed.get('command'):return parsed['command']
    raise FileNotFoundError(f'未找到 Agent：{command}。请安装后加入 PATH，或填写可执行文件/.cmd 绝对路径。')
