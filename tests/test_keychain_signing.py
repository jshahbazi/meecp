"""Exercise CI keychain registration with a disposable, synthetic identity."""

import datetime
import importlib.util
import json
from pathlib import Path
import platform
import secrets
import shlex
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import pytest


def quiet_command(*arguments):
    result = subprocess.run(arguments, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise RuntimeError("native_keychain_test_command_failed")
    return result.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="Requires macOS Keychain and codesign")
def test_registered_keychain_signs_and_cleanup_restores_search_list(monkeypatch, tmp_path):
    module_path = Path(__file__).resolve().parents[1] / ".github/scripts/apple_credentials.py"
    spec = importlib.util.spec_from_file_location("native_apple_credentials", module_path)
    credentials = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(credentials)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    original = shlex.split(quiet_command("/usr/bin/security", "list-keychains", "-d", "user").decode())
    root = credentials.workspace()
    root.mkdir(mode=0o700)
    credentials.write_private(root / credentials.SEARCH_LIST_FILENAME, json.dumps(original).encode())
    keychain = root / "signing.keychain-db"
    password = secrets.token_urlsafe(48)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcpme synthetic signing test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(minutes=10))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False)
                   .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                       key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
                   .sign(key, hashes.SHA256()))
    encryption = (serialization.PrivateFormat.PKCS12.encryption_builder().kdf_rounds(50000)
                  .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
                  .hmac_hash(hashes.SHA1()).build(password.encode()))
    credentials.write_private(root / "test.p12", pkcs12.serialize_key_and_certificates(
        b"synthetic", key, certificate, None, encryption))
    try:
        quiet_command("/usr/bin/security", "create-keychain", "-p", password, str(keychain))
        quiet_command("/usr/bin/security", "unlock-keychain", "-p", password, str(keychain))
        quiet_command("/usr/bin/security", "import", str(root / "test.p12"), "-k", str(keychain),
                      "-P", password, "-T", "/usr/bin/codesign")
        quiet_command("/usr/bin/security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:",
                      "-s", "-k", password, str(keychain))
        credentials.register_keychain(root, keychain)
        source, binary = root / "test.c", root / "test-binary"
        source.write_text("int main(void) { return 0; }\n")
        quiet_command("/usr/bin/cc", str(source), "-o", str(binary))
        fingerprint = certificate.fingerprint(hashes.SHA1()).hex().upper()
        quiet_command("/usr/bin/codesign", "--force", "--sign", fingerprint,
                      "--keychain", str(keychain), str(binary))
        quiet_command("/usr/bin/codesign", "--verify", "--strict", "-R",
                      f'=certificate leaf = H"{fingerprint}"', str(binary))
    except Exception:
        pytest.fail("native_keychain_signing_failed", pytrace=False)
    finally:
        try:
            credentials.cleanup()
        except Exception:
            # Restore even if a helper regression prevents its normal cleanup.
            quiet_command("/usr/bin/security", "list-keychains", "-d", "user", "-s", *original)
            if keychain.exists():
                quiet_command("/usr/bin/security", "delete-keychain", str(keychain))
            pytest.fail("native_keychain_cleanup_failed", pytrace=False)
    restored = shlex.split(quiet_command("/usr/bin/security", "list-keychains", "-d", "user").decode())
    assert restored == original
    assert not root.exists()
