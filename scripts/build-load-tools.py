"""Build licensed iperf3 offline packages on native Linux runners (no containers)."""
import hashlib
import json
import platform
from pathlib import Path
import shutil
import subprocess
import tarfile
from urllib.request import urlretrieve
import zipfile

ROOT=Path(__file__).resolve().parents[1]
VERSION='3.21'
SOURCE_SHA256='656e4405ebd620121de7ceca3eaf43a88f79ea1b857d041a6a0b1314801acdd8'


def main():
    arch=platform.machine()
    if platform.system()!='Linux' or arch not in ('x86_64','aarch64'):raise SystemExit('Native Linux x86_64/aarch64 required')
    work=ROOT/'build'/'iperf3';work.mkdir(parents=True,exist_ok=True)
    source=ROOT/'vendor'/f'iperf-{VERSION}.tar.gz'
    if hashlib.sha256(source.read_bytes()).hexdigest()!=SOURCE_SHA256:raise ValueError('Source checksum mismatch')
    with tarfile.open(source) as archive:archive.extractall(work,filter='data')
    folder=work/f'iperf-{VERSION}'
    subprocess.run(['./configure','--disable-shared','--enable-static','--without-openssl',
                    'CC=musl-gcc','CFLAGS=-O2','LDFLAGS=-static'],cwd=folder,check=True)
    subprocess.run(['make','-j2'],cwd=folder,check=True)
    binary=folder/'src/iperf3'
    subprocess.run([str(binary),'--version'],check=True)
    subprocess.run(['file',str(binary)],check=True)
    assert 'statically linked' in subprocess.check_output(['file',str(binary)],text=True)
    files={'iperf3':binary.read_bytes(),'LICENSE':(folder/'LICENSE').read_bytes(),
           'MUSL-COPYRIGHT':Path('/usr/share/doc/musl/copyright').read_bytes()}
    manifest={'name':'iperf3','version':VERSION,'driver':'iperf3','os':'linux','architecture':arch,
              'entrypoint':'iperf3','executables':['iperf3'],'source_sha256':SOURCE_SHA256,
              'source_url':f'https://downloads.es.net/pub/iperf/iperf-{VERSION}.tar.gz',
              'files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()}}
    output=ROOT/'tool-bundles';output.mkdir(exist_ok=True)
    target=output/f'iperf3-{VERSION}-linux-{arch}.zip'
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():archive.writestr(name,data)
        archive.writestr('tool.json',json.dumps(manifest,indent=2))
    print(target)

if __name__=='__main__':main()
