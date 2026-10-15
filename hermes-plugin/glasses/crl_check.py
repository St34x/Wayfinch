# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin crl --check`` on the signer's published files (T1.178a; ``glasses.admin_crl`` is the CLI).

``check_published`` reads ``device-ca.pem`` (1 or 2 CA certs: two during a rotation overlap, each BasicConstraints
CA:true, no subject pin) and ``devices-crl.pem`` (one CRL per CA), each opened with ``O_NOFOLLOW``, a regular file of
at most 1 MiB; no owner check (display only, every CRL is signature-checked). Both files hold only their PEM blocks
(``CERTIFICATE`` / ``X509 CRL``) and whitespace; any other block (a private key) or text is an error, and so is a CA
whose public key the library cannot load. Each CRL must
verify under exactly one bundle CA that it names as issuer, and each CA must have exactly one CRL. Per CA, errors: a
CA not yet valid, expired or with a bad self-signature, no CRL Number or one outside 0..2^159 (RFC 5280), a number
below the recorded one (rolled back), ``lastUpdate`` over 5 minutes ahead, an expired CRL; warnings (printed with or
without errors): ``nextUpdate`` within 14 days, ``notAfter`` within 180 days. A CA is named by its ``ca_id``: the
first 16 hex of the SHA-256 of its SubjectPublicKeyInfo, as the signer names it (a re-issued cert keeps it).
The check is a deployer's early warning, not the root installer's validation: an unknown critical CRL extension, an
equal CRL Number with other bytes, AKI = SKI and CRL v2 are not checked here.

``CrlRecord`` keeps ``$state_dir/crl-seen.json`` (version 1: the highest CRL Number seen per ``ca_id``), 0600 in an
owner-only dir, no symlink, rewritten under a lock on the dir (temp file + fsync + rename + dir fsync). Missing =
first run; malformed or unsafe = an error line, never rewritten, and every CA and CRL check still runs (without the
rollback comparison). It is raised only for a CA without an error. Messages carry file names, never paths or contents.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

PUBLISHED_CAP = 1 << 20  # bytes per published file
CRL_WARN = timedelta(days=14)
CA_WARN = timedelta(days=180)
CLOCK_SKEW = timedelta(minutes=5)  # a lastUpdate further ahead of the clock than this is an error
CRL_NUMBER_LIMIT = 1 << 159  # RFC 5280 5.2.3: at most 20 octets
RECORD_NAME = "crl-seen.json"
RECORD_VERSION = 1
_CRL_BLOCK = re.compile(rb"-----BEGIN X509 CRL-----\r?\n[A-Za-z0-9+/=\r\n]*-----END X509 CRL-----\r?\n?")
_CERT_BLOCK = re.compile(rb"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]*-----END CERTIFICATE-----\r?\n?")
_CA_ID = re.compile(r"[0-9a-f]{16}")
Crl = x509.CertificateRevocationList
_PARSE_ERRORS = (ValueError, TypeError, x509.DuplicateExtension, x509.UnsupportedGeneralNameType)


class CheckError(Exception):
    """One error line of the check (fixed text: file names, never paths or contents)."""


@dataclass(frozen=True)
class CrlCheck:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _read_regular(path: Path) -> tuple[bytes, os.stat_result]:
    """Bytes and stat of a regular file of at most ``PUBLISHED_CAP`` bytes, never through a symlink; a missing file
    raises ``FileNotFoundError``, anything else unsafe a ``CheckError``."""
    try:  # O_NONBLOCK: a FIFO in place of the file must not block the open
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as exc:  # ELOOP: a symlink in place of the file
        raise CheckError(f"{path.name}: cannot open safely ({type(exc).__name__})") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):  # before fdopen, which refuses a directory with a raw IsADirectoryError
        os.close(fd)
        raise CheckError(f"{path.name}: not a regular file")
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(PUBLISHED_CAP + 1)
    if len(data) > PUBLISHED_CAP:
        raise CheckError(f"{path.name}: larger than {PUBLISHED_CAP} bytes")
    return data, st


def read_published(path: Path) -> bytes:
    try:
        return _read_regular(path)[0]
    except FileNotFoundError:
        raise CheckError(f"{path.name}: missing - the signer has not published it") from None


def _pem_blocks(raw: bytes, block: re.Pattern[bytes], error: str) -> list[bytes]:
    """The PEM blocks of *raw*; ``CheckError(error)`` when there is none or anything but whitespace is left over."""
    blocks = block.findall(raw)
    if not blocks or block.sub(b"", raw).strip():
        raise CheckError(error)
    return blocks


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except (x509.ExtensionNotFound, *_PARSE_ERRORS):
        return False


