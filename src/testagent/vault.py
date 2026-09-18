"""Windows DPAPI at rest; explicit local-development encryption on other OSes."""
import base64
import ctypes
import os
from pathlib import Path
import threading


class Vault:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        if os.name != 'nt':
            from cryptography.fernet import Fernet
            key = self.root / 'development.key'
            if not key.exists():
                fd = os.open(key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(Fernet.generate_key())
            self.fernet = Fernet(key.read_bytes())

    @staticmethod
    def _dpapi(data, decrypt=False):
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        buf = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        fn.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree(target.data)

    def put(self, key, value):
        if not key.replace('-', '').isalnum():
            raise ValueError('Invalid credential reference')
        data = value.encode('utf-8')
        encrypted = self._dpapi(data) if os.name == 'nt' else self.fernet.encrypt(data)
        with self.lock:
            target = self.root / (key + '.secret')
            temporary = target.with_suffix('.tmp')
            temporary.write_bytes(encrypted)
            if os.name != 'nt':
                temporary.chmod(0o600)
            temporary.replace(target)
        return key

    def get(self, key):
        if not key or not key.replace('-', '').isalnum():
            return ''
        with self.lock:
            path = self.root / (key + '.secret')
            if not path.exists():
                return ''
            raw = path.read_bytes()
        return (self._dpapi(raw, True) if os.name == 'nt' else self.fernet.decrypt(raw)).decode('utf-8')

    def delete(self, key):
        if key and key.replace('-', '').isalnum():
            (self.root / (key + '.secret')).unlink(missing_ok=True)

    def redact(self, text):
        text = str(text)
        with self.lock:
            values = [self.get(path.stem) for path in self.root.glob('*.secret')]
        for value in sorted(values, key=len, reverse=True):
            if value:
                text = text.replace(value, '[已隐藏凭据]')
                text = text.replace(__import__('json').dumps(value)[1:-1], '[已隐藏凭据]')
        return text

    def redact_tree(self, value):
        if isinstance(value,str): return self.redact(value)
        if isinstance(value,list): return [self.redact_tree(item) for item in value]
        if isinstance(value,dict):
            # Trusted protocol identifiers are not free text. Redacting a short password
            # inside an operation ID would break authorization and evidence references.
            references={'id','task_id','device_id','operation_id','session_id','file_id','backup_file_id','child_id','step_id','check_id','evidence_operation'}
            return {key:item if key in references else self.redact_tree(item) for key,item in value.items()}
        return value
