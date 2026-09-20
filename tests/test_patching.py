import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from testagent.patching import rebuild_patch


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=Path(self.temp.name)/'中文 安装目录'/'app'
        (self.app/'runtime').mkdir(parents=True)
        (self.app/'portable.json').write_text(json.dumps({'product':'pangea-testagent','version':'1.0.1','runtime':'fixture'}))
        self.runtime={'app/runtime/python.exe':b'python','app/runtime/pythonw.exe':b'pythonw'}
        for name,data in self.runtime.items():(self.app/name.removeprefix('app/')).write_bytes(data)
        (self.app/'obsolete.py').write_text('old code')
        self.files={'app/entry.py':b'entry','app/portable.json':b'{}','app/src/testagent/paths.py':b"VERSION='1.0.2'"}

    def package(self,tamper=False,extra=None):
        manifest={'product':'pangea-testagent','schema_version':2,'kind':'application-patch','version':'1.0.2','runtime':'fixture',
                  'files':{n:hashlib.sha256(v).hexdigest() for n,v in self.files.items()},
                  'runtime_files':{n:hashlib.sha256(v).hexdigest() for n,v in self.runtime.items()}}
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as archive:
            for n,v in self.files.items():archive.writestr(n,b'changed' if tamper else v)
            if extra:archive.writestr(extra,b'bad')
            archive.writestr('update-manifest.json',json.dumps(manifest))
        return out.getvalue()

    def test_rebuild_is_legacy_compatible_and_excludes_old_code(self):
        result=rebuild_patch(self.package(),self.app)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            manifest=json.loads(archive.read('update-manifest.json'))
            self.assertEqual(manifest['schema_version'],1)
            self.assertEqual(set(manifest['files']),set(self.files)|set(self.runtime))
            for name,digest in manifest['files'].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(),digest)
        self.assertTrue((self.app/'obsolete.py').exists())

    def test_web_import_reconstructs_patch_in_staging(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from testagent.updates import Updates
        updater=Updates(SimpleNamespace(root=self.app.parent/'data',core=None))
        with patch('testagent.updates.ROOT',self.app),patch('testagent.updates.VERSION','1.0.1'):
            result=updater.inspect(self.package())
        staged=Path(result['stage'])/'app'
        self.assertEqual((staged/'runtime/python.exe').read_bytes(),b'python')
        self.assertFalse((staged/'obsolete.py').exists())

    def test_download_wrapper_is_imported_in_backend(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from testagent.updates import Updates
        wrapper=io.BytesIO()
        with zipfile.ZipFile(wrapper,'w') as archive:
            archive.writestr('pangea-testagent-1.0.2-windows-x64-patch.zip',self.package())
        updater=Updates(SimpleNamespace(root=self.app.parent/'data',core=None))
        with patch('testagent.updates.ROOT',self.app),patch('testagent.updates.VERSION','1.0.1'):
            result=updater.inspect(wrapper.getvalue())
        self.assertEqual(result['version'],'1.0.2')

    def test_runtime_mismatch_requires_full_package(self):
        (self.app/'runtime/python.exe').write_bytes(b'wrong runtime')
        with self.assertRaisesRegex(ValueError,'运行时不兼容'):rebuild_patch(self.package(),self.app)

    def test_tampered_payload_and_unlisted_file_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'校验失败'):rebuild_patch(self.package(tamper=True),self.app)
        with self.assertRaisesRegex(ValueError,'清单不完整'):rebuild_patch(self.package(extra='app/extra.py'),self.app)
        with self.assertRaisesRegex(ValueError,'路径无效'):rebuild_patch(self.package(extra='../escape'),self.app)


if __name__=='__main__':unittest.main()
