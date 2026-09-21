import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import zipfile

from testagent.tool_library import inspect, normalize_package, ToolLibrary


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    return output.getvalue()


def vdbench(machine=62):
    elf = bytearray(64)
    elf[:6] = b'\x7fELF\x02\x01'
    elf[18:20] = machine.to_bytes(2, 'little')
    return {'vdbench': b'#!/bin/sh\njava -jar vdbench.jar\n',
            'vdbench.jar': b'fixture jar', 'linux/libvdbench.so': bytes(elf),
            'LICENSE': b'fixture license'}


class ToolImportTests(unittest.TestCase):
    def test_raw_wrapped_and_repeated_import_are_deployable(self):
        for prefix in ('', 'vdbench50407/', 'outer/inner/'):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                library = ToolLibrary(SimpleNamespace(root=Path(directory)))
                payload = archive({prefix+n: b for n, b in vdbench().items()})
                result = library.import_zip(payload, '5.04.07')
                self.assertEqual(result['architecture'], 'x86_64')
                self.assertEqual(result['driver'], 'vdbench')
                self.assertEqual(library.import_zip(payload, '5.04.07')['id'], result['id'])
                self.assertEqual(sum(t['id']==result['id'] for t in library.list()), 1)
                self.assertEqual(len(list((Path(directory)/'tools').glob('*.zip'))), 1)
                stored = (Path(directory)/'tools'/(result['id']+'.zip')).read_bytes()
                self.assertEqual(hashlib.sha256(stored).hexdigest(), result['id'])
                with zipfile.ZipFile(io.BytesIO(stored)) as z:
                    manifest = json.loads(z.read('tool.json'))
                    for name, body in vdbench().items():
                        self.assertEqual(z.read(name), body)
                        self.assertEqual(manifest['files'][name], hashlib.sha256(body).hexdigest())
                    self.assertIn(manifest['entrypoint'], manifest['executables'])

    def test_http_import_options_and_error_response(self):
        import threading
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from testagent.core import Core
        from testagent.server import make_server
        with tempfile.TemporaryDirectory() as directory:
            core=Core(Path(directory)/'db.sqlite3')
            server=make_server(core)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_port)
            try:
                with urlopen(base+'/api/state') as response: token=json.load(response)['token']
                def send(payload,query=''):
                    return urlopen(Request(base+'/api/tools/import'+query,data=payload,
                        headers={'Content-Type':'application/zip','X-Testagent-Token':token}))
                with send(archive(vdbench()),'?version=5.04.07&architecture=x86_64') as response:
                    imported=json.load(response)['result']
                    self.assertEqual(imported['version'],'5.04.07')
                with self.assertRaises(HTTPError) as caught:send(archive({'readme':b'x'}))
                self.assertIn('无法识别',json.load(caught.exception)['error'])
                with urlopen(base+'/api/tools') as response:self.assertEqual(sum(t['id']==imported['id'] for t in json.load(response)),1)
            finally:
                server.shutdown();server.server_close();thread.join();core.db.close()

    def test_standard_wrapper_preserves_validation(self):
        normalized = normalize_package(archive(vdbench()), '5.04.07')
        self.assertEqual(normalize_package(normalized), normalized)
        with zipfile.ZipFile(io.BytesIO(normalized)) as z:
            files = {n: z.read(n) for n in z.namelist()}
        self.assertEqual(inspect(normalize_package(archive({'outer/'+n:b for n,b in files.items()})))['version'], '5.04.07')
        files['vdbench.jar'] = b'tampered'
        with self.assertRaisesRegex(ValueError, '校验失败'):
            normalize_package(archive({'outer/'+n:b for n,b in files.items()}))

    def test_architecture_and_required_metadata(self):
        self.assertEqual(inspect(normalize_package(archive(vdbench(183)), '5.04.07'))['architecture'], 'aarch64')
        with self.assertRaisesRegex(ValueError, '不一致'):
            normalize_package(archive(vdbench()), '5.04.07', 'aarch64')
        with self.assertRaisesRegex(ValueError, '实际版本'):
            normalize_package(archive(vdbench()))
        files = vdbench(); del files['linux/libvdbench.so']
        with self.assertRaisesRegex(ValueError, '架构'):
            normalize_package(archive(files), '5.04.07')
        self.assertEqual(inspect(normalize_package(archive(files), '5.04.07', 'x86_64'))['architecture'], 'x86_64')
        del files['vdbench.jar']
        with self.assertRaisesRegex(ValueError, 'vdbench.jar'):
            normalize_package(archive(files), '5.04.07', 'x86_64')

    def test_32_bit_companion_does_not_make_64_bit_architecture_ambiguous(self):
        files=vdbench()
        files['linux/linux32.so']=vdbench(3)['linux/libvdbench.so']
        self.assertEqual(inspect(normalize_package(archive(files),'5.04.07'))['architecture'],'x86_64')
        files['linux/arm64.so']=vdbench(183)['linux/libvdbench.so']
        with self.assertRaisesRegex(ValueError,'无法唯一识别'):
            normalize_package(archive(files),'5.04.07')
        self.assertEqual(inspect(normalize_package(archive(files),'5.04.07','aarch64'))['architecture'],'aarch64')
        with self.assertRaisesRegex(ValueError,'不一致'):
            normalize_package(archive(vdbench(3)),'5.04.07','x86_64')

    def test_invalid_packages_have_actionable_errors(self):
        for payload, message in ((b'not zip', 'ZIP'), (archive({'readme': b'x'}), '无法识别'),
                                 (archive({'tool.json': b'broken'}), 'JSON'),
                                 (archive({'tool.json': b'[]'}), 'JSON 对象'),
                                 (archive({'../vdbench': b'x'}), '相对文件路径')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                normalize_package(payload)
        with self.assertRaisesRegex(ValueError, '缺少根目录 tool.json'):
            inspect(archive(vdbench()))


if __name__ == '__main__':
    unittest.main()
