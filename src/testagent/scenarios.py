"""Bundled scenario definitions, versioned in each task snapshot."""
import json
from .paths import ROOT
from .packages import from_folder
from .skills import validate


def package(ident):
    if ident != 'nic-bandwidth':
        raise ValueError('预设场景不存在')
    value = from_folder(ROOT / 'skills' / ident)
    result = validate(value)
    if result['mode'] != 'structured':
        raise ValueError('预设场景校验失败：' + '; '.join(result['errors']))
    return value


def catalog():
    value = package('nic-bandwidth')
    return [{'id': value['id'], 'name': value['name'], 'version': value['version'],
             'contract': json.loads(value['files']['contract.json'])}]


def validate_parameters(ident, values, roles):
    if ident!='nic-bandwidth':return
    import ipaddress
    if roles['client']==roles['server']:raise ValueError('网卡场景需要绑定两台不同设备')
    pairs=[('client_ip','server_ip')]
    if values['mode']=='整卡双端口':
        pairs.append(('client2_ip','server2_ip'))
        if values['port']==values['port2']:raise ValueError('整卡两对监听端口必须不同')
        for side in ('client','server'):
            if values[side+'_interface']==values[side+'2_interface']:raise ValueError('整卡模式需要两个不同端口')
            if ipaddress.ip_address(values[side+'_ip'])==ipaddress.ip_address(values[side+'2_ip']):raise ValueError('整卡端口测试 IP 必须不同')
    for source,target in pairs:
        a,b=ipaddress.ip_address(values[source]),ipaddress.ip_address(values[target])
        if a==b or a.version!=b.version:raise ValueError('每对测试 IP 必须不同且地址族一致')
