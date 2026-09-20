import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from testagent.packages import from_folder, from_zip, to_zip
from testagent.paths import ROOT
from testagent.skills import validate, validate_parameters


class NicPresetTests(unittest.TestCase):
    def test_package_and_card_parameters(self):
        package = from_folder(ROOT / 'skills/nic-bandwidth')
        self.assertEqual(validate(package), {'mode': 'structured', 'errors': []})
        roundtrip = from_zip(to_zip(package))
        self.assertEqual(validate(roundtrip), {'mode': 'structured', 'errors': []})
        self.assertEqual(roundtrip['files']['contract.json'], package['files']['contract.json'])
        contract = json.loads(package['files']['contract.json'])
        values = dict(client_interface='eth1', client_ip='192.0.2.1',
                      server_interface='eth1', server_ip='192.0.2.2')
        self.assertEqual(validate_parameters(contract, values)['mode'], '单端口')
        with self.assertRaises(ValueError):
            validate_parameters(contract, dict(values, mode='整卡双端口'))
        values.update(mode='整卡双端口', client2_interface='eth2', client2_ip='198.51.100.1',
                      server2_interface='eth2', server2_ip='198.51.100.2')
        self.assertEqual(validate_parameters(contract, values)['port2'], 5202)

    def test_builtin_available_without_import_and_frozen_in_task(self):
        from testagent.core import Core
        from testagent.catalog import Catalog
        with tempfile.TemporaryDirectory() as directory:
            core = Core(Path(directory) / 'db.sqlite3')
            self.addCleanup(core.db.close)
            catalog = Catalog(core, Path(directory) / 'data')
            self.assertEqual(catalog.state()['skills'], [])
            self.assertEqual(catalog.state()['scenarios'][0]['id'], 'nic-bandwidth')
            a = catalog.save_device({'name':'a','address':'192.0.2.10','username':'root'})
            b = catalog.save_device({'name':'b','address':'192.0.2.20','username':'root'})
            env = catalog.save_environment({'name':'test','roles':{'client':a,'server':b},'policy':'confirm'})
            profile = catalog.save_profile({'name':'agent','kind':'openai','config':{'base_url':'http://127.0.0.1:9/v1'}})
            ident = core.create_task('card',env,'','',profile,'model',
                {'client_interface':'eth1','client_ip':'192.0.2.1','server_interface':'eth1','server_ip':'192.0.2.2'},
                scenario='nic-bandwidth')
            snapshot = core.task(ident)['snapshot']
            self.assertEqual(snapshot['scenario_id'], 'nic-bandwidth')
            self.assertIn('scripts/inspect.sh', snapshot['skill']['files'])
            self.assertEqual(catalog.state()['skills'], [])



if __name__ == '__main__':
    unittest.main()