def load_cas(raw: bytes, name: str) -> list[x509.Certificate]:
    error = f"{name}: not a valid PEM certificate bundle"
    try:
        cas = [x509.load_pem_x509_certificate(block) for block in _pem_blocks(raw, _CERT_BLOCK, error)]
    except ValueError:
        raise CheckError(error) from None
    if not 1 <= len(cas) <= 2:
        raise CheckError(f"{name}: holds {len(cas)} certificates, 1 or 2 expected")
    if not all(_is_ca(cert) for cert in cas):
        raise CheckError(f"{name}: a certificate in it is not a CA (BasicConstraints CA:true)")
    try:  # every later ca_id() call is safe once each CA's key loads here
        for cert in cas:
            ca_id(cert)
    except (UnsupportedAlgorithm, *_PARSE_ERRORS):
        raise CheckError(f"{name}: a certificate in it has a public key the check cannot read") from None
    return cas


def load_crls(raw: bytes, name: str) -> list[x509.CertificateRevocationList]:
    error = f"{name}: not a valid PEM CRL bundle"
    try:
        return [x509.load_pem_x509_crl(block) for block in _pem_blocks(raw, _CRL_BLOCK, error)]
    except ValueError:
        raise CheckError(error) from None


def ca_id(cert: x509.Certificate) -> str:
    """The signer's ``ca_id`` of a CA (brief section 11): 16 hex of the SHA-256 of its SubjectPublicKeyInfo."""
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()[:16]


def _signed_by(crl: x509.CertificateRevocationList, ca: x509.Certificate) -> bool:
    try:
        return crl.issuer == ca.subject and crl.is_signature_valid(ca.public_key())  # type: ignore[arg-type]
    except (UnsupportedAlgorithm, *_PARSE_ERRORS):
        return False


def pair(
    cas: list[x509.Certificate], crls: list[Crl], crl_name: str
) -> tuple[list[tuple[x509.Certificate, Crl]], list[str]]:
    """(CA, its CRL) pairs and the pairing errors: each CRL under exactly one CA, each CA with exactly one CRL."""
    errors, owners = [], []
    for crl in crls:
        issuers = [ca for ca in cas if _signed_by(crl, ca)]
        if len(issuers) != 1:
            errors.append(f"{crl_name}: a CRL in it is not signed by exactly one CA of the published CA bundle")
        else:
            owners.append((issuers[0], crl))
    pairs = []
    for ca in cas:
        mine = [crl for owner, crl in owners if owner is ca]
        if len(mine) == 1:
            pairs.append((ca, mine[0]))
        else:
            errors.append(f"CA {ca_id(ca)}: {len(mine)} CRLs in {crl_name}, exactly 1 expected")
    return pairs, errors


def crl_number(crl: x509.CertificateRevocationList) -> int:
    """The CRL Number of *crl*; ``x509.ExtensionNotFound`` without one."""
    return int(crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number)


def _ca_errors(ca: x509.Certificate, now: datetime) -> list[str]:
    errors = []
    if now < ca.not_valid_before_utc:
        errors.append("the CA is not yet valid: check the clock")
    if now >= ca.not_valid_after_utc:
        errors.append("the CA has expired")
    try:
        ca.verify_directly_issued_by(ca)
    except (InvalidSignature, UnsupportedAlgorithm, *_PARSE_ERRORS):
        errors.append("the CA's self-signature does not verify")
    return errors


def _crl_errors(crl: x509.CertificateRevocationList, seen: int, now: datetime) -> list[str]:
    try:
        number = crl_number(crl)
    except x509.ExtensionNotFound:
        return ["the CRL has no CRL Number"]
    if not 0 <= number < CRL_NUMBER_LIMIT:
        return ["the CRL Number is outside 0..2^159 (RFC 5280: at most 20 octets)"]
    errors = []
    if number < seen:
        errors.append(f"the CRL number {number} is lower than the recorded {seen}: an older CRL was put back")
    if crl.last_update_utc - now > CLOCK_SKEW:
        errors.append("the CRL's lastUpdate is more than 5 minutes ahead of this host's clock: check the clock")
    if crl.next_update_utc is None or now >= crl.next_update_utc:
        errors.append("the CRL has expired: every device is locked out at the edge")
    return errors


def _pair_warnings(crl: x509.CertificateRevocationList, ca: x509.Certificate, now: datetime) -> list[str]:
    warnings = []
    next_update = crl.next_update_utc
    if next_update is not None and timedelta(0) < next_update - now <= CRL_WARN:
        warnings.append(
            f"the CRL expires in {(next_update - now).days} days (warning at 14 days): the signer re-signs it daily"
        )
    if timedelta(0) < ca.not_valid_after_utc - now <= CA_WARN:
        warnings.append(f"the CA expires in {(ca.not_valid_after_utc - now).days} days: plan a CA rotation")
    return warnings


