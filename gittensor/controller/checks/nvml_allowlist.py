# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The served NVML allowlist: which ``libnvidia-ml.so.1`` md5s are genuine for each driver version.

Lium keeps the same map (``services/const.py LIB_NVIDIA_ML_DIGESTS``) and its check has two holes we do not copy
(``checks/nvml_digest.py:27-52``): an empty driver string passes outright, and an unknown driver keeps its
verification while the map "catches up". Here an empty driver, an unknown driver, an empty digest and a mismatch all
fail — closed — and the unknown driver is named in the evidence so we can vet it and add it. The map is served from
our config (a file or URL), never baked into the code.
"""

import json
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Set

from gittensor.controller.checks import why as w
from gittensor.controller.checks.verdict import CheckResult

CHECK_NAME = 'nvml_digest'


class NvmlAllowlist:
    def __init__(self, digests: Mapping[str, Iterable[str]], source: str = ''):
        self.by_driver: Dict[str, Set[str]] = {
            str(driver).strip(): {str(d).strip().lower() for d in md5s if str(d).strip()}
            for driver, md5s in digests.items()
            if str(driver).strip()
        }
        self.source = source

    @classmethod
    def from_json(cls, text: str, source: str = '') -> 'NvmlAllowlist':
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError('nvml allowlist must be a JSON object {driver: [md5, ...]}')
        return cls({k: ([v] if isinstance(v, str) else v) for k, v in data.items()}, source)

    @classmethod
    def from_file(cls, path) -> 'NvmlAllowlist':
        return cls.from_json(Path(path).read_text(), source=str(path))

    @classmethod
    def from_url(cls, url: str, timeout: float = 10.0) -> 'NvmlAllowlist':
        import requests  # deferred: the check path itself is dependency-free

        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return cls.from_json(r.text, source=url)

    @classmethod
    def load(cls, location: str) -> 'NvmlAllowlist':
        """A file path or an http(s) URL; '' is an empty allowlist (everything fails closed)."""
        if not location:
            return cls({}, source='')
        if location.startswith(('http://', 'https://')):
            return cls.from_url(location)
        return cls.from_file(location)

    @property
    def drivers(self) -> Set[str]:
        return set(self.by_driver)

    def judge(self, driver: Optional[str], md5: Optional[str], kernel_driver: str = '') -> CheckResult:
        driver = (driver or '').strip()
        md5 = (md5 or '').strip().lower()
        evidence = {'driver': driver, 'kernel_driver': kernel_driver, 'md5': md5, 'allowlist': self.source}
        if not driver:
            return CheckResult(
                CHECK_NAME,
                False,
                {**evidence, 'reason': 'empty driver string', w.PUBLIC: {'code': w.NVML_DRIVER_MISSING}},  # fmt: skip
            )
        if kernel_driver and kernel_driver != driver:
            return CheckResult(
                CHECK_NAME,
                False,
                {
                    **evidence,
                    'reason': 'nvidia-smi driver disagrees with the kernel module',
                    w.PUBLIC: {'code': w.NVML_DRIVER_DISAGREES},
                },
            )
        if not md5:
            return CheckResult(
                CHECK_NAME,
                False,
                {
                    **evidence,
                    'reason': 'libnvidia-ml.so.1 not found or unhashed',
                    w.PUBLIC: {'code': w.NVML_LIBRARY_MISSING},
                },  # fmt: skip
            )
        expected = self.by_driver.get(driver)
        if expected is None:
            # The driver version is the box's, and naming it on a public page tells the internet what to target.
            return CheckResult(
                CHECK_NAME,
                False,
                {
                    **evidence,
                    'reason': 'unknown driver (fails closed; vet and add to allowlist)',
                    w.PUBLIC: {'code': w.NVML_DRIVER_UNKNOWN},
                },
            )
        if md5 not in expected:
            return CheckResult(
                CHECK_NAME,
                False,
                {
                    **evidence,
                    'reason': 'digest mismatch',
                    'expected': sorted(expected),
                    w.PUBLIC: {'code': w.NVML_DIGEST_MISMATCH},
                },
            )
        return CheckResult(CHECK_NAME, True, evidence)
