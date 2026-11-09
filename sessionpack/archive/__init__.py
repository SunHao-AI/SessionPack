"""归档：构建 / 校验 / 安装。对外符号与拆包前一致。"""
from sessionpack.archive.build import (
    FORMAT_NAME,
    FORMAT_VERSION,
    MANIFEST_NAME,
    ArchiveResult,
    build_archive,
    canonical_zip_name,
    compute_archive_id,
)
from sessionpack.archive.install import ArchiveImportResult, import_archive
from sessionpack.archive.repak import (  # noqa: F401
    RepakError,
    repak_excluding_session,
)
from sessionpack.archive.verify import (
    ArchiveError,
    VerifyIssue,
    VerifyResult,
    verify_archive,
)

__all__ = [
    "FORMAT_NAME", "FORMAT_VERSION", "MANIFEST_NAME",
    "ArchiveResult", "VerifyIssue", "VerifyResult", "ArchiveImportResult",
    "ArchiveError", "canonical_zip_name", "compute_archive_id", "build_archive",
    "verify_archive", "import_archive",
]