def _check_private(st: os.stat_result, what: str) -> None:
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise CheckError(f"{what}: must be owned by the plugin user with no group/other access")


def _parse_record(raw: bytes) -> dict[str, int]:
    with contextlib.suppress(ValueError, RecursionError):
        doc = json.loads(raw)
        if isinstance(doc, dict) and set(doc) == {"version", "crls"} and isinstance(crls := doc["crls"], dict):
            numbers_ok = all(
                _CA_ID.fullmatch(k) and type(n) is int and 0 <= n < CRL_NUMBER_LIMIT for k, n in crls.items()
            )
            if type(doc["version"]) is int and doc["version"] == RECORD_VERSION and numbers_ok:
                return dict(crls)
    raise CheckError(f"{RECORD_NAME}: malformed - it is never rewritten; fix or remove it by hand")


class CrlRecord:
    """``crl-seen.json`` under *state_dir*: the highest CRL Number seen per CA (see the module docstring)."""

    def __init__(self, state_dir: os.PathLike[str] | str) -> None:
        self._dir = Path(state_dir)
        self._file = self._dir / RECORD_NAME

    def read(self) -> dict[str, int]:
        """The recorded numbers; ``{}`` on the first run; ``CheckError`` when the record is unsafe or malformed."""
        try:
            raw, st = _read_regular(self._file)
        except FileNotFoundError:
            return {}
        _check_private(st, RECORD_NAME)
        return _parse_record(raw)

    def raise_to(self, numbers: dict[str, int]) -> None:
        """Raise the recorded number of each CA in *numbers* to it; a recorded number never goes down."""
        try:
            with self._locked() as dir_fd:
                seen = self.read()
                merged = {**seen, **{key: max(n, seen.get(key, 0)) for key, n in numbers.items()}}
                if merged != seen:
                    self._write(merged, dir_fd)
        except OSError as exc:
            raise CheckError(f"{RECORD_NAME}: cannot write it ({type(exc).__name__})") from None

    @contextlib.contextmanager
    def _locked(self) -> Iterator[int]:
        with contextlib.suppress(FileExistsError):
            os.mkdir(self._dir, 0o700)
        fd = os.open(self._dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            _check_private(os.fstat(fd), "state dir")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            os.close(fd)  # releases the lock

    def _write(self, numbers: dict[str, int], dir_fd: int) -> None:
        data = json.dumps({"version": RECORD_VERSION, "crls": numbers}, indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=f".{RECORD_NAME}.", suffix=".tmp", dir=self._dir)  # mode 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._file)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)  # only left over when the write or the rename failed
        os.fsync(dir_fd)


def _check_pair(ca: x509.Certificate, crl: x509.CertificateRevocationList, seen: int, now: datetime) -> CrlCheck:
    try:  # cryptography parses the extensions and the dates only when they are read
        errors = _ca_errors(ca, now) + _crl_errors(crl, seen, now)
        return CrlCheck(tuple(errors), tuple(_pair_warnings(crl, ca, now)))
    except _PARSE_ERRORS:
        return CrlCheck(("the CRL is not a valid CRL",), ())


def _read_record(record: CrlRecord) -> tuple[dict[str, int], list[str]]:
    """The recorded numbers and ``[]``, or ``{}`` and the record's error line (the pair checks still run)."""
    try:
        return record.read(), []
    except CheckError as exc:
        return {}, [str(exc)]


def check_published(ca_path: Path, crl_path: Path, state_dir: os.PathLike[str] | str) -> CrlCheck:
    """Errors and warnings of the published CA and CRL bundles; raises the record for every CA without an error
    (never when the record itself has an error)."""
    now = _utcnow().astimezone(UTC)
    record = CrlRecord(state_dir)
    seen, record_errors = _read_record(record)
    try:
        cas = load_cas(read_published(ca_path), ca_path.name)
        crls = load_crls(read_published(crl_path), crl_path.name)
    except CheckError as exc:
        return CrlCheck((str(exc), *record_errors), ())
    pairs, errors = pair(cas, crls, crl_path.name)
    warnings, clean = [], {}
    for ca, crl in pairs:
        key = ca_id(ca)
        found = _check_pair(ca, crl, seen.get(key, 0), now)
        errors += [f"CA {key}: {line}" for line in found.errors]
        warnings += [f"CA {key}: {line}" for line in found.warnings]
        if not found.errors:
            clean[key] = crl_number(crl)
    try:
        if clean and not record_errors:
            record.raise_to(clean)
    except CheckError as exc:
        errors.append(str(exc))
    return CrlCheck(tuple(record_errors + errors), tuple(warnings))
